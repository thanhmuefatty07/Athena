import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cloud_experiments.mcp_sdk import driver


def _resolve_newest_binary(candidates, env_key: str):
    override = os.environ.get(env_key, "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda item: item.stat().st_mtime_ns) if existing else None


def _typescript_dependencies_present(ts_server: Path) -> bool:
    if (ts_server.parent / "node_modules" / "@modelcontextprotocol" / "server" / "package.json").is_file():
        return True
    node_path_env = os.environ.get("NODE_PATH", "")
    for entry in node_path_env.split(os.pathsep):
        cleaned = entry.strip()
        if cleaned and (Path(cleaned) / "@modelcontextprotocol" / "server" / "package.json").is_file():
            return True
    return False


def _local_sdk_artifacts(require_core_probe: bool = False):
    rust_name = "athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture"
    rust_bin = _resolve_newest_binary(
        (
            driver.ROOT / "rust" / "target" / "release" / rust_name,
            driver.ROOT / "rust" / "target" / "debug" / rust_name,
        ),
        "ATHENA_MCP_RUST_BIN",
    )
    node_exe = shutil.which("node")
    node_path = Path(node_exe) if node_exe is not None else None
    ts_override = os.environ.get("ATHENA_MCP_TS_SERVER", "").strip()
    ts_server = Path(ts_override) if ts_override else (driver.ROOT / "typescript" / "server.mjs")
    if not ts_server.is_file() or not _typescript_dependencies_present(ts_server):
        ts_server = None
    core_probe = None
    if require_core_probe:
        probe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
        core_probe = _resolve_newest_binary(
            (
                driver.ROOT.parent.parent / "target" / "release" / probe_name,
                driver.ROOT.parent.parent / "target" / "debug" / probe_name,
            ),
            "ATHENA_CORE_PROBE_BIN",
        )
    if rust_bin is None or node_path is None or ts_server is None or (require_core_probe and core_probe is None):
        return None
    return rust_bin, node_path, ts_server, core_probe


class StreamableHTTPProgressResponseTests(unittest.TestCase):
    def test_ignores_empty_priming_event_and_selects_terminal_after_matching_progress(self):
        progress = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": "progress-1", "progress": 0},
        }
        terminal = {"jsonrpc": "2.0", "id": "call-1", "result": {"content": []}}
        body = b"data:\n\n" + b"".join(
            b"event: message\ndata: " + json.dumps(message).encode("utf-8") + b"\n\n"
            for message in (progress, terminal)
        )

        self.assertEqual(
            driver._decode_sse_response(body, "call-1", "progress-1"), terminal
        )


class LegacyProtocolRejectionTests(unittest.TestCase):
    def test_accepts_the_pinned_unsupported_version_response(self):
        response = {
            "jsonrpc": "2.0",
            "id": "legacy-1",
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {
                    "requested": "2025-11-25",
                    "supported": ["2026-07-28"],
                },
            },
        }

        self.assertEqual(driver._validate_legacy_stdio_response(response, "legacy-1"), -32022)

    def test_rejects_an_unrelated_server_error(self):
        response = {
            "jsonrpc": "2.0",
            "id": "legacy-1",
            "error": {"code": -32603, "message": "Internal error"},
        }

        with self.assertRaises(driver.DriverError):
            driver._validate_legacy_stdio_response(response, "legacy-1")

    def test_rejects_an_error_that_names_the_wrong_protocol_version(self):
        response = {
            "jsonrpc": "2.0",
            "id": "legacy-1",
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {
                    "requested": "2025-03-26",
                    "supported": ["2026-07-28"],
                },
            },
        }

        with self.assertRaises(driver.DriverError):
            driver._validate_legacy_stdio_response(response, "legacy-1")

    def test_rejects_a_response_that_contains_both_result_and_error(self):
        response = {
            "jsonrpc": "2.0",
            "id": "legacy-1",
            "result": {},
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {
                    "requested": "2025-11-25",
                    "supported": ["2026-07-28"],
                },
            },
        }

        with self.assertRaises(driver.DriverError):
            driver._validate_legacy_stdio_response(response, "legacy-1")


