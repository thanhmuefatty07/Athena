"""Consume creation time from the HTTP budget and reap owned request workers.

Credentials travel only over an anonymous stdin pipe. The worker has no proxy,
redirect, retry, shell, or arbitrary endpoint facility.
An OS process-creation call itself may be uninterruptible.
"""

import base64
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit


class TransportError(RuntimeError):
    pass


_WORKER_COMMAND = (sys.executable, "-I", "-B", str(Path(__file__).resolve()))
_SECRET = re.compile(r"/v1/projects/[0-9]+/secrets/[A-Za-z0-9_-]{1,255}/versions/(?:latest|[1-9][0-9]*):access\Z")
_METADATA = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"


def _allowed_endpoint(method, url):
    parsed = urlsplit(url)
    if parsed.fragment or method not in ("GET", "POST"):
        return False
    if url == _METADATA:
        return method == "GET"
    if parsed.scheme != "https":
        return False
    if parsed.netloc == "secretmanager.googleapis.com":
        return method == "GET" and not parsed.query and bool(_SECRET.fullmatch(parsed.path))
    paths = {"integrate.api.nvidia.com": {"/v1/models": "GET", "/v1/chat/completions": "POST"},
             "openrouter.ai": {"/api/v1/models": "GET", "/api/v1/key": "GET", "/api/v1/chat/completions": "POST"}}
    return paths.get(parsed.netloc, {}).get(parsed.path) == method


def _validate(payload):
    if (not isinstance(payload, dict) or set(payload) != {"method", "url", "headers", "body", "timeout_s", "max_bytes"}
            or not isinstance(payload["url"], str) or len(payload["url"]) > 2048
            or not _allowed_endpoint(payload["method"], payload["url"])
            or type(payload["timeout_s"]) not in (int, float) or not math.isfinite(payload["timeout_s"])
            or not 0 < payload["timeout_s"] <= 60
            or type(payload["max_bytes"]) is not int or not 0 < payload["max_bytes"] <= 1048576
            or not isinstance(payload["headers"], dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or not k
                   or any(c in k + v for c in "\r\n") for k, v in payload["headers"].items())
            or len(json.dumps(payload["headers"])) > 16384
            or payload["body"] is not None and (not isinstance(payload["body"], str) or len(payload["body"]) > 3 * 1048576)):
        raise TransportError("HTTP_REQUEST_DENIED")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def _exchange(payload):
    _validate(payload)
    body = base64.b64decode(payload["body"], validate=True) if payload["body"] is not None else None
    if body is not None and len(body) > 2 * 1048576:
        raise TransportError("HTTP_REQUEST_BYTE_LIMIT")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                  request.HTTPSHandler(context=ssl.create_default_context()))
    req = request.Request(payload["url"], data=body, headers=payload["headers"], method=payload["method"])
    try:
        response = opener.open(req, timeout=payload["timeout_s"])
    except error.HTTPError as exc:
        response = exc
    with response:
        headers = dict(response.headers.items())
        if len(json.dumps(headers)) > 16384:
            raise TransportError("HTTP_RESPONSE_HEADER_LIMIT")
        data = response.read(payload["max_bytes"] + 1)
        if len(data) > payload["max_bytes"]:
            raise TransportError("HTTP_RESPONSE_BYTE_LIMIT")
        return {"status": response.code, "headers": headers, "body": base64.b64encode(data).decode("ascii")}


def bounded_request(method, url, headers, body, timeout_s, max_bytes):
    started = time.monotonic()
    payload = dict(method=method, url=url, headers=headers,
                   body=base64.b64encode(body).decode("ascii") if body is not None else None,
                   timeout_s=timeout_s, max_bytes=max_bytes)
    _validate(payload)
    data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
    remaining = timeout_s - (time.monotonic() - started)
    if remaining <= 0:
        raise TransportError("HTTP_DEADLINE")
    # No provider key, cloud token, Python path override or proxy is inherited.
    environment = {k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "LANG") if k in os.environ}
    try:
        with subprocess.Popen((*_WORKER_COMMAND, str(os.getpid()), str(remaining)), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment) as worker:
            try:
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(worker.args, timeout_s)
                output, _ = worker.communicate(input=data, timeout=remaining)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
                raise TransportError("HTTP_DEADLINE") from None
            except BaseException:
                worker.kill()
                worker.wait()
                raise
            returncode = worker.returncode
    except OSError:
        raise TransportError("HTTP_WORKER_FAILED") from None
    if returncode != 0 or len(output) > 2 * max_bytes + 65536:
        raise TransportError("HTTP_WORKER_FAILED")
    try:
        reply = json.loads(output)
        if set(reply) != {"status", "headers", "body"} or type(reply["status"]) is not int or not 100 <= reply["status"] <= 599:
            raise ValueError()
        if not isinstance(reply["headers"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in reply["headers"].items()):
            raise ValueError()
        decoded = base64.b64decode(reply["body"], validate=True)
        if len(decoded) > max_bytes:
            raise ValueError()
        return reply["status"], reply["headers"], decoded
    except (ValueError, TypeError, KeyError):
        raise TransportError("HTTP_INVALID_WORKER_REPLY") from None


def _bind_parent(parent_pid, seconds):
    if type(parent_pid) is not int or parent_pid <= 1 or not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise TransportError("HTTP_WORKER_OWNERSHIP_DENIED")
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(ctypes.c_int(1), ctypes.c_ulong(signal.SIGKILL),
                      ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            raise TransportError("HTTP_WORKER_OWNERSHIP_DENIED")
        if os.getppid() != parent_pid:
            raise TransportError("HTTP_WORKER_PARENT_CHANGED")
        # Independent of the parent's cleanup path, including stalled headers/DNS.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    else:
        # Preparation hosts retain a finite worker lifetime; Linux is the live target.
        import threading
        timer = threading.Timer(seconds, lambda: os._exit(124))
        timer.daemon = True
        timer.start()


def _worker_main():
    try:
        _bind_parent(int(sys.argv[-2]), float(sys.argv[-1]))
        data = sys.stdin.buffer.read(4 * 1048576 + 1)
        if len(data) > 4 * 1048576:
            return 1
        reply = _exchange(json.loads(data))
        sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=True, allow_nan=False).encode("ascii"))
        return 0
    except Exception:
        return 1  # No headers, payload, token, exception text or traceback in diagnostics.


if __name__ == "__main__":
    raise SystemExit(_worker_main())
