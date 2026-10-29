"""Bounded MCP protocol and Rust-core integration preflight driver."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from cloud_experiments import campaign
from cloud_experiments.agent_lab import LabError, RustProbeProducer, run_task, tasks, validate_aae

CONTRACT = json.loads((ROOT / "protocol_contract.json").read_text(encoding="utf-8"))
CORE_FIXTURES = json.loads((ROOT / "core_fixtures.json").read_text(encoding="utf-8"))
PROTOCOL_VERSION = CONTRACT["protocol_version"]
MAX_FRAME_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_STDERR_BYTES = 65_536
MAX_CORE_INPUT_BYTES = 131_072
MAX_CORE_OUTPUT_BYTES = 131_072
RPC_TIMEOUT_SECONDS = 8.0
STARTUP_TIMEOUT_SECONDS = 10.0


class DriverError(RuntimeError):
    pass


def _request(method: str, request_id: str, params: dict[str, Any], version: str = PROTOCOL_VERSION,
             progress_token: str | None = None) -> dict[str, Any]:
    metadata = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "athena-l03-driver", "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if progress_token is not None:
        metadata["progressToken"] = progress_token
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            **params,
            "_meta": metadata,
        },
    }


def _legacy_initialize(request_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "athena-l03-legacy-probe", "version": "1.0.0"},
        },
    }


def _wire_bytes(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _assert_response(response: dict[str, Any], request_id: str) -> dict[str, Any]:
    if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
        raise DriverError(f"unexpected response identity for {request_id}")
    return response


def _validate_discovery(response: dict[str, Any]) -> None:
    result = response.get("result")
    if not isinstance(result, dict):
        raise DriverError("server/discover did not return a result")
    supported = result.get("supportedVersions")
    if not isinstance(supported, list) or PROTOCOL_VERSION not in supported:
        raise DriverError("server/discover omitted pinned protocol version")
    if any(version.startswith("2025-") for version in supported):
        raise DriverError("strict modern fixture advertised a legacy protocol version")


def _validate_tools(response: dict[str, Any]) -> None:
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise DriverError("tools/list did not return a tool array")
    tool = next((item for item in tools if item.get("name") == CONTRACT["tool_name"]), None)
    if not isinstance(tool, dict):
        raise DriverError("tools/list omitted the fixture tool")
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    fixture_case_schema = properties.get("fixture_case") if isinstance(properties, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else []
    seen_refs: set[str] = set()
    while isinstance(fixture_case_schema, dict) and "$ref" in fixture_case_schema:
        reference = fixture_case_schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen_refs:
            raise DriverError("tool schema contains a non-local or cyclic reference")
        seen_refs.add(reference)
        target: Any = schema
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise DriverError("tool schema contains a dangling local reference")
            target = target[part]
        fixture_case_schema = target
    if (not isinstance(fixture_case_schema, dict)
            or fixture_case_schema.get("type") != "string"
            or set(fixture_case_schema.get("enum", [])) != {"long", "short"}
            or "fixture_case" not in required):
        raise DriverError("tool schema differs from the fixed fixture-case contract")


def _expected_core_outputs(core_probe: Path) -> dict[str, str]:
    if not core_probe.is_file():
        raise DriverError("provide an existing Rust core_probe executable")
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        if key in os.environ
    }
    outputs = {}
    for fixture_case in ("long", "short"):
        fixture = CORE_FIXTURES[fixture_case]
        input_bytes = json.dumps(
            fixture["input"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(input_bytes) > MAX_CORE_INPUT_BYTES:
            raise DriverError(f"{fixture_case} core fixture exceeds the input limit")
        try:
            completed = subprocess.run(
                [str(core_probe), "export", "-", "0"],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=PROJECT_ROOT,
                env=environment,
                timeout=RPC_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DriverError(f"core_probe timed out for {fixture_case}") from exc
        if completed.returncode != 0:
            raise DriverError(f"core_probe failed for {fixture_case} (exit {completed.returncode})")
        if not completed.stdout or len(completed.stdout) > MAX_CORE_OUTPUT_BYTES:
            raise DriverError(f"core_probe output for {fixture_case} violates the byte bound")
        try:
            parsed = validate_aae(
                completed.stdout,
                fixture["input"]["url"],
                max_bytes=MAX_CORE_OUTPUT_BYTES,
            )
        except (ImportError, LabError, ValueError) as exc:
            raise DriverError(f"core_probe output for {fixture_case} is not valid AAE: {exc}") from exc
        if parsed["meta"].get("fetched_at_unix_ms") != fixture["input"]["observed_at_unix_ms"]:
            raise DriverError(f"core_probe observation time differs for {fixture_case}")
        block_text = "\n".join(block["text"] for block in parsed["blocks"])
        if any(fragment not in block_text for fragment in fixture["expected_contains"]):
            raise DriverError(f"core_probe output omitted expected fixture text for {fixture_case}")
        link_urls = {link["url"] for link in parsed["links"]}
        expected_link = fixture["expected_link"]
        if (expected_link is not None and expected_link not in link_urls) or (
            expected_link is None and link_urls
        ):
            raise DriverError(f"core_probe links differ from the {fixture_case} fixture oracle")
        outputs[fixture_case] = completed.stdout.decode("utf-8")
    return outputs


def _validate_tool_result(
    response: dict[str, Any], fixture_case: str, expected_text: str
) -> int:
    result = response.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    text = content[0].get("text") if isinstance(content, list) and content else None
    if not isinstance(text, str) or text != expected_text:
        raise DriverError(f"tool result differs from the Rust core oracle for {fixture_case}: {str(text)[:120]!r}")
    try:
        fixture = CORE_FIXTURES[fixture_case]
        parsed = validate_aae(
            text.encode("utf-8"), fixture["input"]["url"], max_bytes=MAX_CORE_OUTPUT_BYTES
        )
    except (ImportError, LabError, ValueError) as exc:
        raise DriverError(f"tool result is not valid Athena AAE for {fixture_case}: {exc}") from exc
    if parsed["meta"].get("fetched_at_unix_ms") != fixture["input"]["observed_at_unix_ms"]:
        raise DriverError("tool result observation timestamp differs from its fixture")
    block_text = "\n".join(block["text"] for block in parsed["blocks"])
    if any(fragment not in block_text for fragment in fixture["expected_contains"]):
        raise DriverError("tool result omitted expected core fixture content")
    link_urls = {link["url"] for link in parsed["links"]}
    expected_link = fixture["expected_link"]
    if (expected_link is not None and expected_link not in link_urls) or (
        expected_link is None and link_urls
    ):
        raise DriverError("tool result links differ from the fixture oracle")
    return len(text.encode("utf-8"))


def _validate_tool_rejection(
    response: dict[str, Any], request_id: str, expected_core: dict[str, str]
) -> dict[str, Any]:
    """Require an explicit schema rejection without standardizing SDK wording."""
    _assert_response(response, request_id)
    error = response.get("error")
    if isinstance(error, dict):
        if "result" in response or type(error.get("code")) is not int:
            raise DriverError("invalid-schema JSON-RPC error had an invalid response shape")
        evidence = json.dumps(error, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw = evidence.encode("utf-8")
        return {
            "rejected": True,
            "response_kind": "json_rpc_error",
            "error_code": error["code"],
            "error_utf8_bytes": len(raw),
            "error_sha256": hashlib.sha256(raw).hexdigest(),
        }

    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is not True:
        raise DriverError(f"server accepted invalid tool arguments for {request_id}")
    content = result.get("content")
    messages = [block.get("text") for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)] if isinstance(content, list) else []
    evidence = "\n".join(messages)
    if not evidence.strip() or any(payload in evidence for payload in expected_core.values()):
        raise DriverError(f"server returned no independent schema-rejection evidence for {request_id}")
    raw = evidence.encode("utf-8")
    return {
        "rejected": True,
        "response_kind": "tool_error_result",
        "error_utf8_bytes": len(raw),
        "error_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _validate_protocol_error(body: bytes, expected_code: int | tuple[int, ...], *,
                             expected_id: str,
                             expected_requested_version: str | None = None) -> int:
    try:
        response = json.loads(body)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise DriverError("protocol rejection omitted a JSON-RPC error response") from exc
    if not isinstance(response, dict):
        raise DriverError("protocol rejection did not return a JSON-RPC object")
    if response.get("id") != expected_id or response.get("jsonrpc") != "2.0":
        raise DriverError("protocol rejection did not preserve the JSON-RPC response identity")
    error = response.get("error")
    if "result" in response or not isinstance(error, dict) or type(error.get("code")) is not int:
        raise DriverError("protocol rejection did not return one well-formed JSON-RPC error")
    allowed_codes = {expected_code} if isinstance(expected_code, int) else set(expected_code)
    if error["code"] not in allowed_codes:
        raise DriverError(f"protocol rejection had an unexpected JSON-RPC error: {error!r}")
    error_code = error["code"]
    if error_code == -32022:
        data = error.get("data")
        versions = data.get("supported") if isinstance(data, dict) else None
        if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
            raise DriverError("unsupported-version error omitted the pinned supported version")
        if (expected_requested_version is not None
                and data.get("requested") != expected_requested_version):
            raise DriverError("unsupported-version error did not identify the rejected request version")
    return error_code


def _validate_legacy_stdio_response(response: dict[str, Any], request_id: str) -> int:
    """Require the pinned unsupported-version error, not an arbitrary server error."""
    _assert_response(response, request_id)
    return _validate_protocol_error(
        _wire_bytes(response), -32022, expected_id=request_id,
        expected_requested_version="2025-11-25",
    )


def _make_command(sdk: str, transport: str, rust_bin: Path, node: Path, ts_server: Path, port: int | None) -> list[str]:
    if sdk == "rust":
        command = [str(rust_bin), transport]
    else:
        command = [str(node), str(ts_server), transport]
    if transport == "http":
        if port is None:
            raise DriverError("HTTP command requires a reserved port")
        command.append(str(port))
    return command


def _spawn(
    command: list[str], token: str, core_probe: Path, *, stdio: bool, cwd: Path | None = None,
    tenant: str = "tenant-a", bound_task_id: str | None = None,
    max_inflight: int | None = None,
) -> tuple[subprocess.Popen[bytes], bytearray, threading.Thread]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    env.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "RUST_LOG": "off"})
    env["ATHENA_L03_AUTH_TOKEN"] = token
    env["ATHENA_L03_TENANT"] = tenant
    if bound_task_id is not None:
        env["ATHENA_L03_BOUND_TASK"] = bound_task_id
    if max_inflight is not None:
        env["ATHENA_MCP_MAX_INFLIGHT"] = str(max_inflight)
    env["ATHENA_CORE_PROBE_BIN"] = str(core_probe)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdio else subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdio else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
        cwd=ROOT if cwd is None else cwd,
        bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    stderr_buffer = bytearray()

    def drain_stderr() -> None:
        assert process.stderr is not None
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            if len(stderr_buffer) < MAX_STDERR_BYTES:
                stderr_buffer.extend(chunk[:MAX_STDERR_BYTES - len(stderr_buffer)])

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    return process, stderr_buffer, thread


def _reader(process: subprocess.Popen[bytes]) -> queue.Queue[bytes | None | BaseException]:
    if process.stdout is None:
        raise DriverError("stdio process has no stdout pipe")
    messages: queue.Queue[bytes | None | BaseException] = queue.Queue(maxsize=16)

    def read_lines() -> None:
        assert process.stdout is not None
        while True:
            try:
                line = process.stdout.readline(MAX_FRAME_BYTES + 2)
                if not line:
                    messages.put(None)
                    return
                if len(line) > MAX_FRAME_BYTES + 1 or not line.endswith(b"\n"):
                    messages.put(DriverError("stdio output exceeded the bounded frame or missed newline"))
                    return
                messages.put(line[:-1].removesuffix(b"\r"))
            except BaseException as exc:
                messages.put(exc)
                return

    threading.Thread(target=read_lines, daemon=True).start()
    return messages


def _stdio_rpc(process: subprocess.Popen[bytes], responses: queue.Queue[bytes | None | BaseException],
               message: dict[str, Any], request_hashes: dict[str, str], timings: dict[str, int]) -> dict[str, Any]:
    if process.stdin is None:
        raise DriverError("stdio process has no stdin pipe")
    request_id = str(message["id"])
    raw = _wire_bytes(message)
    if len(raw) > MAX_FRAME_BYTES:
        raise DriverError("driver request exceeds its configured frame bound")
    request_hashes[request_id] = hashlib.sha256(raw).hexdigest()
    started = time.perf_counter_ns()
    process.stdin.write(raw + b"\n")
    process.stdin.flush()
    try:
        line = responses.get(timeout=RPC_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        raise DriverError(f"stdio request timed out: {request_id}") from exc
    timings[request_id] = time.perf_counter_ns() - started
    if line is None:
        raise DriverError(f"stdio server closed before responding to {request_id}")
    if isinstance(line, BaseException):
        raise DriverError(f"stdio read failed: {line}")
    if len(line) > MAX_FRAME_BYTES:
        raise DriverError("stdio response exceeded its configured frame bound")
    try:
        response = json.loads(line)
    except json.JSONDecodeError as exc:
        raise DriverError(f"stdio emitted non-JSON stdout: {line[:160]!r}") from exc
    return _assert_response(response, request_id)


def _run_stdio(
    sdk: str,
    rust_bin: Path,
    node: Path,
    ts_server: Path,
    core_probe: Path,
    expected_core: dict[str, str],
) -> dict[str, Any]:
    command = _make_command(sdk, "stdio", rust_bin, node, ts_server, None)
    request_hashes: dict[str, str] = {}
    timings: dict[str, int] = {}
    payload_bytes: dict[str, int] = {}
    stderr_bytes = 0
    modern_process, modern_stderr, modern_stderr_thread = _spawn(
        command, secrets.token_urlsafe(32), core_probe, stdio=True
    )
    modern_responses = _reader(modern_process)
    try:
        # Modern-capable clients probe first on stdio; keep this connection on
        # the 2026 lifecycle for the complete modern request sequence.
        discovery = _stdio_rpc(modern_process, modern_responses,
                               _request("server/discover", "discover-1", {}), request_hashes, timings)
        _validate_discovery(discovery)
        listed = _stdio_rpc(modern_process, modern_responses,
                            _request("tools/list", "list-1", {}), request_hashes, timings)
        _validate_tools(listed)
        for fixture_case in ("long", "short"):
            response = _stdio_rpc(
                modern_process,
                modern_responses,
                _request("tools/call", f"call-{fixture_case}", {
                    "name": CONTRACT["tool_name"],
                    "arguments": {"fixture_case": fixture_case},
                }),
                request_hashes,
                timings,
            )
            payload_bytes[fixture_case] = _validate_tool_result(
                response, fixture_case, expected_core[fixture_case]
            )
        invalid_rejections: dict[str, Any] = {}
        for case, arguments in CONTRACT["invalid_arguments"].items():
            request_id = f"invalid-{case}"
            response = _stdio_rpc(
                modern_process,
                modern_responses,
                _request("tools/call", request_id, {
                    "name": CONTRACT["tool_name"],
                    "arguments": arguments,
                }),
                request_hashes,
                timings,
            )
            invalid_rejections[case] = _validate_tool_rejection(response, request_id, expected_core)
        modern_process.stdin.close()
        try:
            exit_code = modern_process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise DriverError("stdio server did not exit after stdin EOF") from exc
        if exit_code != 0:
            raise DriverError(f"stdio server exited {exit_code}; stderr={bytes(modern_stderr).decode('utf-8', 'replace')[-1200:]}")
        modern_stderr_thread.join(timeout=1.0)
        unexpected = []
        while True:
            try:
                item = modern_responses.get_nowait()
            except queue.Empty:
                break
            if item not in (None, b""):
                unexpected.append(item)
        if unexpected:
            raise DriverError("stdio emitted extra frames after the expected response sequence")
        stderr_bytes += len(modern_stderr)
    finally:
        if modern_process.poll() is None:
            modern_process.terminate()
            try:
                modern_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                modern_process.kill()
                modern_process.wait(timeout=2.0)

    # A strict Rust server can end its single-client stdio process after
    # rejecting a legacy initialize. Probe that behavior in its own process so
    # the rejected legacy lifecycle cannot contaminate the modern sequence.
    legacy_process, legacy_stderr, legacy_stderr_thread = _spawn(
        command, secrets.token_urlsafe(32), core_probe, stdio=True
    )
    legacy_responses = _reader(legacy_process)
    try:
        legacy = _stdio_rpc(legacy_process, legacy_responses, _legacy_initialize("legacy-1"),
                            request_hashes, timings)
        legacy_error_code = _validate_legacy_stdio_response(legacy, "legacy-1")
        legacy_process.stdin.close()
        try:
            legacy_exit_code = legacy_process.wait(timeout=2.0)
            stopped_by_supervisor = False
        except subprocess.TimeoutExpired:
            legacy_process.terminate()
            try:
                legacy_exit_code = legacy_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                legacy_process.kill()
                legacy_exit_code = legacy_process.wait(timeout=2.0)
            stopped_by_supervisor = True
        legacy_stderr_thread.join(timeout=1.0)
        stderr_bytes += len(legacy_stderr)
    finally:
        if legacy_process.poll() is None:
            legacy_process.terminate()
            try:
                legacy_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                legacy_process.kill()
                legacy_process.wait(timeout=2.0)
    return {
            "sdk": sdk,
            "transport": "stdio",
            "legacy_initialize_rejected": True,
            "legacy_error_code": legacy_error_code,
            "legacy_probe_process_exit_code": legacy_exit_code,
            "legacy_probe_stopped_by_supervisor": stopped_by_supervisor,
            "protocol_version": PROTOCOL_VERSION,
            "request_sha256": request_hashes,
            "single_sample_roundtrip_ns": timings,
            "payload_utf8_bytes": payload_bytes,
            "invalid_argument_rejections": invalid_rejections,
            "process_exit_after_modern_eof": exit_code,
            "stderr_bytes_captured": stderr_bytes,
    }


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http(process: subprocess.Popen[bytes], port: int, stderr_buffer: bytearray, stderr_thread: threading.Thread) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr_thread.join(timeout=1.0)
            raise DriverError(f"HTTP server exited during startup ({process.returncode}); stderr={bytes(stderr_buffer).decode('utf-8', 'replace')[-1200:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise DriverError("HTTP server did not bind to loopback before the startup deadline")


def _decode_sse_messages(body: bytes) -> list[dict[str, Any]]:
    messages = []
    data_lines: list[bytes] = []

    def flush_event() -> None:
        if not data_lines:
            return
        payload = b"\n".join(data_lines)
        data_lines.clear()
        if not payload.strip():
            return
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise DriverError("SSE event contained invalid JSON") from error
        if not isinstance(message, dict):
            raise DriverError("SSE event did not contain a JSON-RPC object")
        messages.append(message)

    for line in body.splitlines():
        if not line:
            flush_event()
        elif line.startswith(b"data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(b" ") else value)
    flush_event()
    return messages


def _decode_sse_response(body: bytes, request_id: Any,
                         expected_progress_token: str | None = None) -> dict[str, Any]:
    messages = _decode_sse_messages(body)
    terminal = [(index, message) for index, message in enumerate(messages)
                if message.get("id") == request_id
                and ("result" in message or "error" in message)]
    if len(terminal) != 1:
        raise DriverError("SSE response did not contain exactly one terminal response for this request")
    terminal_index, response = terminal[0]
    if expected_progress_token is not None:
        has_progress = any(
            message.get("method") == "notifications/progress"
            and isinstance(message.get("params"), dict)
            and message["params"].get("progressToken") == expected_progress_token
            for message in messages[:terminal_index]
        )
        if not has_progress:
            raise DriverError("SSE response omitted progress for the requested progress token")
    return response


def _http_exchange(port: int, message: dict[str, Any], token: str, *, origin: str | None = None,
                   auth_token: str | None = None, header_version: str | None = PROTOCOL_VERSION,
                   raw_body: bytes | None = None,
                   expected_progress_token: str | None = None) -> tuple[int, dict[str, Any] | None, bytes, int]:
    raw = _wire_bytes(message) if raw_body is None else raw_body
    if len(raw) > 131_072:
        raise DriverError("HTTP driver body exceeds its hard 128 KiB send bound")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {auth_token if auth_token is not None else token}",
        "Content-Type": "application/json",
        "Mcp-Method": str(message.get("method", "")),
        "Host": f"127.0.0.1:{port}",
        "Origin": origin if origin is not None else f"http://127.0.0.1:{port}",
    }
    if header_version is not None:
        headers["MCP-Protocol-Version"] = header_version
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        headers["Mcp-Name"] = params["name"]
    started = time.perf_counter_ns()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=RPC_TIMEOUT_SECONDS)
    try:
        connection.request("POST", "/mcp", body=raw, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise DriverError("HTTP response exceeded the 1 MiB capture bound")
        elapsed = time.perf_counter_ns() - started
        if response.status not in (200,):
            return response.status, None, body, elapsed
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            if expected_progress_token is not None:
                raise DriverError("HTTP server did not stream the requested progress notification")
            decoded = json.loads(body)
        elif content_type == "text/event-stream":
            decoded = _decode_sse_response(body, message.get("id"), expected_progress_token)
        else:
            raise DriverError(f"unexpected HTTP MCP content type: {content_type!r}")
        return response.status, decoded, body, elapsed
    finally:
        connection.close()


def _run_http(
    sdk: str,
    rust_bin: Path,
    node: Path,
    ts_server: Path,
    core_probe: Path,
    expected_core: dict[str, str],
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    port = _reserve_port()
    process, stderr_buffer, stderr_thread = _spawn(
        _make_command(sdk, "http", rust_bin, node, ts_server, port), token, core_probe, stdio=False
    )
    try:
        _wait_for_http(process, port, stderr_buffer, stderr_thread)
        valid_discovery = _request("server/discover", "discover-1", {})
        status, _, _, _ = _http_exchange(port, valid_discovery, token, auth_token="wrong-token")
        if status != 401:
            raise DriverError(f"HTTP auth boundary returned {status}, expected 401")
        status, _, _, _ = _http_exchange(port, valid_discovery, token, origin="https://attacker.invalid")
        if status != 403:
            raise DriverError(f"HTTP Origin boundary returned {status}, expected 403")
        legacy = _legacy_initialize("legacy-1")
        status, _, legacy_body, _ = _http_exchange(port, legacy, token, header_version="2025-11-25")
        if status != 400:
            raise DriverError(f"strict HTTP server accepted or misclassified legacy initialize (status {status})")
        legacy_error_code = _validate_protocol_error(
            legacy_body, -32022, expected_id="legacy-1",
            expected_requested_version="2025-11-25",
        )
        status, _, missing_version_body, _ = _http_exchange(
            port, valid_discovery, token, header_version=None
        )
        if status != 400:
            raise DriverError(f"HTTP server accepted a request without MCP-Protocol-Version (status {status})")
        missing_version_error_code = _validate_protocol_error(
            missing_version_body, (-32020, -32022), expected_id="discover-1"
        )
        mismatch = _request("server/discover", "mismatch-1", {}, version="2025-11-25")
        status, _, mismatch_body, _ = _http_exchange(port, mismatch, token, header_version=PROTOCOL_VERSION)
        if status != 400:
            raise DriverError(f"HTTP metadata/header mismatch returned {status}, expected 400")
        mismatch_error_code = _validate_protocol_error(mismatch_body, -32020, expected_id="mismatch-1")
        oversized = {
            "jsonrpc": "2.0",
            "id": "oversize-1",
            "method": "server/discover",
            "params": {"padding": "x" * (CONTRACT["max_http_request_bytes"] + 1024)},
        }
        oversized_raw = _wire_bytes(oversized)
        status, _, _, _ = _http_exchange(port, oversized, token, raw_body=oversized_raw)
        if status != 413:
            raise DriverError(f"HTTP body bound returned {status}, expected 413")

        request_hashes: dict[str, str] = {}
        timings: dict[str, int] = {}
        response_bytes: dict[str, int] = {}
        progress_tokens_verified: list[str] = []

        def call(message: dict[str, Any]) -> dict[str, Any]:
            request_id = str(message["id"])
            raw = _wire_bytes(message)
            request_hashes[request_id] = hashlib.sha256(raw).hexdigest()
            params = message.get("params")
            meta = params.get("_meta") if isinstance(params, dict) else None
            progress_token = meta.get("progressToken") if isinstance(meta, dict) else None
            status_code, response, body, elapsed = _http_exchange(
                port, message, token,
                expected_progress_token=progress_token if isinstance(progress_token, str) else None)
            timings[request_id] = elapsed
            if status_code != 200 or response is None:
                raise DriverError(f"HTTP MCP request {request_id} failed with {status_code}: {body[:240]!r}")
            if response.get("id") != request_id or response.get("jsonrpc") != "2.0":
                raise DriverError(f"HTTP MCP response identity mismatch for {request_id}")
            response_bytes[request_id] = len(body)
            if isinstance(progress_token, str):
                progress_tokens_verified.append(progress_token)
            return response

        discovery = call(valid_discovery)
        _validate_discovery(discovery)
        listed = call(_request("tools/list", "list-1", {}))
        _validate_tools(listed)
        payload_bytes: dict[str, int] = {}
        for fixture_case in ("long", "short"):
            response = call(_request("tools/call", f"call-{fixture_case}", {
                "name": CONTRACT["tool_name"],
                "arguments": {"fixture_case": fixture_case},
            }, progress_token="http-progress-short" if fixture_case == "short" else None))
            payload_bytes[fixture_case] = _validate_tool_result(
                response, fixture_case, expected_core[fixture_case]
            )
        reconnected = call(_request("tools/call", "reconnect-short", {
            "name": CONTRACT["tool_name"],
            "arguments": {"fixture_case": "short"},
        }))
        if _validate_tool_result(reconnected, "short", expected_core["short"]) != payload_bytes["short"]:
            raise DriverError("stateless HTTP reconnect changed the fixture result")
        invalid_rejections: dict[str, Any] = {}
        for case, arguments in CONTRACT["invalid_arguments"].items():
            request_id = f"invalid-{case}"
            response = call(_request("tools/call", request_id, {
                "name": CONTRACT["tool_name"],
                "arguments": arguments,
            }))
            invalid_rejections[case] = _validate_tool_rejection(response, request_id, expected_core)
        return {
            "sdk": sdk,
            "transport": "streamable-http",
            "legacy_initialize_rejected": True,
            "bad_auth_status": 401,
            "bad_origin_status": 403,
            "missing_version_header_status": 400,
            "legacy_error_code": legacy_error_code,
            "missing_version_error_code": missing_version_error_code,
            "mismatch_error_code": mismatch_error_code,
            "metadata_mismatch_status": 400,
            "oversized_body_status": 413,
            "protocol_version": PROTOCOL_VERSION,
            "request_sha256": request_hashes,
            "single_sample_roundtrip_ns": timings,
            "response_wire_bytes": response_bytes,
            "payload_utf8_bytes": payload_bytes,
            "http_progress_notification_verified": "http-progress-short" in progress_tokens_verified,
            "invalid_argument_rejections": invalid_rejections,
            "stateless_reconnect_verified": True,
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        stderr_thread.join(timeout=1.0)


def _process_running(pid: int) -> bool:
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise DriverError("invalid owned producer PID")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise DriverError("owned producer process observation failed")
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise DriverError("owned producer exit observation failed")
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise DriverError("owned producer process observation denied") from error
    return True


def _producer_pid(path: Path) -> int:
    with path.open("rb") as stream:
        raw = stream.read(33)
    if not raw or len(raw) > 32 or not raw.isdigit():
        raise DriverError("controlled producer did not record a bounded PID")
    return int(raw)


def _validate_core_failure(response: dict[str, Any], expected: str) -> None:
    message = ""
    if "result" in response and "error" not in response:
        result = response["result"]
        if (isinstance(result, dict) and result.get("isError") is True
                and result.get("resultType") == "complete"):
            message = " ".join(item["text"] for item in result.get("content", [])
                               if isinstance(item, dict) and item.get("type") == "text"
                               and isinstance(item.get("text"), str))
    if expected not in message:
        raise DriverError(f"controlled core failure expected {expected}, received {message[:160]!r}")


def _start_inflight_http_call(
    port: int, message: dict[str, Any], token: str, pid_path: Path, progress_token: str, *,
    exit_timeout_s: float = 2.0,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, bytes, int]:
    raw = _wire_bytes(message)
    if len(raw) > 131_072:
        raise DriverError("HTTP cancellation request exceeded the bounded request size")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Mcp-Method": str(message.get("method", "")),
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        headers["Mcp-Name"] = params["name"]

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=RPC_TIMEOUT_SECONDS)
    try:
        connection.request("POST", "/mcp", body=raw, headers=headers)
        startup_deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        pid = None
        while pid is None:
            if pid_path.is_file():
                try:
                    pid = _producer_pid(pid_path)
                except (DriverError, OSError):
                    pass
            if time.monotonic() >= startup_deadline:
                raise DriverError("controlled HTTP request never started its owned core child")
            if pid is None:
                time.sleep(.02)
        if not _process_running(pid):
            raise DriverError("controlled core child exited before HTTP caller disconnect")

        if connection.sock is None:
            raise DriverError("HTTP connection disappeared before progress notification")
        connection.sock.settimeout(exit_timeout_s)
        try:
            response = connection.getresponse()
        except TimeoutError as error:
            raise DriverError("MCP server did not emit progress before completing the core request") from error
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if response.status != 200 or content_type != "text/event-stream":
            raise DriverError("MCP progress did not open an SSE response before core completion")
        event = bytearray()
        notification = None
        while len(event) < 8192 and notification is None:
            chunk = response.read(1)
            if not chunk:
                break
            event.extend(chunk)
            if event.endswith(b"\n\n") or event.endswith(b"\r\n\r\n"):
                messages = _decode_sse_messages(bytes(event))
                if messages:
                    notification = messages[-1]
        params = notification.get("params") if isinstance(notification, dict) else None
        if (not isinstance(notification, dict) or notification.get("method") != "notifications/progress"
                or not isinstance(params, dict) or params.get("progressToken") != progress_token):
            raise DriverError("MCP server did not echo the caller's progress token before core completion")
        if not _process_running(pid):
            raise DriverError("core child completed before the initial progress notification")
        return connection, response, bytes(event), pid
    except BaseException:
        connection.close()
        raise


def _abort_inflight_http_call(
    connection: http.client.HTTPConnection, pid: int, *, exit_timeout_s: float = 2.0,
) -> float:
    try:
        disconnected_at = time.monotonic()
        sock = connection.sock
        if sock is None:
            raise DriverError("HTTP connection disappeared before caller disconnect")
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

        deadline = disconnected_at + exit_timeout_s
        while _process_running(pid):
            if time.monotonic() >= deadline:
                raise DriverError("HTTP caller disconnect did not promptly stop the owned core child")
            time.sleep(.02)
        return time.monotonic() - disconnected_at
    finally:
        connection.close()


def _finish_inflight_http_call(
    connection: http.client.HTTPConnection, response: http.client.HTTPResponse,
    initial_event: bytes, request_id: str, progress_token: str,
) -> dict[str, Any]:
    try:
        if connection.sock is not None:
            connection.sock.settimeout(RPC_TIMEOUT_SECONDS)
        remaining = response.read(MAX_RESPONSE_BYTES + 1 - len(initial_event))
        body = initial_event + remaining
        if len(body) > MAX_RESPONSE_BYTES:
            raise DriverError("HTTP response exceeded the 1 MiB capture bound")
        decoded = _decode_sse_response(body, request_id, progress_token)
        return _assert_response(decoded, request_id)
    finally:
        connection.close()


def _disconnect_http_call(port: int, message: dict[str, Any], token: str,
                         pid_path: Path, progress_token: str, *,
                         exit_timeout_s: float = 2.0) -> float:
    connection, _, _, pid = _start_inflight_http_call(
        port, message, token, pid_path, progress_token, exit_timeout_s=exit_timeout_s)
    return _abort_inflight_http_call(connection, pid, exit_timeout_s=exit_timeout_s)


def _synthetic_short_aae() -> str:
    fixture = CORE_FIXTURES["short"]
    return "\n".join((
        json.dumps({
            "type": "meta",
            "version": "aae/1",
            "source_url": fixture["input"]["url"],
            "fetched_at_unix_ms": fixture["input"]["observed_at_unix_ms"],
            "content_hash": "0" * 64,
            "total_blocks": 1,
        }, separators=(",", ":")),
        json.dumps({
            "type": "block",
            "id": "1" * 64,
            "text": "Short core fixture. Small bounded research payload.",
            "role": "body",
            "links": [],
        }, separators=(",", ":")),
        json.dumps({"type": "end", "block_count": 1}, separators=(",", ":")),
    )) + "\n"


def _spawned_pids(root: Path) -> list[int]:
    log_path = root / "probe-spawns.log"
    if not log_path.is_file():
        return []
    pids = []
    for line in log_path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        _, _, raw_pid = line.partition(":")
        if not raw_pid.isdigit():
            raise DriverError("controlled concurrency producer logged a non-numeric PID")
        pids.append(int(raw_pid))
    return pids


def _run_http_disconnect_controls(rust_bin: Path, node: Path, ts_server: Path,
                                 sdks: tuple[str, ...] = ("rust", "typescript-sdk")) -> list[dict[str, Any]]:
    results = []
    for sdk in sdks:
        with tempfile.TemporaryDirectory(prefix="athena-mcp-http-cancel-") as folder:
            root = Path(folder)
            script = ("import os,sys,time\nfrom pathlib import Path\n"
                      "Path('probe-child.pid').write_text(str(os.getpid()),encoding='ascii')\n"
                      "sys.stdin.buffer.read(131073)\n"
                      "deadline=time.monotonic()+12\n"
                      "while time.monotonic()<deadline:time.sleep(.05)\n")
            (root / "export").write_text(script, encoding="utf-8")
            token = secrets.token_urlsafe(32)
            port = _reserve_port()
            process, stderr_buffer, stderr_thread = _spawn(
                _make_command(sdk, "http", rust_bin, node, ts_server, port),
                token, Path(sys.executable), stdio=False, cwd=root)
            pid_path = root / "probe-child.pid"
            record = {"sdk": sdk, "transport": "http", "scenario": "caller_disconnect",
                      "status": "FAIL", "maximum_child_stop_ms": 2000,
                      "progress_notification_required": True}
            try:
                _wait_for_http(process, port, stderr_buffer, stderr_thread)
                progress_token = f"progress-cancel-{sdk}"
                call = _request("tools/call", f"cancel-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}},
                    progress_token=progress_token)
                elapsed = _disconnect_http_call(port, call, token, pid_path, progress_token)
                record.update(status="PASS", producer_exit_observed=True,
                              progress_notification_observed=True,
                              child_stop_ms=round(elapsed * 1000, 3))
            except (DriverError, OSError, http.client.HTTPException) as error:
                record["error"] = str(error)
                record["error_type"] = type(error).__name__
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                stderr_thread.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                if pid_path.is_file():
                    pid = _producer_pid(pid_path)
                    deadline = time.monotonic() + 13
                    while _process_running(pid):
                        if time.monotonic() >= deadline:
                            raise DriverError("controlled cancellation child did not exit before fixture cleanup")
                        time.sleep(.05)
            results.append(record)
    if any(row["status"] != "PASS" for row in results):
        raise DriverError("HTTP caller-disconnect controls: " + json.dumps(results, sort_keys=True))
    return results


def _run_http_concurrency_controls(
    rust_bin: Path, node: Path, ts_server: Path,
    sdks: tuple[str, ...] = ("rust", "typescript-sdk"),
) -> list[dict[str, Any]]:
    expected_short = _synthetic_short_aae()
    expected_short_bytes = expected_short.encode("utf-8")
    script = (
        "import json,os,sys,time\nfrom pathlib import Path\n"
        f"EXPECTED={expected_short_bytes!r}\n"
        "raw=sys.stdin.buffer.read(131073)\n"
        "payload=json.loads(raw.decode('utf-8'))\n"
        "tag='short' if str(payload.get('url','')).endswith('/short') else 'long'\n"
        "pid_text=str(os.getpid())\n"
        "with Path('probe-spawns.log').open('a',encoding='ascii') as handle:\n"
        " handle.write(f'{tag}:{pid_text}\\n')\n"
        "tmp=Path(f'probe-{tag}.{pid_text}.tmp')\n"
        "tmp.write_text(pid_text,encoding='ascii')\n"
        "tmp.replace(Path(f'probe-{tag}.pid'))\n"
        "release=Path(f'release-{tag}')\n"
        "deadline=time.monotonic()+12\n"
        "while time.monotonic()<deadline:\n"
        " if release.is_dir():\n"
        "  sys.stdout.buffer.write(EXPECTED);sys.stdout.buffer.flush();sys.exit(0)\n"
        " time.sleep(.02)\n"
    )
    results = []
    for sdk in sdks:
        with tempfile.TemporaryDirectory(prefix="athena-mcp-http-sat-") as folder:
            root = Path(folder)
            (root / "export").write_text(script, encoding="utf-8")
            token = secrets.token_urlsafe(32)
            port = _reserve_port()
            process, stderr_buffer, stderr_thread = _spawn(
                _make_command(sdk, "http", rust_bin, node, ts_server, port),
                token, Path(sys.executable), stdio=False, cwd=root, max_inflight=1)
            conn_1 = None
            record = {
                "sdk": sdk, "transport": "http", "scenario": "admission_saturation",
                "max_inflight": 1, "expected": "CORE_PROBE_ADMISSION_SATURATED", "status": "FAIL",
            }
            try:
                _wait_for_http(process, port, stderr_buffer, stderr_thread)
                progress_token = f"progress-sat-{sdk}"
                call_1 = _request("tools/call", f"sat-1-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "long"}},
                    progress_token=progress_token)
                conn_1, _, _, pid_1 = _start_inflight_http_call(
                    port, call_1, token, root / "probe-long.pid", progress_token)
                call_2 = _request("tools/call", f"sat-2-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}})
                status_2, resp_2, _, _ = _http_exchange(port, call_2, token)
                if status_2 != 200 or resp_2 is None:
                    raise DriverError("saturated HTTP admission check did not return a JSON-RPC response")
                _validate_core_failure(_assert_response(resp_2, str(call_2["id"])), "CORE_PROBE_ADMISSION_SATURATED")
                if (root / "probe-short.pid").exists() or _spawned_pids(root) != [pid_1]:
                    raise DriverError("saturated HTTP request spawned an extra core child process")
                if not _process_running(pid_1):
                    raise DriverError("in-flight core child exited during saturated admission rejection")
                stop_s = _abort_inflight_http_call(conn_1, pid_1)
                conn_1 = None
                (root / "release-short").mkdir()
                recover_deadline = time.monotonic() + 1.0
                recovered = False
                attempt = 0
                while time.monotonic() < recover_deadline:
                    attempt += 1
                    call_3 = _request("tools/call", f"sat-recover-{sdk}-{attempt}", {
                        "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}})
                    status_3, resp_3, _, _ = _http_exchange(port, call_3, token)
                    if status_3 != 200 or resp_3 is None:
                        raise DriverError("post-disconnect recovery request failed at HTTP layer")
                    resp_3 = _assert_response(resp_3, str(call_3["id"]))
                    result_3 = resp_3.get("result")
                    if isinstance(result_3, dict) and result_3.get("isError") is True:
                        _validate_core_failure(resp_3, "CORE_PROBE_ADMISSION_SATURATED")
                        time.sleep(.02)
                        continue
                    _validate_tool_result(resp_3, "short", expected_short)
                    recovered = True
                    break
                if not recovered:
                    raise DriverError("admission slot did not recover after disconnected child exited")
                pids = _spawned_pids(root)
                if len(pids) != 2 or pids[0] != pid_1 or pids[1] == pid_1:
                    raise DriverError("slot recovery did not record a single fresh producer child")
                record.update(
                    status="PASS", producer_exit_observed=True,
                    rejected_without_spawn=True, slot_recovered_after_disconnect=True,
                    child_stop_ms=round(stop_s * 1000, 3),
                )
            except (DriverError, OSError, http.client.HTTPException, json.JSONDecodeError) as error:
                record["error"] = str(error)
                record["error_type"] = type(error).__name__
            finally:
                if conn_1 is not None:
                    conn_1.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                stderr_thread.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                for pid in _spawned_pids(root):
                    deadline = time.monotonic() + 13
                    while _process_running(pid):
                        if time.monotonic() >= deadline:
                            raise DriverError("controlled saturation child did not exit before fixture cleanup")
                        time.sleep(.05)
            results.append(record)

        with tempfile.TemporaryDirectory(prefix="athena-mcp-http-sib-") as folder:
            root = Path(folder)
            (root / "export").write_text(script, encoding="utf-8")
            token = secrets.token_urlsafe(32)
            port = _reserve_port()
            process, stderr_buffer, stderr_thread = _spawn(
                _make_command(sdk, "http", rust_bin, node, ts_server, port),
                token, Path(sys.executable), stdio=False, cwd=root, max_inflight=2)
            conn_a = None
            conn_b = None
            record = {
                "sdk": sdk, "transport": "http", "scenario": "sibling_disconnect_isolation",
                "max_inflight": 2, "expected": "CORE_PROBE_ADMISSION_SATURATED", "status": "FAIL",
            }
            try:
                _wait_for_http(process, port, stderr_buffer, stderr_thread)
                token_a = f"progress-sib-a-{sdk}"
                token_b = f"progress-sib-b-{sdk}"
                call_a = _request("tools/call", f"sib-a-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "long"}},
                    progress_token=token_a)
                call_b = _request("tools/call", f"sib-b-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}},
                    progress_token=token_b)
                conn_a, _, _, pid_a = _start_inflight_http_call(
                    port, call_a, token, root / "probe-long.pid", token_a)
                conn_b, resp_b, event_b, pid_b = _start_inflight_http_call(
                    port, call_b, token, root / "probe-short.pid", token_b)
                if pid_a == pid_b or not _process_running(pid_a) or not _process_running(pid_b):
                    raise DriverError("concurrent HTTP requests did not start two distinct live core children")
                call_c = _request("tools/call", f"sib-overflow-{sdk}", {
                    "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}})
                status_c, resp_c, _, _ = _http_exchange(port, call_c, token)
                if status_c != 200 or resp_c is None:
                    raise DriverError("third concurrent HTTP request did not return a JSON-RPC response")
                _validate_core_failure(_assert_response(resp_c, str(call_c["id"])), "CORE_PROBE_ADMISSION_SATURATED")
                if _spawned_pids(root) != [pid_a, pid_b]:
                    raise DriverError("third concurrent HTTP request spawned an unpermitted child process")
                stop_a_s = _abort_inflight_http_call(conn_a, pid_a)
                conn_a = None
                if not _process_running(pid_b):
                    raise DriverError("disconnecting caller A terminated sibling caller B's core child")
                (root / "release-short").mkdir()
                completed_b = _finish_inflight_http_call(
                    conn_b, resp_b, event_b, str(call_b["id"]), token_b)
                conn_b = None
                _validate_tool_result(completed_b, "short", expected_short)
                exit_deadline = time.monotonic() + 2.0
                while _process_running(pid_b):
                    if time.monotonic() >= exit_deadline:
                        raise DriverError("sibling caller B child remained alive after returning its response")
                    time.sleep(.02)
                record.update(
                    status="PASS", producer_exit_observed=True,
                    sibling_completed_after_disconnect=True,
                    third_caller_rejected_without_spawn=True,
                    disconnected_child_stop_ms=round(stop_a_s * 1000, 3),
                )
            except (DriverError, OSError, http.client.HTTPException, json.JSONDecodeError) as error:
                record["error"] = str(error)
                record["error_type"] = type(error).__name__
            finally:
                for conn in (conn_a, conn_b):
                    if conn is not None:
                        conn.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                stderr_thread.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                for pid in _spawned_pids(root):
                    deadline = time.monotonic() + 13
                    while _process_running(pid):
                        if time.monotonic() >= deadline:
                            raise DriverError("controlled sibling child did not exit before fixture cleanup")
                        time.sleep(.05)
            results.append(record)
    if any(row["status"] != "PASS" for row in results):
        raise DriverError("HTTP concurrency controls: " + json.dumps(results, sort_keys=True))
    return results


def _run_core_failure_controls(rust_bin: Path, node: Path, ts_server: Path,
                               transports: tuple[str, ...]) -> list[dict[str, Any]]:
    scenarios = {"oversize": "CORE_PROBE_OUTPUT_TOO_LARGE",
                 "invalid_utf8": "CORE_PROBE_OUTPUT_INVALID_UTF8",
                 "truncated": "CORE_PROBE_OUTPUT_INVALID",
                 "timeout": "CORE_PROBE_TIMEOUT"}
    results = []
    for transport in transports:
        for sdk in ("rust", "typescript-sdk"):
            for scenario, expected in scenarios.items():
                with tempfile.TemporaryDirectory(prefix="athena-mcp-core-failure-") as folder:
                    root = Path(folder)
                    script = ("import os,sys,time\nfrom pathlib import Path\n"
                              "Path('probe-child.pid').write_text(str(os.getpid()),encoding='ascii')\n"
                              "sys.stdin.buffer.read(131073)\n"
                              f"mode={scenario!r}\n"
                              "if mode=='invalid_utf8':\n sys.stdout.buffer.write(b'\\xff\\n');sys.stdout.buffer.flush();sys.exit(0)\n"
                              "if mode=='truncated':\n sys.stdout.buffer.write(b'{}');sys.stdout.buffer.flush();sys.exit(0)\n"
                              "if mode=='oversize':\n sys.stdout.buffer.write(b'x'*131073);sys.stdout.buffer.flush()\n"
                              "deadline=time.monotonic()+12\n"
                              "while time.monotonic()<deadline:time.sleep(.05)\n")
                    (root / "export").write_text(script, encoding="utf-8")
                    token = secrets.token_urlsafe(32)
                    port = _reserve_port() if transport == "http" else None
                    process, stderr_buffer, stderr_thread = _spawn(
                        _make_command(sdk, transport, rust_bin, node, ts_server, port),
                        token, Path(sys.executable), stdio=transport == "stdio", cwd=root)
                    record = {"sdk": sdk, "transport": transport, "scenario": scenario,
                              "expected": expected, "status": "FAIL"}
                    try:
                        discovery = _request("server/discover", "discover-1", {})
                        call = _request("tools/call", "core-failure-" + scenario, {
                            "name": CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}})
                        if transport == "stdio":
                            responses = _reader(process)
                            hashes, timings = {}, {}
                            _validate_discovery(_stdio_rpc(process, responses, discovery, hashes, timings))
                            response = _stdio_rpc(process, responses, call, hashes, timings)
                        else:
                            _wait_for_http(process, port, stderr_buffer, stderr_thread)
                            status, discovered, _, _ = _http_exchange(port, discovery, token)
                            if status != 200 or discovered is None:
                                raise DriverError("controlled failure discovery rejected")
                            _validate_discovery(_assert_response(discovered, "discover-1"))
                            status, response, _, _ = _http_exchange(port, call, token)
                            if status != 200 or response is None:
                                raise DriverError("controlled core failure lost its HTTP RPC result")
                            response = _assert_response(response, str(call["id"]))
                        _validate_core_failure(response, expected)
                        if _process_running(_producer_pid(root / "probe-child.pid")):
                            raise DriverError("core error returned before the owned producer exited")
                        record.update(status="PASS", producer_exit_observed=True)
                    except (DriverError, OSError, http.client.HTTPException, json.JSONDecodeError) as error:
                        record["error"] = str(error)
                        record["error_type"] = type(error).__name__
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                        stderr_thread.join(timeout=1)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None and not stream.closed:
                                stream.close()
                        pid_path = root / "probe-child.pid"
                        if pid_path.is_file():
                            pid = _producer_pid(pid_path)
                            deadline = time.monotonic() + 13
                            while _process_running(pid):
                                if time.monotonic() >= deadline:
                                    raise DriverError("controlled producer did not finish before fixture cleanup")
                                time.sleep(.05)
                    results.append(record)
    if "http" in transports:
        results.extend(_run_http_disconnect_controls(rust_bin, node, ts_server))
        results.extend(_run_http_concurrency_controls(rust_bin, node, ts_server))
    if any(row["status"] != "PASS" for row in results):
        raise DriverError("core failure controls: " + json.dumps(results, sort_keys=True))
    return results


def _task_aae(response: dict[str, Any]) -> bytes:
    result = response.get("result")
    if ("error" in response or not isinstance(result, dict)
            or result.get("resultType") != "complete" or result.get("isError") is True):
        raise LabError("MCP_TASK_TOOL_FAILED")
    content = result.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)):
        raise LabError("MCP_TASK_PAYLOAD_INVALID")
    data = content[0]["text"].encode("utf-8")
    if not data.endswith(b"\n") or len(data) > MAX_CORE_OUTPUT_BYTES:
        raise LabError("MCP_TASK_PAYLOAD_INVALID")
    return data


_BLAKE3_IV = (
    0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
    0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
)
_BLAKE3_PERM = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)


def _blake3_g(s: list[int], a: int, b: int, c: int, d: int, mx: int, my: int) -> None:
    s[a] = (s[a] + s[b] + mx) & 0xFFFFFFFF
    x = s[d] ^ s[a]
    s[d] = ((x >> 16) | (x << 16)) & 0xFFFFFFFF
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    x = s[b] ^ s[c]
    s[b] = ((x >> 12) | (x << 20)) & 0xFFFFFFFF
    s[a] = (s[a] + s[b] + my) & 0xFFFFFFFF
    x = s[d] ^ s[a]
    s[d] = ((x >> 8) | (x << 24)) & 0xFFFFFFFF
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    x = s[b] ^ s[c]
    s[b] = ((x >> 7) | (x << 25)) & 0xFFFFFFFF


def _blake3_hex(data: bytes) -> str:
    if len(data) > 1024:
        raise DriverError("single-chunk BLAKE3 helper only supports inputs up to 1024 bytes")
    cv = list(_BLAKE3_IV)
    num_blocks = max(1, (len(data) + 63) // 64)
    for block_idx in range(num_blocks):
        block = data[block_idx * 64 : (block_idx + 1) * 64]
        padded = block + b"\x00" * (64 - len(block))
        m = [int.from_bytes(padded[i * 4 : (i + 1) * 4], "little") for i in range(16)]
        flags = (1 if block_idx == 0 else 0) | (10 if block_idx == num_blocks - 1 else 0)
        s = cv + list(_BLAKE3_IV[:4]) + [0, 0, len(block), flags]
        for r in range(7):
            _blake3_g(s, 0, 4, 8, 12, m[0], m[1])
            _blake3_g(s, 1, 5, 9, 13, m[2], m[3])
            _blake3_g(s, 2, 6, 10, 14, m[4], m[5])
            _blake3_g(s, 3, 7, 11, 15, m[6], m[7])
            _blake3_g(s, 0, 5, 10, 15, m[8], m[9])
            _blake3_g(s, 1, 6, 11, 12, m[10], m[11])
            _blake3_g(s, 2, 7, 8, 13, m[12], m[13])
            _blake3_g(s, 3, 4, 9, 14, m[14], m[15])
            if r < 6:
                m = [m[i] for i in _BLAKE3_PERM]
        cv = [s[i] ^ s[i + 8] for i in range(8)]
    return b"".join(word.to_bytes(4, "little") for word in cv).hex()


def _task_handle_token(tenant: str, bound_task_id: str, task_id: str, source_id: str, revision: str) -> str:
    buf = bytearray(b"athena-task-handle/1\x00")
    for field in (tenant, bound_task_id, task_id, source_id, revision):
        encoded = field.encode("utf-8")
        buf.extend(len(encoded).to_bytes(8, "little"))
        buf.extend(encoded)
    return "ev_" + _blake3_hex(bytes(buf))


class SdkTaskProducer(RustProbeProducer):
    """Existing independent native evidence gate after a real SDK roundtrip."""
    kind = "MCP_SCOPED_NATIVE_AAE_PRODUCER"

    def __init__(self, core_probe: Path, task: dict[str, Any], rpc):
        super().__init__(core_probe, max_output_bytes=MAX_CORE_OUTPUT_BYTES)
        self.task_id, self.tenant, self.query = task["id"], task["tenant"], task["request"].get("query")
        self.rpc, self.calls = rpc, []

    def mint_handle(self, packet: dict[str, Any]) -> str:
        return _task_handle_token(
            self.tenant, self.task_id, self.task_id, packet["source_id"], packet["revision"]
        )

    def produce(self, document, query=None):
        if query != self.query:
            raise LabError("MCP_TASK_QUERY_MISMATCH")
        data = _task_aae(self.rpc("tools/call", {
            "name": "athena_extract_document",
            "arguments": {"task_id": self.task_id, "source_id": document["id"],
                          "revision": document["revision"]},
        }))
        evidence = self.validated_evidence(data, document)
        self.calls.append({"tool": "athena_extract_document",
                           "source_id": document["id"], "revision": document["revision"],
                           "aae_sha256": hashlib.sha256(data).hexdigest(), "aae_bytes": len(data)})
        return evidence

    def expand_handle(self, document, revision: str, handle: str, *, task: dict[str, Any] | None = None):
        data = _task_aae(self.rpc("tools/call", {
            "name": "athena_expand_handle",
            "arguments": {"task_id": self.task_id, "source_id": document["id"],
                          "revision": revision, "handle": handle},
        }))
        evidence = self.validated_evidence(data, document)
        self.calls.append({"tool": "athena_expand_handle",
                           "source_id": document["id"], "revision": revision, "handle": handle,
                           "aae_sha256": hashlib.sha256(data).hexdigest(), "aae_bytes": len(data)})
        return evidence

    def search_task(self, task: dict[str, Any], query: str):
        if query != self.query:
            raise LabError("MCP_TASK_QUERY_MISMATCH")
        data = _task_aae(self.rpc("tools/call", {
            "name": "athena_search_task",
            "arguments": {"task_id": self.task_id, "query": query},
        }))
        results = self.validated_search_results(data, task, query)
        self.calls.append({"tool": "athena_search_task",
                           "query": query, "candidate_count": len(results["candidates"]),
                           "aae_sha256": hashlib.sha256(data).hexdigest(), "aae_bytes": len(data)})
        return results


def _validate_task_schema(response: dict[str, Any]) -> None:
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    for tool_name, fields in (
        ("athena_extract_document", {"task_id", "source_id", "revision"}),
        ("athena_expand_handle", {"task_id", "source_id", "revision", "handle"}),
        ("athena_search_task", {"task_id", "query"}),
    ):
        tool = next((item for item in tools if isinstance(item, dict)
                     and item.get("name") == tool_name), None) if isinstance(tools, list) else None
        schema = tool.get("inputSchema") if isinstance(tool, dict) else None
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or set(schema.get("properties", {})) != fields
                or set(schema.get("required", [])) != fields
                or schema.get("additionalProperties") is not False):
            raise DriverError("task tool schema exposes caller-owned scope or differs between arms")


def _validate_task_denial(response: dict[str, Any]) -> None:
    _validate_core_failure(response, "SCOPED_TASK_DENIED")
    result = response["result"]
    content = result.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or content[0].get("type") != "text"
            or content[0].get("text") != "SCOPED_TASK_DENIED"
            or "structuredContent" in result):
        raise DriverError("task denial leaked data or did not use the bounded denial contract")


@contextmanager
def _task_rpc(sdk: str, transport: str, rust_bin: Path, node: Path, ts_server: Path,
              core_probe: Path, task_id: str, tenant: str):
    token = secrets.token_urlsafe(32)
    port = _reserve_port() if transport == "http" else None
    command = _make_command(sdk, transport, rust_bin, node, ts_server, port)
    started = time.perf_counter_ns()
    process, stderr, thread = _spawn(command, token, core_probe, stdio=transport == "stdio",
                                     tenant=tenant, bound_task_id=task_id)
    hashes, timings = {}, {}
    metrics = {"startup_and_discovery_wall_ns": None, "request_canonical_bytes": 0,
               "response_canonical_bytes": 0, "request_sha256": hashes, "rpc_roundtrip_ns": timings}
    try:
        responses = _reader(process) if transport == "stdio" else None
        if transport == "http":
            _wait_for_http(process, port, stderr, thread)

        def rpc(method, params):
            request_id = f"task-{len(hashes) + 1}"
            message = _request(method, request_id, params)
            metrics["request_canonical_bytes"] += len(_wire_bytes(message))
            if transport == "stdio":
                response = _stdio_rpc(process, responses, message, hashes, timings)
            else:
                hashes[request_id] = hashlib.sha256(_wire_bytes(message)).hexdigest()
                status, response, _, elapsed = _http_exchange(port, message, token)
                timings[request_id] = elapsed
                if status != 200 or not isinstance(response, dict):
                    raise DriverError(f"task HTTP RPC failed with status {status}")
                _assert_response(response, request_id)
            metrics["response_canonical_bytes"] += len(_wire_bytes(response))
            return response

        _validate_discovery(rpc("server/discover", {}))
        _validate_task_schema(rpc("tools/list", {}))
        metrics["startup_and_discovery_wall_ns"] = time.perf_counter_ns() - started
        yield rpc, metrics
        if transport == "stdio":
            process.stdin.close()
            if process.wait(timeout=2) != 0:
                raise DriverError("task SDK process failed after EOF")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        metrics.update(process_lifetime_wall_ns=time.perf_counter_ns() - started,
                       sdk_exit_code=process.returncode, stderr_bytes=len(stderr),
                       stop_policy="EOF" if transport == "stdio" else "OWNED_PROCESS_TERMINATION")


def _run_task_consumers(rust_bin: Path, node: Path, ts_server: Path, core_probe: Path,
                        transports: tuple[str, ...], task_ids: tuple[str, ...] | None) -> dict[str, Any]:
    corpus = tasks()
    if task_ids is not None and (not task_ids or len(set(task_ids)) != len(task_ids)
                                 or set(task_ids) - {task["id"] for task in corpus}):
        raise DriverError("task selection must name unique frozen task IDs")
    selected = [task for task in corpus if task_ids is None or task["id"] in task_ids]
    rows, processes, scope_controls = [], [], []
    for transport in transports:
        for sdk in ("rust", "typescript-sdk"):
            for task in selected:
                with _task_rpc(sdk, transport, rust_bin, node, ts_server, core_probe,
                               task["id"], task["tenant"]) as (rpc, metrics):
                    for representation in ("full", "compact"):
                        before = {key: metrics[key] for key in
                                  ("request_canonical_bytes", "response_canonical_bytes")}
                        before_calls, before_rpc_ns = len(metrics["rpc_roundtrip_ns"]), sum(metrics["rpc_roundtrip_ns"].values())
                        producer = SdkTaskProducer(core_probe, task, rpc)
                        outcome = run_task(task, "W2", representation, producer=producer)
                        if (outcome["errors"] or not outcome["quality"]["fact_contract_satisfied"]
                                or not outcome["quality"]["contract_satisfied"]
                                or (task["family"] == "F04" and outcome["quality"]["table_contract_status"] != "SATISFIED")):
                            raise DriverError(f"task consumer failed: {sdk}/{transport}/{task['id']}/{representation}: "
                                              f"{outcome['errors']} {outcome['quality']}")
                        if (task["request"].get("mode") == "query"
                                and not any(call["tool"] == "athena_search_task" for call in producer.calls)):
                            raise DriverError(f"query task consumer did not invoke athena_search_task: {sdk}/{transport}/{task['id']}/{representation}")
                        if outcome["ledger"]["retained_state_after_close_bytes"] != 0:
                            raise DriverError("task consumer retained handle state after close")
                        rows.append({"sdk": sdk, "transport": transport, "task_id": task["id"],
                                     "representation": representation, "actual_model": False,
                                     "quality": outcome["quality"], "answer": outcome["answer"],
                                     "ledger": outcome["ledger"], "native_calls": producer.calls,
                                     "boundary_gaps": outcome["boundary_gaps"]})
                        rows[-1]["mcp_payload_metrics"] = {
                            **{key: metrics[key] - before[key] for key in before},
                            "rpc_count": len(metrics["rpc_roundtrip_ns"]) - before_calls,
                            "rpc_roundtrip_ns": sum(metrics["rpc_roundtrip_ns"].values()) - before_rpc_ns,
                        }
                processes.append({"sdk":sdk, "transport":transport, "task_id":task["id"], "metrics":metrics})

            safe_handle = _task_handle_token("tenant-a", "F08-screen", "F08-screen", "safe", "r1")
            for bound, tenant, calls, expand_calls, search_calls in (
                ("F08-screen", "tenant-a", [
                    ("foreign-document", {"task_id":"F08-screen", "source_id":"foreign", "revision":"r1"}),
                    ("wrong-task", {"task_id":"F08-confirm", "source_id":"safe", "revision":"r1"}),
                    ("unknown-source", {"task_id":"F08-screen", "source_id":"absent", "revision":"r1"}),
                    ("wrong-revision", {"task_id":"F08-screen", "source_id":"safe", "revision":"r2"}),
                ], [
                    ("expand-wrong-handle", {"task_id":"F08-screen", "source_id":"safe", "revision":"r1", "handle":"ev_tampered"}),
                    ("expand-wrong-revision", {"task_id":"F08-screen", "source_id":"safe", "revision":"r2", "handle":safe_handle}),
                    ("expand-wrong-task", {"task_id":"F08-confirm", "source_id":"safe", "revision":"r1", "handle":safe_handle}),
                    ("expand-foreign-document", {"task_id":"F08-screen", "source_id":"foreign", "revision":"r1", "handle":safe_handle}),
                ], [
                    ("search-unsearchable-task", {"task_id":"F08-screen", "query":"catalog mass and capacity"}),
                    ("search-wrong-task", {"task_id":"F01-screen", "query":"catalog mass and capacity"}),
                ]),
                ("F07-screen", "tenant-a", [
                    ("http-denied", {"task_id":"F07-screen", "source_id":"denied", "revision":"r1"}),
                    ("orphan-304", {"task_id":"F07-screen", "source_id":"orphan304", "revision":"r1"}),
                ], [], []),
                ("F08-screen", "tenant-b", [
                    ("wrong-principal", {"task_id":"F08-screen", "source_id":"safe", "revision":"r1"}),
                ], [
                    ("expand-wrong-principal", {"task_id":"F08-screen", "source_id":"safe", "revision":"r1", "handle":safe_handle}),
                ], []),
                ("F01-screen", "tenant-a", [], [], [
                    ("search-wrong-query", {"task_id":"F01-screen", "query":"wrong query"}),
                ]),
                ("F01-screen", "tenant-b", [], [], [
                    ("search-wrong-principal", {"task_id":"F01-screen", "query":"catalog mass and capacity"}),
                ]),
            ):
                with _task_rpc(sdk, transport, rust_bin, node, ts_server, core_probe,
                               bound, tenant) as (rpc, _):
                    if bound == "F08-screen" and tenant == "tenant-a":
                        safe = next(task for task in corpus if task["id"] == bound)
                        producer = SdkTaskProducer(core_probe, safe, rpc)
                        safe_doc = next(doc for doc in safe["documents"] if doc["id"] == "safe")
                        producer.produce(safe_doc)
                        producer.expand_handle(safe_doc, "r1", safe_handle)
                        response = rpc("tools/call", {"name":"athena_extract_document", "arguments": {
                            "task_id":bound, "source_id":"safe", "revision":"r1", "principal_tenant":"tenant-b"}})
                        _validate_tool_rejection(response, response["id"], {})
                        scope_controls.append({"sdk":sdk, "transport":transport,
                                               "case":"caller-scope-override", "status":"PASS"})
                        expand_override = rpc("tools/call", {"name":"athena_expand_handle", "arguments": {
                            "task_id":bound, "source_id":"safe", "revision":"r1", "handle":safe_handle,
                            "principal_tenant":"tenant-b"}})
                        _validate_tool_rejection(expand_override, expand_override["id"], {})
                        scope_controls.append({"sdk":sdk, "transport":transport,
                                               "case":"expand-caller-scope-override", "status":"PASS"})
                        search_override = rpc("tools/call", {"name":"athena_search_task", "arguments": {
                            "task_id":bound, "query":"catalog mass and capacity",
                            "principal_tenant":"tenant-b"}})
                        _validate_tool_rejection(search_override, search_override["id"], {})
                        scope_controls.append({"sdk":sdk, "transport":transport,
                                               "case":"search-caller-scope-override", "status":"PASS"})
                    if bound == "F01-screen" and tenant == "tenant-a":
                        f01 = next(task for task in corpus if task["id"] == bound)
                        producer = SdkTaskProducer(core_probe, f01, rpc)
                        producer.search_task(f01, f01["request"]["query"])
                    for case, arguments in calls:
                        response = rpc("tools/call", {"name":"athena_extract_document", "arguments":arguments})
                        _validate_task_denial(response)
                        scope_controls.append({"sdk":sdk, "transport":transport, "case":case, "status":"PASS"})
                    for case, arguments in expand_calls:
                        response = rpc("tools/call", {"name":"athena_expand_handle", "arguments":arguments})
                        _validate_task_denial(response)
                        scope_controls.append({"sdk":sdk, "transport":transport, "case":case, "status":"PASS"})
                    for case, arguments in search_calls:
                        response = rpc("tools/call", {"name":"athena_search_task", "arguments":arguments})
                        _validate_task_denial(response)
                        scope_controls.append({"sdk":sdk, "transport":transport, "case":case, "status":"PASS"})
    parity = {}
    for transport in transports:
        for task in selected:
            for representation in ("full", "compact"):
                pair = [row for row in rows if row["transport"] == transport
                        and row["task_id"] == task["id"] and row["representation"] == representation]
                equal = (pair[0]["native_calls"] == pair[1]["native_calls"]
                         and pair[0]["answer"] == pair[1]["answer"] and pair[0]["quality"] == pair[1]["quality"])
                if not equal:
                    raise DriverError("SDK arms disagree on task evidence or independently scored answer")
                parity[f"{transport}/{task['id']}/{representation}"] = equal
    return {"classification":"FROZEN_SYNTHETIC_TASK_REPLAY_NOT_REAL_MODEL_OR_COST",
            "selected_task_ids":[task["id"] for task in selected], "results":rows, "sdk_processes":processes,
            "scope_controls":scope_controls, "evidence_and_answer_parity":parity,
            "remaining_limits":["Task search candidate retrieval, document extraction, F04 HTML table association, and compact handle expansion execute through MCP SDK tools and core_probe; workflow orchestration and scoring run in deterministic Python replay",
                                "No real model, live web, target Linux or performance adoption evidence",
                                "Canonical payload counts exclude HTTP headers, framing and network costs; startup is separate from task ledger"]}


def run(
    rust_bin: Path,
    node: Path,
    ts_server: Path,
    core_probe: Path,
    transports: tuple[str, ...],
    *, task_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if not rust_bin.is_file() or not node.is_file() or not ts_server.is_file() or not core_probe.is_file():
        raise DriverError("provide existing Rust/TypeScript MCP artifacts and Rust core_probe")
    expected_core = _expected_core_outputs(core_probe.resolve())
    results = []
    for transport in transports:
        for sdk in ("rust", "typescript-sdk"):
            if transport == "stdio":
                result = _run_stdio(sdk, rust_bin, node, ts_server, core_probe, expected_core)
            elif transport == "http":
                result = _run_http(sdk, rust_bin, node, ts_server, core_probe, expected_core)
            else:
                raise DriverError(f"unsupported transport: {transport}")
            results.append(result)

    parity = {}
    for transport in transports:
        left = next(item for item in results if item["transport"] == ("stdio" if transport == "stdio" else "streamable-http") and item["sdk"] == "rust")
        right = next(item for item in results if item["transport"] == ("stdio" if transport == "stdio" else "streamable-http") and item["sdk"] == "typescript-sdk")
        parity[transport] = left["request_sha256"] == right["request_sha256"]
        if not parity[transport]:
            raise DriverError(f"the independent driver emitted different request bytes across SDKs ({transport})")
    failure_controls = _run_core_failure_controls(rust_bin, node, ts_server, transports)
    task_consumers = _run_task_consumers(rust_bin, node, ts_server, core_probe, transports, task_ids)
    return {
        "schema": "athena-mcp-core-integration-preflight/1",
        "classification": "LOCAL_MCP_AND_RUST_CORE_INTEGRATION_PREFLIGHT_NOT_A_PERFORMANCE_DECISION",
        "protocol_version": PROTOCOL_VERSION,
        "transports": list(transports),
        "same_request_bytes_across_sdk_arms": parity,
        "results": results,
        "core_failure_controls": failure_controls,
        "task_consumers": task_consumers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-bin", required=True, type=Path)
    parser.add_argument("--node", type=Path, default=None)
    parser.add_argument("--typescript-server", "--ts-server", dest="typescript_server", required=True, type=Path)
    parser.add_argument("--core-probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("stdio", "http", "both"), default="both")
    parser.add_argument("--task-id", action="append", help="Optional explicit frozen task subset; default: all tasks")
    parser.add_argument("--out", type=Path, default=None, help="Optional destination path for the JSON report")
    args = parser.parse_args(argv)
    transports = ("stdio", "http") if args.transport == "both" else (args.transport,)
    try:
        node_path = args.node
        if node_path is None:
            resolved_node = shutil.which("node")
            if resolved_node is None:
                raise DriverError("node executable not found on PATH; pass --node explicitly")
            node_path = Path(resolved_node)
        report = run(
            args.rust_bin.resolve(),
            node_path.resolve(),
            args.typescript_server.resolve(),
            args.core_probe.resolve(),
            transports,
            task_ids=None if args.task_id is None else tuple(args.task_id),
        )
        payload = {"status": "PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT", **report}
        if args.out is not None:
            if args.out.is_symlink():
                raise DriverError("output path cannot be a symlink")
            campaign.write_json(args.out, payload, max_bytes=32 * 1024 * 1024)
    except Exception as exc:
        print(json.dumps({"schema": "athena-mcp-core-integration-preflight/1", "status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