class CoreResultValidationTests(unittest.TestCase):
    def setUp(self):
        fixture = driver.CORE_FIXTURES["short"]
        self.payload = "\n".join((
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
        self.response = {
            "result": {"content": [{"type": "text", "text": self.payload}]}
        }

    def test_accepts_aae_with_expected_core_fixture_content(self):
        self.assertGreater(
            driver._validate_tool_result(self.response, "short", self.payload), 0
        )

    def test_rejects_the_previous_static_non_aae_payload(self):
        payload = '{"facts":[],"record_id":"fixture-01","fixture_case":"short"}'
        response = {"result": {"content": [{"type": "text", "text": payload}]}}
        with self.assertRaises(driver.DriverError):
            driver._validate_tool_result(response, "short", payload)

    def test_task_consumer_rejects_error_labelled_evidence(self):
        self.response["result"]["resultType"] = "complete"
        self.assertEqual(driver._task_aae(self.response), self.payload.encode("utf-8"))
        self.response["result"]["isError"] = True
        with self.assertRaisesRegex(driver.LabError, "MCP_TASK_TOOL_FAILED"):
            driver._task_aae(self.response)
        f01 = next(card for card in driver.tasks() if card["id"] == "F01-screen")
        producer = driver.RustProbeProducer(Path(sys.executable).resolve(strict=True))
        valid_search = {
            "schema": "athena-task-search-results/1",
            "task_id": "F01-screen",
            "tenant": "tenant-a",
            "query": f01["request"]["query"],
            "candidates": [
                {
                    "source_id": "a",
                    "source_url": f01["documents"][0]["url"],
                    "revision": "r1",
                    "freshness": "fresh",
                    "observed_at_unix_ms": f01["documents"][0]["observed_at_unix_ms"],
                    "passages": f01["search_candidates"][0]["passages"],
                }
            ],
        }
        valid_bytes = (json.dumps(valid_search, separators=(",", ":")) + "\n").encode("utf-8")
        checked = producer.validated_search_results(valid_bytes, f01, f01["request"]["query"])
        self.assertEqual(checked["candidates"], f01["search_candidates"])
        for mutator in (
            lambda obj: obj.update(task_id="F02-screen"),
            lambda obj: obj.update(tenant="tenant-b"),
            lambda obj: obj.update(query="other query"),
            lambda obj: obj.update(extra=True),
            lambda obj: obj.update(candidates=[]),
            lambda obj: obj["candidates"][0].update(revision="r999"),
            lambda obj: obj["candidates"][0].update(freshness="stale"),
            lambda obj: obj["candidates"][0].update(observed_at_unix_ms=True),
            lambda obj: obj["candidates"][0]["passages"][0].update(
                text="mass = 999 | kg; capacity = 8 | L;"
            ),
            lambda obj: obj["candidates"].append(dict(obj["candidates"][0])),
        ):
            bad_obj = json.loads(valid_bytes.decode("utf-8"))
            mutator(bad_obj)
            bad_bytes = (json.dumps(bad_obj, separators=(",", ":")) + "\n").encode("utf-8")
            with self.assertRaises(driver.LabError):
                producer.validated_search_results(bad_bytes, f01, f01["request"]["query"])


class CandidateInputBoundaryTests(unittest.TestCase):
    def test_frozen_inputs_match_task_sources_without_oracles(self):
        path = driver.ROOT / "task_inputs.json"
        self.assertLessEqual(path.stat().st_size, 1048576)
        registry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(registry), {"schema", "tasks"})
        self.assertEqual(registry["schema"], "athena-mcp-task-inputs/1")
        fields = {"id", "tenant", "revision", "status", "response_kind", "cached_body",
                  "html", "url", "observed_at_unix_ms"}
        candidate_fields = {"source_id", "revision", "freshness", "passages"}
        expected = []
        for task in driver.tasks():
            expected.append({
                "id": task["id"],
                "tenant": task["tenant"],
                "query": task["request"].get("query"),
                "documents": [
                    {key: value for key, value in doc.items() if key in fields}
                    for doc in task["documents"]
                ],
                "search_candidates": [
                    {
                        key: (
                            [{"locator": p["locator"], "text": p["text"]} for p in value]
                            if key == "passages"
                            else value
                        )
                        for key, value in candidate.items()
                        if key in candidate_fields
                    }
                    for candidate in task["search_candidates"]
                ],
            })
        self.assertEqual(registry["tasks"], expected)


class OwnedProcessObservationTests(unittest.TestCase):
    def test_observes_the_owned_child_before_and_after_exit(self):
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import sys;sys.stdin.buffer.read(1)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
                                      if key in os.environ})
        try:
            self.assertTrue(driver._process_running(child.pid))
            child.communicate(b"x", timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertFalse(driver._process_running(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


class ConcurrencyControlTests(unittest.TestCase):
    def test_validates_admission_saturation_core_failure_contract(self):
        saturated = {
            "result": {
                "resultType": "complete",
                "isError": True,
                "content": [{"type": "text", "text": "CORE_PROBE_ADMISSION_SATURATED"}],
            }
        }
        driver._validate_core_failure(saturated, "CORE_PROBE_ADMISSION_SATURATED")
        with self.assertRaises(driver.DriverError):
            driver._validate_core_failure(
                {
                    "result": {
                        "resultType": "complete",
                        "isError": False,
                        "content": [{"type": "text", "text": "CORE_PROBE_ADMISSION_SATURATED"}],
                    }
                },
                "CORE_PROBE_ADMISSION_SATURATED",
            )

    def test_synthetic_short_aae_satisfies_short_fixture_oracle(self):
        payload = driver._synthetic_short_aae()
        response = {"result": {"content": [{"type": "text", "text": payload}]}}
        self.assertGreater(driver._validate_tool_result(response, "short", payload), 0)

    def test_http_concurrency_and_sibling_isolation_across_sdks(self):
        artifacts = _local_sdk_artifacts()
        if artifacts is None:
            self.skipTest("local MCP SDK artifacts unavailable")
        rust_bin, node_path, ts_server, _ = artifacts
        rows = driver._run_http_concurrency_controls(rust_bin, node_path, ts_server)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["status"], "PASS")
            self.assertTrue(row["producer_exit_observed"])

    def test_rejects_invalid_max_inflight_at_startup_across_sdks(self):
        artifacts = _local_sdk_artifacts()
        if artifacts is None:
            self.skipTest("local MCP SDK artifacts unavailable")
        rust_bin, node_path, ts_server, _ = artifacts
        base_env = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "NODE_PATH")
            if key in os.environ
        }
        base_env["ATHENA_L03_AUTH_TOKEN"] = "test-token"
        base_env["ATHENA_CORE_PROBE_BIN"] = sys.executable
        for sdk_cmd in ([str(rust_bin), "stdio"], [str(node_path), str(ts_server), "stdio"]):
            for invalid in ("0", "9", "-1", "abc", ""):
                env = {**base_env, "ATHENA_MCP_MAX_INFLIGHT": invalid}
                completed = subprocess.run(
                    sdk_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=driver.ROOT,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0, f"{sdk_cmd} accepted {invalid!r}")

    def test_grandchild_stdout_inheritance_does_not_leak_admission_slot_across_sdks(self):
        import secrets
        import time

        artifacts = _local_sdk_artifacts()
        if artifacts is None:
            self.skipTest("local MCP SDK artifacts unavailable")
        rust_bin, node_path, ts_server, _ = artifacts
        expected_short = driver._synthetic_short_aae()
        script = (
            "import json,os,subprocess,sys,time\n"
            "from pathlib import Path\n"
            f"EXPECTED={expected_short.encode('utf-8')!r}\n"
            "payload=json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
            "tag='short' if str(payload.get('url','')).endswith('/short') else 'long'\n"
            "if tag=='long':\n"
            " gc=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(10)'],close_fds=False)\n"
            " Path('grandchild.pid').write_text(str(gc.pid),encoding='ascii')\n"
            " tmp=Path(f'probe-long.{os.getpid()}.tmp')\n"
            " tmp.write_text(str(os.getpid()),encoding='ascii')\n"
            " tmp.replace(Path('probe-long.pid'))\n"
            " time.sleep(10)\n"
            "else:\n"
            " sys.stdout.buffer.write(EXPECTED);sys.stdout.buffer.flush();sys.exit(0)\n"
        )
        for sdk in ("rust", "typescript-sdk"):
            with tempfile.TemporaryDirectory(prefix="athena-mcp-gc-") as folder:
                root = Path(folder)
                (root / "export").write_text(script, encoding="utf-8")
                token = secrets.token_urlsafe(32)
                port = driver._reserve_port()
                process, stderr_buffer, stderr_thread = driver._spawn(
                    driver._make_command(sdk, "http", rust_bin, node_path, ts_server, port),
                    token, Path(sys.executable), stdio=False, cwd=root, max_inflight=1)
                conn_1 = None
                try:
                    driver._wait_for_http(process, port, stderr_buffer, stderr_thread)
                    progress_token = f"progress-gc-{sdk}"
                    call_1 = driver._request(
                        "tools/call", f"gc-1-{sdk}",
                        {"name": driver.CONTRACT["tool_name"], "arguments": {"fixture_case": "long"}},
                        progress_token=progress_token,
                    )
                    conn_1, _, _, pid_1 = driver._start_inflight_http_call(
                        port, call_1, token, root / "probe-long.pid", progress_token)
                    gc_pid = int((root / "grandchild.pid").read_text(encoding="ascii").strip())
                    self.assertTrue(driver._process_running(gc_pid))
                    driver._abort_inflight_http_call(conn_1, pid_1)
                    conn_1 = None
                    self.assertTrue(
                        driver._process_running(gc_pid),
                        f"{sdk} grandchild exited before inherited-pipe slot recovery check",
                    )
                    recover_deadline = time.monotonic() + 1.0
                    recovered = False
                    attempt = 0
                    while time.monotonic() < recover_deadline:
                        attempt += 1
                        call_2 = driver._request(
                            "tools/call", f"gc-2-{sdk}-{attempt}",
                            {"name": driver.CONTRACT["tool_name"], "arguments": {"fixture_case": "short"}},
                        )
                        status_2, resp_2, _, _ = driver._http_exchange(port, call_2, token)
                        self.assertEqual(status_2, 200)
                        resp_2 = driver._assert_response(resp_2, str(call_2["id"]))
                        result_2 = resp_2.get("result")
                        if isinstance(result_2, dict) and result_2.get("isError") is True:
                            driver._validate_core_failure(resp_2, "CORE_PROBE_ADMISSION_SATURATED")
                            time.sleep(0.02)
                            continue
                        self.assertGreater(
                            driver._validate_tool_result(resp_2, "short", expected_short),
                            0,
                        )
                        recovered = True
                        break
                    self.assertTrue(recovered, f"{sdk} admission slot did not recover while grandchild held stdout")
                    self.assertTrue(
                        driver._process_running(gc_pid),
                        f"{sdk} grandchild exited before inherited-pipe slot recovery completed",
                    )
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
                    if (root / "grandchild.pid").is_file():
                        gc_pid = int((root / "grandchild.pid").read_text(encoding="ascii").strip())
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/F", "/PID", str(gc_pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=False,
                            )
                        else:
                            try:
                                os.kill(gc_pid, 9)
                            except OSError:
                                pass
                        deadline = time.monotonic() + 2.0
                        while driver._process_running(gc_pid) and time.monotonic() < deadline:
                            time.sleep(0.02)

    def test_missing_or_empty_scoped_task_context_suppresses_progress_across_sdks(self):
        import secrets

        artifacts = _local_sdk_artifacts()
        if artifacts is None:
            self.skipTest("local MCP SDK artifacts unavailable")
        rust_bin, node_path, ts_server, _ = artifacts
        for sdk in ("rust", "typescript-sdk"):
            for tenant, bound_task_id in (("tenant-a", None), ("", "F07-screen")):
                token = secrets.token_urlsafe(32)
                port = driver._reserve_port()
                process, stderr_buffer, stderr_thread = driver._spawn(
                    driver._make_command(sdk, "http", rust_bin, node_path, ts_server, port),
                    token, Path(sys.executable), stdio=False,
                    tenant=tenant, bound_task_id=bound_task_id)
                try:
                    driver._wait_for_http(process, port, stderr_buffer, stderr_thread)
                    for tool_name, arguments in (
                        (
                            "athena_extract_document",
                            {"task_id": "F07-screen", "source_id": "doc-1", "revision": "r1"},
                        ),
                        (
                            "athena_expand_handle",
                            {
                                "task_id": "F07-screen",
                                "source_id": "doc-1",
                                "revision": "r1",
                                "handle": "ev_0",
                            },
                        ),
                        (
                            "athena_search_task",
                            {"task_id": "F01-screen", "query": "catalog mass and capacity"},
                        ),
                    ):
                        call = driver._request(
                            "tools/call",
                            f"ctx-{sdk}-{tool_name}",
                            {"name": tool_name, "arguments": arguments},
                            progress_token=f"progress-ctx-{sdk}-{tool_name}",
                        )
                        status, resp, raw, _ = driver._http_exchange(port, call, token)
                        self.assertEqual(status, 200)
                        self.assertNotIn(b"notifications/progress", raw)
                        driver._validate_core_failure(
                            driver._assert_response(resp, str(call["id"])),
                            "SCOPED_TASK_CONTEXT_MISSING",
                        )
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


