"""Bounded, synthetic loopback TLS evidence for the research service adapter.

This fixture does not establish production DNS/SSRF/egress containment or Engine
success integration. The production dialer's local-address denials stay enabled.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest

from kaggle_experiments.runner import run_bounded_process

PROTOCOL = "athena-core-research-service/1"
MAX_OPENSSL_CAPTURE_BYTES = 64 * 1024


def verify_result(scenario: dict, probe: dict, origin: dict) -> list[str]:
    """Check consumed bytes and independently observed effects, not an exit flag."""
    errors = []
    data = scenario.get("input", scenario)
    if probe.get("protocol") != PROTOCOL:
        return ["probe protocol identity mismatch"]
    if data.get("mode") == "contracts":
        checks = probe.get("checks", [])
        expected = {"production_invalid_validator_before_dns", "production_loopback_denial",
                    "production_ip_literal_denial", "production_non443_denial",
                    "production_engine_zero_admission", "core_leader_cancel_wakes_follower_and_recovers",
                    "core_follower_cancel_preserves_leader", "core_completed_flight_is_not_cache",
                    "blocking_worker_permit_and_negative_control"}
        ids = [check.get("id") for check in checks]
        if len(ids) != len(expected) or set(ids) != expected:
            errors.append("contract checks are missing or duplicated")
        if not all(check.get("passed") is True for check in checks):
            errors.append("a core contract check failed")
        return errors
    mode = data.get("mode", "load")
    if mode not in ("load", "journey", "cancel_leader", "cancel_follower") or probe.get("mode") != mode:
        return ["probe mode differs from the declared experiment"]
    requests = data.get("arrivals", [])
    expected_ids = [request["task_id"] for request in requests]
    rows = probe.get("rows", [])
    ids = [row.get("task_id") for row in rows]
    if len(ids) != len(expected_ids) or len(set(ids)) != len(ids) or set(ids) != set(expected_ids):
        errors.append("offered task identities are missing, duplicated, or unexpected")
    by_id = {request["task_id"]: request for request in requests}
    outcomes = Counter(row.get("outcome") for row in rows)
    counters = probe.get("counters", {})
    if counters.get("offered") != len(requests) or counters.get("completed") != outcomes["completed"]:
        errors.append("offered/completed denominator does not match all task outcomes")
    if counters.get("outcomes") != dict(outcomes):
        errors.append("outcome counters do not reconcile with task rows")
    if counters.get("accepted") != sum(row.get("accepted_ns") is not None for row in rows):
        errors.append("accepted counter does not reconcile with task rows")
    for key in ("active_pending_at_end", "active_origins_at_end", "active_cpu_at_end", "pool_entries_after_drop", "cache_entries_after_drop"):
        if counters.get(key) != 0:
            errors.append(f"undrained probe resource: {key}")
    for key, limit in (("peak_pending", data.get("max_pending", 32)),
                       ("peak_origins", data.get("max_origin_inflight", 4)),
                       ("peak_cpu", data.get("max_origin_inflight", 4)),
                       ("pool_entries_before_drop", data.get("max_pool_entries", 4)),
                       ("cache_entries_before_drop", data.get("max_cache_entries", 4))):
        if not isinstance(counters.get(key), int) or not 0 <= counters[key] <= limit:
            errors.append(f"resource bound not established: {key}")
    if counters.get("available_pending_at_end") != data.get("max_pending", 32):
        errors.append("pending permits were not returned")
    if counters.get("available_origin_at_end") != data.get("max_origin_inflight", 4):
        errors.append("origin permits were not returned")
    if counters.get("available_cpu_at_end") != data.get("max_origin_inflight", 4):
        errors.append("CPU permits were not returned after actual worker joins")
    traces = probe.get("origin_attempts", [])
    trace_ids = [trace.get("attempt_id") for trace in traces]
    if len(trace_ids) != len(set(trace_ids)):
        errors.append("duplicate origin work identity")
    trace_by_id = {trace.get("attempt_id"): trace for trace in traces}
    if counters.get("origin_attempts") != sum(trace.get("attempted") is True for trace in traces):
        errors.append("physical attempt count does not reconcile with transport traces")
    observed = origin.get("requests", [])
    observed_ids = [request.get("attempt_id") for request in observed]
    if len(observed_ids) != len(set(observed_ids)):
        errors.append("an origin attempt produced duplicate requests (possible redirect/retry)")
    observed_by_id = {request.get("attempt_id"): request for request in observed}
    for request in observed:
        trace = trace_by_id.get(request.get("attempt_id"))
        if trace is None or trace.get("attempted") is not True:
            errors.append("origin observed an unattributed physical request")
        elif any(request.get(key) != trace.get(key) for key in ("tenant", "epoch", "source")):
            errors.append("origin request identity differs from its physical trace")
    for row in rows:
        request = by_id.get(row.get("task_id"))
        if request is None:
            continue
        for key in ("scheduled_ns", "offered_ns", "terminal_ns", "latency_ns"):
            if not isinstance(row.get(key), int) or row[key] < 0:
                errors.append(f"missing monotonic timing: {key}")
        if all(isinstance(row.get(key), int) for key in ("scheduled_ns", "offered_ns", "terminal_ns", "latency_ns")):
            schedule_valid = (row["scheduled_ns"] == request.get("at_us", 0) * 1000 if mode == "load"
                              else row.get("input_at_us") == request.get("at_us", 0)
                              and row["scheduled_ns"] >= request.get("at_us", 0) * 1000)
            if not schedule_valid or not row["scheduled_ns"] <= row["offered_ns"] <= row["terminal_ns"]:
                errors.append("offered schedule or terminal timing is inconsistent")
            if row["latency_ns"] != row["terminal_ns"] - row["scheduled_ns"]:
                errors.append("latency omitted scheduled waiting time")
        if row.get("outcome") != "completed":
            if row.get("body") is not None or row.get("representation") is not None:
                errors.append("failed or denied task disclosed a success payload")
            continue
        version = scenario.get("expect", {}).get("versions_by_task", {}).get(request["task_id"], 1)
        expected_body = (f'<p>athena-lab tenant={request["tenant"]} epoch={request.get("epoch", 1)} '
                         f'source={request["source"]} version={version}</p>\n')
        custom_html = scenario.get("lab_options", {}).get("bodies", {}).get(request["source"], "")
        expected_body += custom_html
        if row.get("body") != expected_body:
            errors.append("response body failed independent tenant/source/version oracle")
        try:
            envelope = row.get("representation", "")
            if not envelope.endswith("\n"):
                raise ValueError("AAE envelope lacks terminal newline")
            frames = [json.loads(line) for line in envelope.splitlines()]
            blocks = [frame for frame in frames if frame.get("type") == "block"]
            expected_texts = [f'athena-lab tenant={request["tenant"]} epoch={request.get("epoch", 1)} source={request["source"]} version={version}']
            if custom_html:
                custom_texts = scenario.get("expect", {}).get("block_texts_by_source", {}).get(request["source"])
                if not isinstance(custom_texts, list) or not all(isinstance(text, str) for text in custom_texts):
                    raise ValueError("custom HTML requires independent expected block texts")
                expected_texts += custom_texts
            if not frames or frames[0].get("type") != "meta" or frames[0].get("version") != "aae/1" or frames[0].get("source_url") != f'https://athena-lab.invalid/{request["source"]}' or frames[0].get("fetched_at_unix_ms") != 0:
                raise ValueError("AAE source/version/fixed fixture epoch is wrong")
            if frames[-1].get("type") != "end" or frames[-1].get("block_count") != len(blocks) or frames[0].get("total_blocks") != len(blocks):
                raise ValueError("AAE framing/count is wrong")
            if Counter(block.get("text") for block in blocks) != Counter(expected_texts):
                raise ValueError("AAE projected text failed independent fixture oracle")
            expected_highlights = scenario.get("expect", {}).get("highlights_by_task", {}).get(request["task_id"])
            if expected_highlights is not None and [b.get("text") for b in blocks if b.get("highlight") is True] != expected_highlights:
                raise ValueError("AAE highlights crossed query-specific representation identity")
            if any(frame.get("type") not in {"meta", "block", "link", "end"} for frame in frames) or sum(frame.get("type") == "meta" for frame in frames) != 1 or sum(frame.get("type") == "end" for frame in frames) != 1:
                raise ValueError("AAE contains unexpected control framing")
        except (ValueError, TypeError, AttributeError) as error:
            errors.append("actual AAE output failed oracle: " + str(error))
        if row.get("peer_verified") is not True:
            errors.append("successful task lacks an actual verified TLS peer")
        physical = observed_by_id.get(row.get("origin_id"))
        if row.get("cache_state") == "fresh_hit":
            if not any(r.get("tenant") == request["tenant"] and r.get("epoch") == request.get("epoch", 1)
                       and r.get("source") == request["source"] and r.get("status") == 200 for r in observed):
                errors.append("fresh cache hit has no independently observed stored body")
        elif physical is None or physical.get("status") != row.get("source_status"):
            errors.append("successful source result lacks its observed origin response")
        elif row.get("source_status") == 304:
            expected_tag = f'"lab:{request["tenant"]}:{request.get("epoch", 1)}:{request["source"]}:v{version}"'
            if physical.get("conditional_etag") != expected_tag:
                errors.append("304 result did not revalidate the expected stored representation")
    if mode == "journey":
        last_terminal = 0
        by_task = {row.get("task_id"): row for row in rows}
        for request in requests:
            row = by_task.get(request["task_id"], {})
            if row.get("scheduled_ns", -1) < last_terminal + request.get("at_us", 0) * 1000:
                errors.append("dependent journey offered a step before its predecessor completed")
            last_terminal = row.get("terminal_ns", last_terminal)
    if mode in ("cancel_leader", "cancel_follower"):
        errors.extend(_cancel_evidence_errors(data, probe))
    for trace in traces:
        start, end = trace.get("queue_start_ns"), trace.get("terminal_ns")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            errors.append("origin queue/terminal timing is incomplete")
        service = trace.get("service_start_ns")
        if service is not None and not start <= service <= end:
            errors.append("origin service timing is outside its lifecycle")
    cpu_traces = probe.get("cpu_traces", [])
    if counters.get("actual_cpu_workers_joined") != len(cpu_traces):
        errors.append("actual CPU worker completion does not reconcile with supervisor joins")
    for trace in cpu_traces:
        if trace.get("phase") not in {"parse_projection", "rank_export_aae"} or not all(isinstance(trace.get(key), int) for key in ("queue_start_ns", "execution_start_ns", "execution_end_ns")):
            errors.append("CPU boundary timing is incomplete")
        elif not trace["queue_start_ns"] <= trace["execution_start_ns"] <= trace["execution_end_ns"]:
            errors.append("CPU queue/service lifecycle is inconsistent")
    if origin.get("active_connections") != 0 or origin.get("active_requests") != 0:
        errors.append("actual lab sockets or requests did not drain")
    if origin.get("connection_rejections", 0) or origin.get("request_limit_rejections", 0):
        errors.append("lab resource cap was reached; this cell is not an equivalent completed workload")
    if origin.get("handler_errors", 0):
        errors.append("unexpected actual TLS lab handler failure")
    expect = scenario.get("expect", {})
    for key, actual in (("outcomes", dict(outcomes)), ("origin_requests", len(observed)),
                        ("handshakes", origin.get("handshakes")),
                        ("client_builds", counters.get("client_builds")),
                        ("representation_leaders", counters.get("representation_leaders"))):
        if key in expect and expect[key] != actual:
            errors.append(f"independent scenario expectation failed: {key}: expected {expect[key]!r}, got {actual!r}")
    if "allowed_outcomes" in expect and any(outcome not in expect["allowed_outcomes"] for outcome in outcomes):
        errors.append("scenario produced an unexpected terminal outcome")
    if "max_origin_requests" in expect and len(observed) > expect["max_origin_requests"]:
        errors.append("scenario exceeded expected actual origin work")
    if "origin_statuses" in expect and Counter(r.get("status") for r in observed) != Counter({int(k): v for k, v in expect["origin_statuses"].items()}):
        errors.append("actual conditional/error origin statuses differ from scenario contract")
    if "cpu_phases" in expect and Counter(trace.get("phase") for trace in cpu_traces) != Counter(expect["cpu_phases"]):
        errors.append("actual core phase counts differ from the stored-body lifecycle contract")
    return errors


class OriginStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.perf_counter_ns()
        self.data = {"accepted_connections": 0, "handshakes": 0, "handshake_failures": 0,
                     "active_connections": 0, "peak_connections": 0, "active_requests": 0,
                     "peak_requests": 0, "connection_rejections": 0, "request_limit_rejections": 0,
                     "known_body_write_bytes": 0, "requests": []}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)


class OriginServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, context, stats, options):
        self.context, self.stats, self.options = context, stats, options
        self.slots = threading.BoundedSemaphore(options["max_connections"])
        super().__init__(("127.0.0.1", 0), OriginHandler)

    def get_request(self):
        raw, address = super().get_request()
        raw.settimeout(1.0)
        with self.stats.lock:
            self.stats.data["accepted_connections"] += 1
        try:
            wrapped = self.context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            with self.stats.lock:
                self.stats.data["handshake_failures"] += 1
            raise OSError("synthetic TLS handshake rejected") from None
        with self.stats.lock:
            data = self.stats.data
            data["handshakes"] += 1
            data["active_connections"] += 1
            data["peak_connections"] = max(data["peak_connections"], data["active_connections"])
        return wrapped, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            with self.stats.lock:
                self.stats.data["connection_rejections"] += 1
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self.stats.lock:
                self.stats.data["active_connections"] -= 1

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        key = "connection_read_errors" if isinstance(error, (OSError, ssl.SSLError)) else "handler_errors"
        with self.stats.lock:
            self.stats.data.setdefault(key, 0)
            self.stats.data[key] += 1


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        source = self.path.removeprefix("/")
        tenant = self.headers.get("X-Lab-Tenant", "")
        try:
            epoch = int(self.headers.get("X-Lab-Epoch", "-1"))
            attempt = int(self.headers.get("X-Lab-Attempt", "-1"))
        except ValueError:
            self.close_connection = True
            return
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value) for value in (source, tenant)) or epoch < 0 or attempt < 0:
            self.close_connection = True
            return
        stats = self.server.stats
        record = {"attempt_id": attempt, "source": source, "tenant": tenant, "epoch": epoch,
                  "conditional_etag": self.headers.get("If-None-Match"),
                  "started_ns": time.perf_counter_ns() - stats.started, "ended_ns": None,
                  "status": None, "known_body_write_bytes": 0, "terminal": "running"}
        with stats.lock:
            if len(stats.data["requests"]) >= self.server.options["max_requests"]:
                stats.data["request_limit_rejections"] += 1
                self.close_connection = True
                return
            prior = sum(r["source"] == source and r["tenant"] == tenant and r["epoch"] == epoch for r in stats.data["requests"])
            revisions = self.server.options["revisions"].get(source, [1])
            version = revisions[min(prior, len(revisions) - 1)]
            record["version"] = version
            stats.data["requests"].append(record)
            stats.data["active_requests"] += 1
            stats.data["peak_requests"] = max(stats.data["peak_requests"], stats.data["active_requests"])
        try:
            delay = self.server.options["slow_delay_ms"] if source.startswith("slow") else self.server.options["delay_ms"]
            if delay:
                time.sleep(delay / 1000)
            etag = f'"lab:{tenant}:{epoch}:{source}:v{version}"'
            body = f"<p>athena-lab tenant={tenant} epoch={epoch} source={source} version={version}</p>\n".encode()
            body += self.server.options["bodies"].get(source, "").encode()
            status = 200
            if source == "orphan304" or self.headers.get("If-None-Match") == etag:
                status, body = 304, b""
            elif source == "redirect":
                status = 302
            elif source == "error":
                status = 500
            elif source == "oversize":
                body = b"X" * 20000
            with stats.lock:
                record["status"] = status
                record["body_bytes"] = len(body)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "max-age=0")
            if source == "redirect":
                self.send_header("Location", f"https://athena-lab.invalid:{self.server.server_address[1]}/redirect_target")
            if source == "encoded":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if body:
                self.wfile.write(body)
                self.wfile.flush()
            with stats.lock:
                record["known_body_write_bytes"] = len(body)
                stats.data["known_body_write_bytes"] += len(body)
                record["terminal"] = "response_written"
        except (OSError, ssl.SSLError):
            self.close_connection = True
            with stats.lock:
                record["terminal"] = "client_disconnected"
        finally:
            with stats.lock:
                record["ended_ns"] = time.perf_counter_ns() - stats.started
                stats.data["active_requests"] -= 1


def _scratch_root(path):
    root = Path(path).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("scratch links/junctions are not allowed")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _run_openssl(command, *, cwd=None, timeout):
    result = run_bounded_process(command, cwd=cwd, timeout=timeout,
                                 output_limit=MAX_OPENSSL_CAPTURE_BYTES)
    if result.status == "OUTPUT_LIMIT":
        raise RuntimeError("OpenSSL output exceeded the enforced 64 KiB limit")
    if result.status == "TIMEOUT":
        raise TimeoutError("OpenSSL command exceeded its bounded runtime")
    if result.status != "EXITED":
        raise RuntimeError("OpenSSL process handling failed: " + result.status)
    return result


def _openssl_path():
    candidates = [shutil.which("openssl")]
    if os.name == "nt":
        candidates.append(str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            result = _run_openssl([candidate, "version"], timeout=5)
            version = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and version.startswith("OpenSSL "):
                return candidate, version.strip()
    raise RuntimeError("verified OpenSSL executable is required for ephemeral synthetic certificates")


class TLSLab:
    """Own only a bounded TemporaryDirectory and a bounded loopback server."""

    def __init__(self, scratch_root, **options):
        defaults = {"delay_ms": 2, "slow_delay_ms": 150, "max_connections": 32,
                    "max_requests": 512, "certificate_mode": "valid", "bodies": {}, "revisions": {}}
        if set(options) - set(defaults):
            raise ValueError("unknown TLS lab option")
        defaults.update(options)
        if not 0 <= defaults["delay_ms"] <= 250 or not 0 <= defaults["slow_delay_ms"] <= 250 or not 1 <= defaults["max_connections"] <= 32 or not 1 <= defaults["max_requests"] <= 512 or defaults["certificate_mode"] not in {"valid", "wrong_hostname", "untrusted_ca", "wrong_pin"}:
            raise ValueError("lab resource or certificate mode is outside the admitted envelope")
        bodies = defaults["bodies"]
        if not isinstance(bodies, dict) or len(bodies) > 16 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", key) or not isinstance(value, str) or len(value.encode()) > 12000 for key, value in bodies.items()) or sum(len(value.encode()) for value in bodies.values()) > 65536:
            raise ValueError("synthetic HTML body fixtures exceed their bound")
        revisions = defaults["revisions"]
        if not isinstance(revisions, dict) or len(revisions) > 16 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", key) or not isinstance(values, list) or not 1 <= len(values) <= 16 or any(not isinstance(value, int) or not 1 <= value <= 10 for value in values) for key, values in revisions.items()):
            raise ValueError("synthetic revision sequences exceed their bound")
        self.options = defaults
        self.root = _scratch_root(scratch_root)
        self.stats = OriginStats()
        self.server = self.thread = self.temporary = self.directory = None
        self.cleanup = {}

    def _command(self, *arguments):
        result = _run_openssl([self.openssl, *map(str, arguments)], cwd=self.directory, timeout=10)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"synthetic certificate generation failed with exit {result.returncode}: {stderr[-1000:]}")

    def _ca(self, prefix):
        self._command("req", "-new", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
                      "-keyout", self.directory / f"{prefix}.key", "-out", self.directory / f"{prefix}.pem",
                      "-subj", "/CN=Athena synthetic research CA", "-addext", "basicConstraints=critical,CA:TRUE",
                      "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    def __enter__(self):
        try:
            self.openssl, self.openssl_version = _openssl_path()
            self.temporary = tempfile.TemporaryDirectory(prefix="tls-", dir=self.root)
            self.directory = Path(self.temporary.name)
            if os.name != "nt":
                self.directory.chmod(0o700)
            self._ca("ca")
            hostname = "wrong-host.invalid" if self.options["certificate_mode"] == "wrong_hostname" else "athena-lab.invalid"
            self._command("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", self.directory / "server.key",
                          "-out", self.directory / "server.csr", "-subj", f"/CN={hostname}")
            (self.directory / "leaf.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:" + hostname + "\n", encoding="ascii")
            self._command("x509", "-req", "-in", self.directory / "server.csr", "-CA", self.directory / "ca.pem",
                          "-CAkey", self.directory / "ca.key", "-CAcreateserial", "-days", "1", "-sha256",
                          "-extfile", self.directory / "leaf.ext", "-out", self.directory / "server.pem")
            self._command("x509", "-in", self.directory / "server.pem", "-outform", "DER", "-out", self.directory / "peer.der")
            trust_path = self.directory / "ca.pem"
            if self.options["certificate_mode"] == "untrusted_ca":
                self._ca("other-ca")
                trust_path = self.directory / "other-ca.pem"
            pin_path = self.directory / "peer.der"
            if self.options["certificate_mode"] == "wrong_pin":
                pin_path = self.directory / "wrong-pin.der"
                pin_path.write_bytes(b"synthetic deliberately wrong leaf certificate pin")
            self.certificate_sha256 = hashlib.sha256((self.directory / "peer.der").read_bytes()).hexdigest()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(self.directory / "server.pem", self.directory / "server.key")
            self.server = OriginServer(context, self.stats, self.options)
            self.lab_input = {"scope": "synthetic-loopback-tls-v1", "host": "athena-lab.invalid",
                              "address": f"127.0.0.1:{self.server.server_address[1]}",
                              "ca_pem_path": str(trust_path), "peer_der_path": str(pin_path)}
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=False)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exception_type, exception, tb):
        start = time.perf_counter_ns()
        address = self.server.server_address if self.server else None
        try:
            if self.thread and self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=3)
            if self.server:
                self.server.server_close()
        finally:
            if self.temporary:
                directory = self.directory.resolve(strict=True)
                if not directory.is_relative_to(self.root) or directory == self.root or self.directory.is_symlink() or (hasattr(self.directory, "is_junction") and self.directory.is_junction()):
                    raise RuntimeError("temporary lab cleanup target escaped its owned scratch root")
                self.temporary.cleanup()
        port_closed = None
        if address:
            try:
                with socket.create_connection(address, timeout=0.2):
                    port_closed = False
            except OSError:
                port_closed = True
        self.cleanup = {"duration_ns": time.perf_counter_ns() - start,
                        "server_thread_stopped": self.thread is None or not self.thread.is_alive(),
                        "private_fixture_removed": self.directory is None or not self.directory.exists(),
                        "port_closed_after_shutdown": port_closed}
        return False


def _cancel_evidence_errors(data, probe):
    expected = (["cancelled", "shared_cancelled", "completed"] if data["mode"] == "cancel_leader"
                else ["completed", "cancelled"])
    requests, rows = data.get("arrivals", []), {r.get("task_id"): r for r in probe.get("rows", [])}
    overlap = probe.get("cancellation_control", {})
    errors = []
    if (overlap.get("leader_polled_pending") is not True or overlap.get("follower_polled_pending") is not True
            or overlap.get("source_leaders_before_cancel") != 1):
        errors.append("cancellation control did not establish an actual shared-source follower before cancellation")
    if len(requests) != len(expected) or any(rows.get(r["task_id"], {}).get("outcome") != outcome
                                           for r, outcome in zip(requests, expected)):
        errors.append("cancellation roles or recovery outcomes differ from the independent contract")
    if len(requests) == 3:
        retry = rows.get(requests[2]["task_id"], {})
        preceding = [rows.get(r["task_id"], {}).get("terminal_ns") for r in requests[:2]]
        if not all(isinstance(t, int) for t in preceding) or retry.get("scheduled_ns", -1) < max(preceding):
            errors.append("recovery was offered before the cancelled shared flight became terminal")
    return errors


def run_scenario(binary, scenario, scratch_root, timeout_s=45):
    """Run one finite cell; preserve sanitized failed evidence, remove all keys."""
    if not 1 <= timeout_s <= 60:
        raise ValueError("subprocess timeout must be within 1..60 seconds")
    binary = Path(binary).resolve(strict=True)
    root = _scratch_root(scratch_root)
    data = copy.deepcopy(scenario.get("input", scenario))
    data.setdefault("protocol", PROTOCOL)
    report = {"protocol": PROTOCOL, "scenario_id": scenario.get("id", "explicit-cell"),
              "process": {}, "origin": {}, "cleanup": {}, "probe": {}, "oracle_errors": [],
              "scope": "synthetic local TLS adapter; production permitted-public-network and deployed service NOT VERIFIED",
              "authenticity": "NOT VERIFIED: probe, origin observer, and harness share OS authority"}
    started = time.perf_counter_ns()
    lab = None

    def execute_probe(input_path):
        command = [str(binary), str(input_path)]
        result = run_bounded_process(command, timeout=timeout_s, output_limit=4 * 1024 * 1024)
        report["process"].update({"elapsed_ns": result.elapsed_ns, "exit_code": result.returncode,
                                  "status": result.status, "stderr": result.stderr.decode("utf-8", errors="replace")[-8192:],
                                  "stdout_bytes": len(result.stdout),
                                  "captured_output_bytes": result.captured_output_bytes,
                                  "output_limit_bytes": result.output_limit_bytes})
        if result.status == "OUTPUT_LIMIT":
            raise ValueError("probe output exceeded the enforced 4 MiB combined pipe limit")
        if result.status == "TIMEOUT":
            report["process"]["timeout_s"] = timeout_s
            raise TimeoutError("probe process exceeded its bounded runtime")
        if result.status != "EXITED":
            raise RuntimeError("probe pipe handling failed: " + result.status)
        return result.stdout.decode("utf-8", errors="strict"), result.returncode

    try:
        if data.get("mode") == "contracts":
            temporary = tempfile.TemporaryDirectory(prefix="contracts-", dir=root)
            try:
                input_path = Path(temporary.name) / "input.json"
                input_path.write_text(json.dumps(data), encoding="utf-8")
                stdout, returncode = execute_probe(input_path)
            finally:
                temporary.cleanup()
                report["cleanup"] = {"private_fixture_removed": not Path(temporary.name).exists(),
                                      "server_thread_stopped": True}
        else:
            with TLSLab(root, **scenario.get("lab_options", {})) as lab:
                data["lab"] = lab.lab_input
                input_path = lab.directory / "input.json"
                input_path.write_text(json.dumps(data), encoding="utf-8")
                stdout, returncode = execute_probe(input_path)
                report["origin_at_probe_exit"] = lab.stats.snapshot()
            report["origin"] = lab.stats.snapshot()
            report["origin"]["certificate_sha256"] = lab.certificate_sha256
            report["origin"]["openssl_version"] = lab.openssl_version
            report["origin"]["openssl_ssl_runtime"] = ssl.OPENSSL_VERSION
            report["origin"]["body_counter_limit"] = "bytes accepted by the server's write, not independently acknowledged TCP bytes"
            report["cleanup"] = lab.cleanup
        report["probe"] = json.loads(stdout)
        report["oracle_errors"] = verify_result({**scenario, "input": data}, report["probe"], report["origin"])
        if returncode:
            report["oracle_errors"].append(f"probe process exit {returncode}")
    except Exception as error:
        report["oracle_errors"].append(f"{type(error).__name__}: {str(error)[:1500]}")
        if lab:
            report["origin"] = lab.stats.snapshot()
            report["cleanup"] = lab.cleanup
        if isinstance(error, TimeoutError):
            report["process"].setdefault("timeout_s", timeout_s)
        report["failure_trace"] = traceback.format_exc(limit=4)[-4000:]
    report["total_elapsed_ns"] = time.perf_counter_ns() - started
    if not report["cleanup"].get("private_fixture_removed") or not report["cleanup"].get("server_thread_stopped"):
        report["oracle_errors"].append("owned fixture or server teardown was not verified")
    if report["cleanup"].get("port_closed_after_shutdown") is False:
        report["oracle_errors"].append("lab port remained reachable after shutdown")
    report["oracle_passed"] = not report["oracle_errors"]
    if not report["oracle_passed"]:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="failure-", suffix=".json", dir=root, delete=False) as failure:
            json.dump(report, failure, indent=2)
            report["failure_evidence"] = failure.name
    return report


def _arrival(index, at_us=0, **values):
    return {"task_id": f"t{index}", "at_us": at_us, "source": "article", "tenant": "A", "query": "q", "epoch": 1, **values}


def cleanup_failure_control(scratch_root):
    class ControlledLabFailure(RuntimeError):
        pass

    lab = TLSLab(scratch_root)
    observed_failure = False
    try:
        with lab:
            raise ControlledLabFailure("controlled failure after actual TLS server startup")
    except ControlledLabFailure:
        observed_failure = True
    cleanup = lab.cleanup
    return {"id": "lab-exception-cleanup", "passed": observed_failure
            and cleanup.get("private_fixture_removed") is True
            and cleanup.get("server_thread_stopped") is True
            and cleanup.get("port_closed_after_shutdown") is True,
            "cleanup": cleanup, "origin": lab.stats.snapshot()}


def correctness_scenarios():
    """Distinct finite behavior controls; these are not confirmation samples."""
    result = [{"id": "core-contracts", "input": {"protocol": PROTOCOL, "mode": "contracts"}}]
    for variant, handshakes in (("client_per_fetch", 6), ("policy_pool", 1)):
        result.append({"id": f"reuse-{variant}", "input": {"protocol": PROTOCOL, "mode": "load", "variant": variant,
                       "arrivals": [_arrival(i, i * 80000) for i in range(6)]},
                       "expect": {"outcomes": {"completed": 6}, "origin_requests": 6,
                                  "handshakes": handshakes, "client_builds": handshakes}})
    result.append({"id": "stored-body-revision-update", "input": {"protocol": PROTOCOL, "mode": "load", "variant": "policy_pool",
                   "arrivals": [_arrival(i, i * 100000, cache_mode="revalidate") for i in range(4)]},
                   "lab_options": {"revisions": {"article": [1, 1, 2, 2]}},
                   "expect": {"outcomes": {"completed": 4}, "origin_requests": 4,
                              "origin_statuses": {200: 2, 304: 2}, "versions_by_task": {"t0": 1, "t1": 1, "t2": 2, "t3": 2},
                              "cpu_phases": {"parse_projection": 2, "rank_export_aae": 4}}})
    result.append({"id": "stored-304-fresh-revoked-errors", "input": {"protocol": PROTOCOL, "mode": "load", "variant": "policy_pool",
                   "fresh_for_ms": 100, "revocations": [{"tenant": "A", "epoch": 1, "at_us": 220000}],
                   "arrivals": [_arrival(0, 0, cache_mode="revalidate"), _arrival(1, 70000, cache_mode="revalidate"),
                                _arrival(2, 140000, cache_mode="fresh"), _arrival(3, 250000, cache_mode="fresh"),
                                _arrival(4, 320000, tenant="B", cache_mode="revalidate"),
                                _arrival(5, 390000, tenant="B", source="orphan304", cache_mode="revalidate"),
                                _arrival(6, 460000, tenant="B", source="redirect"),
                                _arrival(7, 530000, tenant="B", source="oversize"),
                                _arrival(8, 600000, tenant="B", source="encoded")]},
                   "expect": {"outcomes": {"completed": 4, "revoked": 1, "orphan_304": 1, "http_302": 1, "too_large": 1, "unsupported_encoding": 1},
                              "origin_requests": 7, "origin_statuses": {200: 4, 304: 2, 302: 1}}})
    result.append({"id": "effective-pool-partition", "input": {"protocol": PROTOCOL, "mode": "load", "variant": "policy_pool", "max_pool_entries": 4,
                   "arrivals": [_arrival(0, 0), _arrival(1, 80000, tenant="B"), _arrival(2, 160000, epoch=2),
                                _arrival(3, 240000, max_body_bytes=8), _arrival(4, 320000),
                                _arrival(5, 400000, privacy="isolated")]},
                   "expect": {"outcomes": {"completed": 5, "too_large": 1}, "origin_requests": 6, "client_builds": 5, "handshakes": 5}})
    result.append({"id": "hot-burst-pending-bound", "input": {"protocol": PROTOCOL, "mode": "load", "max_pending": 8, "max_origin_inflight": 2,
                   "arrivals": [_arrival(i, query=f"q{i % 3}") for i in range(24)]}, "lab_options": {"delay_ms": 50},
                   "expect": {"outcomes": {"completed": 8, "pending_rejected": 16}, "origin_requests": 1}})
    result.append({"id": "cold-burst-reject", "input": {"protocol": PROTOCOL, "mode": "load", "max_pending": 12, "max_origin_inflight": 2,
                   "arrivals": [_arrival(i, source=f"cold{i}") for i in range(12)]}, "lab_options": {"delay_ms": 50},
                   "expect": {"outcomes": {"completed": 2, "origin_busy": 10}, "origin_requests": 2}})
    result.append({"id": "cold-burst-bounded-wait", "input": {"protocol": PROTOCOL, "mode": "load", "queue_mode": "bounded_wait",
                   "max_pending": 6, "max_origin_inflight": 2, "arrivals": [_arrival(i, source=f"cold{i}") for i in range(6)]},
                   "lab_options": {"delay_ms": 20}, "expect": {"outcomes": {"completed": 6}, "origin_requests": 6}})
    result.append({"id": "absolute-deadline-drain", "input": {"protocol": PROTOCOL, "mode": "load", "queue_mode": "bounded_wait",
                   "deadline_ms": 25, "max_pending": 6, "max_origin_inflight": 2,
                   "arrivals": [_arrival(i, source=f"slow{i}") for i in range(6)]},
                   "expect": {"allowed_outcomes": ["timed_out", "transport_timeout"], "max_origin_requests": 2}})
    result.append({"id": "follower-cancel-real-origin", "input": {"protocol": PROTOCOL, "mode": "cancel_follower",
                   "arrivals": [_arrival(0, query="leader"), _arrival(1, query="follower")]},
                   "lab_options": {"delay_ms": 50}, "expect": {"outcomes": {"completed": 1, "cancelled": 1}, "origin_requests": 1}})
    result.append({"id": "leader-cancel-real-origin", "input": {"protocol": PROTOCOL, "mode": "cancel_leader",
                   "arrivals": [_arrival(0, query="leader"), _arrival(1, query="follower"), _arrival(2, query="retry")]},
                   "lab_options": {"delay_ms": 60}, "expect": {"outcomes": {"completed": 1, "cancelled": 1, "shared_cancelled": 1}, "max_origin_requests": 2}})
    result.append({"id": "shared-source-error-not-cached", "input": {"protocol": PROTOCOL, "mode": "load",
                   "arrivals": [_arrival(i, source="error", query=f"q{i % 3}") for i in range(8)] + [_arrival(8, 150000, source="error")]},
                   "lab_options": {"delay_ms": 30}, "expect": {"outcomes": {"http_500": 9}, "origin_requests": 2}})
    for mode, outcome, observed in (("wrong_hostname", "transport_error", 0), ("untrusted_ca", "transport_error", 0),
                                    ("wrong_pin", "tls_peer_certificate_mismatch", 1)):
        result.append({"id": f"tls-denied-{mode}", "input": {"protocol": PROTOCOL, "mode": "load", "arrivals": [_arrival(0)]},
                       "lab_options": {"certificate_mode": mode}, "expect": {"outcomes": {outcome: 1}, "origin_requests": observed}})
    stateful = {"reuse-client_per_fetch", "reuse-policy_pool", "stored-body-revision-update",
                "stored-304-fresh-revoked-errors", "effective-pool-partition"}
    for scenario in result:
        if scenario["id"] in stateful:
            data = scenario["input"]
            data.update(mode="journey", deadline_ms=5000, pool_lifetime_ms=30000)
            for arrival in data["arrivals"]:
                arrival["at_us"] = 0
            if scenario["id"] == "stored-304-fresh-revoked-errors":
                data["revocations"] = []
                data["fresh_for_ms"] = 1000
                data["arrivals"][3]["revoke_before"] = True
    result.append({"id": "query-specific-native-representation", "input": {
        "protocol": PROTOCOL, "mode": "load", "arrivals": [_arrival(0, query="alpha"), _arrival(1, query="beta")]},
        "lab_options": {"delay_ms": 50, "bodies": {"article": "<p>alpha</p><p>beta</p>"}},
        "expect": {"outcomes": {"completed": 2}, "origin_requests": 1, "representation_leaders": 2,
                   "block_texts_by_source": {"article": ["alpha", "beta"]},
                   "highlights_by_task": {"t0": ["alpha"], "t1": ["beta"]}}})
    return result


class OracleTests(unittest.TestCase):
    def test_tls_certificate_tool_capture_rejects_output_over_hard_cap(self):
        with tempfile.TemporaryDirectory(prefix="athena-openssl-output-") as folder:
            lab = TLSLab(folder)
            lab.openssl = sys.executable
            lab.directory = Path(folder)
            script = "import os; os.write(1, b'x' * (64 * 1024 + 1))"
            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                lab._command("-c", script)

    @unittest.skipUnless(os.name == "posix", "direct executable-process boundary requires POSIX")
    def test_probe_output_cap_is_enforced_and_failed_fixture_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="athena-service-output-bound-") as folder:
            root = Path(folder)
            binary = root / "noisy-probe"
            binary.write_text("#!/usr/bin/env python3\nimport os,time\n"
                              "chunk=b'x'*65536\n"
                              "for _ in range(65): os.write(1, chunk)\n"
                              "time.sleep(30)\n", encoding="utf-8")
            binary.chmod(0o700)
            scenario = {"id": "output-limit", "input": {"mode": "contracts"}}
            report = run_scenario(binary, scenario, root, timeout_s=5)
            self.assertFalse(report["oracle_passed"])
            self.assertEqual(report["process"]["status"], "OUTPUT_LIMIT")
            self.assertEqual(report["process"]["captured_output_bytes"], 4 * 1024 * 1024)
            self.assertTrue(report["cleanup"]["private_fixture_removed"])

    def test_cancellation_requires_joined_roles_and_recovery_after_terminal(self):
        data = {"mode": "cancel_leader", "arrivals": [{"task_id": name} for name in ("leader", "follower", "retry")]}
        probe = {"cancellation_control": {"leader_polled_pending": True, "follower_polled_pending": True,
                                          "source_leaders_before_cancel": 1},
                 "rows": [{"task_id": "leader", "outcome": "cancelled", "terminal_ns": 40},
                          {"task_id": "follower", "outcome": "shared_cancelled", "terminal_ns": 50},
                          {"task_id": "retry", "outcome": "completed", "scheduled_ns": 60}]}
        self.assertEqual(_cancel_evidence_errors(data, probe), [])
        for defect in ("not-shared", "wrong-role", "early-retry"):
            changed = copy.deepcopy(probe)
            if defect == "not-shared":
                changed["cancellation_control"]["source_leaders_before_cancel"] = 2
            elif defect == "wrong-role":
                changed["rows"][1]["outcome"] = "completed"
            else:
                changed["rows"][2]["scheduled_ns"] = 30
            with self.subTest(defect=defect):
                self.assertEqual(len(_cancel_evidence_errors(data, changed)), 1)

    def test_dependent_journey_keeps_its_mode_and_rejects_load_evidence(self):
        scenario, probe, origin = self.valid_result()
        scenario["mode"] = "journey"
        probe["mode"] = "journey"
        probe["rows"][0]["input_at_us"] = 0
        self.assertEqual(verify_result(scenario, probe, origin), [])
        probe["mode"] = "load"
        self.assertIn("probe mode differs from the declared experiment", verify_result(scenario, probe, origin))

    def valid_result(self):
        scenario = {"arrivals": [{"task_id": "one", "at_us": 0, "source": "article", "tenant": "A",
                                  "query": "q", "epoch": 1}]}
        text = "athena-lab tenant=A epoch=1 source=article version=1"
        body = "<p>" + text + "</p>\n"
        aae = '\n'.join(json.dumps(frame) for frame in [
            {"type": "meta", "version": "aae/1", "source_url": "https://athena-lab.invalid/article",
             "fetched_at_unix_ms": 0, "total_blocks": 1},
            {"type": "block", "text": text}, {"type": "end", "block_count": 1}]) + '\n'
        probe = {"protocol": PROTOCOL, "mode": "load", "rows": [
            {"task_id": "one", "scheduled_ns": 0, "offered_ns": 10, "accepted_ns": 15,
             "terminal_ns": 100, "latency_ns": 100, "outcome": "completed", "body": body,
             "representation": aae, "source_status": 200, "origin_id": 0,
             "cache_state": "origin_200", "peer_verified": True}],
                 "counters": {"offered": 1, "accepted": 1, "completed": 1, "outcomes": {"completed": 1},
                              "origin_attempts": 1, "peak_pending": 1, "peak_origins": 1, "peak_cpu": 0,
                              "active_pending_at_end": 0, "active_origins_at_end": 0, "active_cpu_at_end": 0,
                              "pool_entries_before_drop": 0, "cache_entries_before_drop": 0,
                              "pool_entries_after_drop": 0, "cache_entries_after_drop": 0,
                              "available_pending_at_end": 32, "available_origin_at_end": 4,
                              "available_cpu_at_end": 4, "actual_cpu_workers_joined": 0},
                 "origin_attempts": [{"attempt_id": 0, "attempted": True, "tenant": "A", "epoch": 1,
                                      "source": "article", "queue_start_ns": 20, "terminal_ns": 90,
                                      "service_start_ns": 25}], "cpu_traces": []}
        origin = {"requests": [{"attempt_id": 0, "tenant": "A", "epoch": 1,
                                "source": "article", "status": 200}],
                  "handshakes": 1, "active_connections": 0, "active_requests": 0}
        return scenario, probe, origin

    def test_origin_body_is_an_independent_oracle(self):
        scenario, probe, origin = self.valid_result()
        self.assertEqual(verify_result(scenario, probe, origin), [])
        corrupt = copy.deepcopy(probe)
        corrupt["rows"][0]["body"] = corrupt["rows"][0]["body"].replace("tenant=A", "tenant=B")
        self.assertEqual(verify_result(scenario, corrupt, origin),
                         ["response body failed independent tenant/source/version oracle"])

    def test_missing_task_cannot_disappear_from_denominator(self):
        scenario, probe, origin = self.valid_result()
        self.assertEqual(verify_result(scenario, probe, origin), [])
        probe["rows"] = []
        probe["counters"].update(offered=0, accepted=0, completed=0, outcomes={})
        self.assertIn("offered task identities are missing, duplicated, or unexpected", verify_result(scenario, probe, origin))

    def test_fast_timing_cannot_replace_actual_aae_content(self):
        scenario, probe, origin = self.valid_result()
        self.assertEqual(verify_result(scenario, probe, origin), [])
        probe["rows"][0]["representation"] = probe["rows"][0]["representation"].replace("source=article", "source=wrong")
        self.assertEqual(verify_result(scenario, probe, origin),
                         ["actual AAE output failed oracle: AAE projected text failed independent fixture oracle"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-oracle", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--scenario-id")
    options = parser.parse_args()
    if options.self_test_oracle:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(OracleTests))
        return 0 if result.wasSuccessful() else 1
    if options.list_scenarios:
        print(json.dumps(correctness_scenarios(), indent=2))
        return 0
    if not options.binary or not options.scratch_root:
        parser.error("--binary and --scratch-root are required for actual execution")
    if options.self_test:
        results = [run_scenario(options.binary, scenario, options.scratch_root)
                   for scenario in correctness_scenarios()]
        cleanup_control = cleanup_failure_control(options.scratch_root)
        passed = all(result["oracle_passed"] for result in results) and cleanup_control["passed"]
        print(json.dumps({"protocol": PROTOCOL, "passed": passed, "scenarios": results,
                          "cleanup_failure_control": cleanup_control}))
        return 0 if passed else 1
    if options.input:
        scenario = json.loads(options.input.read_text(encoding="utf-8"))
    elif options.scenario_id:
        scenario = next((scenario for scenario in correctness_scenarios() if scenario["id"] == options.scenario_id), None)
        if scenario is None:
            parser.error("unknown scenario ID")
    else:
        parser.error("select --self-test, --input, or --scenario-id")
    result = run_scenario(options.binary, scenario, options.scratch_root)
    print(json.dumps(result))
    return 0 if result["oracle_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
