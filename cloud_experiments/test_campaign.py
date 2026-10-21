"""Cloud campaign controls: evidence pairing, selection and source isolation."""
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cloud_experiments import campaign, research
from cloud_experiments import agent_lab


class CampaignControls(unittest.TestCase):
    def test_dependency_advisory_http_redirect_cannot_reach_a_second_origin(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        from urllib.error import HTTPError
        from urllib.request import Request

        received = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received.append(("POST", self.path))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                received.append(("GET", self.path))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/start", data=b"synthetic package query")
            try:
                with campaign._open_dependency_advisory_request(request, timeout=2):
                    pass
            except HTTPError:
                pass
            self.assertEqual(received, [("POST", "/start")],
                             "A redirect must be rejected before a second request reaches its target")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_source_preparation_command_stops_at_output_and_time_bounds(self):
        from kaggle_experiments import pack_snapshots
        noisy = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"]
        with self.assertRaisesRegex(ValueError, "output exceeds"):
            pack_snapshots.bounded_command_output(noisy, cwd=Path.cwd(), max_bytes=4096)
        stalled = [sys.executable, "-c", "import time; time.sleep(60)"]
        with self.assertRaisesRegex(TimeoutError, "time bound"):
            pack_snapshots.bounded_command_output(stalled, cwd=Path.cwd(), timeout_s=1, max_bytes=4096)

    def test_source_preparation_command_scrubs_parent_credentials(self):
        from unittest.mock import patch
        from kaggle_experiments import pack_snapshots
        command = [sys.executable, "-c",
                   "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
                   "('NVIDIA_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'HTTP_PROXY')}))"]
        with patch.dict("os.environ", {"NVIDIA_API_KEY": "synthetic", "GOOGLE_APPLICATION_CREDENTIALS": "synthetic",
                                       "HTTP_PROXY": "synthetic"}):
            observed = json.loads(pack_snapshots.bounded_command_output(
                command, cwd=Path.cwd(), max_bytes=4096).decode("utf-8"))
        self.assertEqual(observed, {"NVIDIA_API_KEY": None, "GOOGLE_APPLICATION_CREDENTIALS": None,
                                    "HTTP_PROXY": None})

    def test_json_commit_preserves_other_staging_and_failed_write_keeps_old_state(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-commit-") as folder:
            root = Path(folder)
            target = root / "report.json"
            reserved = root / "report.json.tmp"
            target.write_bytes(b'{"complete":false}\n')
            reserved.write_bytes(b"another writer owns this file")
            campaign.write_json(target, {"complete": True, "value": "é"})
            self.assertTrue(reserved.is_file(), "A JSON commit consumed another writer's staging file")
            self.assertEqual(reserved.read_bytes(), b"another writer owns this file")
            self.assertEqual(json.loads(target.read_bytes()), {"complete": True, "value": "é"})
            before = target.read_bytes()
            with patch("cloud_experiments.campaign.os.replace", side_effect=OSError("commit interrupted")):
                with self.assertRaisesRegex(OSError, "commit interrupted"):
                    campaign.write_json(target, {"complete": False})
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(reserved.read_bytes(), b"another writer owns this file")
            self.assertEqual({p.name for p in root.iterdir()}, {target.name, reserved.name},
                             "A failed commit must remove only its own temporary file")
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            try:
                run.manifest["current_phase"] = "collected"
                run.save()
                manifest_path = run.directory / "manifest.json"
                before = manifest_path.read_bytes()
                run.manifest["current_phase"] = "analysis"
                with patch("cloud_experiments.campaign.os.replace", side_effect=OSError("commit interrupted")):
                    with self.assertRaisesRegex(OSError, "commit interrupted"):
                        run.save()
                self.assertEqual(manifest_path.read_bytes(), before)
                self.assertEqual(json.loads(before)["current_phase"], "collected")
            finally:
                run.sample_stream.close()

    def test_binary_commit_preserves_old_state_and_rejects_over_budget_before_staging(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-binary-commit-") as folder:
            root = Path(folder)
            target = root / "source.tgz"
            target.write_bytes(b"old archive")
            with patch("cloud_experiments.campaign.os.replace", side_effect=OSError("commit interrupted")):
                with self.assertRaisesRegex(OSError, "commit interrupted"):
                    campaign.write_bytes(target, b"new archive")
            self.assertEqual(target.read_bytes(), b"old archive")
            self.assertEqual(list(root.glob(".source.tgz.*.tmp")), [])
            with self.assertRaisesRegex(ValueError, "byte bound"):
                campaign.write_bytes(target, b"too large", max_bytes=3)
            self.assertEqual(target.read_bytes(), b"old archive")

    def test_research_source_bundle_publication_is_atomic(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-research-bundle-") as folder:
            target = Path(folder) / "bundle"
            with patch("cloud_experiments.campaign.os.replace", side_effect=OSError("commit interrupted")):
                with self.assertRaisesRegex(OSError, "commit interrupted"):
                    research.prepare(target)
            self.assertFalse((target / "source.tgz").exists())
            self.assertEqual(list(target.glob(".source.tgz.*.tmp")), [])

    def test_source_collector_rejects_oversize_before_allocating_whole_file(self):
        import tracemalloc
        with tempfile.TemporaryDirectory(prefix="athena-source-cap-") as folder:
            root = Path(folder)
            large = root / "too-large.rs"
            with large.open("wb") as stream:
                stream.truncate(10 * 1024 ** 2)
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(ValueError, "Oversized source"):
                    campaign.source_files(root, ["too-large.rs"])
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 8 * 1024 ** 2 + 256 * 1024,
                            "Admission must bound the read, not read the whole file then reject it")

    def test_source_collector_rejects_missing_declared_input(self):
        with tempfile.TemporaryDirectory(prefix="athena-source-missing-") as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src/lib.rs").write_text("pub fn keep() {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing source input"):
                campaign.source_files(root, ["src", "fixtures"])

    def test_source_snapshot_normalizes_text_line_endings_for_cross_platform_identity(self):
        with tempfile.TemporaryDirectory(prefix="athena-source-eol-") as folder:
            root = Path(folder)
            (root / "crlf.rs").write_bytes(b"fn main() {}\r\n")
            (root / "lf.rs").write_bytes(b"fn main() {}\n")
            snapshots = campaign.source_files(root, ["crlf.rs", "lf.rs"])
            self.assertEqual(snapshots["crlf.rs"], snapshots["lf.rs"])
            self.assertEqual(snapshots["crlf.rs"], b"fn main() {}\n")

    def rows(self):
        return [dict(case="tiny", arm=arm, block=block, iteration=i,
                     order=(0 if arm == "baseline" else 1) ^ (block % 2),
                     ns=ns, fingerprint="a" * 64, phase="confirm")
                for block in range(2) for arm, ns in (("baseline", 100), ("candidate", 80))
                for i in range(2)]

    def test_pairing_rejects_missing_duplicate_wrong_order_or_wrong_output(self):
        rows = self.rows()
        result = campaign.paired_rows(rows, "tiny", "confirm", 2, 2)
        self.assertEqual(result[0], {"baseline": [100, 100], "candidate": [80, 80]})
        wrong = copy.deepcopy(rows)
        wrong[2]["fingerprint"] = "b" * 64
        order = copy.deepcopy(rows)
        order[0]["order"] = 1
        invalid = copy.deepcopy(rows)
        invalid[0]["ns"] = 0
        for data in (rows[:-1], rows + rows[:1], wrong, order, invalid):
            with self.subTest(data=data), self.assertRaises(ValueError):
                campaign.paired_rows(data, "tiny", "confirm", 2, 2)

    def test_confirmation_cannot_reuse_screening_samples(self):
        rows = self.rows()
        rows[0]["phase"] = "screen"
        with self.assertRaises(ValueError):
            campaign.paired_rows(rows, "tiny", "confirm", 2, 2)

    def test_selection_requires_all_cases_and_never_promotes_failed_or_incomplete_arm(self):
        records = {
            "fast_bad": {"correct": False, "ratios": {"a": .1, "b": .1}},
            "partial": {"correct": True, "ratios": {"a": .01}},
            "balanced": {"correct": True, "ratios": {"a": .8, "b": .9}},
            "rival": {"correct": True, "ratios": {"a": .99, "b": .99}},
        }
        self.assertEqual(campaign.select_finalists(records, ["a", "b"], 1), ["balanced"])
        self.assertEqual(campaign.select_finalists({}, ["a"], 2), [])

    def test_snapshot_uses_explicit_sources_and_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src/lib.rs").write_text("pub fn keep() {}\n")
            (root / "secret.json").write_text("not a source input")
            self.assertEqual(set(campaign.source_files(root, ["src"])), {"src/lib.rs"})
            try:
                (root / "src/escape.rs").symlink_to(root / "secret.json")
            except OSError:
                self.skipTest("Host does not permit symlink creation; Linux gate still required")
            with self.assertRaises(ValueError):
                campaign.source_files(root, ["src"])


class DecisionMapControls(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder)
        (root / "src").mkdir()
        for name in ("lib.rs", "main.rs", "core.rs"):
            (root / "src" / name).write_text("", encoding="utf-8")
        (root / "Cargo.toml").write_text('[dependencies]\nbytes = "1"\n', encoding="utf-8")
        spec = {"schema": "athena-core-decisions/1", "decisions": [
            {"id": "storage", "owners": ["src/core.rs"], "dependencies": ["bytes"],
             "question": "Which storage preserves bytes within the memory bound?",
             "alternatives": ["current", "bounded alternative"],
             "oracle": "Literal returned bytes and denied over-limit write",
             "closure": "Exact behavior plus protected full-path measurements",
             "gaps": ["Alternative has not been measured"]},
            {"id": "consumer", "owners": ["src/lib.rs", "src/main.rs"], "dependencies": [],
             "question": "Does the actual consumer preserve the same contract?",
             "alternatives": ["current", "consumer with alternative storage"],
             "oracle": "Literal output through the real consumer",
             "closure": "Affected consumer checks and independent confirmation",
             "gaps": ["Consumer confirmation missing"]}],
            "interactions": [{"id": "storage-consumer", "decisions": ["storage", "consumer"],
                              "mechanism": "Shared bytes and lifetime",
                              "design": "All four valid cells, then full-path confirmation"}]}
        return root, spec

    def test_new_source_and_dependency_cannot_hide_behind_existing_map(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            (root / "src/new.rs").write_text("pub fn additional() {}", encoding="utf-8")
            with (root / "Cargo.toml").open("a", encoding="utf-8") as stream:
                stream.write('url = "2"\n')
            result = campaign.audit_decision_map(root, spec)
            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            self.assertEqual(result["uncovered_sources"], ["src/new.rs"])
            self.assertEqual(result["uncovered_dependencies"], ["url"])

    def test_valid_design_preserves_unknowns_and_never_attests_execution(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            result = campaign.audit_decision_map(root, spec)
            self.assertEqual(result["mapping_status"], "MAPPED")
            self.assertEqual(result["decision_sufficiency"], "NOT_EVALUATED")
            self.assertEqual(result["experiment_execution"], "NOT_EVALUATED")
            self.assertEqual({item["id"] for item in result["unresolved"]}, {"storage", "consumer"})

    def test_invalid_ownership_or_graph_cannot_count_as_design_coverage(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, original = self.fixture(folder)
            invalid = []
            duplicate = copy.deepcopy(original)
            duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
            invalid.append((duplicate, "duplicate_decision"))
            dangling = copy.deepcopy(original)
            dangling["interactions"][0]["decisions"][1] = "unknown"
            invalid.append((dangling, "invalid_interaction"))
            for path in ("src/missing.rs", "../outside.rs"):
                owner = copy.deepcopy(original)
                owner["decisions"][0]["owners"] = [path]
                invalid.append((owner, "invalid_owner"))
            no_oracle = copy.deepcopy(original)
            no_oracle["decisions"][0]["oracle"] = ""
            invalid.append((no_oracle, "invalid_decision"))
            invented = copy.deepcopy(original)
            invented["decisions"][0]["dependencies"] = ["invented"]
            invalid.append((invented, "unknown_dependency"))
            for spec, expected_issue in invalid:
                with self.subTest(expected_issue=expected_issue):
                    result = campaign.audit_decision_map(root, spec)
                    self.assertEqual(result["mapping_status"], "INCOMPLETE")
                    self.assertIn(expected_issue, {issue["code"] for issue in result["issues"]})

    def test_cli_reports_inventory_failure_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            source = root / "decisions.json"
            source.write_text(json.dumps(spec), encoding="utf-8")
            command = [sys.executable, "-B", "-m", "cloud_experiments.campaign", "audit-decisions",
                       str(source), "--root", str(root)]
            result = subprocess.run(command, cwd=campaign.ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision_sufficiency"], "NOT_EVALUATED")
            (root / "src/new.rs").write_text("", encoding="utf-8")
            result = subprocess.run(command, cwd=campaign.ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["uncovered_sources"], ["src/new.rs"])


class AgentBoundaryControls(unittest.TestCase):
    def test_aae_consumer_rejects_interleaved_frames(self):
        valid_meta = {"type": "meta", "version": "aae/1", "source_url": "https://example.org/",
                      "content_hash": "0" * 64, "total_blocks": 1}
        block = {"type": "block", "id": "1" * 64, "role": "paragraph", "text": "fact", "links": [0]}
        link = {"type": "link", "id": 0, "url": "https://example.org/source"}
        end = {"type": "end", "block_count": 1}
        frames = [valid_meta, link, block, end]
        payload = b"\n".join(json.dumps(frame).encode() for frame in frames) + b"\n"
        with self.assertRaisesRegex(agent_lab.LabError, "INVALID_AAE_ORDER"):
            agent_lab.validate_aae(payload, "https://example.org/")

    def test_aae_consumer_rejects_non_contiguous_link_ids(self):
        valid_meta = {"type": "meta", "version": "aae/1", "source_url": "https://example.org/",
                      "content_hash": "0" * 64, "total_blocks": 0}
        link = {"type": "link", "id": 1, "url": "https://example.org/source"}
        end = {"type": "end", "block_count": 0}
        payload = b"\n".join(json.dumps(frame).encode() for frame in (valid_meta, link, end)) + b"\n"
        with self.assertRaisesRegex(agent_lab.LabError, "INVALID_AAE_LINK_ORDER"):
            agent_lab.validate_aae(payload, "https://example.org/")

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(),
                         "POSIX producer process-group boundary")
    def test_native_producer_timeout_reaps_descendant_process_group(self):
        """A producer timeout must not leave a child holding the captured pipe."""
        with tempfile.TemporaryDirectory(prefix="athena-producer-group-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import os,sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]);"
                "time.sleep(30)"
            )
            with self.assertRaisesRegex(agent_lab.LabError, "PRODUCER_TIMEOUT"):
                agent_lab._bounded_process(
                    [sys.executable, "-B", "-c", parent, descendant, str(marker)],
                    timeout_s=1,
                    max_bytes=4096,
                    cwd=root,
                )
            self.assertTrue(marker.exists(), "child must reach the timeout boundary")
            child_status = Path("/proc") / marker.read_text() / "status"
            deadline = time.monotonic() + 3
            while child_status.exists() and time.monotonic() < deadline:
                state = next(
                    (line for line in child_status.read_text().splitlines()
                     if line.startswith("State:")),
                    "",
                )
                if "Z" in state:
                    break
                time.sleep(0.02)
            if child_status.exists():
                state = next(line for line in child_status.read_text().splitlines()
                             if line.startswith("State:"))
                self.assertIn("Z", state, "descendant must not remain runnable")

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(),
                         "POSIX producer process-group boundary")
    def test_native_producer_parent_exit_reaps_descendant_process_group(self):
        """A successful leader exit must also close descendants that own its pipes."""
        with tempfile.TemporaryDirectory(prefix="athena-producer-exit-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import os,sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]);"
                "time.sleep(.3)"
            )
            self.assertEqual(
                agent_lab._bounded_process(
                    [sys.executable, "-B", "-c", parent, descendant, str(marker)],
                    timeout_s=2,
                    max_bytes=4096,
                    cwd=root,
                ),
                b"",
            )
            self.assertTrue(marker.exists(), "child must reach the leader-exit boundary")
            child_status = Path("/proc") / marker.read_text() / "status"
            deadline = time.monotonic() + 3
            while child_status.exists() and time.monotonic() < deadline:
                state = next(
                    (line for line in child_status.read_text().splitlines()
                     if line.startswith("State:")),
                    "",
                )
                if "Z" in state:
                    break
                time.sleep(0.02)
            if child_status.exists():
                state = next(line for line in child_status.read_text().splitlines()
                             if line.startswith("State:"))
                self.assertIn("Z", state, "descendant must not remain runnable")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object boundary")
    def test_native_producer_parent_exit_reaps_descendant_job(self):
        """A Windows producer job must own descendants after leader exit."""
        with tempfile.TemporaryDirectory(prefix="athena-producer-job-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import os,sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]);"
                "time.sleep(.3)"
            )
            self.assertEqual(
                agent_lab._bounded_process(
                    [sys.executable, "-B", "-c", parent, descendant, str(marker)],
                    timeout_s=2,
                    max_bytes=4096,
                    cwd=root,
                ),
                b"",
            )
            self.assertTrue(marker.exists(), "child must reach the leader-exit boundary")
            deadline = time.monotonic() + 3
            child_pid = int(marker.read_text())
            while time.monotonic() < deadline:
                probe = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, check=False,
                )
                if f'"{child_pid}"' not in probe.stdout:
                    break
                time.sleep(0.05)
            else:
                self.fail("descendant must not remain running after producer leader exit")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object boundary")
    def test_native_producer_timeout_reaps_descendant_job(self):
        """A Windows producer timeout must terminate the complete job tree."""
        with tempfile.TemporaryDirectory(prefix="athena-producer-job-timeout-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import os,sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]);"
                "time.sleep(30)"
            )
            with self.assertRaisesRegex(agent_lab.LabError, "PRODUCER_TIMEOUT"):
                agent_lab._bounded_process(
                    [sys.executable, "-B", "-c", parent, descendant, str(marker)],
                    timeout_s=1,
                    max_bytes=4096,
                    cwd=root,
                )
            self.assertTrue(marker.exists(), "child must reach the timeout boundary")
            child_pid = int(marker.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                probe = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, check=False,
                )
                if f'"{child_pid}"' not in probe.stdout:
                    break
                time.sleep(0.05)
            else:
                self.fail("descendant must not remain running after producer timeout")

    def test_provider_boundary_requires_the_unmodified_default_transport(self):
        from datetime import datetime, timezone
        from cloud_experiments.nim import NimClient, NimConfig
        from unittest.mock import patch

        quota = {"utc_date": datetime.now(timezone.utc).date().isoformat(),
                 "remaining_requests": 2, "source": "controlled fixture"}
        openrouter_config = agent_lab.OpenRouterConfig(
            model="synthetic/model:free", api_key="synthetic-private-key", quota=quota,
            allow_live=True, operator_no_charge_acknowledged=True, request_cap=2)
        self.assertEqual(agent_lab.OpenRouterClient(openrouter_config).boundary, "LIVE_OPENROUTER")
        self.assertEqual(agent_lab.OpenRouterClient(
            openrouter_config, transport=agent_lab._PROVIDER_HTTPS_REQUEST).boundary,
            "INJECTED_OPENROUTER_HTTP")
        replaced_openrouter = agent_lab.OpenRouterClient(openrouter_config)
        replaced_openrouter.transport = lambda *args: self.fail("no transport call")
        self.assertEqual(replaced_openrouter.boundary, "INJECTED_OPENROUTER_HTTP")
        with patch.object(agent_lab, "_https_request", side_effect=AssertionError("no transport call")):
            self.assertEqual(agent_lab.OpenRouterClient(openrouter_config).boundary,
                             "INJECTED_OPENROUTER_HTTP")

        with tempfile.TemporaryDirectory(prefix="athena-provider-origin-") as folder:
            nim_config = NimConfig(model="synthetic/model", api_key="synthetic-private-key",
                rate_file=str(Path(folder) / "rate.json"), allow_live=True,
                trial_without_billing_confirmed=True)
            self.assertEqual(NimClient(nim_config).boundary, "LIVE_NVIDIA_NIM")
            self.assertEqual(NimClient(nim_config, transport=agent_lab._PROVIDER_HTTPS_REQUEST).boundary,
                             "INJECTED_NVIDIA_NIM_HTTP")
            replaced_nim = NimClient(nim_config)
            replaced_nim.transport = lambda *args: self.fail("no transport call")
            self.assertEqual(replaced_nim.boundary, "INJECTED_NVIDIA_NIM_HTTP")
            with patch.object(agent_lab, "_https_request", side_effect=AssertionError("no transport call")):
                self.assertEqual(NimClient(nim_config).boundary, "INJECTED_NVIDIA_NIM_HTTP")

    def test_direct_w1_openrouter_path_requires_no_charge_ack_before_provider_io(self):
        from datetime import datetime, timezone
        from unittest.mock import patch

        calls = []

        def transport(method, url, headers, body, timeout_s, max_bytes):
            calls.append((method, url))
            if "/models?" in url:
                payload = {"data": [{"id": "synthetic/model:free", "pricing": {"prompt": "0", "completion": "0"},
                                    "context_length": 8192, "supported_parameters": ["tools"]}]}
            elif url.endswith("/key"):
                payload = {"data": {"is_free_tier": True, "limit_remaining": None}}
            elif url.endswith("/chat/completions"):
                payload = {"model": "synthetic/model:free", "choices": [{"message": {
                    "role": "assistant", "content": '{"status":"abstained","facts":[],"disagreement":false}'}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": "0"}}
            else:
                raise AssertionError(f"Unexpected provider endpoint: {url}")
            return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())

        config = {
            "model": "synthetic/model:free", "api_key": "synthetic-private-key",
            "quota": {"utc_date": datetime.now(timezone.utc).date().isoformat(),
                      "remaining_requests": 1, "source": "controlled fixture"},
            "allow_live": False, "request_cap": 1, "min_interval_s": 3.1,
        }
        with patch.object(agent_lab, "_https_request", side_effect=transport) as direct_client_transport:
            with self.assertRaisesRegex(agent_lab.LabError, "LIVE_MODEL_CALLS_DISABLED"):
                agent_lab.OpenRouterClient(config, transport=agent_lab._https_request)
        direct_client_transport.assert_not_called()

        with patch.object(agent_lab, "_https_request", side_effect=transport) as injected:
            config["transport"] = agent_lab._https_request
            result = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=config)

        self.assertEqual(result["status"], "LIVE_MODEL_CALLS_DISABLED")
        injected.assert_not_called()
        self.assertEqual(calls, [], "W1 must reject before catalog, key, or completion requests")
        self.assertFalse(result["actual_model"])

        config.pop("transport")
        config["allow_live"] = True
        with patch.object(agent_lab, "_https_request", side_effect=transport) as unacknowledged:
            result = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=config)
        self.assertEqual(result["status"], "LIVE_NO_CHARGE_ACK_REQUIRED")
        unacknowledged.assert_not_called()
        self.assertEqual(calls, [])

        config["operator_no_charge_acknowledged"] = True
        with patch.object(agent_lab, "_https_request", side_effect=transport):
            acknowledged = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=config)
        self.assertEqual(acknowledged["status"], "EXECUTED_MODEL")
        self.assertFalse(acknowledged["actual_model"])
        self.assertEqual(acknowledged["model_boundary"], "INJECTED_OPENROUTER_HTTP")
        self.assertIn("Injected transport origin is unverified", acknowledged["boundary_gaps"])
        self.assertEqual([url.rsplit("/", 1)[-1].split("?", 1)[0] for _, url in calls],
                         ["models", "key", "completions"])

    def test_nim_injected_transport_cannot_bypass_live_and_no_billing_gates(self):
        from cloud_experiments.nim import NimClient, NimConfig
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="athena-nim-live-gate-") as folder:
            transport = lambda *args: self.fail("provider transport must not be called")
            for index, (allow_live, confirmed, error) in enumerate((
                    (False, False, "LIVE_MODEL_CALLS_DISABLED"),
                    (True, False, "LIVE_NO_BILLING_ACK_REQUIRED"))):
                with self.subTest(allow_live=allow_live, confirmed=confirmed):
                    config = NimConfig(model="synthetic/model", api_key="synthetic-private-key",
                        rate_file=str(Path(folder) / f"rate-{index}.json"), allow_live=allow_live,
                        trial_without_billing_confirmed=confirmed)
                    wrapped_transport = lambda *args: agent_lab._https_request(*args)
                    for injected_transport in (agent_lab._https_request, wrapped_transport):
                        with patch.object(agent_lab, "_https_request", side_effect=transport) as injected:
                            with self.assertRaisesRegex(agent_lab.LabError, error):
                                NimClient(config, transport=injected_transport)
                        injected.assert_not_called()

    def test_callable_wrappers_and_prebuilt_clients_cannot_bypass_w1_acknowledgements(self):
        from datetime import datetime, timezone
        from cloud_experiments.nim import NimClient, NimConfig

        calls = []

        def network_capable(*args):
            calls.append(args)
            return agent_lab.HttpReply(200, {}, b'{"data":[]}')

        quota = {"utc_date": datetime.now(timezone.utc).date().isoformat(),
                 "remaining_requests": 10, "source": "controlled fixture"}
        openrouter_config = agent_lab.OpenRouterConfig(
            model="synthetic/model:free", api_key="synthetic-private-key", quota=quota,
            allow_live=False, operator_no_charge_acknowledged=False, request_cap=10)
        wrapped_transport = lambda *args: network_capable(*args)
        for transport in (network_capable, wrapped_transport):
            with self.subTest(provider="openrouter", wrapped=transport is wrapped_transport):
                with self.assertRaisesRegex(agent_lab.LabError, "LIVE_MODEL_CALLS_DISABLED"):
                    agent_lab.OpenRouterClient(openrouter_config, transport=transport)
        self.assertEqual(calls, [], "A callback wrapper must not bypass the live-call gate")

        authorized_openrouter = agent_lab.OpenRouterConfig(
            model="synthetic/model:free", api_key="synthetic-private-key", quota=quota,
            allow_live=True, operator_no_charge_acknowledged=True, request_cap=10)
        openrouter = agent_lab.OpenRouterClient(authorized_openrouter, transport=wrapped_transport)
        authorized_openrouter.operator_no_charge_acknowledged = False
        rejected = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=openrouter)
        self.assertEqual(rejected["status"], "LIVE_NO_CHARGE_ACK_REQUIRED")
        self.assertFalse(rejected["actual_model"])
        with self.assertRaisesRegex(agent_lab.LabError, "LIVE_NO_CHARGE_ACK_REQUIRED"):
            openrouter.preflight()
        self.assertEqual(calls, [], "W1 must recheck the OpenRouter acknowledgement before I/O")

        with tempfile.TemporaryDirectory(prefix="athena-prebuilt-w1-gate-") as folder:
            nim_config = NimConfig(model="vendor/model", api_key="synthetic-private-key",
                rate_file=str(Path(folder) / "rate.json"), allow_live=True,
                trial_without_billing_confirmed=True)
            nim = NimClient(nim_config, transport=wrapped_transport)
            nim_config.allow_live = False
            result = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=nim)
            self.assertEqual(result["status"], "LIVE_MODEL_CALLS_DISABLED")
            self.assertFalse(result["actual_model"])
            with self.assertRaisesRegex(agent_lab.LabError, "LIVE_MODEL_CALLS_DISABLED"):
                nim._call("catalog", "GET", "models")
        self.assertEqual(calls, [], "W1 must recheck mutable prebuilt-client acknowledgement before I/O")

    def test_malformed_model_tool_name_is_a_recorded_task_failure(self):
        from cloud_experiments.nim import NimClient, NimConfig
        with tempfile.TemporaryDirectory(prefix="athena-tool-name-") as folder:
            for index, name in enumerate(([], {"unexpected": "object"})):
                now = [1000.0]
                def transport(method, url, headers, body, timeout_s, max_bytes):
                    payload = {"data": [{"id": "vendor/model"}]} if url.endswith("/models") else {
                        "model": "vendor/model", "choices": [{"message": {"role": "assistant", "tool_calls": [
                            {"id": "invalid", "type": "function", "function": {"name": name, "arguments": "{}"}}]}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                    return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())
                client = NimClient(NimConfig(model="vendor/model", api_key="synthetic-private-key",
                    rate_file=str(Path(folder) / f"rate-{index}.json"), allow_live=True,
                    trial_without_billing_confirmed=True), transport=transport,
                    clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                result = agent_lab.run_task(agent_lab.tasks()[0], "W1", modelconfig=client)
                self.assertEqual(result["status"], "INVALID_OR_DUPLICATE_TOOL_CALL")
                self.assertFalse(result["quality"]["contract_satisfied"])
                self.assertEqual(result["actions"], [])
                self.assertEqual(result["usage"]["requests"], 1)
                self.assertEqual(result["ledger"]["retained_state_after_close_bytes"], 0)

    def test_nim_admission_denial_does_not_count_an_unsent_inference(self):
        from cloud_experiments.nim import NimClient, NimConfig
        with tempfile.TemporaryDirectory(prefix="athena-nim-admission-") as folder:
            dispatched = []
            def transport(method, url, headers, body, timeout_s, max_bytes):
                dispatched.append(url)
                return agent_lab.HttpReply(200, {}, b'{"data":[{"id":"vendor/model"}]}')
            client = NimClient(NimConfig(model="vendor/model", api_key="synthetic-private-key", timeout_s=1,
                rate_file=str(Path(folder) / "rate.json"), allow_live=True, trial_without_billing_confirmed=True),
                transport=transport, clock=lambda: 1000.0)
            with self.assertRaisesRegex(agent_lab.LabError, "RATE_ADMISSION_EXCEEDS_DEADLINE"):
                client.complete([], [], max_tokens=16, max_context_bytes=8192)
            self.assertEqual(client.budget_state()["requests_used"], 0)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(client.budget_state()["transport_attempts"], {"catalog": 1, "chat": 0})
            self.assertEqual(client.budget_state()["admission_failures"], 1)

    def test_task_routing_preserves_facts_and_rejects_wrong_units_or_citations(self):
        cards = {task["id"]: task for task in agent_lab.tasks()}
        for policy in ("W0", "W2"):
            for representation in ("full", "compact"):
                result = agent_lab.run_task(cards["F01-screen"], policy, representation)
                self.assertTrue(result["quality"]["contract_satisfied"])
                self.assertCountEqual([(f["key"], f["value"], f["unit"]) for f in result["answer"]["facts"]],
                                 [("capacity", "8", "L"), ("mass", "12.5", "kg")])
                self.assertNotIn("read", [r["action"] for r in result["actions"]])
                self.assertEqual(result["ledger"]["retained_state_after_close_bytes"], 0)
                for field, value in (("unit", "g"), ("revision", "wrong-revision"), ("value", "999")):
                    answer = copy.deepcopy(result["answer"])
                    answer["facts"][0][field] = value
                    scored = agent_lab.score_answer(cards["F01-screen"], answer, result["actions"],
                                                    result["consumer_packets"], result["visited"])
                    self.assertFalse(scored["contract_satisfied"])
        for identity, required, forbidden in (("F02-screen", "read", None), ("F03-screen", "read", "search")):
            result = agent_lab.run_task(cards[identity])
            self.assertTrue(result["quality"]["contract_satisfied"])
            actions = [r["action"] for r in result["actions"]]
            self.assertIn(required, actions)
            self.assertNotIn(forbidden, actions)
        table = agent_lab.run_task(cards["F04-screen"])
        self.assertTrue(table["quality"]["fact_contract_satisfied"])
        self.assertFalse(table["quality"]["contract_satisfied"])
        self.assertEqual(table["quality"]["table_contract_status"], "NOT_IMPLEMENTED")

    def test_compact_expansion_enforces_scope_revision_expiry_and_releases_state(self):
        clock = [10.0]
        transport = agent_lab.EvidenceTransport("a", "session-a", "snapshot-1", clock=lambda: clock[0], ttl_s=2)
        packet = {"source_id": "s", "revision": "r1", "passages": [], "facts": []}
        compact = json.loads(transport.packet(packet, "compact"))
        args = dict(tenant="a", session="session-a", snapshot_revision="snapshot-1", revision="r1")
        self.assertEqual(json.loads(transport.expand(compact["handle"], **args))["source_id"], "s")
        for key, value in (("tenant", "b"), ("session", "foreign"), ("snapshot_revision", "old"), ("revision", "r2")):
            with self.subTest(key=key), self.assertRaises(agent_lab.LabError):
                transport.expand(compact["handle"], **{**args, key: value})
        clock[0] = 12.0
        with self.assertRaisesRegex(agent_lab.LabError, "HANDLE_EXPIRED"):
            transport.expand(compact["handle"], **args)
        transport.close()
        self.assertEqual(transport.retained_bytes, 0)
        with self.assertRaises(agent_lab.LabError):
            transport.packet(packet, "compact")

    def test_provider_paid_catalog_and_rate_limit_fail_closed_without_secret_output(self):
        from datetime import datetime, timezone
        cfg = agent_lab.OpenRouterConfig(model="synthetic/model:free", api_key="synthetic-private-key",
            quota={"utc_date": datetime.now(timezone.utc).date().isoformat(), "remaining_requests": 2, "source": "controlled fixture"},
            allow_live=True, operator_no_charge_acknowledged=True, request_cap=2)
        def transport(price):
            def respond(method, url, headers, body, timeout_s, max_bytes):
                if "/models?" in url:
                    value = {"data": [{"id": cfg.model, "pricing": {"prompt": price, "completion": "0"},
                                       "context_length": 8192, "supported_parameters": ["tools"]}]}
                    status = 200
                elif url.endswith("/key"):
                    value, status = {"data": {"is_free_tier": True, "limit_remaining": None}}, 200
                else:
                    value, status = {"error": {"code": 429, "message": cfg.api_key}}, 429
                return agent_lab.HttpReply(status, {}, json.dumps(value).encode())
            return respond
        paid = agent_lab.OpenRouterClient(cfg, transport=transport("0.001"))
        with self.assertRaisesRegex(agent_lab.LabError, "NONZERO_OR_UNKNOWN_MODEL_PRICE"):
            paid.preflight()
        client = agent_lab.OpenRouterClient(cfg, transport=transport("0"))
        with self.assertRaisesRegex(agent_lab.LabError, "PROVIDER_HTTP_429"):
            client.complete([], [], max_tokens=16, max_context_bytes=8192)
        self.assertEqual(client.budget_state()["blocked"], "PROVIDER_HTTP_429")
        self.assertEqual(client.budget_state()["requests_used"], 1)
        self.assertNotIn(cfg.api_key, json.dumps(client.events))
        with self.assertRaisesRegex(agent_lab.LabError, "PROVIDER_HTTP_429"):
            client.complete([], [], max_tokens=16, max_context_bytes=8192)
        self.assertEqual(client.budget_state()["requests_used"], 1)

    def test_nim_shared_rate_gate_and_explicit_model_usage_contract(self):
        from cloud_experiments.nim import NimClient, NimConfig
        with tempfile.TemporaryDirectory(prefix="athena-nim-rate-") as folder:
            root = Path(folder)
            rate_file = root / "shared-rate.json"
            now = [1000.0]
            starts = []
            credential = "synthetic-nvidia-secret"

            def sleep(seconds):
                now[0] += seconds

            def transport(method, url, headers, body, timeout_s, max_bytes):
                self.assertEqual(headers["Authorization"], "Bearer " + credential)
                starts.append(now[0])
                if url.endswith("/models"):
                    payload = {"data": [{"id": "vendor/model"}]}
                else:
                    payload = {"model": "vendor/model", "choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
                return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())

            cfg = NimConfig(model="vendor/model", api_key=credential, rate_file=str(rate_file), request_cap=50,
                            allow_live=True, trial_without_billing_confirmed=True)
            self.assertNotIn(credential, repr(cfg))
            first = NimClient(cfg, transport=transport, clock=lambda: now[0], sleep=sleep)
            second = NimClient(cfg, transport=transport, clock=lambda: now[0], sleep=sleep)
            admission = first.preflight()
            self.assertEqual(admission["pricing"], "NOT_VERIFIED_BY_PROVIDER")
            self.assertTrue(admission["operator_no_charge_acknowledged"])
            second._call("catalog", "GET", "models")
            self.assertEqual(len(starts), 2)
            self.assertGreaterEqual(starts[-1] - starts[0], 2.05 - 1e-9)
            now[0] = 2000.0
            attempt_times = [1940.55 + 2.05 * index for index in range(30)]
            campaign.write_json(rate_file, {"schema": "athena-nim-rate/1",
                "key_sha256": hashlib.sha256(credential.encode()).hexdigest(), "attempts": attempt_times})
            quota_client = NimClient(cfg, transport=transport, clock=lambda: now[0], sleep=sleep)
            quota_client._call("catalog", "GET", "models", timeout_s=60)
            self.assertGreaterEqual(starts[-1] - 2000.0, 2.05 - 1e-9)
            data, _ = first.complete([], [], max_tokens=16, max_context_bytes=8192)
            self.assertEqual(data["model"], "vendor/model")
            self.assertEqual(first.budget_state()["total_reported_tokens"], 8)
            self.assertEqual(first.events[-1]["actual_cost_usd"], "UNKNOWN_UNLESS_REPORTED")
            self.assertGreaterEqual(starts[-1] - starts[-2], 2.05 - 1e-9)
            self.assertNotIn(credential, json.dumps(first.events + second.events))

            malformed_path = root / "malformed-state.json"
            malformed_path.write_text(json.dumps({"schema": "athena-nim-rate/1",
                "key_sha256": hashlib.sha256(credential.encode()).hexdigest(), "attempts": [float("nan")]}))
            malformed = NimClient(NimConfig(model="vendor/model", api_key=credential,
                rate_file=str(malformed_path), allow_live=True, trial_without_billing_confirmed=True),
                transport=transport, clock=lambda: now[0], sleep=sleep)
            before = len(starts)
            with self.assertRaisesRegex(agent_lab.LabError, "NIM_RATE_CLOCK_OR_STATE_CHANGED"):
                malformed._call("catalog", "GET", "models")
            self.assertEqual(len(starts), before, "Corrupt quota state must fail before provider I/O")

    def test_nim_429_persists_cooldown_and_rejects_bad_model_or_nonzero_cost(self):
        from cloud_experiments.nim import NimClient, NimConfig
        with tempfile.TemporaryDirectory(prefix="athena-nim-provider-") as folder:
            root = Path(folder)
            now = [2000.0]
            starts = []
            credential = "synthetic-nvidia-secret"
            replies = [agent_lab.HttpReply(429, {"Retry-After": "5"},
                                           json.dumps({"error": {"message": credential}}).encode()),
                       agent_lab.HttpReply(200, {}, b'{"data":[]}')]

            def sleep(seconds):
                now[0] += seconds

            def transport(method, url, headers, body, timeout_s, max_bytes):
                starts.append(now[0])
                return replies.pop(0)

            rate_file = root / "cooldown.json"
            cfg = NimConfig(model="vendor/model", api_key=credential, rate_file=str(rate_file),
                            allow_live=True, trial_without_billing_confirmed=True)
            first = NimClient(cfg, transport=transport, clock=lambda: now[0], sleep=sleep)
            with self.assertRaisesRegex(agent_lab.LabError, "PROVIDER_HTTP_429"):
                first._call("catalog", "GET", "models")
            self.assertEqual(first.blocked, "PROVIDER_HTTP_429")
            self.assertEqual(len(starts), 1, "A rate-limit response must not be retried")
            second = NimClient(cfg, transport=transport, clock=lambda: now[0], sleep=sleep)
            second._call("catalog", "GET", "models")
            self.assertGreaterEqual(starts[1] - starts[0], 5.0)
            self.assertNotIn(credential, json.dumps(first.events + second.events))

            for bad_response, error_code in (
                ({"model": "vendor/other", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                 "MODEL_IDENTITY_CHANGED"),
                ({"model": "vendor/model", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": "0.001"}},
                 "UNEXPECTED_OR_INVALID_COST"),
            ):
                local_now = [3000.0]
                def response_transport(method, url, headers, body, timeout_s, max_bytes):
                    if url.endswith("/models"):
                        value = {"data": [{"id": "vendor/model"}]}
                    else:
                        value = bad_response
                    return agent_lab.HttpReply(200, {}, json.dumps(value).encode())
                isolated = NimClient(NimConfig(model="vendor/model", api_key=credential,
                    rate_file=str(root / (error_code + ".json")), allow_live=True,
                    trial_without_billing_confirmed=True), transport=response_transport,
                    clock=lambda: local_now[0], sleep=lambda delay: local_now.__setitem__(0, local_now[0] + delay))
                with self.assertRaisesRegex(agent_lab.LabError, error_code):
                    isolated.complete([], [], max_tokens=8, max_context_bytes=4096)


class HttpDeadlineControls(unittest.TestCase):
    def test_expired_spawn_budget_cannot_send_credentials_or_wait_for_a_new_http_budget(self):
        from unittest.mock import patch
        from cloud_experiments import http_transport
        now, sent = [0.0], []
        state = {"killed": False, "reaped": False}
        class Process:
            returncode = 0
            args = []
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.wait()
            def communicate(self, input=None, timeout=None):
                if input is not None:
                    sent.append(input)
                return b'{"status":200,"headers":{},"body":"b2s="}', b""
            def kill(self):
                state["killed"] = True
                self.returncode = -9
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                state["reaped"] = True
                return self.returncode
        def spawn(*args, **kwargs):
            now[0] = 2.0
            return Process()
        with patch.object(http_transport.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(subprocess, "Popen", side_effect=spawn):
            with self.assertRaisesRegex(http_transport.TransportError, "HTTP_DEADLINE"):
                http_transport.bounded_request("GET", "https://integrate.api.nvidia.com/v1/models",
                    {"Authorization": "Bearer synthetic-key"}, None, 1.5, 1024)
        self.assertEqual(sent, [], "Expired creation must not transmit the credential-bearing stdin packet")
        self.assertEqual(state, {"killed": True, "reaped": True})

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux parent-death worker boundary")
    def test_killed_parent_cannot_leave_an_active_credential_worker(self):
        import os
        import signal
        from cloud_experiments import http_transport
        with tempfile.TemporaryDirectory(prefix="athena-http-parent-") as folder:
            root = Path(folder)
            ready = root / "worker.pid"
            load = ("import importlib.util,sys;"
                    f"spec=importlib.util.spec_from_file_location('owned_http',{http_transport.__file__!r});"
                    "h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h);")
            child = load + ("import os,time;from pathlib import Path;"
                f"h._exchange=lambda payload: (Path({str(ready)!r}).write_text(str(os.getpid())),time.sleep(20))[1];"
                "sys.exit(h._worker_main())")
            parent_code = load + (f"h._WORKER_COMMAND=(sys.executable,'-B','-c',{child!r});"
                "h.bounded_request('GET','https://integrate.api.nvidia.com/v1/models',{},None,20,1024)")
            parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            pid = None
            def active():
                path = Path("/proc") / str(pid) / "status"
                try:
                    return "Z" not in next(line for line in path.read_text().splitlines() if line.startswith("State:"))
                except FileNotFoundError:
                    return False
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), "The real worker must enter the controlled network boundary")
                pid = int(ready.read_text())
                parent.kill()
                parent.communicate(timeout=3)
                deadline = time.monotonic() + 2
                while active() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(active(), "A dead request owner must not leave an active HTTP worker")
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=3)
                if pid and active():
                    command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                    if str(ready).encode() in command:
                        os.kill(pid, signal.SIGKILL)

    def test_absolute_deadline_cancels_header_body_and_preconnect_stalls_and_reaps_worker(self):
        import os
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch
        from cloud_experiments import http_transport

        received = []
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                received.append(self.path)
                try:
                    headers = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"
                    if self.path == "/headers":
                        for byte in headers:
                            self.connection.sendall(bytes([byte]))
                            time.sleep(.08)
                    else:
                        self.connection.sendall(headers)
                    for _ in range(100):
                        self.connection.sendall(b"x")
                        if self.path == "/body":
                            time.sleep(.04)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        server.daemon_threads = False
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        serving.start()
        readiness = tempfile.TemporaryDirectory(prefix="athena-http-ready-")
        try:
            for mode in ("headers", "body", "preconnect", "normal"):
                with self.subTest(mode=mode):
                    origin = f"http://127.0.0.1:{server.server_port}/{mode}"
                    ready = Path(readiness.name) / mode
                    script = ("import json,sys,time;from cloud_experiments import http_transport as h;"
                              f"origin={origin!r};mode={mode!r};original=h._exchange;"
                              "h._allowed_endpoint=lambda method,url: method=='GET' and url==origin;"
                              "h._exchange=lambda payload: (time.sleep(8) if mode=='preconnect' else None) or original(dict(payload,url=origin));"
                              + (f"from pathlib import Path;Path({str(ready)!r}).write_text('ready');"
                                 if not sys.platform.startswith("linux") else "")
                              + "sys.exit(h._worker_main())")
                    children = []
                    actual_popen = subprocess.Popen
                    prestarted = None
                    if not sys.platform.startswith("linux"):
                        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "LANG") if key in os.environ}
                        prestarted = actual_popen([sys.executable, "-B", "-c", script, str(os.getpid()), "60"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
                    def capture_child(*args, **kwargs):
                        child = prestarted if prestarted is not None else actual_popen(*args, **kwargs)
                        children.append(child)
                        return child
                    try:
                        if prestarted is not None:
                            deadline = time.monotonic() + 10
                            while not ready.exists() and prestarted.poll() is None and time.monotonic() < deadline:
                                time.sleep(.02)
                            self.assertTrue(ready.exists(), "The controlled worker must be ready before testing its HTTP phase")
                        with patch.object(http_transport, "_WORKER_COMMAND", (sys.executable, "-B", "-c", script)), \
                                patch.object(subprocess, "Popen", side_effect=capture_child):
                            started = time.monotonic()
                            if mode == "normal":
                                reply = agent_lab._https_request("GET", "https://integrate.api.nvidia.com/v1/models",
                                    {"Authorization": "Bearer synthetic-key"}, None, 3, 1024)
                                self.assertEqual((reply.status, reply.body), (200, b"x" * 100))
                            else:
                                with self.assertRaisesRegex(agent_lab.LabError, "PROVIDER_TIMEOUT"):
                                    agent_lab._https_request("GET", "https://integrate.api.nvidia.com/v1/models",
                                        {"Authorization": "Bearer synthetic-key"}, None, 1.5, 1024)
                                self.assertLess(time.monotonic() - started, 2.5)
                    finally:
                        if prestarted is not None:
                            if prestarted.poll() is None:
                                prestarted.kill()
                            prestarted.communicate(timeout=3)
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll(), "Timeout must kill and wait for the real worker")
            self.assertCountEqual(received, ["/headers", "/body", "/normal"])
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
            readiness.cleanup()
            self.assertFalse(serving.is_alive())


class GcpSecretRuntimeControls(unittest.TestCase):
    def test_nim_trial_resolves_explicit_secret_resource_without_api_key_environment(self):
        import base64
        import os
        from unittest.mock import patch
        from cloud_experiments import gcp_secrets, nim

        resource = "projects/970441998440/secrets/athena-nvidia-nim-api-key/versions/latest"
        credential = "synthetic-nvidia-runtime-key"
        access_token = "synthetic-gcp-access-token"
        requests = []

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, limit):
                body, self.body = self.body[:limit], self.body[limit:]
                return body

        class Opener:
            def open(self, request, timeout):
                requests.append((request.full_url,
                                 {key.lower(): value for key, value in request.header_items()}, timeout))
                if request.full_url == "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token":
                    return Response(json.dumps({"access_token": access_token}).encode())
                if request.full_url == f"https://secretmanager.googleapis.com/v1/{resource}:access":
                    return Response(json.dumps({"payload": {"data": base64.b64encode(credential.encode()).decode()}}).encode())
                raise AssertionError("Secret retrieval used an unexpected host or path")

        task = agent_lab.tasks()[0]
        with tempfile.TemporaryDirectory(prefix="athena-secret-runtime-") as folder:
            root = Path(folder)
            binary = root / "core_probe"
            binary.write_bytes(b"synthetic probe identity")
            captured = {}
            rate_files = []

            def run_task(card, policy, representation, client, *, producer):
                captured["credential"] = client.config.api_key
                rate_files.append(client.rate_file)
                return {"quality": {"contract_satisfied": True}}

            with patch.dict(os.environ, {"NVIDIA_API_KEY": "",
                                         "NVIDIA_API_KEY_SECRET_RESOURCE": resource}):
                accessor = gcp_secrets.access_secret_resource
                with patch.object(gcp_secrets, "access_secret_resource", side_effect=lambda value: accessor(value, opener=Opener())):
                    with patch.object(agent_lab, "tasks", return_value=[task]):
                        with patch.object(agent_lab, "run_task", side_effect=run_task):
                            from cloud_experiments import nim
                            with patch.object(nim, "_RATE_ROOT", root / "private-rate", create=True):
                                for parent in (root / "first", root / "second"):
                                    report = research.execute_agent(
                                        binary, parent / "trial", policy="W1", model="vendor/model",
                                        request_cap=1, task_ids=[task["id"]], provider="nim",
                                        confirm_provider_no_charge=True)
            persisted = (root / "second" / "trial" / "agent-trials.json").read_text(encoding="utf-8")

        self.assertEqual(report["task_successes"], 1)
        self.assertEqual(captured["credential"], credential)
        self.assertEqual(len(set(rate_files)), 1, "Output directories must not create independent credential quotas")
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[0][0], "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token")
        self.assertEqual(requests[0][1].get("metadata-flavor"), "Google")
        self.assertEqual(requests[1][1].get("authorization"), "Bearer " + access_token)
        self.assertNotIn(credential, persisted)
        self.assertNotIn(access_token, persisted)

    def test_secret_manager_denial_persists_redacted_failure_without_model_call(self):
        import os
        from unittest.mock import patch
        from cloud_experiments import gcp_secrets

        resource = "projects/970441998440/secrets/athena-nvidia-nim-api-key/versions/latest"
        access_token = "synthetic-gcp-denial-token"
        marker = "synthetic-provider-error-must-not-persist"

        class Response:
            def __init__(self, body, status=200):
                self.body = body
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, limit):
                body, self.body = self.body[:limit], self.body[limit:]
                return body

        class Opener:
            def open(self, request, timeout):
                if request.full_url.startswith("http://169.254.169.254/"):
                    return Response(json.dumps({"access_token": access_token}).encode())
                return Response(marker.encode(), status=403)

        task = agent_lab.tasks()[0]
        with tempfile.TemporaryDirectory(prefix="athena-secret-denied-") as folder:
            root = Path(folder)
            binary = root / "core_probe"
            binary.write_bytes(b"synthetic probe identity")
            output = root / "trial"
            with patch.dict(os.environ, {"NVIDIA_API_KEY": "",
                                         "NVIDIA_API_KEY_SECRET_RESOURCE": resource}):
                accessor = gcp_secrets.access_secret_resource
                with patch.object(gcp_secrets, "access_secret_resource", side_effect=lambda value: accessor(value, opener=Opener())):
                    with patch.object(agent_lab, "tasks", return_value=[task]):
                        with patch.object(agent_lab, "run_task") as run_task:
                            with self.assertRaisesRegex(gcp_secrets.SecretAccessError, "SECRET_ACCESS_FAILED"):
                                research.execute_agent(
                                    binary, output, policy="W1", model="vendor/model",
                                    request_cap=1, task_ids=[task["id"]], provider="nim",
                                    confirm_provider_no_charge=True)
            report_path = output / "agent-trials.json"
            self.assertTrue(report_path.is_file(), "Secret denial must leave a recoverable redacted failure record")
            persisted = report_path.read_text(encoding="utf-8")
            record = json.loads(persisted)
            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["error_type"], "SecretAccessError")
            self.assertNotIn(access_token, persisted)
            self.assertNotIn(marker, persisted)
            run_task.assert_not_called()

    def test_live_provider_requires_explicit_no_charge_confirmation_before_secret_access(self):
        import os
        from unittest.mock import patch
        from cloud_experiments import gcp_secrets, nim

        resource = "projects/970441998440/secrets/athena-nvidia-nim-api-key/versions/latest"
        task = agent_lab.tasks()[0]
        with tempfile.TemporaryDirectory(prefix="athena-live-gate-") as folder:
            root = Path(folder)
            binary = root / "core_probe"
            binary.write_bytes(b"synthetic probe identity")
            output = root / "trial"
            with patch.dict(os.environ, {"NVIDIA_API_KEY": "",
                                         "NVIDIA_API_KEY_SECRET_RESOURCE": resource}):
                with patch.object(gcp_secrets, "access_secret_resource", return_value="synthetic-key") as access_secret:
                    with patch.object(nim, "credential_rate_file", return_value=root / "rate.json"):
                        with patch.object(agent_lab, "tasks", return_value=[task]):
                            with patch.object(agent_lab, "run_task", return_value={"quality": {"contract_satisfied": True}}) as run_task:
                                with self.assertRaisesRegex(RuntimeError, "no-charge confirmation"):
                                    research.execute_agent(
                                        binary, output, policy="W1", model="vendor/model",
                                        request_cap=1, task_ids=[task["id"]], provider="nim")

            record = json.loads((output / "agent-trials.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["error_type"], "RuntimeError")
            access_secret.assert_not_called()
            run_task.assert_not_called()

    def test_secret_resource_rejects_url_injection_before_network(self):
        from cloud_experiments import gcp_secrets

        calls = []

        class Opener:
            def open(self, request, timeout):
                calls.append(request.full_url)
                raise AssertionError("Invalid resource reached the network boundary")

        with self.assertRaisesRegex(gcp_secrets.SecretAccessError, "INVALID_SECRET_RESOURCE"):
            gcp_secrets.access_secret_resource(
                "projects/970441998440/secrets/athena-nvidia-nim-api-key/versions/latest?redirect=https://attacker.invalid",
                opener=Opener())
        self.assertEqual(calls, [])


class ExpandedResearchControls(unittest.TestCase):
    def test_declared_scope_cannot_omit_language_interactions(self):
        registry = json.loads((research.ROOT / "cloud_experiments/core_decisions.json").read_text(encoding="utf-8"))
        design = research.manifest_design()
        self.assertEqual(set(design["required_interactions"]), {item["id"] for item in registry["interactions"]})
        expected = {f"L{i:02}" for i in range(1, 7)}
        panels = {item["id"]: item for item in registry["language_panels"]}
        self.assertEqual(set(panels), expected)
        decisions = {item["id"] for item in registry["decisions"]}
        interactions = {item["id"] for item in registry["interactions"]}
        for panel in panels.values():
            self.assertTrue(set(panel["owner_decisions"]) <= decisions)
            self.assertTrue(set(panel["interactions"]) <= interactions)
        missing_panel = json.loads(json.dumps(registry))
        missing_panel["language_panels"] = [item for item in missing_panel["language_panels"] if item["id"] != "L06"]
        audit = campaign.audit_decision_map(research.ROOT, missing_panel)
        self.assertEqual(audit["mapping_status"], "INCOMPLETE")
        self.assertIn({"code": "missing_language_panel", "id": "L06"}, audit["issues"])
        dangling_panel = json.loads(json.dumps(registry))
        dangling_panel["language_panels"][0]["owner_decisions"] = ["H99"]
        audit = campaign.audit_decision_map(research.ROOT, dangling_panel)
        self.assertIn({"code": "invalid_language_panel_owner", "id": "L01"}, audit["issues"])
        malformed_status = json.loads(json.dumps(registry))
        malformed_status["language_panels"][0]["status"] = []
        audit = campaign.audit_decision_map(research.ROOT, malformed_status)
        self.assertIn({"code": "invalid_language_panel", "id": "L01"}, audit["issues"])

    def test_boolean_records_cannot_attest_launch_readiness(self):
        design = research.manifest_design()
        required = (design["required_decisions"] + design["required_interactions"]
                    + [panel["id"] for panel in design["language_panels"]])
        report = research.readiness(design, {name: {"runnable_verified": True} for name in required})
        self.assertNotEqual(report["status"], "READY_WITHIN_DECLARED_SCOPE")
        self.assertEqual(report["decision_sufficiency"], "NOT_EVALUATED")
        self.assertEqual(report["blocking_language_panels"], ["L01", "L02", "L03"])
        self.assertEqual(report["conditional_language_non_admissions"], ["L04", "L05", "L06"])
        self.assertTrue(set(report["blocking_language_panels"]) <= set(report["missing"]))

    def test_cross_owner_design_covers_writer_query_interactions(self):
        import itertools
        from cloud_experiments import contenders
        from cloud_experiments import experimental_design
        variants, _ = research.native_matrix()
        factors = [contenders.metadata(name)["factors"] for name in variants]
        for writer in ("frame", "direct", "buffered", "reuse"):
            for lookup in ("hash", "binary", "linear"):
                with self.subTest(writer=writer, lookup=lookup):
                    self.assertTrue(any(cell["writer"] == writer and cell["lookup"] == lookup for cell in factors),
                                    "No declared cell covers the writer/query interaction")
        design = experimental_design.native_design()
        cross = [contenders.metadata(name)["factors"] for name in design["cross_cells"]]
        baseline = contenders.metadata("baseline")["factors"]
        levels = {axis: {value} for axis, value in baseline.items()
                  if axis not in ("prepared", "table", "html_extractor", "html_headers")}
        for definition in contenders.definitions().values():
            for axis, value in definition["factors"].items():
                if axis in levels:
                    levels[axis].add(value)
        self.assertEqual(set(design["factor_levels"]), set(levels))
        required = list(itertools.combinations(levels, 2)) + [
            ("writer", "hash", "reserve"), ("lookup", "selection", "projection"),
            ("html_library", "projection", "links"), ("json_library", "hash", "cpu"),
            ("lto", "codegen_units", "cpu"), ("writer", "hash", "projection"),
            ("html_library", "lookup", "selection"), ("json_library", "html_library", "digest_library"),
            ("writer", "hash", "lookup", "projection")]
        for axes in required:
            for values in itertools.product(*(sorted(levels[axis], key=str) for axis in axes)):
                expected = dict(zip(axes, values))
                if expected.get("json_library") == "sonic" and expected.get("writer", "frame") != "frame":
                    continue
                self.assertTrue(any(all(cell[axis] == value for axis, value in expected.items()) for cell in cross),
                                "Missing feasible declared tuple: " + str(expected))
        self.assertEqual(design["coverage"]["status"], "COVERED")

    def test_combination_coverage_reports_an_actual_removed_witness(self):
        from cloud_experiments import contenders, experimental_design
        design = experimental_design.native_design()
        retained = [name for name in design["cross_cells"]
                    if not (contenders.metadata(name)["factors"]["writer"] == "reuse"
                            and contenders.metadata(name)["factors"]["lookup"] == "binary")]
        report = experimental_design.coverage(retained)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn({"axes": ["writer", "lookup"], "levels": {"writer": "reuse", "lookup": "binary"}}, report["uncovered"])
        self.assertTrue(any(item["levels"].get("json_library") == "sonic"
                            and item["levels"].get("writer") == "direct" for item in report["infeasible"]))
        self.assertIn("not execution", report["interpretation"])
        with self.assertRaisesRegex(ValueError, "distinct"):
            experimental_design.coverage(["baseline", "baseline"])

    def test_interleaved_collection_rejects_duplicate_native_offsets(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-screen-offsets-") as folder:
            root = Path(folder)
            run = research.ResearchRun(root / "run", "control", "a" * 64,
                {"screen": {"blocks": 2, "iterations": 2, "seed": 91}})
            binaries = {}
            for name in ("baseline", "aae-direct"):
                binaries[name] = root / name
                binaries[name].mkdir()
                (binaries[name] / "core_probe").write_bytes(b"controlled native identity")
            packet = {"raw_ns": [10, 20], "offsets_ns": [1, 1], "fingerprint": "b" * 64}
            try:
                with patch.object(research, "_invoke", return_value=packet):
                    with self.assertRaisesRegex(ValueError, "offset|order"):
                        research._interleaved_screen(run, binaries, {"aae-direct": ["tiny"]},
                            [{"id": "tiny", "stage": "export"}], {"tiny": root / "fixture"}, None)
            finally:
                run.sample_stream.close()

    def test_registered_combination_cannot_skip_service_contracts_before_confirmation(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-combined-gate-") as folder:
            root = Path(folder)
            joint = "projection-fused+links-btree+lib-scraper-lean"
            selected, chosen = [joint], {}
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            try:
                def failed_contract(binary, output):
                    output.mkdir()
                    report = {"status": "CONTRACT_FAILURE", "scenarios": [{"status": "FAIL"}]}
                    campaign.write_json(output / "service-contracts.json", report)
                    return report
                with patch.object(research, "execute_service", side_effect=failed_contract):
                    with self.assertRaisesRegex(RuntimeError, "service contract gate failed"):
                        research._complete_finalist_selection(run, selected, chosen,
                            {joint: root / "binary"}, workspace=None, files={}, cache=None, cases=[], paths={})
                self.assertEqual(selected, [joint])
                self.assertEqual(run.manifest["checks"]["combined_service_contracts"]["status"], "FAIL")
                self.assertFalse((root / "run" / "confirmation-freeze.json").exists())
            finally:
                run.sample_stream.close()

    def test_incompatible_union_still_checks_each_selected_finalist_service_path(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-finalist-gates-") as folder:
            root = Path(folder)
            selected = ["aae-direct", "aae-buffered"]
            chosen = {}
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            try:
                def denied(binary, output):
                    output.mkdir()
                    status = "CONTRACTS_PASS_WITHIN_SCOPE" if binary.parent.name == "aae-direct" else "CONTRACT_FAILURE"
                    report = {"status": status, "scenarios": [{"status": status}]}
                    campaign.write_json(output / "service-contracts.json", report)
                    return report
                with patch.object(research, "execute_service", side_effect=denied):
                    with self.assertRaisesRegex(RuntimeError, "service contract gate failed"):
                        research._complete_finalist_selection(run, selected, chosen,
                            {name: root / name for name in selected},
                            workspace=None, files={}, cache=None, cases=[], paths={})
                self.assertEqual(chosen["combined_finalist"]["status"], "INCOMPATIBLE_FACTORS")
                self.assertEqual(run.manifest["checks"]["finalist_service_contracts:aae-direct"]["status"], "PASS")
                self.assertEqual(run.manifest["checks"]["finalist_service_contracts:aae-buffered"]["status"], "FAIL")
                self.assertFalse((root / "run" / "confirmation-freeze.json").exists())
            finally:
                run.sample_stream.close()

    def test_interrupted_collection_publishes_only_complete_blocks_and_resumes_without_duplicates(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="athena-collection-") as folder:
            root = Path(folder)
            run = research.ResearchRun(root / "run", "control", "d" * 64,
                {"randomization": {"schema": "athena-randomization/1", "seed": 81723091,
                                   "arm_order": "randomized-start-alternating-blocks"}})
            run.sample_stream.close()
            run.sample_stream = research.EvidenceStream(run.directory, 1048576, 1048576)
            binaries = {v: root / v for v in ("baseline", "aae-direct")}
            for directory in binaries.values():
                directory.mkdir()
                (directory / "core_probe").write_bytes(b"frozen-test-executable")
            cases = [{"id": "tiny", "stage": "rank"}]
            packet = {"raw_ns": [10, 20], "offsets_ns": [1, 2], "fingerprint": "a" * 64}
            try:
                with patch.object(research, "_invoke", side_effect=[packet, packet, packet, TimeoutError("interrupted pair")]):
                    with self.assertRaisesRegex(TimeoutError, "interrupted pair"):
                        research._measure(run, binaries, "aae-direct", cases, {"tiny": root / "fixture"},
                                          "pilot", 2, 2, None)
                run.sample_stream.flush()
                rows = [json.loads(line) for path in run.directory.rglob("samples*.jsonl")
                        for line in path.read_text().splitlines()]
                self.assertEqual(len(rows), 4, "An interrupted pair must not enter published scientific observations")
                with patch.object(research, "_invoke", return_value=packet) as invoke:
                    groups, _ = research._measure(run, binaries, "aae-direct", cases, {"tiny": root / "fixture"},
                                                  "pilot", 2, 2, None)
                self.assertEqual(invoke.call_count, 2, "A complete first block must survive re-entry")
                self.assertEqual(groups["tiny"], [{"baseline": [10, 20], "candidate": [10, 20]}] * 2)
                rows = [json.loads(line) for path in run.directory.rglob("samples*.jsonl")
                        for line in path.read_text().splitlines()]
                self.assertEqual(len(rows), 8)
                self.assertEqual({(r["block"], r["arm"], r["iteration"]) for r in rows},
                                 {(b, a, i) for b in range(2) for a in ("baseline", "candidate") for i in range(2)})
                self.assertCountEqual([r["order"] for r in rows if r["arm"] == "baseline"], [0, 0, 1, 1])
                run.manifest["spec"]["randomization"]["seed"] += 1
                with self.assertRaisesRegex(ValueError, "settings changed"):
                    research._measure(run, binaries, "aae-direct", cases, {"tiny": root / "fixture"}, "pilot", 2, 2, None)
            finally:
                run.sample_stream.close()

    def test_build_factors_protect_all_stages_including_composed_variants(self):
        cases = [{"id": "rank", "stage": "rank", "owners": ["H02"]},
                 {"id": "table", "stage": "table", "owners": ["H06"]},
                 {"id": "prepared", "stage": "prepared", "owners": ["H03"]}]
        for variant in ("build-native", "build-lto-full", "build-native+lib-json-sonic", "prepared+build-codegen1"):
            with self.subTest(variant=variant):
                self.assertEqual(research._variant_cases(variant, cases), cases)

    def test_corpus_intake_rejects_overlap_escape_and_changed_bytes(self):
        import hashlib
        from cloud_experiments import workloads
        with tempfile.TemporaryDirectory(prefix="athena-corpus-") as folder:
            root = Path(folder)
            document = {"schema": "athena-research-corpus/1", "rights_review": "explicit fixture permission",
                        "data_classification": "public", "items": []}
            for split, domain, template, text in (("screen", "one.example", "article", "Alpha"),
                                                   ("confirm", "two.example", "docs", "Beta")):
                name = split + ".html"
                data = ("<p>" + text + "</p>").encode()
                (root / name).write_bytes(data)
                document["items"].append({"id": split, "split": split, "file": name,
                    "sha256": hashlib.sha256(data).hexdigest(), "domain_group": domain,
                    "template_family": template, "license": "CC0-1.0", "source_url": "https://" + domain + "/",
                    "expected_blocks": [{"role": "paragraph", "text": text, "links": []}]})
            manifest = root / "corpus.json"
            def save(value):
                manifest.write_text(json.dumps(value), encoding="utf-8")
            save(document)
            accepted = workloads.load_corpus(manifest)
            self.assertEqual({c["fixture"]["html"] for c in accepted["cases"]}, {"<p>Alpha</p>", "<p>Beta</p>"})
            import base64
            from kaggle_experiments import runner
            research.prepare(root / "bundle", corpus_manifest=manifest)
            spec = json.loads((root / "bundle/spec.json").read_text(encoding="utf-8"))
            runner.unpack_snapshot(base64.b64encode((root / "bundle/source.tgz").read_bytes()).decode(),
                                   spec["source_sha256"], spec["files"], root / "source")
            frozen_files = research.verify_snapshot(root / "source", spec)
            self.assertEqual(spec["holdout_kind"], "independent_seed")
            self.assertIn("deny.toml", spec["files"])
            self.assertIn("[licenses]", frozen_files["deny.toml"].decode("utf-8"))
            for split, text in (("screen", "Alpha"), ("confirm", "Beta")):
                cases = research.study_cases(frozen_files, split)
                case = next(c for c in cases if c["id"] == "corpus-" + split)
                self.assertEqual(case["fixture"]["html"], "<p>" + text + "</p>")
                self.assertEqual(research.fixture_inventory(cases, split), spec["fixtures"][split])
            for defect in ("same-domain", "same-template", "same-bytes", "path-escape", "bad-hash", "unreviewed-rights"):
                bad = copy.deepcopy(document)
                item = bad["items"][1]
                if defect == "same-domain":
                    item["domain_group"] = "one.example"
                elif defect == "same-template":
                    item["template_family"] = "article"
                elif defect == "same-bytes":
                    item.update(file="screen.html", sha256=bad["items"][0]["sha256"])
                elif defect == "path-escape":
                    item["file"] = "../outside.html"
                elif defect == "bad-hash":
                    item["sha256"] = "f" * 64
                else:
                    bad["rights_review"] = ""
                save(bad)
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    workloads.load_corpus(manifest)

    def test_library_dossier_uses_direct_rand_edge_and_candidate_lock_is_immutable(self):
        from cloud_experiments import contenders, libraries
        root = Path(__file__).resolve().parents[1]
        report = libraries.dossier(root)
        self.assertEqual(report["libraries"]["rand"]["resolved"]["version"], "0.9.5")
        self.assertEqual(report["libraries"]["scraper"]["status"], "HOLD")
        files = campaign.source_files(root, ["src", "Cargo.toml", "Cargo.lock", "cloud_experiments"])
        changed = contenders.transform(files, "lib-json-sonic+aae-batched4096+lib-scraper-lean")
        self.assertEqual(libraries.verify_build_inputs(
            files, changed, contenders.metadata("lib-json-sonic+aae-batched4096+lib-scraper-lean")["factors"]
        )["status"], "STATIC_INPUTS_VERIFIED_BEFORE_BUILD")
        delta = libraries.lock_delta(files["Cargo.lock"], changed["Cargo.lock"])
        self.assertEqual({p["name"] for p in delta["removed"]}, {"getopts", "unicode-width"})
        self.assertEqual(changed["Cargo.lock"], files[libraries.SONIC_LEAN_LOCK])
        for name in ("Cargo.toml", libraries.SONIC_LOCK):
            altered = dict(files)
            altered[name] += b"\n# unreviewed input change\n"
            with self.subTest(input=name), self.assertRaisesRegex(ValueError, "reviewed"):
                contenders.transform(altered, "lib-json-sonic")
        with self.assertRaisesRegex(ValueError, "bounded streaming"):
            contenders.metadata("lib-json-sonic+aae-direct")

    def test_every_declared_contender_transforms_against_the_current_frozen_core(self):
        from cloud_experiments import campaign, contenders, research
        files = campaign.source_files(research.ROOT, [
            "src", "tests", "fixtures", "Cargo.toml", "Cargo.lock", "deny.toml",
            "rust-toolchain.toml", "engineering-constitution.md", "CORE_RESEARCH_PLAN.md",
            "cloud_experiments", "kaggle_experiments/protocol.py", "kaggle_experiments/runner.py",
            "kaggle_experiments/probe.rs", "kaggle_experiments/test_protocol.py",
            "kaggle_experiments/pack_snapshots.py", "kaggle_experiments/generate_clusters.py",
            "kaggle_results/verify_statistics.py", "kaggle_results/pull_and_analyze.py", "model",
        ])
        for variant in sorted(contenders.definitions()):
            with self.subTest(variant=variant):
                transformed = contenders.transform(files, variant)
                self.assertIn("Cargo.lock", transformed)
                self.assertIn("Cargo.toml", transformed)

    def test_research_build_rejects_unreviewed_manifest_and_lock_before_cargo(self):
        root = research.ROOT
        files = campaign.source_files(root, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
            "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"])

        class RunWithoutCargo:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {"snapshots": {}}
                self.commands = []

            def run(self, command, **kwargs):
                self.commands.append(command)
                raise AssertionError("Cargo ran before static dependency-input verification")

            def save(self):
                return None

        for name in ("Cargo.toml", "Cargo.lock"):
            altered = dict(files)
            altered[name] = altered[name] + b"\n# unreviewed build input\n"
            with tempfile.TemporaryDirectory(prefix="athena-prebuild-lock-") as folder:
                root_path = Path(folder)
                run_dir, workspace, cache = root_path / "run", root_path / "work", root_path / "target"
                for directory in (run_dir, workspace, cache):
                    directory.mkdir()
                run = RunWithoutCargo(run_dir)
                with self.subTest(input=name), self.assertRaisesRegex(ValueError, "Unreviewed baseline Cargo input"):
                    research._build(run, workspace, altered, "baseline", cache)
                self.assertEqual(run.commands, [], "Unreviewed build inputs must be rejected before any command")

    def test_research_build_stops_when_network_namespace_negative_controls_fail(self):
        from unittest.mock import patch
        files = campaign.source_files(research.ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
            "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"])

        class RunWithoutCargo:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {"snapshots": {}}
                self.commands = []

            def run(self, command, **kwargs):
                self.commands.append(command)
                return (b'{"host_netns":"net:[host]","build_netns":"net:[host]",'
                        b'"metadata_tcp_blocked":true,"public_tcp_blocked":true}')

            def save(self):
                return None

        with tempfile.TemporaryDirectory(prefix="athena-build-netns-") as folder:
            root = Path(folder)
            run_dir, workspace, cache = root / "run", root / "work", root / "target"
            for directory in (run_dir, workspace, cache):
                directory.mkdir()
            run = RunWithoutCargo(run_dir)
            with patch.object(research.platform, "system", return_value="Linux"), \
                    patch.object(research, "_current_network_namespace_id", return_value="net:[host]"):
                with self.assertRaisesRegex(RuntimeError, "did not deny metadata and public egress"):
                    research._build(run, workspace, files, "baseline", cache)
            self.assertEqual(len(run.commands), 1)
            self.assertEqual(run.commands[0][0], "unshare")
            self.assertIn("--net", run.commands[0])

    def test_literal_oracle_rejects_corruption_and_wrong_denial_even_with_valid_json(self):
        from cloud_experiments import workloads
        fixture = {"expected_blocks": [{"role": "paragraph", "text": "visible", "links": []}]}
        good = [{"type": "meta", "total_blocks": 1},
                {"type": "block", "role": "paragraph", "text": "visible", "links": []},
                {"type": "end", "block_count": 1}]
        encode = lambda frames: "\n".join(json.dumps(frame) for frame in frames).encode()
        self.assertEqual(workloads.envelope_errors(encode(good), fixture), [])
        for bad in (good[:1] + good[2:], [good[0], {**good[1], "text": "attack"}, good[2]],
                    [good[0], {**good[1], "links": [7]}, good[2]]):
            self.assertTrue(workloads.envelope_errors(encode(bad), fixture))
        denied = {"expected_error": "decoded text byte budget exceeded"}
        self.assertEqual(workloads.envelope_errors(b'{"status":"EXPECTED_ERROR","error":"decoded text byte budget exceeded"}', denied), [])
        self.assertTrue(workloads.envelope_errors(b'{"status":"EXPECTED_ERROR","error":"some other error"}', denied))
        self.assertTrue(workloads.envelope_errors(encode(good), denied))
        resolved = next(c["fixture"] for c in workloads.native_cases("screen") if c["id"] == "resolved-link-identity")
        fragment_frames = [{"type": "meta", "total_blocks": 1},
            {"type": "block", "role": "paragraph", "text": "ABC", "links": [0, 1]},
            {"type": "link", "id": 0, "url": "https://example.org/a#one"},
            {"type": "link", "id": 1, "url": "https://example.org/a#two"},
            {"type": "end", "block_count": 1}]
        self.assertEqual(workloads.envelope_errors(encode(fragment_frames), resolved), [])
        fragment_frames[2]["url"] = "https://example.org/a"
        self.assertTrue(workloads.envelope_errors(encode(fragment_frames), resolved))

    def test_library_panel_includes_real_feature_and_algorithm_interactions(self):
        from cloud_experiments import contenders
        variants, panels = research.native_matrix()
        self.assertIn("aae-batched4096+lib-blake3-pure", variants)
        self.assertIn("projection-fused+links-btree+lib-scraper-lean", variants)
        self.assertIn("I-HASH-LIBRARY", panels)
        self.assertIn("I-PARSER-LIBRARY", panels)
        self.assertEqual(panels["I-HTML-TABLE"], ["baseline", "table-html-selectors"])
        self.assertEqual(panels["I-HTML-HEADERS"], ["baseline", "table-html-scope"])
        self.assertIn("table-html-selectors", variants)
        self.assertIn("table-html-scope", variants)
        self.assertEqual(contenders.metadata("table-html-selectors")["implementation_scopes"], ["html_table_stage_only"])
        self.assertEqual(contenders.metadata("table-html-selectors")["build"]["env"][
            "ATHENA_RESEARCH_TABLE_EXTRACTOR"], "selectors")
        self.assertEqual(contenders.metadata("table-html-scope")["build"]["env"][
            "ATHENA_RESEARCH_TABLE_HEADERS"], "scope")
        self.assertFalse(contenders.compatible("table-html-selectors", "aae-direct"))
        self.assertEqual(set(panels.get("I-JSON-PROJECT", [])), {
            "baseline", "projection-fused", "lib-json-sonic", "projection-fused+lib-json-sonic"})
        self.assertIn("I-JSON-PROJECT", research.manifest_design()["required_interactions"])
        decisions = json.loads((research.ROOT / "cloud_experiments/core_decisions.json").read_text(encoding="utf-8"))
        self.assertIn("I-JSON-PROJECT", {item["id"] for item in decisions["interactions"]})
        with self.assertRaises(ValueError):
            contenders.metadata("lib-blake3-pure+lib-blake3-intrinsics")
        factor_keys = [research.identity(contenders.metadata(v)["factors"]) for v in variants]
        self.assertEqual(len(factor_keys), len(set(factor_keys)))

        from cloud_experiments import workloads
        cases = workloads.native_cases("screen")
        html_cases = [case for case in cases if case["stage"] == "html-table"]
        self.assertEqual({case["id"] for case in html_cases}, {
            "html-table-spans", "html-table-scope-headers", "html-table-nested"})
        self.assertTrue(all(case["owners"] == ["H06"] for case in html_cases))
        assignments = research.screen_plan(
            ["baseline", "table-html-selectors"], {"I-HTML-TABLE": panels["I-HTML-TABLE"]}, cases)
        expected_ids = {case["id"] for case in html_cases}
        self.assertEqual(set(assignments["panel_cases"]["I-HTML-TABLE"]), expected_ids)
        self.assertEqual(set(assignments["assignments"]["table-html-selectors"]), expected_ids)
        self.assertNotIn("html-table-spans", assignments["cross_cases"])

        fixture = html_cases[0]["fixture"]
        observation = {"schema": "athena-html-table-candidate/1", "parse_warnings": 0,
                       "header_associations": "explicit-attributes-only",
                       "cell_text": "normalized DOM text, including nested-table descendant text",
                       "tables": fixture["expected_html_tables"]}
        encoded = json.dumps(observation, separators=(",", ":")).encode()
        self.assertEqual(workloads.envelope_errors(encoded, fixture), [])
        corrupted = json.loads(encoded)
        corrupted["tables"][0]["rows"][1]["cells"][0]["rowspan"] = 1
        self.assertTrue(workloads.envelope_errors(
            json.dumps(corrupted, separators=(",", ":")).encode(), fixture))

        scope_case = next(case for case in html_cases if case["id"] == "html-table-scope-headers")
        scope_fixture = scope_case["fixture"]
        scope_observation = {"schema": "athena-html-table-candidate/1", "parse_warnings": 0,
                             "header_associations": "scope-derived-v1",
                             "cell_text": "normalized DOM text, including nested-table descendant text",
                             "tables": scope_fixture["expected_html_tables_by_header_mode"]["scope"]}
        scope_encoded = json.dumps(scope_observation, separators=(",", ":")).encode()
        self.assertEqual(workloads.envelope_errors(scope_encoded, scope_fixture, header_mode="scope"), [])
        scope_corrupted = json.loads(scope_encoded)
        scope_corrupted["tables"][0]["rows"][1]["cells"][1]["associated_headers"] = ["east", "amount"]
        self.assertTrue(workloads.envelope_errors(
            json.dumps(scope_corrupted, separators=(",", ":")).encode(), scope_fixture, header_mode="scope"))
        with self.assertRaisesRegex(ValueError, "Scope header oracle is limited to HTML table cases"):
            research._oracles(None, None, None, [{"stage": "pipeline"}], {}, header_mode="scope")

        rounds = lambda baseline, candidate: [
            {"baseline": [baseline], "candidate": [candidate]},
            {"baseline": [baseline], "candidate": [candidate]}]
        grouped = {"baseline": {"html-table-spans": rounds(100, 100)},
                   "table-html-selectors": {"html-table-spans": rounds(100, 80)}}
        panel_report = research.interaction_report(
            grouped, {"I-HTML-TABLE": panels["I-HTML-TABLE"]},
            {"I-HTML-TABLE": ["html-table-spans"]})["panels"]["I-HTML-TABLE"]
        self.assertEqual(panel_report["status"], "COMPLETE_CELLS")
        self.assertTrue(panel_report["contrasts"])
        self.assertEqual(panel_report["contrasts"][0]["kind"], "main_effect")
        self.assertAlmostEqual(panel_report["contrasts"][0]["mean_log_contrast"], math.log(.8))

    def test_materialize_places_feature_gated_probe_at_declared_cargo_target(self):
        with tempfile.TemporaryDirectory(prefix="athena-probe-materialize-") as folder:
            root = Path(__file__).resolve().parents[1]
            source = campaign.source_files(root, ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
                                                   "src", "cloud_experiments", "kaggle_experiments/probe.rs"])
            workspace = Path(folder) / "work"
            workspace.mkdir()
            transformed = research._materialize(workspace, source, "baseline")
            probe = workspace / "cloud_experiments/core_probe.rs"
            self.assertEqual(probe.read_bytes(), transformed["cloud_experiments/core_probe.rs"])
            self.assertFalse((workspace / "src/bin/core_probe.rs").exists())
            manifest = (workspace / "Cargo.toml").read_text(encoding="utf-8")
            self.assertIn('path = "cloud_experiments/core_probe.rs"', manifest)
            self.assertIn('required-features = ["research-probes"]', manifest)
            self.assertTrue((workspace / "src/bin/service_probe.rs").is_file())
            self.assertTrue((workspace / "src/bin/research_probe.rs").is_file())

    def test_interaction_cells_measure_declared_common_cohorts_without_losing_build_scope(self):
        cases = [{"id": "pipeline", "stage": "pipeline", "owners": ["H01", "H04", "H05", "H10"]},
                 {"id": "hash", "stage": "hash", "owners": ["H01"]},
                 {"id": "parse", "stage": "parse", "owners": ["H04", "H05"]},
                 {"id": "serialization", "stage": "serialize", "owners": ["H01"]},
                 {"id": "rank", "stage": "rank", "owners": ["H02"]}]
        variants, panels = research.native_matrix()
        selected = {k: panels[k] for k in ("I-JSON-CPU", "I-LIBRARY-SYSTEM", "I-JSON-PROJECT")}
        required = {"I-JSON-CPU": ["hash", "pipeline", "serialization"],
                    "I-LIBRARY-SYSTEM": ["hash", "parse", "pipeline", "serialization"],
                    "I-JSON-PROJECT": ["hash", "parse", "pipeline", "serialization"]}
        plan = research.screen_plan(variants, selected, cases)
        assignments = plan["assignments"]
        for panel, cells in selected.items():
            for cell in cells:
                with self.subTest(panel=panel, cell=cell):
                    self.assertTrue(set(required[panel]).issubset(assignments[cell]),
                                    "Every interaction corner needs the declared common workload")
        self.assertEqual(set(assignments["build-native"]), {"pipeline", "hash", "parse", "serialization", "rank"})
        self.assertEqual(plan["panel_cases"], required)
        groups = {v: {c: [{"baseline": [100], "candidate": [100]},
                           {"baseline": [120], "candidate": [120]}] for c in ids}
                  for v, ids in assignments.items()}
        report = research.interaction_report(groups, selected, plan["panel_cases"])
        for panel in selected:
            self.assertEqual(report["panels"][panel]["cohorts"], required[panel])
            self.assertEqual(report["panels"][panel]["status"], "COMPLETE_CELLS")
            for contrast in report["panels"][panel]["contrasts"]:
                self.assertAlmostEqual(contrast["mean_log_interaction"], 0)
        del groups["lib-scraper-lean"]["hash"]
        with self.assertRaisesRegex(ValueError, "do not share"):
            research.interaction_report(groups, selected, plan["panel_cases"])

    def test_causal_workloads_include_exact_parse_denials_and_hash_boundaries(self):
        cases = research.synthetic_cases("screen")
        fixtures = {case["id"]: case["fixture"] for case in cases}
        self.assertIn("denied-decoded-text", sorted(fixtures))
        self.assertEqual(fixtures.get("denied-decoded-text", {}).get("expected_error"),
                         "decoded text byte budget exceeded")
        expected_attribute_counts = {1_024, 2_048, 4_096, 8_192}
        combined_fixture_bytes = 0
        limits = research.manifest_design()["limits"]
        self.assertEqual(limits["max_fixture_html_bytes"], 4 * 1024 ** 2)
        for split, split_cases in (("screen", cases), ("confirm", research.synthetic_cases("confirm"))):
            combined_fixture_bytes += sum(len(research.canonical(case["fixture"])) for case in split_cases)
            with self.subTest(attribute_split=split):
                attribute_cases = [case for case in split_cases if case["id"].startswith("attribute-heavy-")]
                observed_counts = {int(case["id"].rsplit("-", 1)[1]) for case in attribute_cases}
                self.assertEqual(observed_counts, expected_attribute_counts)
                for attribute_case in attribute_cases:
                    with self.subTest(split=split, case=attribute_case["id"]):
                        attribute_count = int(attribute_case["id"].rsplit("-", 1)[1])
                        attribute_html = attribute_case["fixture"]["html"]
                        html_bytes = len(attribute_html.encode("utf-8"))
                        self.assertEqual(attribute_html.count("='x'"), attribute_count)
                        self.assertLessEqual(html_bytes, limits["max_fixture_html_bytes"])
                        self.assertLess(html_bytes, 1024 ** 2)
                        self.assertLessEqual(len(research.canonical(attribute_case["fixture"])),
                                             limits["max_fixture_file_bytes"])
                        self.assertEqual(attribute_case["stage"], "pipeline")
                        self.assertEqual(attribute_case["owners"], ["H04", "H05", "H10"])
                        self.assertEqual(attribute_case["fixture"]["expected_blocks"],
                                         [{"role": "paragraph", "text": "attribute-soup-sentinel", "links": []}])
        fixture_cap = limits["max_fixture_bytes"]
        self.assertLessEqual(combined_fixture_bytes, fixture_cap)
        for size in (63, 64, 65, 1023, 1024, 1025):
            with self.subTest(size=size):
                self.assertIn("hash-boundary-" + str(size), sorted(fixtures))
                self.assertEqual(len(fixtures["hash-boundary-" + str(size)]["html"].encode()) - 7, size)

    def test_raw_confirmation_rejects_wrong_freeze_missing_samples_and_changed_binary(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix="athena-confirm-") as folder:
            root = Path(folder)
            snapshots = {}
            for arm in ("baseline", "v"):
                binary = root / "bins" / arm / "core_probe"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"controlled native bytes")
                snapshots[arm] = {"binaries": {"core_probe": hashlib.sha256(binary.read_bytes()).hexdigest()}}
            design = {"protocol": research.PROTOCOL, "source_sha256": "a" * 64,
                      "confirm_binaries": {name: snapshots[name]["binaries"] for name in ("baseline", "v")},
                      "confirm": {"blocks": 20, "iterations": 2, "alpha": .025,
                                  "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"]},
                      "holdout_kind": "independent_seed"}
            fixtures = {"screen": [{"id": "s", "sha256": "b" * 64, "lineage": "a"}],
                        "confirm": [{"id": "case", "sha256": "c" * 64, "lineage": "a"}]}
            design["fixtures"] = fixtures
            design["study_id"] = research.identity(design)
            freeze = research.freeze_confirmation(design, ["v"],
                {"status": "COMPLETE", "phase": "pilot", "wall_seconds": 1, "admitted": True,
                 "precision_status": "PLANNED_NOT_GUARANTEED"}, fixtures)
            manifest = {"protocol": research.PROTOCOL, "bundle_id": design["study_id"], "spec": design,
                        "snapshots": snapshots, "sample_shards": ["samples-0000.jsonl"]}
            (root / "manifest.json").write_bytes(research.canonical(manifest))
            (root / "confirmation-freeze.json").write_bytes(research.canonical(freeze))
            rows = []
            for block in range(20):
                for arm, index, ns in (("baseline", 0, 100), ("candidate", 1, 80)):
                    common = {"protocol": research.PROTOCOL, "study_id": design["study_id"], "freeze_id": freeze["freeze_id"],
                              "variant": "v", "case": "case", "phase": "confirm", "arm": arm, "block": block,
                              "order": index ^ (block % 2)}
                    rows.extend({**common, "iteration": i, "ns": ns, "offset_ns": i * 120,
                                 "fingerprint": "d" * 64} for i in range(2))
                    rows.append({**common, "kind": "rss", "unit": "KiB", "value": 1000 - 100 * index})
            raw = root / "samples-0000.jsonl"
            def save(values):
                raw.write_text("".join(json.dumps(r) + "\n" for r in values), encoding="utf-8")
            save(rows)
            groups, rss, _ = research.load_confirmation(root, "v")
            self.assertEqual(groups["case"][0], {"baseline": [100, 100], "candidate": [80, 80]})
            self.assertEqual(rss["case"][0], {"baseline": 1000, "candidate": 900})
            for change in ("wrong-freeze", "missing-rss", "duplicate-cpu", "wrong-case"):
                changed = copy.deepcopy(rows)
                if change == "wrong-freeze":
                    changed[0]["freeze_id"] = "f" * 64
                elif change == "missing-rss":
                    changed.pop()
                elif change == "duplicate-cpu":
                    changed.append(changed[0])
                else:
                    changed[0]["case"] = "hidden-case"
                save(changed)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    research.load_confirmation(root, "v")
            save(rows)
            weaker = copy.deepcopy(design)
            weaker["confirm"]["alpha"] = .05
            changed_freeze = research.freeze_confirmation(weaker, ["v"], freeze["pilot"], fixtures)
            (root / "confirmation-freeze.json").write_bytes(research.canonical(changed_freeze))
            save([{**row, "freeze_id": changed_freeze["freeze_id"]} for row in rows])
            with self.assertRaisesRegex(ValueError, "predeclared"):
                research.load_confirmation(root, "v")
            (root / "confirmation-freeze.json").write_bytes(research.canonical(freeze))
            save(rows)
            randomized = copy.deepcopy(design)
            randomized.pop("study_id")
            randomized["randomization"] = {"schema": "athena-randomization/1", "seed": 81723091,
                                            "arm_order": "randomized-start-alternating-blocks"}
            randomized["study_id"] = research.identity(randomized)
            randomized_freeze = research.freeze_confirmation(randomized, ["v"], freeze["pilot"], fixtures)
            active_manifest = {**manifest, "spec": randomized, "bundle_id": randomized["study_id"]}
            (root / "manifest.json").write_bytes(research.canonical(active_manifest))
            (root / "confirmation-freeze.json").write_bytes(research.canonical(randomized_freeze))
            from cloud_experiments import experimental_design
            offset = experimental_design.arm_start(randomized, "v", "case", "confirm")
            randomized_rows = [{**row, "study_id": randomized["study_id"], "freeze_id": randomized_freeze["freeze_id"],
                                "order": row["order"] ^ offset} for row in rows]
            save(randomized_rows)
            self.assertEqual(research.load_confirmation(root, "v")[0]["case"][0],
                             {"baseline": [100, 100], "candidate": [80, 80]})
            randomized_rows[0]["order"] ^= 1
            save(randomized_rows)
            with self.assertRaisesRegex(ValueError, "order"):
                research.load_confirmation(root, "v")
            (root / "manifest.json").write_bytes(research.canonical(manifest))
            (root / "confirmation-freeze.json").write_bytes(research.canonical(freeze))
            save(rows)
            (root / "bins/v/core_probe").write_bytes(b"different executable")
            with self.assertRaisesRegex(ValueError, "binary"):
                research.load_confirmation(root, "v")

    def test_factor_cells_share_balanced_macroblocks_before_the_next_block(self):
        from cloud_experiments import experimental_design
        assignments = {"a": ["small", "large"], "b": ["small", "large"]}
        schedule = research.screen_schedule(assignments, blocks=3, seed=91)
        self.assertEqual([block for block, _, _ in schedule], [0] * 4 + [1] * 4 + [2] * 4)
        for block in range(3):
            self.assertCountEqual([(case, variant) for b, case, variant in schedule if b == block],
                                  [("small", "a"), ("small", "b"), ("large", "a"), ("large", "b")])
        self.assertEqual(schedule, research.screen_schedule(assignments, blocks=3, seed=91))
        self.assertNotEqual(experimental_design.derive_seed(91, "screen-cell-order"),
                            experimental_design.derive_seed(91, "fixture-generation"))
        policy = {"randomization": {"schema": "athena-randomization/1", "seed": 81723091,
                                     "arm_order": "randomized-start-alternating-blocks"}}
        starts = {experimental_design.arm_start(policy, "v", "case-" + str(i), "screen") for i in range(16)}
        self.assertEqual(starts, {0, 1})
        self.assertEqual(experimental_design.arm_start({}, "v", "case", "confirm"), 0)
        with self.assertRaisesRegex(ValueError, "policy"):
            experimental_design.arm_start({"randomization": {"schema": "unknown"}}, "v", "case", "confirm")

    def test_source_bundle_executes_controls_from_isolated_delivery_root(self):
        import base64
        from kaggle_experiments import runner
        with tempfile.TemporaryDirectory(prefix="athena-delivery-") as folder:
            root = Path(folder)
            research.prepare(root / "bundle")
            spec = json.loads((root / "bundle/spec.json").read_text(encoding="utf-8"))
            runner.unpack_snapshot(base64.b64encode((root / "bundle/source.tgz").read_bytes()).decode(),
                                   spec["source_sha256"], spec["files"], root / "source")
            selected = ["cloud_experiments.test_campaign.AgentBoundaryControls.test_compact_expansion_enforces_scope_revision_expiry_and_releases_state",
                        "kaggle_experiments.test_protocol.StatisticalRegressions.test_fast_median_does_not_accept_a_regressed_tail",
                        "model.test_models.NumericalContracts.test_declared_cost_scenario_is_exact"]
            script = "import sys,unittest; sys.path.insert(0,sys.argv[1]); suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:]); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(0 if result.wasSuccessful() and result.testsRun==3 else 1)"
            command = [sys.executable, "-I", "-B", "-c", script, str(root / "source"), *selected]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            (root / "source/kaggle_results/verify_statistics.py").unlink()
            challenged = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(challenged.returncode, 0)
            self.assertIn("verify_statistics", challenged.stderr)

    def test_owner_slots_and_worst_cohort_prevent_global_mean_hiding_regression(self):
        records = {
            "hero": {"owners": ["H01"], "correct": True, "ratios": {"small": .1, "large": 2}},
            "balanced": {"owners": ["H01"], "correct": True, "ratios": {"small": .95, "large": .96}},
            "rank": {"owners": ["H02"], "correct": True, "ratios": {"query": .99}},
            "partial": {"owners": ["H01"], "correct": True, "ratios": {"small": .01}},
            "wrong": {"owners": ["H02"], "correct": False, "ratios": {"query": .01}},
        }
        result = research.shortlist(records, {"H01": ["small", "large"], "H02": ["query"]})
        self.assertEqual(result["selected"], ["balanced", "rank"])
        self.assertEqual(result["excluded"]["hero"], "UNCONFIRMED_SLOT_LIMIT")
        self.assertEqual(result["excluded"]["partial"], "INCOMPLETE_OR_INVALID")
        self.assertEqual(result["excluded"]["wrong"], "ORACLE_REJECTED")

    def test_freeze_rejects_exploration_reuse_and_false_domain_holdout(self):
        design = {"protocol": research.PROTOCOL, "source_sha256": "a" * 64,
                  "confirm": {"blocks": 24, "iterations": 250, "alpha": .05,
                              "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"]},
                  "holdout_kind": "independent_seed"}
        pilot = {"status": "COMPLETE", "wall_seconds": 2, "phase": "pilot",
                 "precision_status": "PLANNED_NOT_GUARANTEED", "admitted": True}
        fixtures = {"screen": [{"id": "s", "sha256": "b" * 64, "lineage": "template-A"}],
                    "confirm": [{"id": "c", "sha256": "c" * 64, "lineage": "template-A"}]}
        frozen = research.freeze_confirmation(design, ["balanced"], pilot, fixtures)
        self.assertEqual(frozen["family_size"], 4)
        self.assertGreaterEqual(frozen["bootstrap_draws"], 30000)
        self.assertEqual(frozen["claim_scope"], "independent_seed")
        for bad_kind in ("same-bytes", "false-domain", "no-admission"):
            with self.subTest(kind=bad_kind):
                changed, inputs, changed_pilot = copy.deepcopy(design), copy.deepcopy(fixtures), dict(pilot)
                if bad_kind == "same-bytes":
                    inputs["confirm"][0]["sha256"] = "b" * 64
                elif bad_kind == "false-domain":
                    changed["holdout_kind"] = "independent_domain"
                else:
                    changed_pilot["admitted"] = False
                with self.assertRaises(ValueError):
                    research.freeze_confirmation(changed, ["balanced"], changed_pilot, inputs)

    def test_resource_admission_requires_actual_price_credit_and_bounded_work(self):
        estimate = {"wall_seconds": 3600, "evidence_bytes": 1024, "build_bytes": 2048,
                    "analysis_seconds": 600, "replicas": 1, "peak_memory_bytes": 1024,
                    "metadata_bytes": 64, "analysis_bytes": 64, "export_bytes": 2048,
                    "export_seconds": 60, "shutdown_seconds": 40, "memory_reserve_bytes": 1024}
        budget = {"remaining_usd": "100", "ceiling_usd": "300", "reserve_usd": "60",
                  "spent_usd": "10", "hourly_usd": "2", "storage_usd": "1",
                  "price_verified": True, "credit_verified": True, "free_bytes": 8192,
                  "deadline_seconds": 7200, "enforcement_verified": True,
                  "memory_bytes": 8192, "enforced_memory_bytes": 4096}
        admitted = research.admit_resources(estimate, budget)
        self.assertTrue(admitted["admitted"])
        self.assertEqual(admitted["estimated_usd"], "3.388889")
        for field, value in (("price_verified", False), ("credit_verified", False),
                             ("enforcement_verified", False), ("remaining_usd", "62"),
                             ("deadline_seconds", 4200), ("free_bytes", 4096),
                             ("memory_bytes", 1500), ("enforced_memory_bytes", 1500)):
            with self.subTest(field=field):
                changed = {**budget, field: value}
                self.assertFalse(research.admit_resources(estimate, changed)["admitted"])
        self.assertFalse(research.admit_resources({**estimate, "replicas": 2}, budget)["admitted"])
        self.assertFalse(research.admit_resources({**estimate, "required_evidence_bytes": 2048}, budget)["admitted"])
        cases = [{"id": "export", "stage": "export", "owners": ["H01"]},
                 {"id": "rank", "stage": "rank", "owners": ["H02"]},
                 {"id": "pipeline", "stage": "pipeline", "owners": ["H01", "H02"]}]
        phases = {split: cases for split in ("screen", "confirm")}
        forecast = research.forecast_native(["baseline", "aae-direct", "rank-binary"], phases,
            research.manifest_design(), {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 2048},
            {"not_scientific_samples": True, "seconds_per_case_draw_max": .001})
        self.assertEqual(forecast["maximum_confirm_cases"], 2 + 2 + 3)
        self.assertEqual(forecast["maximum_family"], 28)
        self.assertEqual(forecast["maximum_bootstrap_draws"], 112000)
        self.assertEqual(forecast["analysis_seconds"], 1568)
        self.assertGreater(forecast["export_seconds"], 0)
        self.assertEqual(forecast["shutdown_seconds"], 600)
        fixture_phases = {
            split: [{**case, "fixture": {"html": "<p>bounded</p>", "query": None}}
                    for case in cases]
            for split in ("screen", "confirm")
        }
        fixture_file_bytes = len(research.canonical(fixture_phases["screen"][0]["fixture"]))
        fixture_total_bytes = sum(len(research.canonical(case["fixture"]))
                                  for split_cases in fixture_phases.values() for case in split_cases)
        fixture_design = research.manifest_design()
        fixture_design["limits"]["max_fixture_file_bytes"] = fixture_file_bytes
        fixture_design["limits"]["max_fixture_bytes"] = fixture_total_bytes
        fixture_html_bytes = len(fixture_phases["screen"][0]["fixture"]["html"].encode("utf-8"))
        fixture_design["limits"]["max_fixture_html_bytes"] = fixture_html_bytes
        fixture_forecast = research.forecast_native(["baseline", "aae-direct", "rank-binary"],
            fixture_phases, fixture_design,
            {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 2048},
            {"not_scientific_samples": True, "seconds_per_case_draw_max": .001})
        self.assertEqual(fixture_forecast["fixture_bytes"], fixture_total_bytes)
        for field, bound in (("max_fixture_file_bytes", fixture_file_bytes - 1),
                             ("max_fixture_bytes", fixture_total_bytes - 1),
                             ("max_fixture_html_bytes", fixture_html_bytes - 1)):
            with self.subTest(fixture_bound=field):
                changed_design = copy.deepcopy(fixture_design)
                changed_design["limits"][field] = bound
                with self.assertRaisesRegex(ValueError, "fixture.*bound"):
                    research.forecast_native(["baseline", "aae-direct", "rank-binary"],
                        fixture_phases, changed_design,
                        {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 2048},
                        {"not_scientific_samples": True, "seconds_per_case_draw_max": .001})
        variants, _ = research.native_matrix()
        full_design = research.manifest_design()
        full_cases = {split: research.synthetic_cases(split) for split in ("screen", "confirm")}
        full = research.forecast_native(variants, full_cases, full_design,
            {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 1},
            {"not_scientific_samples": True, "seconds_per_case_draw_max": 0})
        self.assertLessEqual(full["required_evidence_bytes"], full["evidence_bytes"],
                             "Current complete matrix cannot fit its encoded-row evidence budget")
        self.assertLessEqual(full["export_bytes"], full_design["limits"]["export_bytes"])
        self.assertLessEqual(sum(full[key] for key in ("evidence_bytes", "build_bytes", "metadata_bytes",
                                                     "analysis_bytes", "export_bytes", "fixture_bytes")) + full_design["limits"]["disk_reserve_bytes"],
                             full_design["limits"]["minimum_free_bytes"])

    def test_fixture_writes_enforce_exact_file_and_combined_caps_before_output(self):
        fixture = {"html": "<p>bounded</p>", "query": None}
        html_bytes = len(fixture["html"].encode("utf-8"))
        phases = {split: [{"id": split + "-case", "fixture": fixture}]
                  for split in ("screen", "confirm")}
        file_bytes = len(research.canonical(fixture))
        total_bytes = 2 * file_bytes
        limits = {"max_fixture_file_bytes": file_bytes, "max_fixture_bytes": total_bytes,
                  "max_fixture_html_bytes": html_bytes}
        with tempfile.TemporaryDirectory(prefix="athena-fixture-bounds-") as folder:
            root = Path(folder) / "exact"
            paths, observed_bytes = research._write_fixtures(root, phases, limits)
            self.assertEqual(observed_bytes, total_bytes)
            self.assertEqual(set(paths), {"screen", "confirm"})
            for split, cases in phases.items():
                written = paths[split][cases[0]["id"]]
                self.assertEqual(written.read_bytes(), research.canonical(fixture))

            for suffix, changed_limits in (
                    ("file-over", {**limits, "max_fixture_file_bytes": file_bytes - 1}),
                    ("aggregate-over", {**limits, "max_fixture_bytes": total_bytes - 1})):
                with self.subTest(bound=suffix):
                    rejected_root = Path(folder) / suffix
                    with self.assertRaisesRegex(ValueError, "(?i)fixture.*bound"):
                        research._write_fixtures(rejected_root, phases, changed_limits)
                    self.assertFalse(rejected_root.exists())

            for field in ("html", "replacement_html"):
                with self.subTest(oversized_html_field=field):
                    oversized_fixture = {"html": "<p>bounded</p>", "query": None,
                                         field: "x" * (html_bytes + 1)}
                    oversized_phases = {
                        split: [{"id": split + "-oversized", "fixture": oversized_fixture}]
                        for split in ("screen", "confirm")
                    }
                    changed_limits = {**limits, "max_fixture_file_bytes": 4096,
                                      "max_fixture_bytes": 8192}
                    rejected_root = Path(folder) / (field + "-over")
                    with self.assertRaisesRegex(ValueError, "(?i)fixture html.*bound"):
                        research._write_fixtures(rejected_root, oversized_phases, changed_limits)
                    self.assertFalse(rejected_root.exists())

            with self.subTest(html_cap_uses_utf8_bytes=True):
                unicode_fixture = {"html": "é" * 8, "query": None}
                unicode_phases = {
                    split: [{"id": split + "-unicode", "fixture": unicode_fixture}]
                    for split in ("screen", "confirm")
                }
                changed_limits = {**limits, "max_fixture_file_bytes": 4096,
                                  "max_fixture_bytes": 8192, "max_fixture_html_bytes": 15}
                rejected_root = Path(folder) / "unicode-html-over"
                with self.assertRaisesRegex(ValueError, "(?i)fixture html.*bound"):
                    research._write_fixtures(rejected_root, unicode_phases, changed_limits)
                self.assertFalse(rejected_root.exists())

            with self.subTest(fixture_identity_cannot_escape_root=True):
                escaped_phases = {
                    split: [{"id": "../../escape", "fixture": fixture}]
                    for split in ("screen", "confirm")
                }
                escaped_root = Path(folder) / "escaped-study"
                adjacent_file = Path(folder) / "escape.json"
                with self.assertRaisesRegex(ValueError, "fixture identity"):
                    research._write_fixtures(escaped_root, escaped_phases, limits)
                self.assertFalse(escaped_root.exists())
                self.assertFalse(adjacent_file.exists())

    def test_expanded_rows_bind_study_split_and_monotonic_offsets(self):
        rows = [{"protocol": research.PROTOCOL, "study_id": "d" * 64, "variant": "v",
                 "case": "q", "phase": "confirm", "block": b, "iteration": i,
                 "arm": a, "order": order ^ (b % 2), "ns": 100 - order * 10,
                 "offset_ns": i * 120, "fingerprint": "e" * 64}
                for b in range(2) for a, order in (("baseline", 0), ("candidate", 1)) for i in range(2)]
        args = dict(study_id="d" * 64, variant="v", case="q", phase="confirm", blocks=2, iterations=2)
        self.assertEqual(research.paired_blocks(rows, **args)[0],
                         {"baseline": [100, 100], "candidate": [90, 90]})
        bad_packets = [rows[:-1], rows + rows[:1]]
        for key, value in (("protocol", campaign.PROTOCOL), ("study_id", "f" * 64),
                           ("phase", "screen"), ("order", 1), ("offset_ns", 120)):
            changed = copy.deepcopy(rows)
            changed[0][key] = value
            bad_packets.append(changed)
        for changed in bad_packets:
            with self.subTest(changed=changed[:1]), self.assertRaises(ValueError):
                research.paired_blocks(changed, **args)

    def test_missing_or_rejected_required_decision_cannot_be_closed_by_green_subset(self):
        evidence = {"H01": {"status": "DECIDED_WITHIN_SCOPE", "oracle": "PASS",
                             "performance": {"large": "PASS_PERFORMANCE"},
                             "scope": "cpu-A corpus-X", "remaining": []}}
        result = research.decision_closure(["H01", "H02"], evidence)
        self.assertEqual(result["overall_decision"], "HOLD")
        self.assertEqual(result["decisions"]["H02"]["status"], "NOT_RUN")
        evidence["H01"]["performance"]["large"] = "REJECT"
        self.assertEqual(research.decision_closure(["H01"], evidence)["overall_decision"], "HOLD")
        self.assertEqual(research.decision_closure([], {})["overall_decision"], "HOLD")

    def test_interaction_requires_complete_paired_cells_and_preserves_covariance(self):
        cells = {"00": [100, 200], "10": [80, 160], "01": [90, 180], "11": [60, 120]}
        self.assertAlmostEqual(research.interaction_contrast(cells), -.1823215567939546)
        with self.assertRaises(ValueError):
            research.interaction_contrast({k: v for k, v in cells.items() if k != "11"})
        with self.assertRaises(ValueError):
            research.interaction_contrast({**cells, "11": [60]})
        panel = {"I-AAE": ["baseline", "aae-direct", "aae-incremental", "aae-direct+aae-incremental"]}
        costs = dict(zip(panel["I-AAE"], (cells["00"], cells["10"], cells["01"], cells["11"])))
        groups = {name: {"case": [{"candidate": [value], "baseline": [100]} for value in values]}
                  for name, values in costs.items()}
        report = research.interaction_report(groups, panel)
        self.assertEqual(report["decision"], "EXPLORATORY_ONLY")
        self.assertAlmostEqual(report["panels"]["I-AAE"]["contrasts"][0]["mean_log_interaction"], -.1823215567939546)
        del groups["aae-direct+aae-incremental"]
        self.assertEqual(research.interaction_report(groups, panel)["panels"]["I-AAE"]["status"], "INCOMPLETE")
        self.assertEqual(research.compose_finalist(["aae-direct+aae-incremental", "rank-binary", "aae-direct"]),
                         "aae-direct+aae-incremental+rank-binary")
        with self.assertRaises(ValueError):
            research.compose_finalist(["aae-direct", "aae-buffered"])
        with self.assertRaises(ValueError):
            research.compose_finalist(["aae-direct", "unknown-factor"])

    def test_bounded_bootstrap_matches_independent_retained_reference(self):
        from kaggle_experiments import protocol
        rounds = [{"baseline": [11, 25, 19], "candidate": [9, 19, 16]},
                  {"baseline": [40, 32, 24], "candidate": [41, 30, 23]},
                  {"baseline": [70, 65, 54], "candidate": [58, 57, 47]}]
        expected, _ = protocol.bootstrap_deltas(rounds, [.5, .95, .99], 200, 781, backend="python")
        actual = research.bounded_bootstrap(rounds, [.5, .95, .99], 200, 781, batch_size=7)
        self.assertEqual(actual, expected)
        # A paired translation must remain the same delta under every resampled block grid.
        shifted = [{"baseline": [10, 20], "candidate": [7, 17]},
                   {"baseline": [30, 40], "candidate": [27, 37]}]
        self.assertEqual(research.bounded_bootstrap(shifted, [.5, .99], 20, 9),
                         {.5: [-3] * 20, .99: [-3] * 20})
        # Actual confirmation shape: ties, zero-weight leading groups and endpoint ranks.
        full = [{"baseline": [100 + (i * 13 + b * 19) % 43 for i in range(250)],
                 "candidate": [97 + (i * 11 + b * 17) % 41 for i in range(250)]}
                for b in range(24)]
        quantiles = [0, .5, .95, .99, 1]
        reference, _ = protocol.bootstrap_deltas(full, quantiles, 65, 781, backend="python")
        self.assertEqual(research.bounded_bootstrap(full, quantiles, 65, 781, batch_size=7), reference)
        import math
        for tail in (1e-6, .025, .5):
            intervals = {q: [v[math.floor(64 * tail)], v[math.ceil(64 * (1 - tail))]]
                         for q, v in reference.items()}
            for batch in (7, 64, 256):
                self.assertEqual(research.bootstrap_intervals(full, quantiles, 65, 781, tail, batch_size=batch), intervals)

    def test_bootstrap_intervals_do_not_retain_all_monte_carlo_draws(self):
        import math
        import tracemalloc
        rounds = [{"baseline": [12000.1 + i * 1.1 + b * 13.3 for i in range(16)],
                   "candidate": [12001.7 + i * 1.2 + b * 12.1 for i in range(16)]} for b in range(4)]
        def retained():
            values = research.bounded_bootstrap(rounds, [.99], 30000, 581)[.99]
            return [values[math.floor(29999 * .025)], values[math.ceil(29999 * .975)]]
        research.bootstrap_intervals(rounds, [.99], 2, 581, .025)
        def measure(function):
            tracemalloc.start()
            try:
                value = function()
                return value, tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        reference, full_peak = measure(retained)
        intervals, tail_peak = measure(lambda: research.bootstrap_intervals(rounds, [.99], 30000, 581, .025))
        self.assertEqual(intervals[.99], reference)
        self.assertLess(tail_peak, full_peak / 2, "Retained draw arrays violate the bounded-tail memory requirement")

    def test_evidence_rollover_enforces_real_bytes_before_any_over_budget_write(self):
        with tempfile.TemporaryDirectory(prefix="athena-evidence-") as folder:
            root = Path(folder)
            with research.EvidenceStream(root, total_limit=12, shard_limit=6) as stream:
                stream.write("é\n")
                stream.write("123\n")
                stream.flush()
                self.assertEqual(stream.total_bytes, 7)
                with self.assertRaises(ValueError):
                    stream.write("12345\n")
                self.assertEqual(stream.total_bytes, 7)
            shards = sorted(root.glob("samples-*.jsonl"))
            self.assertEqual([p.read_bytes() for p in shards], [b"\xc3\xa9\n", b"123\n"])
            try:
                with self.assertRaises(ValueError):
                    stream.write("x\n")
            finally:
                stream.close()


    def confirmation_analysis_fixture(self, root):
        import hashlib
        snapshots = {}
        for arm in ("baseline", "v"):
            binary = root / "bins" / arm / "core_probe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"synthetic analyzer identity; not executable timing evidence")
            snapshots[arm] = {"core_probe": hashlib.sha256(binary.read_bytes()).hexdigest()}
        design = {"protocol": research.PROTOCOL, "source_sha256": "a" * 64,
                  "confirm_binaries": snapshots,
                  "confirm": {"blocks": 20, "iterations": 2, "alpha": .05,
                              "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"]},
                  "holdout_kind": "independent_seed"}
        fixtures = {"screen": [{"id": "s", "sha256": "b" * 64, "lineage": "template"}],
                    "confirm": [{"id": case, "sha256": digest * 64, "lineage": "template"}
                                for case, digest in (("fast", "c"), ("adverse", "d"))]}
        design["fixtures"] = fixtures
        design["study_id"] = research.identity(design)
        frozen = research.freeze_confirmation(design, ["v"],
            {"status": "COMPLETE", "phase": "pilot", "wall_seconds": 1, "admitted": True,
             "precision_status": "PLANNED_NOT_GUARANTEED"}, fixtures)
        manifest = {"protocol": research.PROTOCOL, "bundle_id": design["study_id"], "spec": design,
                    "sample_shards": ["samples-0000.jsonl"]}
        (root / "manifest.json").write_bytes(research.canonical(manifest))
        (root / "confirmation-freeze.json").write_bytes(research.canonical(frozen))
        rows = []
        for case, baseline, candidate, peak in (("fast", 100, 80, 900), ("adverse", 200, 240, 1100)):
            for block in range(20):
                for index, (arm, ns) in enumerate((("baseline", baseline), ("candidate", candidate))):
                    common = {"protocol": research.PROTOCOL, "study_id": design["study_id"],
                              "freeze_id": frozen["freeze_id"], "variant": "v", "case": case,
                              "phase": "confirm", "block": block, "arm": arm, "order": index ^ (block % 2)}
                    rows.extend({**common, "iteration": i, "ns": ns, "offset_ns": i * 400,
                                 "fingerprint": "e" * 64} for i in range(2))
                    rows.append({**common, "kind": "rss", "unit": "KiB", "value": 1000 if index == 0 else peak})
        raw = root / "samples-0000.jsonl"
        raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return raw

    def test_confirmation_analysis_resumes_after_kill_without_reusing_stale_or_partial_results(self):
        import time
        script = '''import sys,time
from pathlib import Path
from cloud_experiments import research
mode,folder,output,marker=sys.argv[1:]
original=research.bootstrap_intervals
calls=0
def controlled(rounds,*args,**kwargs):
    global calls
    calls+=1
    if mode=="interrupt" and calls==3:
        Path(marker).write_bytes(b"second case pending")
        time.sleep(120)
    if mode=="resume" and rounds[0]["baseline"][0]==100:
        raise RuntimeError("Completed case was recomputed after restart")
    return original(rounds,*args,**kwargs)
research.bootstrap_intervals=controlled
sys.argv=["analysis","analyze",folder,"v",output]
raise SystemExit(research.main())
'''
        with tempfile.TemporaryDirectory(prefix="athena-analysis-recovery-") as folder:
            root = Path(folder)
            raw = self.confirmation_analysis_fixture(root)
            output, marker = root / "report.json", root / "pending.marker"
            progress = root / "report.analysis-progress.json"
            command = [sys.executable, "-B", "-c", script, "interrupt", folder, str(output), str(marker)]
            process = subprocess.Popen(command, cwd=campaign.ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 20
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Child did not reach the controlled analysis boundary")
                self.assertTrue(progress.is_file(), "Completed analysis was not checkpointed before the next case")
                saved = json.loads(progress.read_bytes())
                self.assertEqual(saved["completed_cases"], ["fast"])
                self.assertEqual(saved["status"], "RUNNING")
                self.assertFalse(output.exists(), "Interrupted analysis published a complete report")
                contender = subprocess.run([*command[:4], "fresh", *command[5:]], cwd=campaign.ROOT,
                                           capture_output=True, text=True, timeout=15)
                self.assertNotEqual(contender.returncode, 0)
                self.assertIn("analysis already active", contender.stderr.lower())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, "The recovery probe must exercise an actual killed process")
            checkpoint = progress.read_bytes()
            raw_bytes = raw.read_bytes()
            for challenge in ("cached-result", "changed-raw"):
                if challenge == "cached-result":
                    changed = json.loads(checkpoint)
                    changed["results"]["fast"]["cases"]["fast"]["delta_intervals"]["0.5"] = [-999, -999]
                    progress.write_bytes(research.canonical(changed))
                else:
                    raw.write_bytes(raw_bytes.replace(b'"ns": 100', b'"ns": 101', 1))
                rejected = subprocess.run([*command[:4], "fresh", *command[5:]], cwd=campaign.ROOT,
                                          capture_output=True, text=True, timeout=15)
                with self.subTest(challenge=challenge):
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("checkpoint", rejected.stderr.lower())
                    self.assertFalse(output.exists())
                progress.write_bytes(checkpoint)
                raw.write_bytes(raw_bytes)
            resumed = subprocess.run([*command[:4], "resume", *command[5:]], cwd=campaign.ROOT,
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            actual = json.loads(output.read_bytes())
            groups, rss, evidence = research.load_confirmation(root, "v")
            expected = research.analyze_groups(groups, rss, family=evidence["family"], draws=evidence["draws"],
                                               alpha=evidence["alpha"], seed=938201)
            expected["evidence"] = evidence
            context = actual.pop("analysis_context")
            self.assertEqual(context["evidence"], evidence)
            self.assertEqual(context["case_order"], ["fast", "adverse"])
            self.assertEqual(context["sources"]["research.py"],
                             research.protocol.sha256_file(research.__file__))
            self.assertEqual(actual, expected)
            self.assertEqual(actual["cases"]["fast"]["delta_intervals"]["0.5"], [-20, -20])
            self.assertEqual(actual["cases"]["adverse"]["delta_intervals"]["0.99"], [40, 40])
            self.assertEqual(actual["performance_decision"], "REJECT")
            self.assertEqual(actual["overall_decision"], "HOLD")
            final = json.loads(progress.read_bytes())
            self.assertEqual(final["status"], "COMPLETE")
            self.assertEqual(final["completed_cases"], ["fast", "adverse"])
            denied = subprocess.run([sys.executable, "-B", "-m", "cloud_experiments.research", "analyze",
                                     folder, "v", str(raw)], cwd=campaign.ROOT, capture_output=True,
                                    text=True, timeout=15)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("replace required evidence", denied.stderr)
            self.assertEqual(raw.read_bytes(), raw_bytes)

    def test_confirmation_identity_streams_the_shard_without_a_whole_file_copy(self):
        import hashlib
        import tracemalloc
        with tempfile.TemporaryDirectory(prefix="athena-shard-memory-") as folder:
            root = Path(folder)
            raw = self.confirmation_analysis_fixture(root)
            base = raw.read_bytes()
            manifest = json.loads((root / "manifest.json").read_bytes())
            padding = (json.dumps({"protocol": research.PROTOCOL, "study_id": manifest["bundle_id"],
                                   "variant": "baseline", "phase": "screen", "padding": "x" * 60000})
                       + "\n").encode()
            digest = hashlib.sha256(base)
            with raw.open("ab") as stream:
                for _ in range(512):
                    stream.write(padding)
                    digest.update(padding)
            tracemalloc.start()
            try:
                groups, rss, evidence = research.load_confirmation(root, "v")
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(evidence["raw_inputs"][raw.name], digest.hexdigest())
            self.assertEqual(groups["fast"][0], {"baseline": [100, 100], "candidate": [80, 80]})
            self.assertEqual(rss["adverse"][0], {"baseline": 1000, "candidate": 1100})
            self.assertLess(peak, 8 * 1024 ** 2,
                            "Raw identity must not allocate a complete shard after bounded line parsing")


if __name__ == "__main__":
    unittest.main()