class TaskConsumerTableAndHandleTests(unittest.TestCase):
    def test_local_sdk_artifacts_requires_typescript_dependencies_and_honors_env_overrides(self):
        with tempfile.TemporaryDirectory(prefix="athena-mcp-artifacts-") as folder:
            root = Path(folder)
            fake_rust = root / "athena-mcp-rust-fixture"
            fake_probe = root / "core_probe"
            fake_ts = root / "server.mjs"
            for candidate in (fake_rust, fake_probe, fake_ts):
                candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            previous = {
                key: os.environ.get(key)
                for key in ("ATHENA_MCP_RUST_BIN", "ATHENA_MCP_TS_SERVER", "ATHENA_CORE_PROBE_BIN", "NODE_PATH")
            }
            try:
                os.environ["ATHENA_MCP_RUST_BIN"] = str(fake_rust)
                os.environ["ATHENA_MCP_TS_SERVER"] = str(fake_ts)
                os.environ["ATHENA_CORE_PROBE_BIN"] = str(fake_probe)
                os.environ.pop("NODE_PATH", None)
                self.assertIsNone(_local_sdk_artifacts(require_core_probe=True))
                pkg_json = root / "node_modules" / "@modelcontextprotocol" / "server" / "package.json"
                pkg_json.parent.mkdir(parents=True, exist_ok=True)
                pkg_json.write_text('{"name":"@modelcontextprotocol/server"}', encoding="utf-8")
                if shutil.which("node") is not None:
                    resolved = _local_sdk_artifacts(require_core_probe=True)
                    self.assertIsNotNone(resolved)
                    self.assertEqual(resolved[0], fake_rust)
                    self.assertEqual(resolved[2], fake_ts)
                    self.assertEqual(resolved[3], fake_probe)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_single_chunk_blake3_matches_reference_vectors(self):
        self.assertEqual(
            driver._blake3_hex(b""),
            "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
        )
        self.assertEqual(
            driver._blake3_hex(b"abc"),
            "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85",
        )
        self.assertEqual(
            driver._blake3_hex(bytes(range(64))),
            "4eed7141ea4a5cd4b788606bd23f46e212af9cacebacdc7d1f4c6dc7f2511b98",
        )
        self.assertEqual(
            driver._blake3_hex(bytes(range(65))),
            "de1e5fa0be70df6d2be8fffd0e99ceaa8eb6e8c93a63f2d8d1c30ecb6b263dee",
        )
        with self.assertRaises(driver.DriverError):
            driver._blake3_hex(b"x" * 1025)

    def test_f04_table_contract_and_compact_handle_expansion_across_sdks(self):
        artifacts = _local_sdk_artifacts(require_core_probe=True)
        if artifacts is None:
            self.skipTest("local MCP SDK or core_probe artifacts unavailable")
        rust_bin, node_path, ts_server, core_probe = artifacts
        report = driver._run_task_consumers(
            rust_bin, node_path, ts_server, core_probe, ("stdio", "http"), ("F01-screen", "F04-screen")
        )
        self.assertEqual(len(report["results"]), 16)
        for row in report["results"]:
            self.assertTrue(row["quality"]["contract_satisfied"])
            self.assertTrue(row["quality"]["fact_contract_satisfied"])
            if row["task_id"] == "F01-screen":
                self.assertTrue(
                    any(call["tool"] == "athena_search_task" for call in row["native_calls"]),
                    f"query task row did not invoke athena_search_task: {row}",
                )
            if row["task_id"] == "F04-screen":
                self.assertEqual(row["quality"]["table_contract_status"], "SATISFIED")
                self.assertEqual(row["boundary_gaps"], [])
            if row["representation"] == "compact":
                self.assertTrue(
                    any(call["tool"] == "athena_expand_handle" for call in row["native_calls"]),
                    f"compact task row did not invoke athena_expand_handle: {row}",
                )
        self.assertTrue(all(report["evidence_and_answer_parity"].values()))
        expand_cases = {item["case"] for item in report["scope_controls"] if item["case"].startswith("expand-")}
        self.assertEqual(
            expand_cases,
            {
                "expand-caller-scope-override",
                "expand-wrong-handle",
                "expand-wrong-revision",
                "expand-wrong-task",
                "expand-foreign-document",
                "expand-wrong-principal",
            },
        )
        search_cases = {item["case"] for item in report["scope_controls"] if item["case"].startswith("search-")}
        self.assertEqual(
            search_cases,
            {
                "search-caller-scope-override",
                "search-unsearchable-task",
                "search-wrong-task",
                "search-wrong-query",
                "search-wrong-principal",
            },
        )

    def test_expand_handle_rejects_tampered_and_cross_scope_handles_across_sdks(self):
        artifacts = _local_sdk_artifacts(require_core_probe=True)
        if artifacts is None:
            self.skipTest("local MCP SDK or core_probe artifacts unavailable")
        rust_bin, node_path, ts_server, core_probe = artifacts
        cards_by_id = {card["id"]: card for card in driver.tasks()}
        f01_card = cards_by_id["F01-screen"]
        f07_card = cards_by_id["F07-screen"]
        f07_docs = {doc["id"]: doc for doc in f07_card["documents"]}
        f08_card = cards_by_id["F08-screen"]
        f08_docs = {doc["id"]: doc for doc in f08_card["documents"]}
        safe_f08_handle = driver._task_handle_token("tenant-a", "F08-screen", "F08-screen", "safe", "r1")
        foreign_f08_handle = driver._task_handle_token("tenant-a", "F08-screen", "F08-screen", "foreign", "r1")
        denied_f07_handle = driver._task_handle_token("tenant-a", "F07-screen", "F07-screen", "denied", "r1")
        orphan304_f07_handle = driver._task_handle_token("tenant-a", "F07-screen", "F07-screen", "orphan304", "r1")
        for sdk in ("rust", "typescript-sdk"):
            with driver._task_rpc(
                sdk, "http", rust_bin, node_path, ts_server, core_probe, "F08-screen", "tenant-a"
            ) as (rpc, _):
                producer = driver.SdkTaskProducer(core_probe, f08_card, rpc)
                expanded = producer.expand_handle(
                    f08_docs["safe"], "r1", safe_f08_handle, task=f08_card
                )
                self.assertTrue(len(expanded["passages"]) >= 1)
                for bad_handle, doc_id, revision in (
                    (safe_f08_handle + "0", "safe", "r1"),
                    (safe_f08_handle, "safe", "r999"),
                    (foreign_f08_handle, "foreign", "r1"),
                ):
                    with self.assertRaises(driver.LabError):
                        producer.expand_handle(
                            f08_docs[doc_id], revision, bad_handle, task=f08_card
                        )
                override_resp = rpc(
                    "tools/call",
                    {
                        "name": "athena_expand_handle",
                        "arguments": {
                            "task_id": "F08-screen",
                            "source_id": "safe",
                            "revision": "r1",
                            "handle": safe_f08_handle,
                            "principal_tenant": "tenant-b",
                        },
                    },
                )
                driver._validate_tool_rejection(override_resp, override_resp["id"], {})
                unsearchable_resp = rpc(
                    "tools/call",
                    {
                        "name": "athena_search_task",
                        "arguments": {
                            "task_id": "F08-screen",
                            "query": "catalog mass and capacity",
                        },
                    },
                )
                driver._validate_task_denial(unsearchable_resp)
            with driver._task_rpc(
                sdk, "stdio", rust_bin, Path(node_path), ts_server, core_probe, "F01-screen", "tenant-a"
            ) as (rpc_f01, _):
                producer_f01 = driver.SdkTaskProducer(core_probe, f01_card, rpc_f01)
                searched = producer_f01.search_task(f01_card, f01_card["request"]["query"])
                self.assertEqual(len(searched["candidates"]), 1)
                with self.assertRaisesRegex(driver.LabError, "MCP_TASK_QUERY_MISMATCH"):
                    producer_f01.search_task(f01_card, "wrong query")
                wrong_query_resp = rpc_f01(
                    "tools/call",
                    {
                        "name": "athena_search_task",
                        "arguments": {"task_id": "F01-screen", "query": "wrong query"},
                    },
                )
                driver._validate_task_denial(wrong_query_resp)
                search_override_resp = rpc_f01(
                    "tools/call",
                    {
                        "name": "athena_search_task",
                        "arguments": {
                            "task_id": "F01-screen",
                            "query": f01_card["request"]["query"],
                            "principal_tenant": "tenant-b",
                        },
                    },
                )
                driver._validate_tool_rejection(search_override_resp, search_override_resp["id"], {})
            with driver._task_rpc(
                sdk, "stdio", rust_bin, Path(node_path), ts_server, core_probe, "F07-screen", "tenant-a"
            ) as (rpc_f07, _):
                producer_f07 = driver.SdkTaskProducer(core_probe, f07_card, rpc_f07)
                for bad_handle, doc_id in (
                    (denied_f07_handle, "denied"),
                    (orphan304_f07_handle, "orphan304"),
                ):
                    with self.assertRaises(driver.LabError):
                        producer_f07.expand_handle(
                            f07_docs[doc_id], "r1", bad_handle, task=f07_card
                        )


