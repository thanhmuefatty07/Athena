"""NVIDIA-hosted trial adapter with one shared, persistent request gate."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from cloud_experiments import agent_lab, campaign, lifecycle


_RATE_ROOT = Path("/opt/athena/runs/.nim-rate")


def credential_rate_file(api_key):
    """One quota owner per credential on the single research VM, independent of panel/model."""
    root = _RATE_ROOT
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise agent_lab.LabError("INVALID_NIM_RATE_STORE")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if not root.is_dir() or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise agent_lab.LabError("INVALID_NIM_RATE_STORE")
    return root / (hashlib.sha256(api_key.encode()).hexdigest() + ".json")


@dataclass
class NimConfig:
    model: str
    api_key: str = field(repr=False)
    rate_file: str = ""
    allow_live: bool = False
    trial_without_billing_confirmed: bool = False
    request_cap: int = 10
    timeout_s: float = 30
    max_response_bytes: int = 1048576


class NimClient:
    def __init__(self, config, *, transport=None, clock=time.time, sleep=time.sleep):
        self.config = config if isinstance(config, NimConfig) else NimConfig(**config)
        cfg = self.config
        if (not isinstance(cfg.model, str) or not 3 <= len(cfg.model) <= 256 or "/" not in cfg.model
                or any(c.isspace() or ord(c) < 32 for c in cfg.model)
                or not isinstance(cfg.api_key, str) or not 8 <= len(cfg.api_key) <= 1024
                or any(c in cfg.api_key for c in "\r\n")
                or type(cfg.allow_live) is not bool or type(cfg.trial_without_billing_confirmed) is not bool
                or type(cfg.request_cap) is not int or not 1 <= cfg.request_cap <= 50
                or type(cfg.timeout_s) not in (int, float) or not math.isfinite(cfg.timeout_s)
                or not 0 < cfg.timeout_s <= 60
                or type(cfg.max_response_bytes) is not int or not 1024 <= cfg.max_response_bytes <= 1048576
                or not cfg.rate_file):
            raise agent_lab.LabError("INVALID_NIM_CONFIGURATION")
        if not cfg.allow_live:
            raise agent_lab.LabError("LIVE_MODEL_CALLS_DISABLED")
        if not cfg.trial_without_billing_confirmed:
            raise agent_lab.LabError("LIVE_NO_BILLING_ACK_REQUIRED")
        self.rate_file = Path(cfg.rate_file)
        if self.rate_file.is_symlink() or not self.rate_file.parent.is_dir():
            raise agent_lab.LabError("INVALID_NIM_RATE_STORE")
        self.transport = transport if transport is not None else agent_lab._https_request
        self._transport_origin_verified = (
            transport is None and self.transport is agent_lab._PROVIDER_HTTPS_REQUEST)
        self.clock, self.sleep = clock, sleep
        self.events, self.requests_used, self.total_tokens = [], 0, 0
        self.transport_attempts = {"catalog": 0, "chat": 0}
        self.admission_failures = 0
        self.blocked, self.admission = None, None
        self._lock = threading.RLock()

    @property
    def boundary(self):
        if self._transport_origin_verified and self.transport is agent_lab._PROVIDER_HTTPS_REQUEST:
            return "LIVE_NVIDIA_NIM"
        return "INJECTED_NVIDIA_NIM_HTTP"

    def _call(self, kind, method, path, payload=None, timeout_s=None):
        from cloud_experiments.research import _analysis_lock
        cfg = self.config
        if cfg.allow_live is not True:
            raise agent_lab.LabError("LIVE_MODEL_CALLS_DISABLED")
        if cfg.trial_without_billing_confirmed is not True:
            raise agent_lab.LabError("LIVE_NO_BILLING_ACK_REQUIRED")
        timeout = min(timeout_s or cfg.timeout_s, cfg.timeout_s)
        deadline = time.monotonic() + timeout
        body = agent_lab.canonical(payload) if payload is not None else None
        event = {"kind": kind, "boundary": self.boundary, "request_bytes": len(body or b""), "http_status": None}
        if payload is not None:
            event["request_payload"] = agent_lab._redact(payload, cfg.api_key)
        lock = self.rate_file.with_suffix(".lock")
        try:
            with self._lock, _analysis_lock(lock):
                state = lifecycle.read_json(self.rate_file, 65536) if self.rate_file.exists() else {
                    "schema": "athena-nim-rate/1", "key_sha256": hashlib.sha256(cfg.api_key.encode()).hexdigest(), "attempts": []}
                if not isinstance(state, dict):
                    raise agent_lab.LabError("INVALID_NIM_RATE_STATE")
                if state.get("schema") != "athena-nim-rate/1" or state.get("key_sha256") != hashlib.sha256(cfg.api_key.encode()).hexdigest():
                    raise agent_lab.LabError("NIM_RATE_IDENTITY_CHANGED")
                now = self.clock()
                if type(now) not in (int, float) or not math.isfinite(now):
                    raise agent_lab.LabError("NIM_RATE_CLOCK_OR_STATE_CHANGED")
                attempts = state.get("attempts")
                blocked_until = state.get("blocked_until", now)
                if (state.get("provider_blocked", False) is not False
                        or type(blocked_until) not in (int, float) or not math.isfinite(blocked_until)
                        or not isinstance(attempts, list) or len(attempts) > 30
                        or any(type(t) not in (int, float) or not math.isfinite(t) or t > now for t in attempts)
                        or attempts != sorted(attempts)):
                    raise agent_lab.LabError("NIM_RATE_CLOCK_OR_STATE_CHANGED")
                attempts = [value for value in attempts if value > now - 60]
                next_at = max(now, (attempts[-1] + 2.05) if attempts else now, blocked_until)
                if len(attempts) == 30:
                    next_at = max(next_at, attempts[0] + 60.05)
                delay = next_at - now
                if delay >= deadline - time.monotonic():
                    raise agent_lab.LabError("RATE_ADMISSION_EXCEEDS_DEADLINE")
                if delay > 0:
                    self.sleep(delay)
                now = self.clock()
                if type(now) not in (int, float) or not math.isfinite(now):
                    raise agent_lab.LabError("NIM_RATE_CLOCK_OR_STATE_CHANGED")
                attempts = [value for value in attempts if value > now - 60]
                if len(attempts) >= 30 or (attempts and now < attempts[-1] + 2.05):
                    raise agent_lab.LabError("NIM_RATE_ADMISSION_DENIED")
                if time.monotonic() >= deadline:
                    raise agent_lab.LabError("RATE_ADMISSION_EXCEEDS_DEADLINE")
                state["attempts"] = [*attempts, now]
                campaign.write_json(self.rate_file, state, max_bytes=65536)
                self.events.append(event)
                started = time.monotonic()
                # A dispatched attempt remains charged if the transport outcome is unknown.
                self.transport_attempts[kind] += 1
                if kind == "chat":
                    self.requests_used += 1
                reply = self.transport(method, "https://integrate.api.nvidia.com/v1/" + path,
                    {"Authorization": "Bearer " + cfg.api_key, "Accept": "application/json", "Content-Type": "application/json"},
                    body, max(.01, deadline - time.monotonic()), cfg.max_response_bytes)
                if not isinstance(reply, agent_lab.HttpReply) or type(reply.status) is not int or not isinstance(reply.body, bytes):
                    raise agent_lab.LabError("INVALID_HTTP_TRANSPORT_RESULT")
                if len(reply.body) > cfg.max_response_bytes:
                    raise agent_lab.LabError("PROVIDER_RESPONSE_BYTE_LIMIT")
                event.update(http_status=reply.status, response_bytes=len(reply.body), response_sha256=hashlib.sha256(reply.body).hexdigest())
                retry_after = next((str(v) for k, v in reply.headers.items() if k.lower() == "retry-after"), None)
                if retry_after is not None:
                    event["retry_after"] = agent_lab._redact(retry_after[:256], cfg.api_key)
                if reply.status != 200:
                    self.blocked = "PROVIDER_HTTP_" + str(reply.status)
                    if reply.status == 429:
                        seconds = 60.0
                        try:
                            pause = Decimal(retry_after or "60")
                        except (InvalidOperation, ValueError):
                            pause = None
                        if pause is not None and pause.is_finite() and pause >= 0:
                            if pause > Decimal(30 * 86400):
                                state["provider_blocked"] = True
                                campaign.write_json(self.rate_file, state, max_bytes=65536)
                                self.blocked = "NIM_PROVIDER_COOLDOWN_TOO_LONG"
                                raise agent_lab.LabError(self.blocked)
                            seconds = float(pause)
                        elif retry_after is not None:
                            from email.utils import parsedate_to_datetime
                            try:
                                seconds = max(0, parsedate_to_datetime(retry_after).timestamp() - self.clock())
                                if not math.isfinite(seconds) or seconds > 30 * 86400:
                                    state["provider_blocked"] = True
                                    campaign.write_json(self.rate_file, state, max_bytes=65536)
                            except (TypeError, ValueError, OverflowError):
                                seconds = 60
                            if state.get("provider_blocked") is True:
                                self.blocked = "NIM_PROVIDER_COOLDOWN_TOO_LONG"
                                raise agent_lab.LabError(self.blocked)
                        state["blocked_until"] = self.clock() + max(2.05, seconds)
                        campaign.write_json(self.rate_file, state, max_bytes=65536)
                    raise agent_lab.LabError(self.blocked)
                data = agent_lab.decode(reply.body, cfg.max_response_bytes)
                if not isinstance(data, dict) or "error" in data:
                    raise agent_lab.LabError("INVALID_PROVIDER_JSON")
                if kind == "chat":
                    event["response"] = agent_lab._redact(data, cfg.api_key)
                return agent_lab._redact(data, cfg.api_key)
        except agent_lab.LabError as error:
            if self.events and self.events[-1] is event:
                event["error"] = error.code
                event["request_wall_ms"] = (time.monotonic() - started) * 1000 if "started" in locals() else 0
            else:
                self.admission_failures += 1
            raise
        except Exception as error:
            if self.events and self.events[-1] is event:
                event["error"] = "RATE_GATE_OR_TRANSPORT_ERROR"
                event["request_wall_ms"] = (time.monotonic() - started) * 1000 if "started" in locals() else 0
            else:
                self.admission_failures += 1
            raise agent_lab.LabError("RATE_GATE_OR_TRANSPORT_ERROR", type(error).__name__) from error
        finally:
            if self.events and self.events[-1] is event and "request_wall_ms" not in event:
                event["request_wall_ms"] = (time.monotonic() - started) * 1000 if "started" in locals() else 0

    def preflight(self):
        if self.blocked:
            raise agent_lab.LabError(self.blocked)
        if self.admission is None:
            models = self._call("catalog", "GET", "models").get("data")
            if not isinstance(models, list) or sum(isinstance(m, dict) and m.get("id") == self.config.model for m in models) != 1:
                raise agent_lab.LabError("EXPLICIT_NIM_MODEL_UNAVAILABLE")
            self.admission = {"model": self.config.model, "boundary": self.boundary,
                              "catalog_status": "LISTED_NOT_INFERENCE_PROOF",
                              "pricing": "NOT_VERIFIED_BY_PROVIDER",
                              "operator_no_charge_acknowledged": self.config.trial_without_billing_confirmed,
                              "actual_cost_usd": "UNKNOWN_UNLESS_REPORTED", "tool_support": "REQUIRES_ACTUAL_CHAT_RESULT",
                              "quota_gate": "SHARED_ONE_IN_FLIGHT_30_REQUESTS_PER_ROLLING_MINUTE",
                              "checked_at_utc": datetime.now(timezone.utc).isoformat()}
        return self.admission

    def complete(self, messages, tools, *, max_tokens, max_context_bytes, timeout_s=None):
        with self._lock:
            self.preflight()
            if self.blocked:
                raise agent_lab.LabError(self.blocked)
            if self.requests_used >= self.config.request_cap:
                raise agent_lab.LabError("MODEL_REQUEST_LIMIT")
            payload = {"model": self.config.model, "messages": messages, "tools": tools, "tool_choice": "auto",
                       "temperature": 0, "max_tokens": max_tokens, "stream": False}
            if len(agent_lab.canonical(payload)) > max_context_bytes:
                raise agent_lab.LabError("MODEL_CONTEXT_BYTE_LIMIT")
            data = self._call("chat", "POST", "chat/completions", payload, timeout_s)
            if data.get("model") != self.config.model:
                self.blocked = "MODEL_IDENTITY_CHANGED"
                raise agent_lab.LabError(self.blocked)
            usage = data.get("usage")
            if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
                    or usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]
                    or usage["completion_tokens"] > max_tokens):
                self.blocked = "MODEL_USAGE_NOT_REPORTED_OR_INVALID"
                raise agent_lab.LabError(self.blocked)
            self.total_tokens += usage["total_tokens"]
            self.events[-1].update(usage=usage, actual_cost_usd="UNKNOWN_UNLESS_REPORTED")
            if "cost" in usage:
                try:
                    cost = Decimal(str(usage["cost"]))
                    if not cost.is_finite() or cost != 0:
                        raise ValueError("Unexpected provider cost")
                except (InvalidOperation, ValueError):
                    self.blocked = "UNEXPECTED_OR_INVALID_COST"
                    raise agent_lab.LabError(self.blocked)
                self.events[-1]["actual_cost_usd"] = str(cost)
            return data, payload

    def budget_state(self):
        return {"request_cap": self.config.request_cap, "requests_used": self.requests_used,
                "transport_attempts": dict(self.transport_attempts), "admission_failures": self.admission_failures,
                "total_reported_tokens": self.total_tokens, "blocked": self.blocked,
                "remaining_provider_requests": "UNKNOWN", "actual_cost_usd": "UNKNOWN_UNLESS_REPORTED"}