class DriverCliContractTests(unittest.TestCase):
    def test_main_accepts_ts_server_alias_default_node_and_out_file(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        fake_report = {
            "schema": "athena-mcp-core-integration-preflight/1",
            "classification": "LOCAL_MCP_AND_RUST_CORE_INTEGRATION_PREFLIGHT_NOT_A_PERFORMANCE_DECISION",
            "protocol_version": driver.PROTOCOL_VERSION,
            "transports": ["stdio", "http"],
            "same_request_bytes_across_sdk_arms": {"stdio": True, "http": True},
            "results": [],
            "core_failure_controls": [],
            "task_consumers": {},
        }
        with tempfile.TemporaryDirectory(prefix="athena-mcp-cli-") as temp_dir:
            root = Path(temp_dir)
            rust_bin = root / "athena-mcp-rust-fixture"
            node_bin = root / "node"
            ts_server = root / "server.mjs"
            core_probe = root / "core_probe"
            out_path = root / "mcp-sdk-preflight.json"
            for path in (rust_bin, node_bin, ts_server, core_probe):
                path.write_bytes(b"fixture")

            stdout = StringIO()
            with (
                patch.object(driver.shutil, "which", return_value=str(node_bin)),
                patch.object(driver, "run", return_value=fake_report) as mocked_run,
                redirect_stdout(stdout),
            ):
                code = driver.main([
                    "--rust-bin", str(rust_bin),
                    "--ts-server", str(ts_server),
                    "--core-probe", str(core_probe),
                    "--out", str(out_path),
                ])

            self.assertEqual(code, 0)
            mocked_run.assert_called_once_with(
                rust_bin.resolve(),
                node_bin.resolve(),
                ts_server.resolve(),
                core_probe.resolve(),
                ("stdio", "http"),
                task_ids=None,
            )
            saved = json.loads(out_path.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(saved["status"], "PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT")
            self.assertEqual(saved, printed)

            fail_stdout = StringIO()
            with (
                patch.object(driver.shutil, "which", return_value=None),
                redirect_stdout(fail_stdout),
            ):
                fail_code = driver.main([
                    "--rust-bin", str(rust_bin),
                    "--typescript-server", str(ts_server),
                    "--core-probe", str(core_probe),
                ])
            self.assertEqual(fail_code, 1)
            self.assertEqual(json.loads(fail_stdout.getvalue())["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()




