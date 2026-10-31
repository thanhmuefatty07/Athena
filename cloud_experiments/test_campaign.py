"""Cloud campaign controls: evidence pairing, selection and source isolation."""
import copy
import hashlib
import json
import math
import os
import shutil
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
    def test_dependency_advisory_query_respects_expired_campaign_deadline(self):
        from io import BytesIO
        from unittest.mock import patch
        from cloud_experiments import http_transport

        class Run:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {}
                self.deadline = time.monotonic() - 1

            def save(self):
                pass

        graph = {"lock_sha256": "c" * 64, "packages": {"fixture": {
            "name": "fixture", "version": "1.2.3",
            "source": "registry+https://github.com/rust-lang/crates.io-index"}}}
        with tempfile.TemporaryDirectory(prefix="athena-advisory-deadline-") as folder:
            run = Run(Path(folder))
            with patch.object(campaign, "_open_dependency_advisory_request",
                              return_value=BytesIO(b'{"results":[{"vulns":[]}]}'), create=True) as direct, \
                    patch.object(http_transport, "bounded_request") as bounded:
                status = campaign.dependency_advisories(run, graph)
            self.assertEqual(status, "NOT VERIFIED")
            direct.assert_not_called()
            bounded.assert_not_called()

    def test_dependency_advisory_non_success_status_is_not_recorded_as_queried(self):
        from unittest.mock import patch
        from cloud_experiments import http_transport

        class Run:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {}

            def save(self):
                pass

        lock_hash = "d" * 64
        graph = {"lock_sha256": lock_hash, "packages": {"fixture": {
            "name": "fixture", "version": "1.2.3",
            "source": "registry+https://github.com/rust-lang/crates.io-index"}}}
        with tempfile.TemporaryDirectory(prefix="athena-advisory-status-") as folder:
            run = Run(Path(folder))
            with patch.object(http_transport, "bounded_request", return_value=(429, {}, b"rate limited")):
                status = campaign.dependency_advisories(run, graph)
            self.assertEqual(status, "NOT VERIFIED")
            report = json.loads((run.directory / f"dependency-advisories-{lock_hash}.json").read_text())
            self.assertEqual(report["status"], "NOT VERIFIED")
            self.assertIn("HTTP status 429", report["error"])

    def test_dependency_advisories_are_bound_to_each_lock_profile_and_reused(self):
        from unittest.mock import patch
        from cloud_experiments import http_transport

        class Run:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {}
                self.deadline = time.monotonic() + 5

            def save(self):
                pass

        def graph(lock_hash, package):
            return {"lock_sha256": lock_hash, "packages": {package: {
                "name": package, "version": "1.2.3", "source": "registry+https://github.com/rust-lang/crates.io-index"}}}

        with tempfile.TemporaryDirectory(prefix="athena-advisory-profiles-") as folder:
            run = Run(Path(folder))
            graphs = [graph("a" * 64, "fixture-clean"), graph("b" * 64, "fixture-vulnerable")]

            def reply(method, url, headers, body, timeout_s, max_bytes):
                self.assertEqual(method, "POST")
                self.assertEqual(url, "https://api.osv.dev/v1/querybatch")
                self.assertEqual(headers, {"Content-Type": "application/json"})
                self.assertGreater(timeout_s, 0)
                self.assertLessEqual(timeout_s, 5)
                self.assertEqual(max_bytes, 4 * 1024 * 1024)
                query = json.loads(body)["queries"][0]["package"]["name"]
                vulns = [{"id": "RUSTSEC-2026-0382"}] if query == "fixture-vulnerable" else []
                return 200, {}, json.dumps({"results": [{"vulns": vulns}]}).encode()

            with patch.object(http_transport, "bounded_request", side_effect=reply) as opened:
                for item in graphs:
                    status = campaign.dependency_advisories(run, item)
                    self.assertEqual(status, "QUERIED")
                    path = run.directory / ("dependency-advisories-" + item["lock_sha256"] + ".json")
                    self.assertTrue(path.is_file(), "Each distinct lock profile needs its own advisory report")
                    report = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(report["lock_sha256"], item["lock_sha256"])
                campaign.dependency_advisories(run, graphs[0])

            self.assertEqual(opened.call_count, 2, "A profile already queried in this run must not be queried again")
            self.assertEqual(run.manifest["dependency_advisories_by_lock"]["a" * 64]["vulnerable_package_count"], 0)
            self.assertEqual(run.manifest["dependency_advisories_by_lock"]["b" * 64]["vulnerable_package_count"], 1)
            cached = run.directory / ("dependency-advisories-" + "a" * 64 + ".json")
            ambiguous = cached.read_text(encoding="utf-8").replace(
                '"status": "QUERIED"', '"status": "INVALID", "status": "QUERIED"', 1)
            cached.write_text(ambiguous, encoding="utf-8")
            run.manifest["dependency_advisories_by_lock"]["a" * 64]["sha256"] = campaign.protocol.sha256_file(cached)
            with self.assertRaisesRegex(ValueError, "Duplicate JSON object member"):
                campaign.dependency_advisories(run, graphs[0])

    def test_dependency_advisory_http_redirect_cannot_reach_a_second_origin(self):
        import base64
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        from unittest.mock import patch
        from cloud_experiments import http_transport

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
            url = f"http://127.0.0.1:{server.server_port}/start"
            payload = {"method": "POST", "url": url, "headers": {"Content-Type": "application/json"},
                       "body": base64.b64encode(b"synthetic package query").decode("ascii"),
                       "timeout_s": 2, "max_bytes": 1024}
            with patch.object(http_transport, "_allowed_endpoint",
                              side_effect=lambda method, candidate: method == "POST" and candidate == url):
                reply = http_transport._exchange(payload)
            self.assertEqual(reply["status"], 302, "The redirect must be surfaced, not followed")
            self.assertEqual(received, [("POST", "/start")],
                             "A redirect must be rejected before a second request reaches its target")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

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
            exact = root / "exact-limit.bin"
            with exact.open("wb") as stream:
                stream.truncate(8 * 1024 ** 2)
            collected = campaign.source_files(root, ["exact-limit.bin"])
            self.assertEqual(len(collected["exact-limit.bin"]), 8 * 1024 ** 2)
            with exact.open("wb") as stream:
                stream.truncate(8 * 1024 ** 2 + 1)
            with self.assertRaisesRegex(ValueError, "Oversized source input"):
                campaign.source_files(root, ["exact-limit.bin"])

    def test_source_collector_enforces_aggregate_cap_before_reading_next_file(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="athena-source-total-cap-") as folder:
            root = Path(folder)
            names = ("a.rs", "b.rs", "c.rs")
            for name in names:
                with (root / name).open("wb") as stream:
                    stream.truncate(8 * 1024 ** 2)

            opened = []
            original_open = Path.open

            def track_open(path, *args, **kwargs):
                if path.parent == root:
                    opened.append(path.name)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", track_open):
                with self.assertRaisesRegex(ValueError, "Source bundle exceeds budget"):
                    campaign.source_files(root, names)

            self.assertEqual(opened, ["a.rs", "b.rs"],
                             "The collector opened another source file after exhausting its aggregate byte budget")

    def test_source_snapshot_normalizes_text_line_endings_for_cross_platform_identity(self):
        with tempfile.TemporaryDirectory(prefix="athena-source-eol-") as folder:
            root = Path(folder)
            (root / "crlf.rs").write_bytes(b"fn main() {}\r\n")
            (root / "lf.rs").write_bytes(b"fn main() {}\n")
            fixture = root / "tests" / "fixtures" / "wpt-html5" / "line-endings.dat"
            fixture.parent.mkdir(parents=True)
            fixture_bytes = b"#data\r\nalpha\r\nbeta\r#errors\r\n"
            fixture.write_bytes(fixture_bytes)
            snapshots = campaign.source_files(root, ["crlf.rs", "lf.rs", "tests/fixtures"])
            self.assertEqual(snapshots["crlf.rs"], snapshots["lf.rs"])
            self.assertEqual(snapshots["crlf.rs"], b"fn main() {}\n")
            self.assertEqual(snapshots["tests/fixtures/wpt-html5/line-endings.dat"], fixture_bytes,
                             "Data fixtures must retain CRLF and bare-CR bytes as part of the frozen input")
            frozen = {"protocol": research.PROTOCOL,
                      "files": {name: hashlib.sha256(content).hexdigest()
                                for name, content in snapshots.items() if name.startswith("tests/fixtures/")}}
            frozen["study_id"] = research.identity(frozen)
            restored = research.verify_snapshot(root, frozen)
            self.assertEqual(restored["tests/fixtures/wpt-html5/line-endings.dat"], fixture_bytes)
            fixture.write_bytes(fixture_bytes.replace(b"\r\n", b"\n"))
            with self.assertRaisesRegex(ValueError, "Frozen input"):
                research.verify_snapshot(root, frozen)

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

    def test_source_snapshot_excludes_build_and_bytecode_caches_before_read(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="athena-source-target-") as folder:
            root = Path(folder)
            source = root / "cloud_experiments/lexbor_adapter/src/lib.rs"
            source.parent.mkdir(parents=True)
            source.write_text("pub fn source() {}\n", encoding="utf-8")
            bytecode = root / "cloud_experiments/__pycache__/stale.cpython-311.pyc"
            bytecode.parent.mkdir(parents=True)
            bytecode.write_bytes(b"generated bytecode that is not a source input")
            generated = root / "cloud_experiments/lexbor_adapter/target/debug/generated.bin"
            generated.parent.mkdir(parents=True)
            with generated.open("wb") as stream:
                stream.truncate(9 * 1024 * 1024)

            opened_bytecode = []
            original_open = Path.open

            def track_bytecode_open(path, *args, **kwargs):
                if path == bytecode:
                    opened_bytecode.append(path)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", track_bytecode_open):
                snapshot = campaign.source_files(root, ["cloud_experiments"])

            self.assertEqual(opened_bytecode, [], "Generated bytecode must be pruned before reading source inputs")
            self.assertEqual(set(snapshot), {"cloud_experiments/lexbor_adapter/src/lib.rs"})


class DecisionMapControls(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder)
        (root / "src").mkdir()
        for name in ("lib.rs", "main.rs", "core.rs"):
            (root / "src" / name).write_text("", encoding="utf-8")
        (root / "Cargo.toml").write_text('[dependencies]\nbytes = "1"\n', encoding="utf-8")
        research = root / "cloud_experiments"
        research.mkdir()
        library_decisions = {
            "schema": "athena-library-research/1",
            "status": "RESEARCH_NOT_ADOPTION",
            "libraries": {"bytes": {
                "consumer": ["src/core.rs"],
                "purpose": "Owned immutable byte storage",
                "alternatives": ["locked Bytes", "Vec<u8> at the ownership boundary"],
                "gate": "Exact bytes and lifetime behavior",
                "source": "https://docs.rs/bytes",
            }},
        }
        (research / "library_decisions.json").write_text(
            json.dumps(library_decisions), encoding="utf-8")
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

    def test_library_registry_must_cover_current_dependencies_exactly(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            with (root / "Cargo.toml").open("a", encoding="utf-8") as stream:
                stream.write('url = "2"\n')
            spec["decisions"][0]["dependencies"].append("url")
            registry_path = root / "cloud_experiments" / "library_decisions.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["libraries"]["ghost"] = copy.deepcopy(registry["libraries"]["bytes"])
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = campaign.audit_decision_map(root, spec)

            self.assertEqual(result["uncovered_dependencies"], [])
            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            issues = {(item["code"], item["id"]) for item in result["issues"]}
            self.assertIn(("missing_library_decision", "url"), issues)
            self.assertIn(("stale_library_decision", "ghost"), issues)

    def test_h09_inventory_decision_must_cover_every_direct_dependency(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            with (root / "Cargo.toml").open("a", encoding="utf-8") as stream:
                stream.write('url = "2"\n')
            spec["decisions"][0]["dependencies"].append("url")
            spec["decisions"].append({
                "id": "H09", "owners": ["Cargo.toml"], "dependencies": ["bytes"],
                "question": "Does the inventory decision cover each direct dependency?",
                "alternatives": ["current inventory", "complete direct inventory"],
                "oracle": "H09 dependencies exactly match Cargo.toml",
                "closure": "Each direct dependency has an explicit inventory decision",
                "gaps": [],
            })
            spec["language_panels"] = [
                {"id": f"L{i:02}", "admission": "REQUIRED" if i <= 3 else "CONDITIONAL",
                 "status": "NOT_IMPLEMENTED" if i <= 3 else "NOT_ADMITTED",
                 "gate": "Panel admission gate", "evidence_basis": "Fixture declaration",
                 "owner_decisions": ["H09"], "interactions": []}
                for i in range(1, 7)
            ]
            registry_path = root / "cloud_experiments" / "library_decisions.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["libraries"]["url"] = copy.deepcopy(registry["libraries"]["bytes"])
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = campaign.audit_decision_map(root, spec)

            self.assertEqual(result["uncovered_dependencies"], [])
            self.assertEqual(result["uncovered_library_dependencies"], [])
            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            self.assertIn(("missing_inventory_dependency", "H09:url"),
                          {(item["code"], item["id"]) for item in result["issues"]})

    def test_library_decision_requires_a_consumer_gate(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            registry_path = root / "cloud_experiments" / "library_decisions.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["libraries"]["bytes"]["gate"] = ""
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            result = campaign.audit_decision_map(root, spec)

            issues = {(item["code"], item["id"]) for item in result["issues"]}
            self.assertIn(("invalid_library_decision", "bytes"), issues)

    def test_missing_library_registry_cannot_be_reported_as_mapped(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            (root / "cloud_experiments" / "library_decisions.json").unlink()

            result = campaign.audit_decision_map(root, spec)

            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            self.assertIn(("missing_library_registry", "cloud_experiments/library_decisions.json"),
                          {(item["code"], item["id"]) for item in result["issues"]})

    def test_valid_design_preserves_unknowns_and_never_attests_execution(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            spec["decisions"][0]["harness"] = ["cloud_experiments"]
            result = campaign.audit_decision_map(root, spec)
            self.assertEqual(result["mapping_status"], "MAPPED")
            self.assertEqual(result["decision_sufficiency"], "NOT_EVALUATED")
            self.assertEqual(result["experiment_execution"], "NOT_EVALUATED")
            self.assertEqual({item["id"] for item in result["unresolved"]}, {"storage", "consumer"})

    def test_declared_decision_harness_must_resolve_inside_source_root(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            missing = "cloud_experiments/missing-harness.py"
            spec["decisions"][0]["harness"] = [missing]

            result = campaign.audit_decision_map(root, spec)

            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            self.assertIn(("missing_harness_path", "storage:" + missing),
                          {(item["code"], item["id"]) for item in result["issues"]})

            for unsafe in ("../outside.py", "/outside.py", "C:/outside.py"):
                with self.subTest(harness=unsafe):
                    spec["decisions"][0]["harness"] = [unsafe]
                    result = campaign.audit_decision_map(root, spec)
                    self.assertEqual(result["mapping_status"], "INCOMPLETE")
                    self.assertIn(("unsafe_harness_path", "storage:" + unsafe),
                                  {(item["code"], item["id"]) for item in result["issues"]})

    def test_declared_language_panel_harness_must_exist(self):
        with tempfile.TemporaryDirectory(prefix="athena-decision-audit-") as folder:
            root, spec = self.fixture(folder)
            spec["decisions"].append({
                "id": "H09", "owners": ["Cargo.toml"], "dependencies": ["bytes"],
                "question": "Does the inventory decision cover each direct dependency?",
                "alternatives": ["current inventory", "complete direct inventory"],
                "oracle": "H09 dependencies exactly match Cargo.toml",
                "closure": "Each direct dependency has an explicit inventory decision",
                "gaps": [],
            })
            spec["language_panels"] = [
                {"id": f"L{i:02}", "admission": "REQUIRED" if i <= 3 else "CONDITIONAL",
                 "status": "NOT_IMPLEMENTED" if i <= 3 else "NOT_ADMITTED",
                 "gate": "Panel admission gate", "evidence_basis": "Fixture declaration",
                 "owner_decisions": ["H09"], "interactions": []}
                for i in range(1, 7)
            ]
            spec["language_panels"][0]["harness"] = ["cloud_experiments/missing-panel-harness"]

            result = campaign.audit_decision_map(root, spec)

            self.assertEqual(result["mapping_status"], "INCOMPLETE")
            self.assertIn(("missing_harness_path", "L01:cloud_experiments/missing-panel-harness"),
                          {(item["code"], item["id"]) for item in result["issues"]})

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
        native_packet = copy.deepcopy(table["consumer_packets"][0])
        native_packet["table_cells"] = [
            {"locator": "table[0]/row[2]/cell[1]", "row": "North", "column": "Net",
             "group": "Mass", "value": "12.5", "unit": "kg"},
            {"locator": "table[0]/row[2]/cell[2]", "row": "North", "column": "Gross",
             "group": "Mass", "value": "12.7", "unit": "kg"},
        ]
        satisfied = agent_lab.score_answer(cards["F04-screen"], table["answer"], table["actions"],
                                           [native_packet], table["visited"])
        self.assertTrue(satisfied["contract_satisfied"])
        self.assertEqual(satisfied["table_contract_status"], "SATISFIED")
        leaked_nested = copy.deepcopy(native_packet)
        leaked_nested["table_cells"].append(
            {"locator": "table[1]/row[0]/cell[0]", "row": "North", "column": "Gross",
             "group": "Mass", "value": "999", "unit": "kg"}
        )
        failed = agent_lab.score_answer(cards["F04-screen"], table["answer"], table["actions"],
                                        [leaked_nested], table["visited"])
        self.assertFalse(failed["contract_satisfied"])
        self.assertEqual(failed["table_contract_status"], "FAILED")
        exe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
        probe_override = os.environ.get("ATHENA_CORE_PROBE_BIN", "").strip()
        target_root = Path(__file__).resolve().parent.parent / "target"
        probe_candidates = [
            path for path in (
                ([Path(probe_override)] if probe_override else [])
                + [target_root / "release" / exe_name, target_root / "debug" / exe_name]
            )
            if path.is_file()
        ]
        probe_bin = max(probe_candidates, key=lambda path: path.stat().st_mtime, default=None)
        if probe_bin is not None:
            rust_producer = agent_lab.RustProbeProducer(probe_bin)
            for rep in ("full", "compact"):
                f01_res = agent_lab.run_task(cards["F01-screen"], "W0", rep, producer=rust_producer)
                self.assertTrue(f01_res["quality"]["contract_satisfied"])
                self.assertEqual(f01_res["actions"][0]["producer"], "CONFIGURED_NATIVE_AAE_PRODUCER")
                self.assertGreater(f01_res["actions"][0]["producer_bytes"], 0)
                f04_res = agent_lab.run_task(cards["F04-screen"], "W0", rep, producer=rust_producer)
                self.assertTrue(f04_res["quality"]["contract_satisfied"])
                self.assertEqual(f04_res["quality"]["table_contract_status"], "SATISFIED")
                self.assertEqual(
                    f04_res["consumer_packets"][-1]["table_cells"],
                    native_packet["table_cells"],
                )
            with self.assertRaisesRegex(agent_lab.LabError, "SCOPED_TASK_DENIED"):
                rust_producer.search_task(cards["F01-screen"], "wrong query")
            with self.assertRaisesRegex(agent_lab.LabError, "SCOPED_TASK_DENIED"):
                rust_producer.search_task({**cards["F01-screen"], "tenant": "tenant-b"}, cards["F01-screen"]["request"]["query"])
            with self.assertRaisesRegex(agent_lab.LabError, "SCOPED_TASK_DENIED"):
                rust_producer.search_task(cards["F08-screen"], "catalog mass and capacity")
            f04_doc = cards["F04-screen"]["documents"][0]
            valid_handle = rust_producer.mint_handle(
                {
                    "task_id": "F04-screen",
                    "tenant": cards["F04-screen"]["tenant"],
                    "source_id": f04_doc["id"],
                    "revision": f04_doc["revision"],
                }
            )
            self.assertTrue(valid_handle.startswith("ev_"))
            with self.assertRaisesRegex(agent_lab.LabError, "SCOPED_TASK_DENIED"):
                rust_producer.expand_handle(
                    f04_doc, f04_doc["revision"], valid_handle + "0", task=cards["F04-screen"]
                )
            with self.assertRaisesRegex(agent_lab.LabError, "SCOPED_TASK_DENIED"):
                rust_producer.expand_handle(
                    f04_doc,
                    f04_doc["revision"],
                    valid_handle,
                    task={**cards["F04-screen"], "tenant": "tenant-b"},
                )

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

    def test_nim_rate_lock_is_shared_across_processes_until_transport_finishes(self):
        from cloud_experiments.nim import NimClient, NimConfig

        child_script = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cloud_experiments import agent_lab
from cloud_experiments.nim import NimClient, NimConfig
mode = sys.argv[5]
if mode == "bypass":
    from contextlib import nullcontext
    import cloud_experiments.research as research
    research._analysis_lock = lambda path: nullcontext()
def transport(method, url, headers, body, timeout_s, max_bytes):
    Path(sys.argv[3]).write_text("dispatched", encoding="utf-8")
    return agent_lab.HttpReply(200, {}, b'{"data":[]}')
client = NimClient(NimConfig(model="vendor/model", api_key="synthetic-lock-test-key",
    rate_file=sys.argv[2], allow_live=True, trial_without_billing_confirmed=True,
    request_cap=5), transport=transport, clock=lambda: float(sys.argv[4]))
try:
    client._call("catalog", "GET", "models", timeout_s=5)
except agent_lab.LabError as error:
    print(error.code)
else:
    print("DISPATCHED")
"""

        with tempfile.TemporaryDirectory(prefix="athena-nim-process-lock-") as folder:
            root = Path(folder)
            rate_file = root / "shared-rate.json"
            credential = "synthetic-lock-test-key"
            entered_transport = threading.Event()
            release_transport = threading.Event()
            parent_outcomes = []

            def blocking_transport(method, url, headers, body, timeout_s, max_bytes):
                entered_transport.set()
                if not release_transport.wait(8):
                    raise TimeoutError("test did not release the first synthetic transport")
                return agent_lab.HttpReply(200, {}, b'{"data":[]}')

            client = NimClient(NimConfig(model="vendor/model", api_key=credential,
                rate_file=str(rate_file), allow_live=True, trial_without_billing_confirmed=True,
                request_cap=5), transport=blocking_transport, clock=lambda: 1000.0)

            def first_request():
                try:
                    client._call("catalog", "GET", "models", timeout_s=5)
                    parent_outcomes.append(("ok", None))
                except BaseException as error:
                    parent_outcomes.append(("error", error))

            worker = threading.Thread(target=first_request, daemon=True)

            def run_child(mode, now, marker):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", child_script,
                     str(campaign.ROOT), str(rate_file), str(marker), str(now), mode],
                    cwd=campaign.ROOT, capture_output=True, text=True, encoding="utf-8", timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                return result.stdout.strip()

            worker.start()
            try:
                self.assertTrue(entered_transport.wait(8),
                                "first request must hold the credential lock inside transport")

                # Controlled mutant: if the cross-process lock is absent, a request at t=1003
                # is otherwise rate-admissible and must reach the synthetic transport.
                mutant_marker = root / "mutant-dispatched.txt"
                self.assertEqual(run_child("bypass", 1003.0, mutant_marker), "DISPATCHED")
                self.assertEqual(mutant_marker.read_text(encoding="utf-8"), "dispatched")

                # The same admissible request must fail closed while the real lock is held.
                blocked_marker = root / "blocked-dispatched.txt"
                self.assertEqual(run_child("locked", 1006.0, blocked_marker),
                                 "RATE_GATE_OR_TRANSPORT_ERROR")
                self.assertFalse(blocked_marker.exists(),
                                 "a lock collision must not reach provider transport")
            finally:
                release_transport.set()
                worker.join(timeout=8)

            self.assertFalse(worker.is_alive(), "first request thread must be cleaned up")
            self.assertEqual(len(parent_outcomes), 1)
            self.assertEqual(parent_outcomes[0][0], "ok", repr(parent_outcomes[0][1]))

            # Positive control after release: normal admission resumes when the owner exits.
            resumed_marker = root / "resumed-dispatched.txt"
            self.assertEqual(run_child("locked", 1008.0, resumed_marker), "DISPATCHED")
            self.assertEqual(resumed_marker.read_text(encoding="utf-8"), "dispatched")

    def test_expired_model_task_deadline_prevents_provider_preflight_calls(self):
        from datetime import datetime, timezone
        from cloud_experiments.nim import NimClient, NimConfig

        task = agent_lab.tasks()[0]
        clients = []
        with tempfile.TemporaryDirectory(prefix="athena-provider-deadline-") as folder:
            root = Path(folder)
            now = datetime.now(timezone.utc).date().isoformat()
            quota = {"utc_date": now, "remaining_requests": 10, "source": "synthetic test allocation"}

            def transport(method, url, headers, body, timeout_s, max_bytes):
                clients[-1][1].append(url)
                if url.endswith("/models") or "/models?" in url:
                    model = {"id": clients[-1][0]}
                    if model["id"].endswith(":free"):
                        model.update(pricing={"prompt": "0", "completion": "0"},
                                     context_length=8192, supported_parameters=["tools"])
                    payload = {"data": [model]}
                elif url.endswith("/key"):
                    payload = {"data": {"is_free_tier": True, "limit_remaining": None}}
                else:
                    self.fail("Expired model task must not dispatch inference")
                return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())

            configs = (
                ("nim", lambda index: NimClient(NimConfig(model="vendor/model", api_key="synthetic-key",
                    rate_file=str(root / f"nim-{index}.json"), allow_live=True,
                    trial_without_billing_confirmed=True), transport=transport,
                    clock=lambda: time.time(), sleep=lambda _: None)),
                ("openrouter", lambda _: agent_lab.OpenRouterClient(agent_lab.OpenRouterConfig(
                    model="vendor/model:free", api_key="synthetic-key", quota=quota,
                    allow_live=True, operator_no_charge_acknowledged=True), transport=transport)),
            )
            for index, (provider, create_client) in enumerate(configs):
                with self.subTest(provider=provider):
                    calls = []
                    clients.append(("vendor/model" if provider == "nim" else "vendor/model:free", calls))
                    client = create_client(index)
                    primitives = agent_lab.ReplayPrimitives(task)
                    primitives.started -= primitives.limits["max_wall_seconds"] + 1
                    result = {"boundary_gaps": [], "model_requests": [], "model_responses": []}
                    try:
                        with self.assertRaisesRegex(agent_lab.LabError, "TASK_DEADLINE"):
                            agent_lab._model_loop(primitives, client, result)
                        self.assertEqual(calls, [], "Expired tasks must not spend provider requests on preflight")
                        self.assertEqual(client.events, [])
                        self.assertEqual(client.requests_used, 0)
                    finally:
                        primitives.transport.close()

    def test_nim_rate_state_write_expiring_deadline_prevents_provider_io(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import patch
        from cloud_experiments import nim
        from cloud_experiments.nim import NimClient, NimConfig

        with tempfile.TemporaryDirectory(prefix="athena-nim-deadline-write-") as folder:
            rate_file = Path(folder) / "rate.json"
            clock = [0.0]
            calls = []

            def transport(*args):
                calls.append(args)
                return agent_lab.HttpReply(200, {}, b'{"data":[]}')

            client = NimClient(NimConfig(model="vendor/model", api_key="synthetic-key",
                rate_file=str(rate_file), allow_live=True, trial_without_billing_confirmed=True),
                transport=transport, clock=lambda: 100.0, sleep=lambda _: None)
            write_json = campaign.write_json

            def slow_write(path, value, *, max_bytes=None):
                write_json(path, value, max_bytes=max_bytes)
                clock[0] = 0.6

            with patch.object(nim, "time", SimpleNamespace(monotonic=lambda: clock[0])):
                with patch("cloud_experiments.research._analysis_lock", return_value=nullcontext()):
                    with patch.object(campaign, "write_json", side_effect=slow_write):
                        with self.assertRaisesRegex(agent_lab.LabError, "RATE_ADMISSION_EXCEEDS_DEADLINE"):
                            client._call("catalog", "GET", "models", timeout_s=0.5)

            self.assertEqual(calls, [], "Expired request budget must stop before provider transport")
            self.assertEqual(client.events, [])
            self.assertEqual(client.requests_used, 0)
            self.assertEqual(len(json.loads(rate_file.read_text())["attempts"]), 1,
                             "A persisted admission reservation remains conservatively charged")

    def test_complete_shares_deadline_with_provider_revalidation(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import patch
        from cloud_experiments import nim
        from cloud_experiments.nim import NimClient, NimConfig

        with tempfile.TemporaryDirectory(prefix="athena-complete-deadline-") as folder:
            root = Path(folder)
            quota = {"utc_date": datetime.now(timezone.utc).date().isoformat(),
                     "remaining_requests": 3, "source": "synthetic test allocation"}
            for provider in ("openrouter", "nim"):
                with self.subTest(provider=provider):
                    monotonic = [0.0]
                    wall_clock = [1000.0]
                    calls, timeouts = [], []

                    def transport(method, url, headers, body, timeout_s, max_bytes):
                        calls.append(url)
                        timeouts.append(timeout_s)
                        if "/models" in url:
                            if provider == "openrouter":
                                payload = {"data": [{"id": "vendor/model:free",
                                    "pricing": {"prompt": "0", "completion": "0"},
                                    "context_length": 8192, "supported_parameters": ["tools"]}]}
                                monotonic[0] += 3.0
                            else:
                                payload = {"data": [{"id": "vendor/model"}]}
                                monotonic[0] += 5.1
                        elif url.endswith("/key"):
                            payload = {"data": {"is_free_tier": True, "limit_remaining": None}}
                            monotonic[0] += 3.0
                        elif url.endswith("/chat/completions"):
                            payload = {"model": "vendor/model:free" if provider == "openrouter" else "vendor/model",
                                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                          "total_tokens": 2, "cost": "0"}}
                        else:
                            self.fail("Unexpected provider request")
                        return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())

                    if provider == "openrouter":
                        client = agent_lab.OpenRouterClient(agent_lab.OpenRouterConfig(
                            model="vendor/model:free", api_key="synthetic-key", quota=quota,
                            allow_live=True, operator_no_charge_acknowledged=True,
                            request_cap=3, timeout_s=30, min_interval_s=3), transport=transport)
                    else:
                        client = NimClient(NimConfig(model="vendor/model", api_key="synthetic-key",
                            rate_file=str(root / "nim-rate.json"), allow_live=True,
                            trial_without_billing_confirmed=True, request_cap=3, timeout_s=30),
                            transport=transport, clock=lambda: wall_clock[0],
                            sleep=lambda seconds: wall_clock.__setitem__(0, wall_clock[0] + seconds))

                    fake_time = SimpleNamespace(monotonic=lambda: monotonic[0])
                    with patch.object(agent_lab, "time", fake_time), patch.object(nim, "time", fake_time):
                        with self.assertRaisesRegex(agent_lab.LabError, "RATE_ADMISSION_EXCEEDS_DEADLINE"):
                            client.complete([], [], max_tokens=8, max_context_bytes=8192, timeout_s=5)

                    self.assertTrue(calls, "Provider catalog preflight should run within the budget")
                    self.assertFalse(any(url.endswith("/chat/completions") for url in calls),
                                     "Inference must not dispatch after preflight consumes the deadline")
                    self.assertEqual(client.requests_used, 0)
                    self.assertTrue(all(timeout <= 5 for timeout in timeouts),
                                    "Provider requests must receive only the remaining shared budget")

    def test_openrouter_request_serialization_cannot_expire_then_dispatch(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import patch

        monotonic = [0.0]
        calls = []
        quota = {"utc_date": datetime.now(timezone.utc).date().isoformat(),
                 "remaining_requests": 2, "source": "synthetic test allocation"}

        def transport(*args):
            calls.append(args)
            payload = {"model": "vendor/model:free",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2, "cost": "0"}}
            return agent_lab.HttpReply(200, {}, json.dumps(payload).encode())

        client = agent_lab.OpenRouterClient(agent_lab.OpenRouterConfig(
            model="vendor/model:free", api_key="synthetic-key", quota=quota,
            allow_live=True, operator_no_charge_acknowledged=True, request_cap=2), transport=transport)
        client.admission = {"checked_monotonic": 0.0}
        canonical = agent_lab.canonical
        serializations = [0]

        def slow_canonical(value):
            serializations[0] += 1
            result = canonical(value)
            if serializations[0] == 2:
                monotonic[0] = 0.6
            return result

        with patch.object(agent_lab, "time", SimpleNamespace(monotonic=lambda: monotonic[0])):
            with patch.object(agent_lab, "canonical", side_effect=slow_canonical):
                with self.assertRaisesRegex(agent_lab.LabError, "RATE_ADMISSION_EXCEEDS_DEADLINE"):
                    client.complete([], [], max_tokens=8, max_context_bytes=8192, timeout_s=0.5)

        self.assertEqual(calls, [], "Expired local request preparation must stop before provider transport")
        self.assertTrue(client.events)
        self.assertEqual(client.events[-1]["error"], "RATE_ADMISSION_EXCEEDS_DEADLINE")

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
    def test_osv_endpoint_has_only_its_exact_route_and_four_mib_response_budget(self):
        from cloud_experiments import http_transport

        allowed = {"method": "POST", "url": "https://api.osv.dev/v1/querybatch",
                   "headers": {"Content-Type": "application/json"}, "body": None,
                   "timeout_s": 45, "max_bytes": 4 * 1024 * 1024}
        http_transport._validate(allowed)
        for changes in (
            {"url": "https://api.osv.dev/v1/querybatch?redirect=https://example.invalid"},
            {"url": "https://api.osv.dev/v1/other"},
            {"method": "GET"},
            {"max_bytes": 4 * 1024 * 1024 + 1},
        ):
            payload = {**allowed, **changes}
            with self.subTest(changes=changes), self.assertRaises(http_transport.TransportError):
                http_transport._validate(payload)
        with self.assertRaisesRegex(http_transport.TransportError, "HTTP_REQUEST_DENIED"):
            http_transport._validate({**allowed, "url": "https://integrate.api.nvidia.com/v1/models"})

    def test_http_worker_reply_rejects_duplicate_json_status(self):
        from unittest.mock import patch
        from cloud_experiments import http_transport

        class Worker:
            args = ["controlled-worker"]
            returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def communicate(self, input=None, timeout=None):
                self.asserted_input = input
                return b'{"status":200,"status":201,"headers":{},"body":""}', b""

        with patch.object(subprocess, "Popen", return_value=Worker()):
            with self.assertRaisesRegex(http_transport.TransportError, "HTTP_INVALID_WORKER_REPLY"):
                http_transport.bounded_request(
                    "GET", "https://integrate.api.nvidia.com/v1/models", {}, None, 2, 1024)

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
    def test_secret_manager_rejects_ambiguous_metadata_before_secret_request(self):
        import base64
        from cloud_experiments import gcp_secrets

        resource = "projects/970441998440/secrets/athena-nvidia-nim-api-key/versions/latest"
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
                requests.append(request.full_url)
                if request.full_url.startswith("http://169.254.169.254/"):
                    return Response(b'{"access_token":"ambiguous","access_token":"token"}')
                value = base64.b64encode(b"synthetic-secret-value").decode("ascii")
                return Response(json.dumps({"payload": {"data": value}}).encode("utf-8"))

        with self.assertRaisesRegex(gcp_secrets.SecretAccessError, "SECRET_ACCESS_FAILED"):
            gcp_secrets.access_secret_resource(resource, opener=Opener())
        self.assertEqual(len(requests), 1, "Ambiguous metadata must reject before contacting Secret Manager")

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
    def test_research_source_walk_includes_reviewed_sdk_files_and_excludes_installed_dependencies(self):
        from unittest.mock import patch
        from cloud_experiments import campaign

        with tempfile.TemporaryDirectory(prefix="athena-source-node-modules-") as folder:
            root = Path(folder)
            source = root / "cloud_experiments"
            source.mkdir(parents=True)
            (source / "README.md").write_text("reviewed core source\n", encoding="utf-8")
            (source / "driver.py").write_text("# reviewed core source\n", encoding="utf-8")
            sdk_fixture = source / "mcp_sdk" / ".gitignore"
            sdk_fixture.parent.mkdir()
            sdk_fixture.write_text("local SDK study fixture\n", encoding="utf-8")
            installed = (source / "mcp_sdk" / "typescript" / "node_modules" / "unreviewed-package" / "index.js")
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            allowed = ("cloud_experiments/README.md", "cloud_experiments/driver.py",
                       "cloud_experiments/mcp_sdk/.gitignore")

            with patch.object(campaign, "CLOUD_EXPERIMENT_BUNDLE_FILES", allowed):
                self.assertEqual(campaign.reviewed_cloud_experiment_sources(root), list(allowed))
                files = campaign.source_files(root, ["cloud_experiments"])

            self.assertEqual(set(files), set(allowed))
            self.assertEqual(files["cloud_experiments/mcp_sdk/.gitignore"], b"local SDK study fixture\n")
            self.assertNotIn("cloud_experiments/mcp_sdk/typescript/node_modules/unreviewed-package/index.js",
                             files)

    def test_offline_cargo_cache_preflight_checks_unique_locks_and_fails_closed(self):
        import hashlib
        import subprocess
        from unittest.mock import patch
        from cloud_experiments import contenders, libraries

        files = campaign.source_files(research.ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
            "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"])
        variants = ["baseline", "lib-json-sonic"]
        expected_locks = {hashlib.sha256(contenders.transform(files, variant)["Cargo.lock"]).hexdigest()
                          for variant in variants}

        class Run:
            def __init__(self, directory, *, fail_fetch=False):
                self.directory = Path(directory)
                self.manifest = {}
                self.commands = []
                self.fail_fetch = fail_fetch

            def run(self, command, **kwargs):
                self.commands.append((tuple(command), kwargs))
                if self.fail_fetch:
                    raise subprocess.CalledProcessError(101, command, stderr=b"offline cache miss")
                return b""

            def save(self):
                return None

        def metadata(_run, workspace, *, cargo_feature_args):
            return {"lock_sha256": hashlib.sha256((Path(workspace) / "Cargo.lock").read_bytes()).hexdigest(),
                    "packages": {}}

        with tempfile.TemporaryDirectory(prefix="athena-cargo-cache-preflight-test-") as folder:
            root = Path(folder)
            run_dir, cache = root / "run", root / "cargo-target"
            run_dir.mkdir()
            cache.mkdir()
            run = Run(run_dir)
            with patch.object(libraries, "offline_dependency_metadata", side_effect=metadata), \
                    patch.object(libraries, "verify_graph", return_value={"status": "TEST_CONTROL"}):
                report = research._preflight_cargo_cache(run, files, variants, cache)
            fetches = [(command, kwargs) for command, kwargs in run.commands
                       if len(command) > 1 and command[1] == "fetch"]
            self.assertEqual(report["status"], "OFFLINE_CACHE_READY")
            self.assertEqual(len(fetches), len(expected_locks),
                             "Each unique lock profile must be checked exactly once")
            self.assertTrue(all(command == ("cargo", *research.process_isolation.CARGO_FETCH_ARGS)
                                and kwargs.get("network_mode") == "offline-cargo-fetch"
                                for command, kwargs in fetches))
            self.assertEqual({item["lock_sha256"] for item in report["verified_profiles"]}, expected_locks)
            self.assertEqual(list(run_dir.glob("athena-cargo-cache-preflight-*")), [],
                             "Temporary source workspaces must be removed")

            failed = Run(run_dir, fail_fetch=True)
            with patch.object(libraries, "offline_dependency_metadata", side_effect=metadata), \
                    patch.object(libraries, "verify_graph", return_value={"status": "TEST_CONTROL"}):
                with self.assertRaises(subprocess.CalledProcessError):
                    research._preflight_cargo_cache(failed, files, variants, cache)
            self.assertEqual(failed.manifest["cargo_cache_preflight"]["status"], "FAILED")
            self.assertEqual(len(failed.commands), 1,
                             "A missing offline package must stop before metadata/build work")
            self.assertEqual(list(run_dir.glob("athena-cargo-cache-preflight-*")), [],
                             "Failure must also remove temporary source workspaces")

    def test_research_bundle_fails_closed_on_unreviewed_local_file_before_read(self):
        from unittest.mock import patch

        for relative in ("provider-key.json", "mcp_sdk/provider-key.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(prefix="athena-bundle-private-input-") as folder:
                root = Path(folder)
                canary = root / "cloud_experiments" / relative
                canary.parent.mkdir(parents=True)
                canary.write_text('{"synthetic_test_secret":"never bundle"}', encoding="utf-8")
                destination = root / "bundle"
                opened = []
                original_open = Path.open

                def observe_open(path, *args, **kwargs):
                    if path == canary:
                        opened.append(path)
                    return original_open(path, *args, **kwargs)

                failure = None
                with (patch.object(research, "ROOT", root),
                      patch.object(research, "manifest_design", return_value={}),
                      patch("cloud_experiments.libraries.dossier", return_value={}),
                      patch.object(Path, "open", observe_open)):
                    try:
                        research.prepare(destination)
                    except ValueError as error:
                        failure = error

                self.assertEqual(opened, [], "Unreviewed local data must be rejected before it is read for a bundle")
                self.assertIsNotNone(failure, "An unreviewed file in the bundle source tree must fail closed")
                self.assertRegex(str(failure), "Unreviewed cloud experiment file")
                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob(".bundle.prepare-*")), [])

    def test_reqwest_tls_provider_profiles_resolve_one_backend_each(self):
        from cloud_experiments import contenders, libraries

        cargo = shutil.which("cargo")
        self.assertIsNotNone(cargo, "The pinned Rust toolchain is required to inspect the real feature graph")

        class CargoRun:
            def run(self, command, *, cwd, log_output=False):
                if "--offline" not in command:
                    raise AssertionError("Research dependency-graph inspection must stay offline")
                result = subprocess.run([cargo, "+1.99.1", *command[1:]], cwd=cwd,
                                        capture_output=True, timeout=120)
                if result.returncode:
                    raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
                return result.stdout

        files = campaign.source_files(research.ROOT, ["Cargo.toml", "Cargo.lock", "cloud_experiments"])
        profiles = (("baseline", "aws-lc"), ("lib-rustls-ring", "ring"))
        self.assertIn("lib-rustls-ring", contenders.definitions(),
                      "The cloud build path must expose the reviewed provider contender")
        graphs = {}
        for variant, expected_provider in profiles:
            profile = contenders.metadata(variant)
            transformed = contenders.transform(files, variant)
            libraries.verify_build_inputs(files, transformed, profile["factors"])
            graph = libraries.offline_dependency_metadata(
                CargoRun(), research.ROOT, cargo_feature_args=profile["build"]["cargo_feature_args"],
                target="x86_64-unknown-linux-gnu")
            evidence = libraries.verify_graph(files["Cargo.lock"], transformed["Cargo.lock"],
                                              graph, profile["factors"])
            self.assertEqual(evidence["tls_provider"]["selected"], expected_provider)
            self.assertEqual(graph["tls_provider"], expected_provider)
            graphs[variant] = graph

        ring = graphs["lib-rustls-ring"]
        altered = copy.deepcopy(ring)
        rustls_node = next(node for node in altered["nodes"]
                           if altered["packages"][node["id"]]["name"] == "rustls"
                           and altered["packages"][node["id"]]["version"] == "0.23.46")
        rustls_node["features"] = sorted((set(rustls_node["features"]) - {"ring"}) | {"aws-lc-rs"})
        with self.assertRaisesRegex(ValueError, "selected CryptoProvider"):
            libraries.verify_graph(files["Cargo.lock"], files["Cargo.lock"], altered,
                                   contenders.metadata("lib-rustls-ring")["factors"])
        self.assertNotIn("lib-rustls-ring", research.native_matrix()[0],
                         "The provider-by-client-lifetime experiment belongs to the service panel, not core stage timings")

    def test_declared_scope_cannot_omit_language_interactions(self):
        registry = json.loads((research.ROOT / "cloud_experiments/core_decisions.json").read_text(encoding="utf-8"))
        design = research.manifest_design()
        all_interactions = {item["id"] for item in registry["interactions"]}
        expected_interactions = {
            "I-AAE", "I-RANK", "I-PROJECT", "I-ACCESS", "I-AGENT", "I-SYSTEM",
            "I-HASH-LIBRARY", "I-PARSER-LIBRARY", "I-JSON-LIBRARY", "I-LIBRARY-SYSTEM",
            "I-JSON-CPU", "I-JSON-PROJECT", "I-QUERY-PROJECT", "I-COMPILER",
            "I-JSON-HASH-CPU", "I-NATIVE-CROSS-COVERAGE", "I-LANG-PARSE",
            "I-LANG-BROWSER", "I-LANG-AGENT", "I-LANG-BATCH", "I-LANG-SCHED",
            "I-LANG-MODEL", "I-HTML-TABLE", "I-HTML-ATTR-MEMBERSHIP",
        }
        self.assertEqual(all_interactions, expected_interactions)
        self.assertEqual(research.DECLARED_INTERACTION_IDS, expected_interactions)
        required_interactions = set(design["required_interactions"])
        self.assertEqual(all_interactions - required_interactions,
                         {"I-LANG-BATCH", "I-LANG-MODEL"})
        self.assertIn("I-LANG-SCHED", required_interactions)
        self.assertEqual(design["conditional_interaction_non_admissions"],
                         ["I-LANG-BATCH", "I-LANG-MODEL"])
        expected = {f"L{i:02}" for i in range(1, 7)}
        panels = {item["id"]: item for item in registry["language_panels"]}
        self.assertEqual(set(panels), expected)
        decisions = {item["id"] for item in registry["decisions"]}
        interactions = {item["id"] for item in registry["interactions"]}
        for panel in panels.values():
            self.assertTrue(set(panel["owner_decisions"]) <= decisions)
            self.assertTrue(set(panel["interactions"]) <= interactions)
        expected_deferred_by_admission = {
            "L04": {"I-LANG-MODEL"},
            "L05": {"I-LANG-BATCH"},
            "L06": {"I-LANG-BATCH", "I-LANG-MODEL"},
        }
        for panel_id, expected_deferred in expected_deferred_by_admission.items():
            with self.subTest(admitted_panel=panel_id):
                admitted = json.loads(json.dumps(registry))
                next(item for item in admitted["language_panels"] if item["id"] == panel_id)["status"] = "ADMITTED_NOT_IMPLEMENTED"
                _, deferred = research._interaction_scope(admitted)
                self.assertEqual(set(deferred), expected_deferred)
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

    def test_manifest_rejects_a_missing_required_interaction_even_if_inventory_maps(self):
        from unittest.mock import patch

        registry = json.loads((research.ROOT / "cloud_experiments/core_decisions.json").read_text(encoding="utf-8"))
        registry["interactions"] = [item for item in registry["interactions"] if item["id"] != "I-ACCESS"]
        audit = campaign.audit_decision_map(research.ROOT, registry)
        self.assertEqual(audit["mapping_status"], "MAPPED",
                         "Inventory mapping deliberately does not attest the frozen experiment scope")
        with patch.object(research.lifecycle, "read_json", return_value=registry):
            with self.assertRaisesRegex(ValueError, "does not close the declared experiment scope"):
                research.manifest_design()

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

    def test_run_native_cli_exposes_incomplete_research_readiness(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        coverage = {
            "status": "INCOMPLETE",
            "missing": ["H01", "I-AAE", "L01"],
            "blocking_language_panels": ["L01", "L02", "L03"],
            "conditional_language_non_admissions": ["L04", "L05", "L06"],
            "decision_sufficiency": "NOT_EVALUATED",
        }
        manifest = {"status": "COMPLETE", "overall_decision": "HOLD",
                    "current_phase": "finished", "coverage": coverage}
        output = StringIO()
        argv = ["research", "run-native", "source", "spec.json", "output"]
        with patch.object(sys, "argv", argv), patch.object(research, "execute_native", return_value=manifest):
            with redirect_stdout(output):
                status = research.main()

        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["research_readiness"], "INCOMPLETE")
        self.assertEqual(report["missing_obligations"], coverage["missing"])
        self.assertEqual(report["blocking_language_panels"], coverage["blocking_language_panels"])
        self.assertEqual(report["conditional_language_non_admissions"], coverage["conditional_language_non_admissions"])
        self.assertEqual(report["decision_sufficiency"], "NOT_EVALUATED")

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
                  if axis not in ("prepared", "table", "html_extractor", "tls_provider")}
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

    def test_cold_design_generation_avoids_per_candidate_registry_copies(self):
        from unittest.mock import patch
        from cloud_experiments import contenders, experimental_design

        self.addCleanup(experimental_design._requirements.cache_clear)
        self.addCleanup(experimental_design._compiled.cache_clear)
        experimental_design._compiled.cache_clear()
        experimental_design._requirements.cache_clear()
        original_definitions = contenders.definitions
        with patch.object(contenders, "definitions", wraps=original_definitions) as public_registry:
            design = experimental_design.native_design()

        self.assertEqual(design["coverage"]["status"], "COVERED")
        self.assertEqual(design["coverage"]["covered_tuples"], design["coverage"]["required_tuples"])
        self.assertEqual(len(design["variants"]), 225)
        self.assertLessEqual(public_registry.call_count, 1,
                         "Hot candidate enumeration must not deep-copy the public registry per candidate")
        public_copy = contenders.definitions()
        public_copy["baseline"]["factors"]["writer"] = "mutated-copy"
        self.assertEqual(contenders.metadata("baseline")["factors"]["writer"], "frame",
                         "The public registry must remain an isolated defensive copy")

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
                def failed_contract(binary, output, *, deadline=None, isolate_process_tree=False):
                    self.assertIs(isolate_process_tree, run.isolate_children)
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
                def denied(binary, output, *, deadline=None, isolate_process_tree=False):
                    self.assertIs(isolate_process_tree, run.isolate_children)
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
            document = {"schema": "athena-research-corpus/2",
                        "dataset": {"id": "athena/synthetic-corpus",
                            "revision": {"kind": "kaggle_version", "value": 1},
                            "source_url": "https://example.org/athena-synthetic-corpus",
                            "license_id": "CC0-1.0",
                            "license_source_url": "https://creativecommons.org/publicdomain/zero/1.0/"},
                        "rights_review": {"decision": "ALLOW_CLOUD_STAGING", "reviewed_on": "2026-10-24",
                            "evidence_urls": ["https://example.org/synthetic-fixture-rights"],
                            "rationale": "Synthetic test HTML only; this fixture checks schema behavior, not legal review."},
                        "data_classification": "public", "items": []}
            for split, domain, template, text in (("screen", "one.example", "article", "Alpha"),
                                                   ("confirm", "two.example", "docs", "Beta")):
                name = split + ".html"
                data = ("<p>" + text + "</p>").encode()
                (root / name).write_bytes(data)
                document["items"].append({"id": split, "split": split, "file": name,
                    "sha256": hashlib.sha256(data).hexdigest(), "domain_group": domain,
                    "template_family": template, "license": "CC0-1.0", "source_url": "https://" + domain + "/",
                    "license_source_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                    "expected_blocks": [{"role": "paragraph", "text": text, "links": []}]})
            manifest = root / "corpus.json"
            def save(value):
                manifest.write_text(json.dumps(value), encoding="utf-8")
            save(document)
            accepted = workloads.load_corpus(manifest)
            self.assertEqual({c["fixture"]["html"] for c in accepted["cases"]}, {"<p>Alpha</p>", "<p>Beta</p>"})
            self.assertEqual(accepted["dataset"], document["dataset"])
            self.assertEqual(accepted["rights_review"], document["rights_review"])
            serialized = json.dumps(document)
            ambiguous = serialized.replace('"schema": "athena-research-corpus/2"',
                                           '"schema": "wrong", "schema": "athena-research-corpus/2"', 1)
            self.assertNotEqual(ambiguous, serialized)
            manifest.write_text(ambiguous, encoding="utf-8")
            with self.assertRaises(ValueError):
                workloads.load_corpus(manifest)
            save(document)
            import base64
            from kaggle_experiments import runner
            research.prepare(root / "bundle", corpus_manifest=manifest)
            spec = json.loads((root / "bundle/spec.json").read_text(encoding="utf-8"))
            self.assertEqual(spec["corpus"]["dataset"], document["dataset"])
            self.assertEqual(spec["corpus"]["rights_review"], document["rights_review"])
            runner.unpack_snapshot(base64.b64encode((root / "bundle/source.tgz").read_bytes()).decode(),
                                   spec["source_sha256"], spec["files"], root / "source")
            frozen_files = research.verify_snapshot(root / "source", spec)
            self.assertEqual(spec["holdout_kind"], "independent_seed")
            for split, text in (("screen", "Alpha"), ("confirm", "Beta")):
                cases = research.study_cases(frozen_files, split)
                case = next(c for c in cases if c["id"] == "corpus-" + split)
                self.assertEqual(case["fixture"]["html"], "<p>" + text + "</p>")
                self.assertEqual(case["provenance"]["dataset_id"], document["dataset"]["id"])
                self.assertEqual(case["provenance"]["dataset_revision"], document["dataset"]["revision"])
                self.assertEqual(case["provenance"]["license_source_url"],
                                 "https://creativecommons.org/publicdomain/zero/1.0/")
                self.assertEqual(research.fixture_inventory(cases, split), spec["fixtures"][split])
            for defect in ("same-domain", "spoofed-domain-group", "same-template", "same-bytes", "path-escape", "bad-hash",
                           "unreviewed-rights", "mutable-revision", "missing-dataset-license-evidence",
                           "missing-content-license-evidence"):
                bad = copy.deepcopy(document)
                item = bad["items"][1]
                if defect == "same-domain":
                    item["domain_group"] = "one.example"
                elif defect == "spoofed-domain-group":
                    item["source_url"] = "https://one.example/"
                elif defect == "same-template":
                    item["template_family"] = "article"
                elif defect == "same-bytes":
                    item.update(file="screen.html", sha256=bad["items"][0]["sha256"])
                elif defect == "path-escape":
                    item["file"] = "../outside.html"
                elif defect == "bad-hash":
                    item["sha256"] = "f" * 64
                elif defect == "unreviewed-rights":
                    bad["rights_review"]["decision"] = "DENY_CLOUD_STAGING"
                elif defect == "mutable-revision":
                    bad["dataset"]["revision"] = {"kind": "git_commit", "value": "main"}
                elif defect == "missing-dataset-license-evidence":
                    bad["dataset"].pop("license_source_url")
                else:
                    item.pop("license_source_url")
                save(bad)
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    workloads.load_corpus(manifest)

    def test_corpus_intake_rejects_legacy_unpinned_provenance(self):
        import hashlib
        from cloud_experiments import workloads
        with tempfile.TemporaryDirectory(prefix="athena-corpus-provenance-") as folder:
            root = Path(folder)
            document = {"schema": "athena-research-corpus/1", "rights_review": "reviewed",
                        "data_classification": "public", "items": []}
            for split, domain, text in (("screen", "one.example", "Alpha"),
                                        ("confirm", "two.example", "Beta")):
                name = split + ".html"
                data = ("<p>" + text + "</p>").encode()
                (root / name).write_bytes(data)
                document["items"].append({"id": split, "split": split, "file": name,
                    "sha256": hashlib.sha256(data).hexdigest(), "domain_group": domain,
                    "template_family": split, "license": "CC0-1.0",
                    "source_url": "https://" + domain + "/",
                    "expected_blocks": [{"role": "paragraph", "text": text, "links": []}]})
            manifest = root / "corpus.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pinned dataset/license provenance"):
                workloads.load_corpus(manifest)

    def test_library_dossier_uses_direct_rand_edge_and_candidate_lock_is_immutable(self):
        from cloud_experiments import contenders, libraries
        root = Path(__file__).resolve().parents[1]
        report = libraries.dossier(root)
        self.assertEqual(report["libraries"]["rand"]["resolved"]["version"], "0.9.5")
        self.assertEqual(report["libraries"]["rustls"]["resolved"]["version"], "0.23.46")
        self.assertEqual(report["libraries"]["scraper"]["status"], "HOLD")
        files = campaign.source_files(root, ["src", "Cargo.toml", "Cargo.lock", "cloud_experiments"])

        import tomllib
        profiles = (
            ({}, "Cargo.lock", libraries.LOCK_SHA256),
            ({"html_library": "lean"}, libraries.LEAN_LOCK, libraries.LEAN_LOCK_SHA256),
            ({"json_library": "sonic"}, libraries.SONIC_LOCK, libraries.SONIC_LOCK_SHA256),
            ({"json_library": "sonic", "html_library": "lean"},
             libraries.SONIC_LEAN_LOCK, libraries.SONIC_LEAN_LOCK_SHA256),
            ({"html_library": "modern"},
             "cloud_experiments/locks/scraper-0.29.0-html5ever-0.40.1.lock",
             libraries.MODERN_LOCK_SHA256),
            ({"json_library": "sonic", "html_library": "modern"},
             "cloud_experiments/locks/sonic-0.5.10-scraper-0.29.0-html5ever-0.40.1.lock",
             libraries.SONIC_MODERN_LOCK_SHA256),
        )
        for factors, lock_path, expected_digest in profiles:
            with self.subTest(profile=lock_path):
                selected = libraries.transform(files, factors)
                lock_bytes = files[lock_path]
                self.assertEqual(selected["Cargo.lock"], lock_bytes)
                self.assertEqual(hashlib.sha256(lock_bytes).hexdigest(), expected_digest)
                locked_packages = tomllib.loads(lock_bytes.decode())["package"]
                rustls_lock_entry = next(package for package in locked_packages
                                         if package["name"] == "rustls" and package["version"] == "0.23.46")
                self.assertIn("aws-lc-rs", rustls_lock_entry["dependencies"])
                self.assertIn("ring", rustls_lock_entry["dependencies"],
                              "shared candidate lock must resolve both supported TLS provider profiles")
                tls_versions = {package["name"]: package["version"]
                                for package in locked_packages
                                if package["name"] in {"rustls-webpki", "webpki-root-certs"}}
                self.assertEqual(tls_versions, {"rustls-webpki": "0.103.16", "webpki-root-certs": "1.0.10"})
                if factors.get("html_library") == "modern":
                    manifest = selected["Cargo.toml"].decode()
                    self.assertIn('html5ever = "=0.40.1"', manifest)
                    self.assertIn('scraper = "=0.29.0"', manifest)
                    selected_packages = {p["name"]: p["version"]
                        for p in tomllib.loads(lock_bytes.decode())["package"]
                        if p["name"] in {"html5ever", "scraper", "sonic-rs"}}
                    expected_packages = {"html5ever": "0.40.1", "scraper": "0.29.0"}
                    if factors.get("json_library") == "sonic":
                        expected_packages["sonic-rs"] = "0.5.10"
                    self.assertEqual(selected_packages, expected_packages)

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

    def test_html_attribute_lookup_candidate_uses_pinned_local_parser_and_lock(self):
        from cloud_experiments import libraries

        root = Path(__file__).resolve().parents[1]
        files = campaign.source_files(root, ["Cargo.toml", "Cargo.lock", "cloud_experiments"])
        factors = {"html_attr_lookup": "hybrid32"}
        transformed = libraries.transform(files, factors)

        self.assertIn(
            '[patch.crates-io]\nhtml5ever = { path = "research-html5ever-0.39.0-hybrid32" }',
            transformed["Cargo.toml"].decode(),
            "The candidate must compile the pinned tokenizer source under test, not a registry copy",
        )
        lock_path = "cloud_experiments/locks/html5ever-0.39.0-attr-index.lock"
        self.assertEqual(transformed["Cargo.lock"], files[lock_path])
        self.assertEqual(
            hashlib.sha256(transformed["Cargo.lock"]).hexdigest(),
            libraries.ATTR_CURRENT_LOCK_SHA256,
        )

        modern = libraries.transform(files, {"html_library": "modern", "html_attr_lookup": "hybrid32"})
        self.assertIn(
            '[patch.crates-io]\nhtml5ever = { path = "research-html5ever-0.40.1-hybrid32" }',
            modern["Cargo.toml"].decode(),
        )
        self.assertEqual(
            hashlib.sha256(modern["Cargo.lock"]).hexdigest(),
            libraries.ATTR_MODERN_LOCK_SHA256,
        )
        modern_evidence = libraries.verify_build_inputs(
            files, modern, {"html_library": "modern", "html_attr_lookup": "hybrid32"})
        self.assertEqual(modern_evidence["candidate_parser"]["version"], "0.40.1")
        evidence = libraries.verify_build_inputs(
            files, transformed, {"html_attr_lookup": "hybrid32"})
        self.assertEqual(evidence["candidate_parser"]["version"], "0.39.0")
        self.assertEqual(evidence["candidate_parser"]["strategy"], "hybrid32")
        self.assertEqual(evidence["candidate_parser"]["status"], "ARCHIVE_AND_TRANSFORM_HASH_VERIFIED")

        from cloud_experiments import parser_study
        patched = parser_study.patched_tokenizer(files, "0.40.1", "hybrid32").decode("utf-8")
        self.assertIn("current_tag_attr_index: RefCell<HashSet<LocalName>>,", patched)
        self.assertIn("(attrs.len() == ATHENA_ATTR_INDEX_THRESHOLD && had_duplicate_attributes)", patched)
        self.assertIn("if attrs.len() < ATHENA_ATTR_INDEX_THRESHOLD {", patched)
        altered = dict(files)
        archive_path, _ = parser_study.CRATES["0.39.0"]
        altered[archive_path] = b"unreviewed" + files[archive_path]
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            parser_study.patched_tokenizer(altered, "0.39.0", "hybrid32")

    def test_wpt_differential_detects_tree_and_warning_changes_from_a_literal_oracle(self):
        from cloud_experiments import parser_study

        inventory = parser_study.WPT_REPORT_LIMITS
        cases = [{"fixture": f"fixture-{number // 100}.dat", "case": number + 1,
                  "actual_tree_blake3": "a" * 64, "expected_tree_blake3": "a" * 64,
                  "warnings": 0, "expected_warning_count": 0}
                 for number in range(inventory["executed"])]
        report = {"schema": "athena-wpt-differential/1", **inventory,
                  "tree_mismatches": 0, "annotation_count_deltas": 0, "cases": cases}
        self.assertEqual(parser_study.parse_wpt_report("ATHENA_WPT_REPORT_JSON=" + json.dumps(report)), report)
        corrupted = json.loads(json.dumps(report))
        corrupted["cases"][0]["warnings"] = 1
        with self.assertRaisesRegex(ValueError, "summary does not match"):
            parser_study.parse_wpt_report("ATHENA_WPT_REPORT_JSON=" + json.dumps(corrupted))

        reference = [
            {"fixture": "tree.dat", "case": 1, "actual_tree_blake3": "a" * 64,
             "expected_tree_blake3": "a" * 64, "warnings": 0, "expected_warning_count": 0},
            {"fixture": "warning.dat", "case": 7, "actual_tree_blake3": "b" * 64,
             "expected_tree_blake3": "a" * 64, "warnings": 3, "expected_warning_count": 2},
        ]
        candidate = [
            {"fixture": "tree.dat", "case": 1, "actual_tree_blake3": "c" * 64,
             "expected_tree_blake3": "a" * 64, "warnings": 0, "expected_warning_count": 0},
            {"fixture": "warning.dat", "case": 7, "actual_tree_blake3": "b" * 64,
             "expected_tree_blake3": "a" * 64, "warnings": 4, "expected_warning_count": 2},
        ]
        delta = parser_study.compare_wpt_cases(reference, candidate)

        self.assertEqual(delta["case_count"], 2)
        self.assertEqual(delta["changed_tree_cases"], ["tree.dat:1"])
        self.assertEqual(delta["changed_warning_cases"], ["warning.dat:7"])
        self.assertEqual(delta["new_tree_mismatches"], ["tree.dat:1"])
        self.assertEqual(delta["new_warning_deltas"], [])
        self.assertEqual(delta["status"], "CHANGED_REQUIRES_REVIEW")

    def test_wpt_campaign_persists_bounded_reports_and_gates_same_tuple_parity(self):
        from unittest.mock import patch
        from cloud_experiments import contenders, parser_study

        inventory = parser_study.WPT_REPORT_LIMITS

        def report_with_change(changed=False):
            cases = []
            tree_mismatches = annotation_deltas = 0
            for number in range(inventory["executed"]):
                tree = "b" * 64 if number < 105 else "a" * 64
                warnings = 1 if number < 482 else 0
                if changed and number == 1000:
                    tree, warnings = "c" * 64, 2
                tree_mismatches += tree != "a" * 64
                annotation_deltas += warnings != 0
                cases.append({"fixture": f"fixture-{number // 100}.dat", "case": number + 1,
                              "actual_tree_blake3": tree, "expected_tree_blake3": "a" * 64,
                              "warnings": warnings, "expected_warning_count": 0})
            return {"schema": "athena-wpt-differential/1", **inventory,
                    "tree_mismatches": tree_mismatches,
                    "annotation_count_deltas": annotation_deltas, "cases": cases}

        class Run:
            def __init__(self, directory):
                self.directory = Path(directory)
                self.manifest = {"spec": research.manifest_design(), "wpt_reports": {},
                                 "wpt_comparisons": {}, "wpt_candidate_gates": {}}
                self.saves = 0
                self.storage_checks = 0

            def save(self):
                self.saves += 1

            def guard_storage(self):
                self.storage_checks += 1

        outputs = iter(report_with_change(changed=value) for value in (False, False, True))
        prior_environment = os.environ.get("ATHENA_WPT_REPORT_ONLY")
        with tempfile.TemporaryDirectory(prefix="athena-wpt-gate-") as folder:
            run = Run(folder)

            def fake_cargo(_run, command, *, cwd, timeout):
                self.assertIn("wpt_html_conformance", command)
                self.assertEqual(command[-2:], ["--ignored", "--nocapture"])
                self.assertEqual(os.environ.get("ATHENA_WPT_REPORT_ONLY"), "1")
                self.assertEqual(timeout, 1200)
                return "ATHENA_WPT_REPORT_JSON=" + json.dumps(next(outputs), separators=(",", ":"))

            with patch.object(research, "_run_isolated_cargo", side_effect=fake_cargo):
                baseline = research._run_wpt_report(
                    run, folder, "baseline", contenders.metadata("baseline"))
                candidate = research._run_wpt_report(
                    run, folder, "html-attrs-hash", contenders.metadata("html-attrs-hash"))
                self.assertEqual(candidate["summary"]["tree_mismatches"], 105)
                self.assertEqual(candidate["summary"]["annotation_count_deltas"], 482)
                self.assertEqual(run.manifest["wpt_candidate_gates"]["html-attrs-hash"]["status"],
                                 "SEMANTICALLY_IDENTICAL")
                research._verify_resumed_wpt(run, "html-attrs-hash",
                    contenders.metadata("html-attrs-hash"),
                    {"wpt_report_receipt": candidate,
                     "wpt_comparison": run.manifest["wpt_comparisons"]["html-attrs-hash"]})
                with self.assertRaisesRegex(ValueError, "changed pinned WPT"):
                    research._run_wpt_report(run, folder, "html-attrs-hybrid8",
                        contenders.metadata("html-attrs-hybrid8"))

            self.assertEqual(run.manifest["wpt_reports"]["baseline"], baseline)
            self.assertEqual(run.manifest["wpt_candidate_gates"]["html-attrs-hybrid8"]["status"],
                             "CHANGED_REQUIRES_REVIEW")
            self.assertEqual(run.manifest["wpt_comparisons"]["html-attrs-hybrid8"]["changed_tree_cases"],
                             ["fixture-10.dat:1001"])
            self.assertTrue((Path(folder) / candidate["artifact"]).is_file())
            self.assertEqual(run.saves, 3)
            self.assertEqual(run.storage_checks, 3)
        self.assertEqual(os.environ.get("ATHENA_WPT_REPORT_ONLY"), prior_environment)

    def test_dependency_policy_rejects_duplicate_library_object_members(self):
        from cloud_experiments import libraries

        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="athena-library-policy-") as folder:
            root = Path(folder)
            (root / "cloud_experiments").mkdir()
            for name in ("Cargo.toml", "Cargo.lock"):
                shutil.copy2(source / name, root / name)
            policy = root / "cloud_experiments/library_decisions.json"
            text = (source / "cloud_experiments/library_decisions.json").read_text(encoding="utf-8")
            policy.write_text(text, encoding="utf-8")
            self.assertEqual(libraries.dossier(root)["libraries"]["rand"]["resolved"]["version"], "0.9.5")
            policy.write_text(text.replace('"rand": {', '"rand": {"purpose":"ambiguous"}, "rand": {', 1),
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate JSON object member"):
                libraries.dossier(root)

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

    def test_research_build_queries_the_selected_lock_profile_before_building(self):
        from unittest.mock import patch
        from cloud_experiments import contenders, libraries

        root = research.ROOT
        files = campaign.source_files(root, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
            "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"])
        variant = "lib-json-sonic"
        selected = contenders.transform(files, variant)
        lock_sha256 = hashlib.sha256(selected["Cargo.lock"]).hexdigest()
        graph = {"lock_sha256": lock_sha256, "packages": {"sonic-rs": {
            "name": "sonic-rs", "version": "0.5.10", "source": "registry+https://github.com/rust-lang/crates.io-index"}}}
        events = []

        class Run:
            def __init__(self, directory):
                self.directory = directory
                self.manifest = {"snapshots": {}}

            def run(self, command, **_kwargs):
                events.append(("command", tuple(map(str, command)), dict(_kwargs)))
                return b""

            def guard_storage(self):
                return None

            def save(self):
                return None

        def isolated(_run, command, **_kwargs):
            events.append(("isolated-build", tuple(map(str, command))))
            return b""

        def advisory(_run, actual_graph):
            events.append(("advisory", actual_graph["lock_sha256"]))
            return "QUERIED"

        with tempfile.TemporaryDirectory(prefix="athena-advisory-build-order-") as folder:
            root_path = Path(folder)
            run_dir, workspace, cache = root_path / "run", root_path / "work", root_path / "target"
            release = cache / "release"
            for directory in (run_dir, workspace, release):
                directory.mkdir(parents=True)
            for name in ("core_probe", "service_probe", "research_probe", "athena-core-v2", "bench_core"):
                (release / name).write_bytes(b"synthetic binary placeholder")
            run = Run(run_dir)
            with (patch("cloud_experiments.research._verify_build_network_isolation", return_value={
                    "status": "PROCESS_CHECKED_PID_AND_NETWORK_NAMESPACES", "host_netns": "host", "child_netns": "build",
                    "host_pidns": "host-pid", "child_pidns": "child-pid",
                    "metadata_tcp_blocked": True, "public_tcp_blocked": True}),
                  patch("cloud_experiments.research._run_isolated_cargo", side_effect=isolated),
                  patch("cloud_experiments.research.libraries.offline_dependency_metadata",
                        side_effect=lambda *_a, **_k: graph) as dependency_metadata,
                  patch("cloud_experiments.research.campaign.dependency_advisories", side_effect=advisory),
                  patch("cloud_experiments.libraries.verify_graph", return_value={"status": "TEST_CONTROL"})):
                research._build(run, workspace, files, variant, cache)

        advisory_index = next((index for index, item in enumerate(events) if item[0] == "advisory"), None)
        build_index = next(index for index, item in enumerate(events) if item[0] == "isolated-build")
        self.assertIsNotNone(advisory_index, "The candidate lock profile must be queried before build commands")
        self.assertEqual(events[advisory_index][1], lock_sha256)
        self.assertLess(advisory_index, build_index)
        fetches = [event for event in events if event[0] == "command"
                   and event[1] == ("cargo", *research.process_isolation.CARGO_FETCH_ARGS)]
        self.assertEqual(len(fetches), 1, "The reviewed lock profile must be fetched exactly once")
        self.assertEqual(fetches[0][1],
                         ("cargo", "fetch", "--offline", "--locked", "--target", "x86_64-unknown-linux-gnu"),
                         "Dependency fetch must require the pre-seeded local cache")
        self.assertEqual(fetches[0][2].get("network_mode"), "offline-cargo-fetch",
                         "Dependency fetch must use the network-denied child boundary")
        self.assertTrue(dependency_metadata.call_count >= 2)
        self.assertTrue(all("cargo_feature_args" in call.kwargs
                            for call in dependency_metadata.call_args_list),
                        "The research build must use the cloud-only offline graph adapter")
        self.assertEqual(run.manifest["snapshots"][variant]["build_network_isolation"]["cargo_fetch_policy"],
                         research.process_isolation.CARGO_FETCH_POLICY,
                         "Resume evidence must bind the offline dependency-cache policy")

    def test_research_build_rejects_ancestor_cargo_network_configuration_before_fetch(self):
        from unittest.mock import patch

        files = campaign.source_files(research.ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
            "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"])

        class Run:
            def __init__(self, directory):
                self.directory = Path(directory)
                self.manifest = {"snapshots": {}}
                self.commands = []

            def run(self, command, **_kwargs):
                self.commands.append(tuple(map(str, command)))
                return b""

            def save(self):
                return None

        with tempfile.TemporaryDirectory(prefix="athena-cargo-config-boundary-") as folder:
            root = Path(folder)
            run_dir, workspace, cache = root / "run", root / "work", root / "target"
            cargo_config = root / ".cargo" / "config.toml"
            for directory in (run_dir, workspace, cache, cargo_config.parent):
                directory.mkdir(parents=True, exist_ok=True)
            cargo_config.write_text('[source.crates-io]\nreplace-with = "unreviewed"\n',
                                    encoding="utf-8")
            run = Run(run_dir)
            previous_environment = dict(os.environ)
            with patch.object(research, "_verify_build_network_isolation", return_value={
                    "status": "TEST_CONTROL"}):
                with self.assertRaisesRegex(ValueError, "Cargo configuration"):
                    research._build(run, workspace, files, "baseline", cache)
            self.assertEqual(run.commands, [],
                             "An ancestor Cargo config must be rejected before any Cargo command")
            self.assertEqual(dict(os.environ), previous_environment,
                             "The private Cargo environment must be restored after rejection")

    def test_research_build_stops_when_pid_or_network_namespace_negative_controls_fail(self):
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
                return (b'{"host_netns":"net:[host]","child_netns":"net:[host]",'
                        b'"host_pidns":"pid:[host]","child_pidns":"pid:[host]",'
                        b'"metadata_tcp_blocked":true,"public_tcp_blocked":true}')

            def save(self):
                return None

        with tempfile.TemporaryDirectory(prefix="athena-build-netns-") as folder:
            root = Path(folder)
            run_dir, workspace, cache = root / "run", root / "work", root / "target"
            for directory in (run_dir, workspace, cache):
                directory.mkdir()
            run = RunWithoutCargo(run_dir)
            with (
                patch.object(research.platform, "system", return_value="Linux"),
                patch.object(research, "_current_network_namespace_id", return_value="net:[host]"),
                patch.object(research, "_current_pid_namespace_id", return_value="pid:[host]"),
                patch.object(research, "_network_namespace_command",
                             side_effect=lambda command: ["unshare", "--net", "--pid", "--mount-proc", "--", *command]),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not isolate PIDs and deny metadata/public egress"):
                    research._build(run, workspace, files, "baseline", cache)
            self.assertEqual(len(run.commands), 1)
            self.assertEqual(run.commands[0][0], "unshare")
            self.assertIn("--net", run.commands[0])
            self.assertIn("--pid", run.commands[0])

    def test_native_isolation_preflight_rejects_non_x86_64_before_child_launch(self):
        from unittest.mock import patch

        class Run:
            def __init__(self):
                self.commands = []

            def run(self, command, **_kwargs):
                self.commands.append(command)
                return b""

        run = Run()
        with patch.object(research.platform, "system", return_value="Linux"), \
                patch.object(research.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(RuntimeError, "pinned to x86_64 Linux"):
                research._verify_build_network_isolation(run)
        self.assertEqual(run.commands, [],
                         "An unsupported native target must fail before launching a child")

    def test_service_cli_requires_isolation_instead_of_bypassing_it_on_windows(self):
        from unittest.mock import patch

        with (patch.object(research.sys, "argv",
                           ["research", "run-service", "fixture-bin", "fixture-output"]),
              patch.object(research.platform, "system", return_value="Windows"),
              patch.object(research, "execute_service",
                           return_value={"status": "CONTRACTS_PASS_WITHIN_SCOPE", "scenarios": []}) as execute,
              patch("builtins.print")):
            self.assertEqual(research.main(), 0)
        execute.assert_called_once_with("fixture-bin", "fixture-output", scenario_ids=None,
                                        isolate_process_tree=True)

    def test_literal_oracle_rejects_corruption_and_wrong_denial_even_with_valid_json(self):
        from cloud_experiments import workloads
        fixture = {"expected_blocks": [{"role": "paragraph", "text": "visible", "links": []}]}
        good = [{"type": "meta", "total_blocks": 1},
                {"type": "block", "role": "paragraph", "text": "visible", "links": []},
                {"type": "end", "block_count": 1}]
        encode = lambda frames: "\n".join(json.dumps(frame) for frame in frames).encode()
        self.assertEqual(workloads.envelope_errors(encode(good), fixture), [])
        ambiguous_aae = encode(good).replace(b'"text": "visible"',
                                             b'"text": "attack", "text": "visible"', 1)
        self.assertTrue(workloads.envelope_errors(ambiguous_aae, fixture))
        for bad in (good[:1] + good[2:], [good[0], {**good[1], "text": "attack"}, good[2]],
                    [good[0], {**good[1], "links": [7]}, good[2]]):
            self.assertTrue(workloads.envelope_errors(encode(bad), fixture))
        denied = {"expected_error": "decoded text byte budget exceeded"}
        self.assertEqual(workloads.envelope_errors(b'{"status":"EXPECTED_ERROR","error":"decoded text byte budget exceeded"}', denied), [])
        self.assertTrue(workloads.envelope_errors(b'{"status":"EXPECTED_ERROR","error":"some other error"}', denied))
        self.assertTrue(workloads.envelope_errors(encode(good), denied))
        parser_fixture = {
            "expected_parser_html_contains": ['data-a00000="first"'],
            "expected_parser_html_excludes": ['data-a00000="replacement"'],
        }
        parser_good = {"html": '<div data-a00000="first"></div>', "nodes": 1}
        parser_raw = json.dumps(parser_good).encode()
        self.assertEqual(workloads.envelope_errors(parser_raw, parser_fixture), [])
        self.assertTrue(workloads.envelope_errors(
            json.dumps({**parser_good, "html": '<div data-a00000="replacement"></div>'}).encode(),
            parser_fixture))
        self.assertTrue(workloads.envelope_errors(b"not-json", parser_fixture))
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
        from cloud_experiments import contenders, experimental_design
        variants, panels = research.native_matrix()
        self.assertIn("aae-batched4096+lib-blake3-pure", variants)
        self.assertIn("projection-fused+links-btree+lib-scraper-lean", variants)
        self.assertIn("I-HASH-LIBRARY", panels)
        self.assertIn("I-PARSER-LIBRARY", panels)
        self.assertIn("lib-scraper-modern", variants)
        self.assertIn("projection-fused+links-btree+lib-scraper-modern", panels["I-PARSER-LIBRARY"])
        self.assertEqual(len(panels["I-PARSER-LIBRARY"]), 16)
        self.assertEqual(panels["I-HTML-TABLE"], ["baseline", "table-html-selectors"])
        self.assertEqual(panels["I-HTML-ATTR-MEMBERSHIP"], [
            "baseline", "html-attrs-hash", "html-attrs-hybrid8",
            "html-attrs-hybrid32", "html-attrs-hybrid128",
            "html-attrs-modern-vector", "html-attrs-modern-hash",
            "html-attrs-modern-hybrid8", "html-attrs-modern-hybrid32",
            "html-attrs-modern-hybrid128",
        ])
        self.assertLess(variants.index("baseline"), variants.index("html-attrs-modern-vector"))
        self.assertLess(variants.index("html-attrs-modern-vector"),
                        variants.index("html-attrs-modern-hash"))
        self.assertTrue(set(panels["I-HTML-ATTR-MEMBERSHIP"]) <= set(variants))
        for variant in panels["I-HTML-ATTR-MEMBERSHIP"][1:]:
            with self.subTest(parser_candidate=variant):
                self.assertEqual(contenders.metadata(variant)["decisions"], ["H04"])
                self.assertEqual(contenders.metadata(variant)["implementation_scopes"],
                                 ["html_attr_membership_stage_only"])
        self.assertLessEqual(len(variants), experimental_design.MAX_VARIANTS)
        self.assertIn("table-html-selectors", variants)
        self.assertEqual(contenders.metadata("table-html-selectors")["implementation_scopes"], ["html_table_stage_only"])
        self.assertEqual(contenders.metadata("table-html-selectors")["build"]["env"][
            "ATHENA_RESEARCH_TABLE_EXTRACTOR"], "selectors")
        self.assertFalse(contenders.compatible("table-html-selectors", "aae-direct"))
        self.assertEqual(set(panels.get("I-JSON-PROJECT", [])), {
            "baseline", "projection-fused", "lib-json-sonic", "projection-fused+lib-json-sonic"})
        campaign_design = research.manifest_design()
        self.assertIn("I-JSON-PROJECT", campaign_design["required_interactions"])
        self.assertIn("I-HTML-ATTR-MEMBERSHIP", campaign_design["required_interactions"])
        self.assertIn("I-HTML-ATTR-MEMBERSHIP", research.readiness(campaign_design, {})["missing"])
        cases = research.synthetic_cases("screen")
        boundary_ids = {case["id"] for case in cases if case["id"].startswith("attribute-boundary-")}
        panel_plan = research.screen_plan(
            variants, {"I-HTML-ATTR-MEMBERSHIP": panels["I-HTML-ATTR-MEMBERSHIP"]}, cases)
        self.assertTrue(boundary_ids)
        duplicate_ids = {case["id"] for case in cases if case["id"].startswith("attribute-duplicate-")}
        self.assertTrue(boundary_ids.issubset(
            set(panel_plan["panel_cases"]["I-HTML-ATTR-MEMBERSHIP"])))
        self.assertTrue(duplicate_ids.issubset(
            set(panel_plan["panel_cases"]["I-HTML-ATTR-MEMBERSHIP"])))
        for variant in panels["I-HTML-ATTR-MEMBERSHIP"]:
            if variant == "baseline":
                continue
            with self.subTest(attribute_stage_scope=variant):
                selected_cases = research._variant_cases(variant, cases)
                self.assertTrue(selected_cases)
                self.assertEqual({case["stage"] for case in selected_cases}, {"parser"})
        confirm_scoped_cases = research._variant_cases(
            "html-attrs-hybrid32", research.synthetic_cases("confirm"))
        self.assertTrue(confirm_scoped_cases)
        self.assertEqual({case["stage"] for case in confirm_scoped_cases}, {"parser"})
        panel_case_ids = set(panel_plan["panel_cases"]["I-HTML-ATTR-MEMBERSHIP"])
        self.assertEqual({case["stage"] for case in cases if case["id"] in panel_case_ids}, {"parser"})
        composed = research.compose_finalist(["aae-direct", "html-attrs-hybrid32"])
        composed_case_ids = {case["id"] for case in research._variant_cases(composed, cases)}
        self.assertTrue({"pipeline-tiny-plain", "hash-article", "attribute-boundary-8"}
                        .issubset(composed_case_ids))
        decisions = json.loads((research.ROOT / "cloud_experiments/core_decisions.json").read_text(encoding="utf-8"))
        self.assertIn("I-JSON-PROJECT", {item["id"] for item in decisions["interactions"]})
        attr_panel = next(item for item in decisions["interactions"]
                          if item["id"] == "I-HTML-ATTR-MEMBERSHIP")
        self.assertEqual(attr_panel["status"], "IMPLEMENTED_UNVERIFIED")
        self.assertEqual(set(panels["I-HTML-ATTR-MEMBERSHIP"]),
                         set(experimental_design.native_design()["factorial_panels"]["I-HTML-ATTR-MEMBERSHIP"]))
        with self.assertRaises(ValueError):
            contenders.metadata("lib-blake3-pure+lib-blake3-intrinsics")
        factor_keys = [research.identity(contenders.metadata(v)["factors"]) for v in variants]
        self.assertEqual(len(factor_keys), len(set(factor_keys)))

        from cloud_experiments import workloads
        cases = workloads.native_cases("screen")
        html_cases = [case for case in cases if case["stage"] == "html-table"]
        self.assertEqual({case["id"] for case in html_cases}, {"html-table-spans", "html-table-nested"})
        self.assertTrue(all(case["owners"] == ["H06"] for case in html_cases))
        assignments = research.screen_plan(
            ["baseline", "table-html-selectors"], {"I-HTML-TABLE": panels["I-HTML-TABLE"]}, cases)
        expected_ids = {case["id"] for case in html_cases}
        self.assertEqual(set(assignments["panel_cases"]["I-HTML-TABLE"]), expected_ids)
        self.assertEqual(set(assignments["assignments"]["table-html-selectors"]), expected_ids)
        self.assertNotIn("html-table-spans", assignments["cross_cases"])

        fixture = html_cases[0]["fixture"]
        observation = {"schema": "athena-html-table-candidate/2", "parse_warnings": 0,
                       "header_associations": "explicit-attributes-only",
                       "cell_text": "normalized DOM text, excluding nested-table descendants from each cell",
                       "tables": fixture["expected_html_tables"]}
        encoded = json.dumps(observation, separators=(",", ":")).encode()
        self.assertEqual(workloads.envelope_errors(encoded, fixture), [])
        corrupted = json.loads(encoded)
        corrupted["tables"][0]["rows"][1]["cells"][0]["rowspan"] = 1
        self.assertTrue(workloads.envelope_errors(
            json.dumps(corrupted, separators=(",", ":")).encode(), fixture))

        nested_fixture = next(case["fixture"] for case in html_cases if case["id"] == "html-table-nested")
        nested_observation = {"schema": "athena-html-table-candidate/2", "parse_warnings": 0,
                              "header_associations": "explicit-attributes-only",
                              "cell_text": "normalized DOM text, excluding nested-table descendants from each cell",
                              "tables": copy.deepcopy(nested_fixture["expected_html_tables"])}
        nested_observation["tables"][0]["rows"][0]["cells"][0]["text"] = "outer inner after"
        self.assertTrue(workloads.envelope_errors(
            json.dumps(nested_observation, separators=(",", ":")).encode(), nested_fixture),
            "the independent table oracle must reject nested-table text leaking into its parent cell")

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
            self.assertTrue((workspace / "cloud_experiments/mcp_sdk/task_inputs.json").is_file())
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
        expected_attribute_boundary_counts = {
            0, 1, 2, 4, 7, 8, 9, 16, 31, 32, 33, 64, 65, 127, 128, 129}
        expected_duplicate_thresholds = {8, 32, 128}
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
                boundary_cases = [case for case in split_cases
                                  if case["id"].startswith("attribute-boundary-")]
                observed_boundaries = {
                    int(case["id"].rsplit("-", 1)[1]) for case in boundary_cases}
                self.assertEqual(observed_boundaries, expected_attribute_boundary_counts)
                for boundary_case in boundary_cases:
                    count = int(boundary_case["id"].rsplit("-", 1)[1])
                    html = boundary_case["fixture"]["html"]
                    with self.subTest(split=split, attribute_boundary=count):
                        self.assertEqual(boundary_case["stage"], "parser")
                        self.assertEqual(boundary_case["owners"], ["H04"])
                        self.assertEqual(html.count("='x'"), count)
                        self.assertLessEqual(len(html.encode("utf-8")), limits["max_fixture_html_bytes"])
                duplicate_cases = [case for case in split_cases
                                   if case["id"].startswith("attribute-duplicate-")]
                duplicate_thresholds = {
                    int(case["id"].rsplit("-", 1)[1]) for case in duplicate_cases}
                self.assertEqual(duplicate_thresholds, expected_duplicate_thresholds)
                for duplicate_case in duplicate_cases:
                    threshold = int(duplicate_case["id"].rsplit("-", 1)[1])
                    html = duplicate_case["fixture"]["html"]
                    with self.subTest(split=split, duplicate_threshold=threshold):
                        self.assertEqual(duplicate_case["stage"], "parser")
                        self.assertEqual(duplicate_case["owners"], ["H04"])
                        self.assertIn('data-a00000="first"',
                                      duplicate_case["fixture"]["expected_parser_html_contains"])
                        self.assertIn('data-a00000="replacement"',
                                      duplicate_case["fixture"]["expected_parser_html_excludes"])
                        self.assertEqual(html.count("='x'"), threshold - 1)
                        self.assertEqual(html.count("='replacement'"), 1)
                        self.assertEqual(html.count("='first'"), 1)
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
            manifest_path = root / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            bundle_member = ('"bundle_id":"' + design["study_id"] + '"').encode("ascii")
            self.assertIn(bundle_member, manifest_bytes)
            manifest_path.write_bytes(manifest_bytes.replace(
                bundle_member, ('"bundle_id":"' + "0" * 64 + '","bundle_id":"'
                                + design["study_id"] + '"').encode("ascii"), 1))
            with self.assertRaisesRegex(ValueError, "Duplicate JSON object member"):
                research.load_confirmation(root, "v")
            manifest_path.write_bytes(manifest_bytes)
            raw_bytes = raw.read_bytes()
            variant_member = b'"variant": "v"'
            self.assertIn(variant_member, raw_bytes)
            raw.write_bytes(raw_bytes.replace(
                variant_member, b'"variant": "other", "variant": "v"', 1))
            with self.assertRaisesRegex(ValueError, "Duplicate JSON object member"):
                research.load_confirmation(root, "v")
            raw.write_bytes(raw_bytes)
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
            for relative in (
                "Cargo.toml",
                "Cargo.lock",
                "rust-toolchain.toml",
                "src/projection.rs",
                "model/benchmark.py",
                "model/test_benchmark.py",
                "cloud_experiments/research.py",
                "cloud_experiments/test_campaign.py",
                "cloud_experiments/lexbor/build_and_smoke.sh",
                "cloud_experiments/lexbor/canary.c",
                "cloud_experiments/lexbor_adapter/Cargo.toml",
                "cloud_experiments/lexbor_adapter/Cargo.lock",
                "cloud_experiments/lexbor_adapter/build.rs",
                "cloud_experiments/lexbor_adapter/src/lexbor_shim.c",
                "cloud_experiments/lexbor_adapter/src/lib.rs",
                "cloud_experiments/lexbor_adapter/src/tables.rs",
                "cloud_experiments/lexbor_adapter/tests/semantic_parity.rs",
                "cloud_experiments/mcp_sdk/.gitignore",
                "cloud_experiments/mcp_sdk/README.md",
                "cloud_experiments/mcp_sdk/__init__.py",
                "cloud_experiments/mcp_sdk/core_fixtures.json",
                "cloud_experiments/mcp_sdk/driver.py",
                "cloud_experiments/mcp_sdk/protocol_contract.json",
                "cloud_experiments/mcp_sdk/task_inputs.json",
                "cloud_experiments/mcp_sdk/rust/Cargo.toml",
                "cloud_experiments/mcp_sdk/rust/Cargo.lock",
                "cloud_experiments/mcp_sdk/rust/src/main.rs",
                "cloud_experiments/mcp_sdk/test_driver.py",
                "cloud_experiments/mcp_sdk/typescript/package.json",
                "cloud_experiments/mcp_sdk/typescript/package-lock.json",
                "cloud_experiments/mcp_sdk/typescript/server.mjs",
            ):
                packaged = root / "source" / relative
                current = research.ROOT / relative
                self.assertIn(relative, spec["files"], f"Frozen spec omitted required research input: {relative}")
                self.assertTrue(packaged.is_file(), f"Current research source was omitted: {relative}")
                canonical = campaign.canonical_source_bytes(current.read_bytes(), relative)
                self.assertEqual(spec["files"][relative], hashlib.sha256(canonical).hexdigest(),
                                 f"Frozen spec must bind the current canonical bytes for {relative}")
                self.assertEqual(
                    hashlib.sha256(packaged.read_bytes()).digest(),
                    hashlib.sha256(canonical).digest(),
                    f"Bundle must carry the current canonical bytes for {relative}",
                )
            selected = ["cloud_experiments.test_campaign.AgentBoundaryControls.test_compact_expansion_enforces_scope_revision_expiry_and_releases_state",
                        "cloud_experiments.test_campaign.ExpandedResearchControls.test_run_native_cli_exposes_incomplete_research_readiness",
                        "cloud_experiments.mcp_sdk.test_driver",
                        "kaggle_experiments.test_protocol.GeneratedEntryPointControls",
                        "kaggle_experiments.test_protocol.StatisticalRegressions.test_fast_median_does_not_accept_a_regressed_tail",
                        "model.test_models.NumericalContracts.test_declared_cost_scenario_is_exact",
                        "model.test_benchmark.BenchmarkFixtureOwnership.test_compare_preserves_neighbor_files_and_cleans_owned_fixtures"]
            script = """import os,sys,unittest
sys.path.insert(0,sys.argv[1]); os.chdir(sys.argv[1])
loader=unittest.defaultTestLoader
targets=[loader.loadTestsFromName(name) for name in sys.argv[2:]]
def collected(suite):
    return sum(1 for _ in suite)
missing=[name for name,suite in zip(sys.argv[2:],targets) if collected(suite)==0]
result=unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(targets))
if missing:
    print('Selected contract target collected no tests: '+', '.join(missing), file=sys.stderr)
sys.exit(0 if not missing and result.wasSuccessful() else 1)
"""
            command = [sys.executable, "-I", "-B", "-c", script, str(root / "source"), *selected]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            fixture = root / "source/cloud_experiments/mcp_sdk/core_fixtures.json"
            original = fixture.read_bytes()
            fixture.unlink()
            missing_fixture = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(missing_fixture.returncode, 0)
            self.assertIn("core_fixtures.json", missing_fixture.stderr)
            fixture.write_bytes(original)
            (root / "source/kaggle_results/verify_statistics.py").unlink()
            challenged = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(challenged.returncode, 0)
            self.assertIn("verify_statistics", challenged.stderr)

    def test_failed_research_prepare_leaves_no_partial_bundle_and_allows_retry(self):
        with tempfile.TemporaryDirectory(prefix="athena-prepare-recovery-") as folder:
            root = Path(folder)
            destination = root / "bundle"
            malformed_corpus = root / "malformed-corpus.json"
            malformed_corpus.write_text("{", encoding="utf-8")

            with self.assertRaises(ValueError):
                research.prepare(destination, corpus_manifest=malformed_corpus)

            self.assertFalse(destination.exists(), "A failed preparation must not reserve its destination")
            self.assertEqual(list(root.glob(".bundle.prepare-*")), [],
                             "A failed preparation must clean only its own staging directory")

            with self.assertRaisesRegex(ValueError, "Declare campaign duration"):
                research.prepare(destination, wall_seconds=0)
            self.assertFalse(destination.exists(), "A late failure must not publish an incomplete bundle")
            self.assertEqual(list(root.glob(".bundle.prepare-*")), [],
                             "A late failure must remove staging after the archive was written")

            result = research.prepare(destination)
            self.assertEqual(result["status"], "PREPARED_NOT_LAUNCH_APPROVED")
            self.assertTrue((destination / "source.tgz").is_file())
            self.assertTrue((destination / "spec.json").is_file())
            self.assertEqual(list(root.glob(".bundle.prepare-*")), [])

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
        retained = research.admit_resources({**estimate, "retained_export_bytes": 4096}, budget)
        self.assertFalse(retained["admitted"], "Retained failure archives must consume disk admission")
        self.assertIn("disk_capacity", retained["reasons"])
        self.assertEqual(retained["disk_with_export_bytes"], 9344)
        self.assertFalse(research.admit_resources({**estimate, "retained_export_bytes": -1}, budget)["admitted"])
        exact_deadline = research.admit_resources(estimate, {**budget, "deadline_seconds": 70200})
        self.assertTrue(exact_deadline["admitted"])
        self.assertEqual(exact_deadline["deadline_cost_usd"], "40.000000")
        deadline_cost = research.admit_resources(estimate, {**budget, "deadline_seconds": 70201})
        self.assertFalse(deadline_cost["admitted"],
                         "A low runtime forecast cannot authorize an unaffordable hard deadline")
        self.assertIn("deadline_cost_ceiling", deadline_cost["reasons"])
        self.assertEqual(deadline_cost["deadline_cost_usd"], "40.000556")
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
        h07_profile = {"status": "PROFILED_RESOURCE_ONLY", "profile_draws": 30000,
                       "repeat_wall_seconds": [1.0, 1.1, 1.2],
                       "estimated_full_analysis_seconds": 48,
                       "process_peak_rss_bytes": 4 * 1024 ** 3,
                       "numpy_version": "2.0.0", "synthetic_values_not_measurements": True}
        forecast_pilot = {"wall_seconds": 1, "baseline_build_wall_seconds": 1,
                          "peak_memory_bytes": 2048,
                          "service_analysis_resource_profile": h07_profile}
        forecast = research.forecast_native(["baseline", "aae-direct", "rank-binary"], phases,
            research.manifest_design(), forecast_pilot,
            {"not_scientific_samples": True, "seconds_per_case_draw_max": .001})
        legacy_forecast = research.forecast_native(["baseline", "aae-direct", "rank-binary"], phases,
            research.manifest_design(), {key: value for key, value in forecast_pilot.items()
                                         if key != "service_analysis_resource_profile"},
            {"not_scientific_samples": True, "seconds_per_case_draw_max": .001})
        self.assertEqual(forecast["maximum_confirm_cases"], 2 + 2 + 3)
        self.assertEqual(forecast["maximum_family"], 28)
        self.assertEqual(forecast["maximum_bootstrap_draws"], 112000)
        self.assertEqual(forecast["wpt_report_bytes_included"],
                         research.manifest_design()["limits"]["wpt_reports_total_bytes"])
        self.assertEqual(forecast["analysis_seconds"], 1568)
        self.assertGreater(forecast["h07_service_study_seconds_included"], 0)
        self.assertEqual(forecast["h07_service_study_evidence_bytes_included"], 768 * 1024 ** 2)
        self.assertEqual(forecast["required_evidence_bytes"] - legacy_forecast["required_evidence_bytes"],
                         forecast["h07_service_study_evidence_bytes_included"])
        self.assertEqual(forecast["wall_seconds"] - legacy_forecast["wall_seconds"],
                         forecast["h07_service_study_seconds_included"])
        self.assertGreater(forecast["export_seconds"], 0)
        self.assertEqual(forecast["shutdown_seconds"], 600)
        self.assertEqual(forecast["retained_export_bytes"], 2 * research.manifest_design()["limits"]["export_bytes"])
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
        missing_h07_memory = {**h07_profile, "process_peak_rss_bytes": None}
        with self.assertRaisesRegex(ValueError, "measured process peak RSS"):
            research.forecast_native(variants, full_cases, full_design,
                {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 1,
                 "service_analysis_resource_profile": missing_h07_memory},
                {"not_scientific_samples": True, "seconds_per_case_draw_max": 0})
        full = research.forecast_native(variants, full_cases, full_design,
            {"wall_seconds": 1, "baseline_build_wall_seconds": 1, "peak_memory_bytes": 1,
             "service_analysis_resource_profile": h07_profile,
             "service_analysis_profile_wall_seconds": 4},
            {"not_scientific_samples": True, "seconds_per_case_draw_max": 0})
        self.assertEqual(full["h07_service_study_evidence_bytes_included"], 768 * 1024 ** 2)
        self.assertGreaterEqual(full["peak_memory_bytes"], 8 * 1024 ** 3,
                                "H07 profiled peak RSS must affect full-matrix memory admission")
        self.assertLessEqual(full["required_evidence_bytes"], full["evidence_bytes"],
                             "Current complete matrix cannot fit its encoded-row evidence budget")
        self.assertLessEqual(full["export_bytes"], full_design["limits"]["export_bytes"])
        self.assertLessEqual(sum(full[key] for key in ("evidence_bytes", "build_bytes", "metadata_bytes",
                                                     "analysis_bytes", "export_bytes", "retained_export_bytes",
                                                     "fixture_bytes")) + full_design["limits"]["disk_reserve_bytes"],
                             full_design["limits"]["minimum_free_bytes"])

    def test_native_resume_disk_admission_credits_only_the_frozen_owned_run(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="athena-resume-disk-") as folder:
            root, output = Path(folder), Path(folder) / "run"
            design = {"study_id": "a" * 64, "limits": {
                "minimum_free_bytes": 1024, "disk_reserve_bytes": 64, "command_seconds": 30,
                "build_bytes": 1024, "evidence_bytes": 1024, "metadata_bytes": 128,
                "log_bytes": 128, "analysis_bytes": 128, "max_fixture_bytes": 256,
                "wpt_reports_total_bytes": 128, "export_bytes": 512}}
            request = {"pilot_only": False, "variants": None, "budget_sha256": "b" * 64}
            deadline = time.time() + 120
            run = research.ResearchRun(output, "admission", design["study_id"], design, isolate_children=True)
            run.manifest.update(execution_request=request, campaign_deadline_unix=deadline)
            run.finish(InterruptedError("controlled interruption"))
            original = (output / "manifest.json").read_bytes()
            arguments = {"resume": True, "execution_request": request, "campaign_deadline_unix": deadline}
            with patch.object(research.shutil, "disk_usage", return_value=SimpleNamespace(free=768)), patch.object(
                    research.lifecycle, "reusable_storage_bytes", return_value=512):
                previous, admission = research._native_disk_admission(output, design, {"free_bytes": 4096}, **arguments)
                self.assertEqual(previous["bundle_id"], design["study_id"])
                self.assertEqual(admission["reusable_allocated_bytes"], 512)
                self.assertEqual(admission["admission_bytes"], 1216)
                _, capped = research._native_disk_admission(output, design, {"free_bytes": 1000}, **arguments)
                self.assertEqual(capped["admission_bytes"], 1000)
                with self.assertRaisesRegex(ValueError, "Resume.*identity"):
                    research._native_disk_admission(output, design, {"free_bytes": 4096}, **{
                        **arguments, "execution_request": {**request, "budget_sha256": "c" * 64}})
                with self.assertRaisesRegex(RuntimeError, "Insufficient free capacity"):
                    research._native_disk_admission(output, design, {"free_bytes": 4096}, **{**arguments, "resume": False})
            with patch.object(research.shutil, "disk_usage", return_value=SimpleNamespace(free=32)), patch.object(
                    research.lifecycle, "reusable_storage_bytes", return_value=512):
                with self.assertRaises(research.lifecycle.StorageGuardError):
                    research._native_disk_admission(output, design, {"free_bytes": 4096}, **arguments)
            self.assertEqual((output / "manifest.json").read_bytes(), original)
            for snapshot in range(3):
                (root / ("run-evidence-" + str(snapshot) * 64 + ".tgz")).write_bytes(b"retained")
            with patch.object(research.shutil, "disk_usage", return_value=SimpleNamespace(free=4096)):
                with self.assertRaises(research.lifecycle.StorageGuardError):
                    research._native_disk_admission(output, design, {"free_bytes": 4096}, **arguments)

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


    def confirmation_analysis_fixture(self, root, *, atomic=False):
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
        if atomic:
            from cloud_experiments import lifecycle
            manifest.update(collection_layout="atomic-blocks/1", environment_binding="f" * 64)
            context = {"protocol": research.PROTOCOL, "study_id": design["study_id"],
                       "environment": manifest["environment_binding"], "source_sha256": design["source_sha256"]}
            journal = lifecycle.CollectionJournal(root, context, 2 * 1024 ** 2, "a" * 32)
            for block in range(design["confirm"]["blocks"]):
                descriptor = {"channel": "confirm:v", "binding": {"freeze_id": frozen["freeze_id"]}, "block": block}
                list(journal.collect(descriptor, lambda b=block: (row for row in rows if row["block"] == b)))
            manifest["sample_shards"] = journal.shards
            (root / "manifest.json").write_bytes(research.canonical(manifest))
            return root / journal.shards[0]
        raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return raw

    def test_confirmation_metadata_obeys_existing_lifecycle_byte_bounds(self):
        with tempfile.TemporaryDirectory(prefix="athena-analysis-limits-") as folder:
            root = Path(folder)
            self.confirmation_analysis_fixture(root)
            for name, limit in (("manifest.json", 16 * 1024 ** 2),
                                ("confirmation-freeze.json", 8 * 1024 ** 2)):
                path = root / name
                original = path.read_bytes()
                with self.subTest(name=name):
                    try:
                        remaining = limit - len(original)
                        with path.open("ab") as stream:
                            while remaining:
                                chunk = b" " * min(65536, remaining)
                                stream.write(chunk)
                                remaining -= len(chunk)
                        self.assertEqual(research.load_confirmation(root, "v")[0]["fast"][0],
                                         {"baseline": [100, 100], "candidate": [80, 80]})
                        with path.open("ab") as stream:
                            stream.write(b" ")
                        with self.assertRaisesRegex(ValueError, "byte bound"):
                            research.load_confirmation(root, "v")
                    finally:
                        path.write_bytes(original)

    def test_confirmation_stops_at_frozen_row_count_before_decoding_corrupt_tail(self):
        with tempfile.TemporaryDirectory(prefix="athena-analysis-row-cap-") as folder:
            root = Path(folder)
            raw = self.confirmation_analysis_fixture(root)
            groups, _, _ = research.load_confirmation(root, "v")
            self.assertEqual(len(groups["fast"]), 20)
            self.assertEqual(groups["fast"][0], {"baseline": [100, 100], "candidate": [80, 80]})
            original = raw.read_bytes()
            extra = root / "samples-0001.jsonl"
            extra.write_bytes(original.splitlines(keepends=True)[0] + b"unreachable corrupt tail\n")
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sample_shards"].append(extra.name)
            manifest_path.write_bytes(research.canonical(manifest))
            with self.assertRaisesRegex(ValueError, "Excess native observations for frozen cohort: fast"):
                research.load_confirmation(root, "v")
            self.assertEqual(raw.read_bytes(), original)
            self.assertEqual(extra.read_bytes(), original.splitlines(keepends=True)[0] + b"unreachable corrupt tail\n")

    def test_confirmation_analysis_cannot_write_into_committed_collection_blocks(self):
        with tempfile.TemporaryDirectory(prefix="athena-analysis-inputs-") as folder:
            root = Path(folder)
            raw = self.confirmation_analysis_fixture(root, atomic=True)
            receipt = raw.parent / "receipt.json"
            original = {path.name: path.read_bytes() for path in raw.parent.iterdir()}
            research.load_confirmation(root, "v")
            for output in (receipt, raw.parent / "analysis.json", root / "bins/baseline/analysis.json"):
                with self.subTest(output=output.relative_to(root)), self.assertRaisesRegex(ValueError, "replace required evidence"):
                    research.analyze_confirmation(root, "v", output)
            self.assertEqual({path.name: path.read_bytes() for path in raw.parent.iterdir()}, original)
            self.assertEqual({path.name for path in (root / "bins/baseline").iterdir()}, {"core_probe"})

    def test_confirmation_checkpoint_binds_consumed_collection_receipts(self):
        with tempfile.TemporaryDirectory(prefix="athena-analysis-receipts-") as folder:
            root = Path(folder)
            raw = self.confirmation_analysis_fixture(root, atomic=True)
            receipt = raw.parent / "receipt.json"
            original_receipt = receipt.read_bytes()
            output = root / "report.json"
            initial = research.analyze_confirmation(root, "v", output)
            self.assertEqual(initial["evidence"]["environment_binding"], "f" * 64)
            output_bytes = output.read_bytes()
            progress = root / "report.analysis-progress.json"
            progress_bytes = progress.read_bytes()
            changed = json.loads(original_receipt)
            changed["collection_wall_seconds"] += 1
            receipt.write_bytes(research.canonical(changed))
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                research.analyze_confirmation(root, "v", output)
            self.assertEqual(output.read_bytes(), output_bytes)
            self.assertEqual(progress.read_bytes(), progress_bytes)
            receipt.write_bytes(original_receipt)
            self.assertEqual(research.analyze_confirmation(root, "v", output), initial)
            expected = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (root / "collection").glob("*/receipt.json")}
            self.assertEqual(initial["evidence"]["receipt_inputs"], expected)

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
                deadline = time.monotonic() + 60
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

    def test_h07_provider_study_freezes_complete_randomized_factorial_before_sampling(self):
        from cloud_experiments import service_study

        design = service_study.make_design()
        self.assertEqual(design["protocol"], service_study.PROTOCOL)
        self.assertEqual(len(design["cells"]), 6)
        self.assertEqual(design["blocks"], 24)
        self.assertEqual(design["requests_per_cell"], 256,
                         "Each arm needs at least 50 tail observations for p99")
        planned = service_study.block_schedule(design)
        self.assertEqual(len(planned), 24)
        for block in planned:
            self.assertEqual(set(block["order"]), {cell["id"] for cell in design["cells"]})
            self.assertEqual(len(block["order"]), len(set(block["order"])))
        self.assertEqual(planned, service_study.block_schedule(design),
                         "A frozen seed must reproduce the same predeclared order")
        changed = service_study.make_design(seed=design["seed"] + 1)
        self.assertNotEqual(planned, service_study.block_schedule(changed))
        broken = copy.deepcopy(planned)
        broken[0]["order"].pop()
        with self.assertRaisesRegex(ValueError, "complete factorial"):
            service_study.validate_schedule(design, broken)

    def test_h07_analysis_rejects_missing_or_duplicate_block_cell_observations(self):
        from cloud_experiments import service_study

        design = service_study.make_design()
        scheduled = service_study.block_schedule(design)
        cells = {cell["id"]: cell for cell in design["cells"]}
        observations = [
            {"block": block["block"], "order": order, "cell": cell,
             "latencies_ns": [100] * design["requests_per_cell"], "process_cpu_ns": 1000,
             "peak_rss_kib": 4096, "completed": design["requests_per_cell"],
             "origin_requests": design["requests_per_cell"],
             "handshakes": cells[cell]["expected_handshakes"],
             "client_builds": cells[cell]["expected_client_builds"],
             "response_body_bytes": service_study.expected_body_write_bytes(design),
             "stdout_sha256": "a" * 64}
            for block in scheduled for order, cell in enumerate(block["order"])
        ]
        self.assertEqual(service_study.validate_observation_grid(design, scheduled, observations), [])
        with self.assertRaisesRegex(ValueError, "missing, duplicate, or unplanned"):
            service_study.validate_observation_grid(design, scheduled, observations[:-1])
        duplicate = copy.deepcopy(observations)
        duplicate[-1] = copy.deepcopy(duplicate[0])
        with self.assertRaisesRegex(ValueError, "missing, duplicate, or unplanned"):
            service_study.validate_observation_grid(design, scheduled, duplicate)
        wrong_handshake = copy.deepcopy(observations)
        wrong_handshake[0]["handshakes"] -= 1
        with self.assertRaisesRegex(ValueError, "handshake contract"):
            service_study.validate_observation_grid(design, scheduled, wrong_handshake)
        import time
        with self.assertRaisesRegex(TimeoutError, "admitted wall deadline"):
            service_study.analyze(design, scheduled, observations, draws=120000,
                                  deadline=time.monotonic() - 1)

    def test_h07_resource_profile_is_non_scientific_and_produces_a_bounded_reserve(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from cloud_experiments import research, service_study

        design = service_study.make_design()
        ticks = iter((0, .25, .25, .5, .5, .75))
        clock = SimpleNamespace(monotonic=lambda: 100.0, perf_counter=lambda: next(ticks))
        with patch.object(service_study, "time", clock), \
                patch.object(research, "bootstrap_intervals", return_value={}) as bootstrap:
            profile = service_study.resource_profile(design, max_wall_seconds=30)
        calls = bootstrap.call_args_list
        self.assertEqual(len(calls), design["profile_repeats"] * 3)
        self.assertEqual([call.args[1] for call in calls],
                         [[0.5, 0.95, 0.99], [0.5], [0.5]] * design["profile_repeats"])
        self.assertTrue(all(call.args[2] == design["profile_draws"] for call in calls))
        self.assertTrue(all(call.kwargs["deadline"] == 130.0 for call in calls))
        self.assertEqual(profile["status"], "PROFILED_RESOURCE_ONLY")
        self.assertTrue(profile["synthetic_values_not_measurements"])
        self.assertEqual(profile["repeat_wall_seconds"], [.25, .25, .25])
        self.assertEqual(profile["estimated_full_analysis_seconds"], 10)
        reserve = service_study.execution_reserve(design, profile, contract_scenarios=17)
        self.assertEqual(reserve["analysis_seconds"], 20)
        self.assertGreater(reserve["total_wall_seconds"], reserve["measurement_seconds"])
        self.assertEqual(reserve["maximum_evidence_bytes"], 768 * 1024 ** 2)
        self.assertEqual(reserve["maximum_block_bytes"], 30 * 1024 ** 2)
        self.assertLessEqual(design["article_fixture_bytes"], 4096)
        self.assertEqual(service_study.expected_body_write_bytes(design),
                         design["requests_per_cell"] * (len(b"<p>athena-lab tenant=A epoch=1 source=article version=1</p>\n")
                         + design["article_fixture_bytes"]))
        with self.assertRaisesRegex(ValueError, "resource-only profile"):
            service_study.execution_reserve(design, {"status": "PROFILED_RESOURCE_ONLY"}, contract_scenarios=17)
        with self.assertRaisesRegex(ValueError, "bounded resource-only profile"):
            service_study.execution_reserve(design,
                {**profile, "estimated_full_analysis_seconds": design["analysis_timeout_seconds"]},
                contract_scenarios=17)
        with self.assertRaisesRegex(ValueError, "bounded resource-only profile"):
            service_study.execution_reserve(design,
                {**profile, "repeat_wall_seconds": [12.5, .25, .25], "estimated_full_analysis_seconds": 500},
                contract_scenarios=17)

    def test_bootstrap_deadline_stops_between_bounded_batches(self):
        import time
        rounds = [{"baseline": [1, 2, 3], "candidate": [1, 2, 4]}] * 2
        with self.assertRaisesRegex(TimeoutError, "admitted wall deadline"):
            research.bootstrap_intervals(rounds, [0.5], 30000, 17, .001,
                                         deadline=time.monotonic() - 1)

    def test_warm_cargo_cache_warms_unique_lock_profiles_and_subprojects_then_verifies_offline(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        files = campaign.source_files(
            research.ROOT,
            ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock",
             "rust-toolchain.toml", "cloud_experiments", "kaggle_experiments/probe.rs"],
        )
        files = {k: v for k, v in files.items() if "__pycache__" not in Path(k).parts}
        design = research.manifest_design()
        design["files"] = {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}
        design["study_id"] = research.identity(design)

        with tempfile.TemporaryDirectory(prefix="athena-warm-cache-test-") as folder:
            root = Path(folder)
            source_dir = root / "source"
            for rel, data in files.items():
                dest = source_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            spec_path = root / "spec.json"
            campaign.write_json(spec_path, design)
            cache_dir = root / "cargo-target"

            commands = []
            observed_vendor_manifests = []
            observed_lexbor_parent_manifests = []

            def fake_runner(argv, *, cwd, timeout):
                cwd_path = Path(cwd)
                commands.append((tuple(argv), cwd_path.name, os.environ.get("CARGO_HOME")))
                if argv == ["rustc", "-Vv"]:
                    return b"rustc 1.99.1 (pinned-linux-toolchain)\n"
                vendor_manifest = cwd_path / "research-html5ever-0.39.0-hash" / "Cargo.toml"
                if vendor_manifest.is_file():
                    observed_vendor_manifests.append(vendor_manifest.name)
                if cwd_path.name == "lexbor_adapter":
                    observed_lexbor_parent_manifests.append((cwd_path.parent.parent / "Cargo.toml").is_file())
                return b""

            def fake_metadata(_run, workspace, **_kwargs):
                lock_bytes = (Path(workspace) / "Cargo.lock").read_bytes()
                return {
                    "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
                    "packages": {"athena": {"name": "athena", "version": "0.1.0", "source": None}},
                }

            selected_variants = ["baseline", "aae-direct", "lib-json-sonic", "html-attrs-hash"]
            with (
                patch.object(research.libraries, "offline_dependency_metadata", side_effect=fake_metadata),
                patch.object(research.libraries, "verify_graph", return_value={"status": "TEST_CONTROL"}),
            ):
                report = research.warm_cargo_cache(
                    source_dir, spec_path, cache_dir,
                    variants=selected_variants, include_subprojects=True,
                    stage_sdk_artifacts=True, runner_fn=fake_runner,
                )

            self.assertEqual(report["status"], "OFFLINE_CACHE_READY")
            self.assertEqual(report["planned_variants"],
                             ["baseline", "aae-direct", "lib-json-sonic", "html-attrs-hash", "lib-rustls-ring"])
            self.assertEqual(len(report["verified_profiles"]), 3)
            self.assertEqual([item["project"] for item in report["subprojects"]],
                             ["cloud_experiments/mcp_sdk/rust", "cloud_experiments/lexbor_adapter"])
            self.assertEqual(report["staged_sdk_artifacts"]["status"], "SDK_ARTIFACTS_STAGED")
            self.assertGreaterEqual(report["cache_bytes"], 0)
            rust_name = "athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture"
            probe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
            self.assertEqual(
                Path(report["staged_sdk_artifacts"]["core_probe"]),
                cache_dir.resolve() / "release" / probe_name,
            )
            self.assertEqual(
                Path(report["staged_sdk_artifacts"]["rust_sdk_fixture"]),
                cache_dir.resolve() / "mcp-sdk-rust-target" / "release" / rust_name,
            )
            self.assertEqual(
                Path(report["staged_sdk_artifacts"]["typescript_server"]),
                cache_dir.resolve() / "mcp-sdk-ts" / "typescript" / "server.mjs",
            )
            self.assertEqual(
                Path(report["staged_sdk_artifacts"]["lexbor_work_root"]),
                cache_dir.resolve() / "lexbor-v3.0.0",
            )
            self.assertEqual(
                Path(report["staged_sdk_artifacts"]["lexbor_adapter_target"]),
                cache_dir.resolve() / "lexbor-adapter-target",
            )
            self.assertTrue((cache_dir / "mcp-sdk-ts" / "protocol_contract.json").is_file())
            self.assertTrue((cache_dir / "mcp-sdk-ts" / "core_fixtures.json").is_file())
            self.assertTrue((cache_dir / "mcp-sdk-ts" / "typescript" / "server.mjs").is_file())
            self.assertFalse((source_dir / "cloud_experiments" / "mcp_sdk" / "typescript" / "node_modules").exists())
            self.assertTrue(observed_vendor_manifests,
                            "Attribute-index lock profile warmup must materialize the local html5ever vendor crate")
            self.assertTrue(observed_lexbor_parent_manifests and all(observed_lexbor_parent_manifests),
                            "lexbor_adapter subproject warmup must preserve ../.. parent Cargo.toml resolution")
            online_fetches = [cmd for cmd, _, _ in commands
                              if cmd == ("cargo", "fetch", "--locked", "--target", "x86_64-unknown-linux-gnu")]
            offline_fetches = [cmd for cmd, _, _ in commands
                               if cmd == ("cargo", *research.process_isolation.CARGO_FETCH_ARGS)]
            npm_installs = [cmd for cmd, _, _ in commands
                            if cmd == ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")]
            lexbor_smokes = [cmd for cmd, _, _ in commands
                             if len(cmd) == 3 and cmd[0] == "bash" and cmd[1].endswith("build_and_smoke.sh")]
            self.assertEqual(len(online_fetches), 3 + 2)
            self.assertEqual(len(offline_fetches), 3 + 2)
            self.assertEqual(len(npm_installs), 1)
            self.assertEqual(len(lexbor_smokes), 1)
            self.assertEqual(list(cache_dir.glob("athena-cargo-cache-warm-*")), [])

            with self.assertRaisesRegex(ValueError, "outside the frozen source tree"):
                research.warm_cargo_cache(
                    source_dir, spec_path, source_dir / "inside-cache",
                    variants=["baseline"], include_subprojects=False, runner_fn=fake_runner,
                )

            cargo_config = root / ".cargo" / "config.toml"
            cargo_config.parent.mkdir(parents=True, exist_ok=True)
            cargo_config.write_text('[source.crates-io]\nreplace-with = "unreviewed"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cargo configuration"):
                research.warm_cargo_cache(
                    source_dir, spec_path, cache_dir,
                    variants=["baseline"], include_subprojects=False, runner_fn=fake_runner,
                )
            self.assertEqual(list(cache_dir.glob("athena-cargo-cache-warm-*")), [])

            cargo_config.unlink()
            bounded_envs = []

            def fake_bounded_process(argv, *, cwd=None, timeout=600, output_limit=0, env=None):
                del cwd, timeout, output_limit
                bounded_envs.append(dict(env or {}))
                stdout = b"rustc 1.99.1 (pinned-linux-toolchain)\n" if list(argv) == ["rustc", "-Vv"] else b""
                return research.runner.BoundedProcessResult(0, stdout, b"", "EXITED", 1, 1024)

            with (
                patch.dict(os.environ, {"ATHENA_LEXBOR_ARCHIVE_CACHE": str(cache_dir / "lexbor-archives")}, clear=False),
                patch.object(research.runner, "run_bounded_process", side_effect=fake_bounded_process),
                patch.object(research.libraries, "offline_dependency_metadata", side_effect=fake_metadata),
                patch.object(research.libraries, "verify_graph", return_value={"status": "TEST_CONTROL"}),
            ):
                default_report = research.warm_cargo_cache(
                    source_dir, spec_path, cache_dir,
                    variants=["baseline"], include_subprojects=False,
                )
            self.assertEqual(default_report["status"], "OFFLINE_CACHE_READY")
            self.assertTrue(any(env.get("CARGO_HOME") == str((cache_dir / "cargo-home").resolve()) for env in bounded_envs))
            self.assertTrue(all(env.get("ATHENA_LEXBOR_ARCHIVE_CACHE") == str(cache_dir / "lexbor-archives") for env in bounded_envs))

            timed_out = research.runner.BoundedProcessResult(None, b"", b"", "TIMEOUT", 1, 1024)
            with patch.object(research.runner, "run_bounded_process", return_value=timed_out):
                with self.assertRaisesRegex(TimeoutError, "Cargo cache warmup command timed out"):
                    research.warm_cargo_cache(
                        source_dir, spec_path, cache_dir,
                        variants=["baseline"], include_subprojects=False,
                    )

            stdout_buf = StringIO()
            argv = [
                "research", "warm-cache", str(source_dir), str(spec_path), str(cache_dir),
                "--pilot-only", "--skip-subprojects", "--stage-sdk-artifacts",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(research, "warm_cargo_cache", return_value=report) as mocked_warm,
                redirect_stdout(stdout_buf),
            ):
                self.assertEqual(research.main(), 0)
            mocked_warm.assert_called_once_with(
                str(source_dir), str(spec_path), str(cache_dir),
                variants=None, pilot_only=True, include_subprojects=False,
                stage_sdk_artifacts=True,
            )
            self.assertEqual(json.loads(stdout_buf.getvalue())["status"], "OFFLINE_CACHE_READY")

    def test_staged_language_panels_and_technical_coverage_preserve_incomplete_readiness(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="athena-tech-cov-") as folder:
            root = Path(folder)
            source = root / "source"
            output = root / "output"
            cache = root / "cargo-target"
            bins = output / "bins" / "baseline"
            for directory in (source, output, cache, bins):
                directory.mkdir(parents=True, exist_ok=True)
            rust_name = "athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture"
            probe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
            rust_bin = cache / "mcp-sdk-rust-target" / "release" / rust_name
            ts_server = cache / "mcp-sdk-ts" / "typescript" / "server.mjs"
            ts_pkg = ts_server.parent / "node_modules" / "@modelcontextprotocol" / "server" / "package.json"
            core_probe = cache / "release" / probe_name
            for path in (rust_bin, ts_server, ts_pkg, core_probe):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"staged")
            lexbor_commit = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe"
            lexbor_src = cache / "lexbor-v3.0.0" / f"lexbor-{lexbor_commit}" / "CMakeLists.txt"
            lexbor_lib = cache / "lexbor-v3.0.0" / "build" / "liblexbor_static.a"
            for path in (lexbor_src, lexbor_lib):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"lexbor")

            design = research.manifest_design()
            run = research.ResearchRun(output / "run", "expanded-native", "a" * 64, design)
            try:
                observed_driver_argv = []
                observed_cargo_argv = []

                def fake_run(cmd, **_kwargs):
                    argv = [str(x) for x in cmd]
                    if "cloud_experiments.mcp_sdk.driver" in argv:
                        observed_driver_argv.append(argv)
                        out_idx = argv.index("--out") + 1
                        campaign.write_json(Path(argv[out_idx]), {
                            "schema": "athena-mcp-core-integration-preflight/1",
                            "status": "PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT",
                        })
                    return b""

                def fake_isolated_cargo(_run, cmd, **_kwargs):
                    observed_cargo_argv.append([str(x) for x in cmd])
                    return b""

                isolated_env = {
                    key: ""
                    for key in (
                        "ATHENA_MCP_RUST_BIN",
                        "ATHENA_MCP_TS_SERVER",
                        "ATHENA_CORE_PROBE_BIN",
                        "ATHENA_LEXBOR_SOURCE_DIR",
                        "ATHENA_LEXBOR_BUILD_DIR",
                        "ATHENA_LEXBOR_LIB_DIR",
                        "ATHENA_LEXBOR_ADAPTER_TARGET_DIR",
                        "ATHENA_LEXBOR_WORK_ROOT",
                    )
                }
                with (
                    patch.dict(os.environ, isolated_env, clear=False),
                    patch.object(run, "run", side_effect=fake_run),
                    patch.object(research, "_run_isolated_cargo", side_effect=fake_isolated_cargo),
                    patch.object(research.shutil, "which", return_value=str(core_probe)),
                ):
                    research._run_staged_language_panel_checks(
                        run, source, run.directory, cache, bins,
                    )

                self.assertEqual(len(observed_driver_argv), 1)
                self.assertIn(str(rust_bin), observed_driver_argv[0])
                self.assertIn(str(ts_server), observed_driver_argv[0])
                self.assertIn(str(core_probe), observed_driver_argv[0])
                self.assertEqual(len(observed_cargo_argv), 1)
                self.assertIn(str(cache / "lexbor-adapter-target"), observed_cargo_argv[0])

                campaign.write_json(run.directory / "screen-interactions.json", {
                    "panels": {
                        panel_id: {
                            "status": "COMPLETE_CELLS",
                            "cohorts": ["parse-article"],
                            "contrasts": [{"kind": "interaction"}],
                        }
                        for panel_id in ("I-AAE", "I-RANK", "I-PROJECT", "I-COMPILER", "I-HTML-TABLE")
                    },
                })
                campaign.write_json(run.directory / "screen-cross-coverage.json", {
                    "status": "COVERED",
                    "required_tuples": 395,
                    "covered_tuples": 395,
                    "requirements_sha256": "e" * 64,
                    "measurement_status": "SCREEN_ONLY_NO_CONFIRMATION",
                })
                campaign.write_json(run.directory / "screening.json", {
                    "aae-direct": {"owners": ["H01"], "correct": True, "ratios": {"hash-article": 0.94}},
                    "rank-binary": {"owners": ["H02"], "correct": True, "ratios": {"rank-1-dense": 0.91}},
                    "prepared": {"owners": ["H03"], "correct": True, "ratios": {"prepared-8": 0.85}},
                    "projection-fused": {"owners": ["H04", "H05"], "correct": True, "ratios": {"parse-article": 0.96}},
                    "table-sparse": {"owners": ["H06"], "correct": True, "ratios": {"table-narrow": 0.98}},
                    "lib-scraper-modern": {"owners": ["H04", "H05", "H09"], "correct": True, "ratios": {"parse-article": 0.97}},
                    "build-lto-full": {"owners": ["H10"], "correct": True, "ratios": {"pipeline-article": 0.93}},
                })
                campaign.write_json(run.directory / "selection.json", {
                    "selected": ["aae-direct", "rank-binary"],
                    "owner_selected": ["aae-direct", "rank-binary"],
                    "confirmation_selected": ["aae-direct", "rank-binary", "aae-direct+rank-binary"],
                    "combined_finalist": {"variant": "aae-direct+rank-binary"},
                })
                run.manifest["wpt_reports"] = {"baseline": {"sha256": "b" * 64}}
                run.manifest["wpt_comparisons"] = {"html-attrs-hash": {"status": "SEMANTICALLY_IDENTICAL"}}
                run.manifest["h07_service_study"] = {
                    "status": "COMPLETE",
                    "analysis_sha256": "c" * 64,
                    "study_manifest_sha256": "d" * 64,
                    "performance_decision": "REJECT_SERVICE_POOLING_CONTENDERS",
                }
                run.manifest["candidate_decisions"] = {
                    "aae-direct": {"synthetic-small": "ADOPT_CANDIDATE"},
                }
                technical = research._update_technical_coverage(run)
                self.assertEqual(technical["L03"]["status"], "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED")
                self.assertEqual(technical["L01"]["status"], "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED")
                self.assertEqual(technical["I-HTML-ATTR-MEMBERSHIP"]["wpt_report_variants"], ["baseline"])
                self.assertEqual(technical["I-NATIVE-CROSS-COVERAGE"]["covered_tuples"], 395)
                self.assertEqual(technical["I-AAE"]["panel_status"], "COMPLETE_CELLS")
                self.assertEqual(technical["I-LANG-PARSE"]["l01_artifact"], "l01-lexbor-semantic-parity.json")
                self.assertEqual(technical["I-HTML-TABLE"]["l01_artifact"], "l01-lexbor-semantic-parity.json")
                self.assertEqual(technical["I-LANG-AGENT"]["l03_artifact"], "l03-mcp-sdk-preflight.json")
                self.assertEqual(technical["I-AGENT"]["l03_artifact"], "l03-mcp-sdk-preflight.json")
                self.assertEqual(technical["H07"]["analysis_sha256"], "c" * 64)
                self.assertEqual(technical["I-SYSTEM"]["h07_analysis_sha256"], "c" * 64)
                self.assertEqual(technical["H14"]["combined_finalist"], {"variant": "aae-direct+rank-binary"})
                self.assertIn("aae-direct", technical["H01"]["screened_variants"])
                self.assertIn("aae-direct", technical["H01"]["confirmed_variants"])
                for idx in range(1, 15):
                    decision_id = f"H{idx:02}"
                    self.assertEqual(
                        technical[decision_id]["status"],
                        "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED",
                        f"{decision_id} should have a subset receipt",
                    )
                    self.assertFalse(technical[decision_id]["runnable_verified"])
                    self.assertTrue(
                        bool(technical[decision_id].get("screened_variants")),
                        f"{decision_id} should record screened_variants from screening.json",
                    )
                coverage = research.readiness(design, technical)
                self.assertEqual(coverage["status"], "INCOMPLETE")
                self.assertIn("L01", coverage["blocking_language_panels"])
                self.assertIn("L03", coverage["blocking_language_panels"])
                self.assertEqual(
                    coverage["language_panel_records"]["L03"]["artifact"],
                    "l03-mcp-sdk-preflight.json",
                )
                self.assertEqual(
                    coverage["language_panel_records"]["L01"]["artifact"],
                    "l01-lexbor-semantic-parity.json",
                )
            finally:
                run.sample_stream.close()


class AgentPanelAggregationControls(unittest.TestCase):
    def _write_batch(self, root, name, cards, *, representation="full", partial_task=None,
                     model_boundary="LIVE_NVIDIA_NIM"):
        corpus_path = Path(agent_lab.__file__).with_name("agent_tasks.json")
        corpus_sha256 = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
        task_rows = []
        for card in cards:
            complete = card["id"] != partial_task
            task_rows.append({
                "task_id": card["id"],
                "family": card["family"],
                "split": card["split"],
                "execution_status": "COMPLETE" if complete else "PARTIAL",
                "actual_model": True,
                "model_boundary": model_boundary,
                "model_requests": [{"model": "nvidia/nemotron-3.5-bolt-e4b"}],
                "usage": {"requests": 1, "reported_total_tokens": 20},
                "quality": {"contract_satisfied": card["id"] != "F01-screen"},
            })
        config = {
            "provider": "nim",
            "model": "nvidia/nemotron-3.5-bolt-e4b",
            "policy": "W1",
            "representation": representation,
            "request_cap": 8 * len(cards),
            "selected_task_ids": [card["id"] for card in cards],
            "task_corpus_sha256": corpus_sha256,
        }
        report = {
            "protocol": research.PROTOCOL,
            "kind": "native-agent-task-panel",
            "producer_sha256": "a" * 64,
            "policy": "W1",
            "representation": representation,
            "status": "PANEL_EXECUTED",
            "decision_sufficiency": "NOT_EVALUATED",
            "result_authenticity": research.AUTHENTICITY,
            "run_configuration": config,
            "tasks": task_rows,
            "task_successes": sum(row["quality"]["contract_satisfied"] for row in task_rows),
            "task_denominator": len(task_rows),
            "attempted_tasks": len(task_rows),
        }
        path = root / name
        campaign.write_json(path, report)
        return path

    def test_merge_agent_panel_shards_preserves_all_outcomes_and_provenance(self):
        merge = getattr(research, "merge_agent_panels", None)
        self.assertTrue(callable(merge), "merge_agent_panels must implement the complete shard contract")
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-") as directory:
            root = Path(directory)
            inputs = [
                self._write_batch(root, "screen-a.json", cards[:4]),
                self._write_batch(root, "screen-b.json", cards[4:]),
            ]
            output = root / "merged.json"
            result = merge(inputs, output, split="screen")

            expected_ids = [row["id"] for row in cards]
            self.assertEqual(result["status"], "PANEL_EXECUTED")
            self.assertEqual([row["task_id"] for row in result["tasks"]], expected_ids)
            self.assertEqual(result["task_denominator"], 8)
            self.assertEqual(result["task_successes"], 7)
            self.assertEqual(result["decision_sufficiency"], "NOT_EVALUATED")
            self.assertEqual(len(result["batches"]), 2)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)

    def test_merge_agent_cli_writes_the_full_panel_summary(self):
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-cli-") as directory:
            root = Path(directory)
            inputs = [
                self._write_batch(root, "cli-a.json", cards[:4]),
                self._write_batch(root, "cli-b.json", cards[4:]),
            ]
            output = root / "cli-merged.json"
            command = [
                sys.executable, "-B", "-m", "cloud_experiments.research", "merge-agent",
                str(output), *(str(path) for path in inputs), "--split", "screen",
            ]
            completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                       capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            summary = json.loads(completed.stdout)
            merged = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "PANEL_EXECUTED")
            self.assertEqual(summary["task_denominator"], 8)
            self.assertEqual(merged["task_successes"], 7)

    def test_merge_agent_panel_rejects_coverage_and_condition_mismatches(self):
        merge = getattr(research, "merge_agent_panels", None)
        self.assertTrue(callable(merge), "merge_agent_panels must reject invalid shard evidence")
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-invalid-") as directory:
            root = Path(directory)
            duplicate = [
                self._write_batch(root, "duplicate-a.json", cards[:4]),
                self._write_batch(root, "duplicate-b.json", cards[3:7]),
            ]
            with self.assertRaisesRegex(ValueError, "AGENT_PANEL_COVERAGE_MISMATCH"):
                merge(duplicate, root / "duplicate-out.json", split="screen")

            mixed = [
                self._write_batch(root, "mixed-a.json", cards[:4]),
                self._write_batch(root, "mixed-b.json", cards[4:], representation="compact"),
            ]
            with self.assertRaisesRegex(ValueError, "AGENT_PANEL_MIXED_CONDITION"):
                merge(mixed, root / "mixed-out.json", split="screen")

    def test_merge_agent_panel_rejects_a_partial_task_without_hiding_its_raw_batch(self):
        merge = getattr(research, "merge_agent_panels", None)
        self.assertTrue(callable(merge), "merge_agent_panels must reject incomplete task execution")
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-partial-") as directory:
            root = Path(directory)
            inputs = [
                self._write_batch(root, "partial-a.json", cards[:4], partial_task=cards[0]["id"]),
                self._write_batch(root, "partial-b.json", cards[4:]),
            ]
            output = root / "partial-out.json"
            with self.assertRaisesRegex(ValueError, "AGENT_PANEL_INCOMPLETE_TASK"):
                merge(inputs, output, split="screen")
            self.assertTrue(all(path.is_file() for path in inputs))
            self.assertFalse(output.exists())

    def test_merge_agent_panel_requires_the_declared_live_nim_boundary(self):
        merge = getattr(research, "merge_agent_panels", None)
        self.assertTrue(callable(merge), "merge_agent_panels must verify the live provider boundary")
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-boundary-") as directory:
            root = Path(directory)
            inputs = [
                self._write_batch(root, "boundary-a.json", cards[:4], model_boundary="LOCAL_REPLAY"),
                self._write_batch(root, "boundary-b.json", cards[4:]),
            ]
            with self.assertRaisesRegex(ValueError, "AGENT_PANEL_INCOMPLETE_TASK"):
                research.merge_agent_panels(inputs, root / "boundary-out.json", split="screen")

    def test_merge_agent_panel_rejects_duplicate_json_members(self):
        merge = getattr(research, "merge_agent_panels", None)
        self.assertTrue(callable(merge), "merge_agent_panels must parse evidence without ambiguous JSON")
        cards = [row for row in agent_lab.tasks() if row["split"] == "screen"]
        with tempfile.TemporaryDirectory(prefix="athena-agent-merge-json-") as directory:
            root = Path(directory)
            inputs = [
                self._write_batch(root, "json-a.json", cards[:4]),
                self._write_batch(root, "json-b.json", cards[4:]),
            ]
            raw = inputs[0].read_text(encoding="utf-8")
            raw = raw.replace('"status": "PANEL_EXECUTED"',
                              '"status": "FAILED", "status": "PANEL_EXECUTED"', 1)
            inputs[0].write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "AGENT_PANEL_INVALID_REPORT"):
                merge(inputs, root / "json-out.json", split="screen")


class HighEfficiencyDesignAndForecastTests(unittest.TestCase):
    def test_high_efficiency_design_retains_above_95_percent_quality_with_fractional_runs(self):
        design = research.experimental_design.high_efficiency_design(target_quality=0.95)
        self.assertEqual(design["schema"], "athena-high-efficiency-design/1")
        metrics = design["efficiency"]
        self.assertEqual(metrics["schema"], "athena-design-efficiency-report/1")
        self.assertEqual(metrics["reference_variant_count"], 225)
        self.assertEqual(metrics["variant_count"], 88)
        self.assertGreaterEqual(metrics["variant_reduction_ratio"], 0.60)
        self.assertEqual(metrics["main_effect_levels_covered"], 36)
        self.assertEqual(metrics["main_effect_levels_total"], 36)
        self.assertEqual(metrics["main_effect_coverage_ratio"], 1.0)
        self.assertEqual(metrics["single_arm_contenders_covered"], 36)
        self.assertEqual(metrics["single_arm_contenders_total"], 36)
        self.assertEqual(metrics["single_arm_retention_ratio"], 1.0)
        self.assertEqual(metrics["pairwise_tuples_covered"], 591)
        self.assertEqual(metrics["pairwise_tuples_total"], 591)
        self.assertEqual(metrics["pairwise_coverage_ratio"], 1.0)
        self.assertEqual(metrics["mechanism_tuples_covered"], 828)
        self.assertEqual(metrics["mechanism_tuples_total"], 871)
        self.assertGreaterEqual(metrics["mechanism_group_coverage_ratio"], 0.95)
        self.assertEqual(metrics["higher_order_tuples_covered"], 237)
        self.assertEqual(metrics["higher_order_tuples_total"], 280)
        self.assertEqual(metrics["panel_corners_covered"], 41)
        self.assertEqual(metrics["panel_corners_total"], 139)
        self.assertGreaterEqual(metrics["composite_quality_score"], 0.98)

        compact = research.experimental_design.high_efficiency_design(
            target_quality=0.95, include_single_arms=False
        )
        compact_metrics = compact["efficiency"]
        self.assertEqual(compact_metrics["variant_count"], 54)
        self.assertGreaterEqual(compact_metrics["variant_reduction_ratio"], 0.75)
        self.assertEqual(compact_metrics["main_effect_coverage_ratio"], 1.0)
        self.assertEqual(compact_metrics["pairwise_coverage_ratio"], 1.0)
        self.assertGreaterEqual(compact_metrics["mechanism_group_coverage_ratio"], 0.95)
        self.assertGreaterEqual(compact_metrics["composite_quality_score"], 0.98)

        strict_ho = research.experimental_design.high_efficiency_design(
            target_quality=0.95, include_single_arms=True, enforce_higher_order=True
        )
        self.assertEqual(strict_ho["efficiency"]["variant_count"], 116)
        self.assertEqual(strict_ho["efficiency"]["higher_order_tuples_covered"], 266)
        self.assertEqual(strict_ho["efficiency"]["higher_order_tuples_total"], 280)
        self.assertGreaterEqual(strict_ho["efficiency"]["higher_order_coverage_ratio"], 0.95)

        full_cov = research.experimental_design.high_efficiency_design(
            target_quality=1.0, include_single_arms=True, enforce_higher_order=True
        )
        self.assertEqual(full_cov["efficiency"]["variant_count"], 130)
        self.assertEqual(full_cov["efficiency"]["mechanism_tuples_covered"], 871)
        self.assertEqual(full_cov["efficiency"]["higher_order_tuples_covered"], 280)
        self.assertEqual(full_cov["efficiency"]["coverage_status"], "COVERED")

        core = research.experimental_design.whole_path_core_panel()
        self.assertEqual(core["schema"], "athena-whole-path-core-panel/1")
        self.assertEqual(
            core["cloud_015_variants"],
            [
                "baseline",
                "aae-direct",
                "aae-reuse",
                "aae-buffered",
                "aae-smallreserve",
                "aae-batched4096",
                "aae-batched16384",
                "rank-binary",
                "rank-linear",
                "rank-select",
                "rank-binary+rank-select",
                "rank-linear+rank-select",
                "table-html-selectors",
            ],
        )
        self.assertTrue(set(core["cloud_015_variants"]).issubset(core["variants"]))
        core_eff = core["efficiency_within_active_subspace"]
        self.assertEqual(core_eff["reference_variant_count"], len(core["variants"]))
        self.assertEqual(core_eff["composite_quality_score"], 1.0)
        self.assertEqual(core_eff["higher_order_composite_quality_score"], 1.0)
        self.assertEqual(core_eff["mechanism_tuples_covered"], core_eff["mechanism_tuples_total"])
        self.assertEqual(core_eff["higher_order_tuples_covered"], core_eff["higher_order_tuples_total"])
        full_eff = core["efficiency_vs_full_design"]
        self.assertEqual(full_eff["reference_variant_count"], 225)
        self.assertEqual(full_eff["variant_count"], len(core["variants"]))
        self.assertLess(full_eff["composite_quality_score"], 1.0)
        self.assertLess(full_eff["higher_order_composite_quality_score"], full_eff["composite_quality_score"])
        self.assertGreaterEqual(strict_ho["efficiency"]["higher_order_composite_quality_score"], 0.95)
        self.assertEqual(full_cov["efficiency"]["higher_order_composite_quality_score"], 1.0)

    def test_forecast_native_deduplicates_owner_confirm_cases_and_weights_by_case_pilot_ns(self):
        design = research.manifest_design()
        phases = {split: research.synthetic_cases(split) for split in ("screen", "confirm")}
        panel = ["baseline", "prepared", "table-sparse", "table-html-selectors"]
        case_pilot_ns = {
            c["id"]: (500_000_000 if c["id"].startswith("attribute-heavy-") else 100_000)
            for c in phases["screen"]
        }
        pilot = {
            "wall_seconds": 85.0,
            "case_pilot_ns": case_pilot_ns,
            "baseline_build_wall_seconds": 60.0,
            "peak_memory_bytes": 64 * 1024 ** 2,
        }
        profile = {"not_scientific_samples": True, "seconds_per_case_draw_max": 0.0001}
        forecast = research.forecast_native(panel, phases, design, pilot, profile)
        uniform_forecast = research.forecast_native(
            panel,
            phases,
            design,
            {k: v for k, v in pilot.items() if k != "case_pilot_ns"},
            profile,
        )
        self.assertEqual(forecast["maximum_confirm_cases"], 18)
        self.assertEqual(forecast["maximum_family"], 72)
        self.assertEqual(forecast["maximum_bootstrap_draws"], 288000)
        self.assertLess(
            forecast["wall_seconds"] - forecast["finalist_service_gate_seconds_reserve"],
            2000,
        )
        self.assertLess(forecast["wall_seconds"], uniform_forecast["wall_seconds"])

        # Partial case_pilot_ns must fall back to mean per-case duration rather than 0.0
        partial_pilot = dict(pilot)
        partial_pilot["case_pilot_ns"] = {"attribute-heavy-1": 500_000_000}
        partial_forecast = research.forecast_native(panel, phases, design, partial_pilot, profile)
        self.assertGreater(
            partial_forecast["wall_seconds"] - partial_forecast["finalist_service_gate_seconds_reserve"],
            forecast["wall_seconds"] - forecast["finalist_service_gate_seconds_reserve"],
        )

        # Multi-owner panel with multiple candidates per owner must bound confirm_wall_s by owner slots
        h02_heavy_panel = [
            "baseline",
            "rank-binary",
            "rank-linear",
            "rank-select",
            "rank-binary+rank-select",
            "rank-linear+rank-select",
            "prepared",
        ]
        single_h02_panel = ["baseline", "rank-binary", "prepared"]
        uniform_pilot = {
            "wall_seconds": 85.0,
            "case_pilot_ns": {c["id"]: 5_000_000 for c in phases["screen"]},
            "baseline_build_wall_seconds": 60.0,
            "peak_memory_bytes": 64 * 1024 ** 2,
        }
        f_multi = research.forecast_native(h02_heavy_panel, phases, design, uniform_pilot, profile)
        f_single = research.forecast_native(single_h02_panel, phases, design, uniform_pilot, profile)
        self.assertEqual(f_multi["maximum_confirm_cases"], f_single["maximum_confirm_cases"])

        rounds = [{"baseline": [100 + b * 7 + (i % 13) for i in range(10)], "candidate": [98 + b * 7 + (i % 11) for i in range(10)]} for b in range(6)]
        real_profile = research.profile_analysis(rounds, design["confirm"], draws=512)
        cloud_015_panel = research.experimental_design.whole_path_core_panel()["cloud_015_variants"]
        f_015 = research.forecast_native(cloud_015_panel, phases, design, pilot, real_profile)
        self.assertEqual(f_015["maximum_confirm_cases"], 223)
        self.assertEqual(f_015["maximum_family"], 892)
        self.assertLess(
            f_015["wall_seconds"] + f_015["analysis_seconds"] + f_015["export_seconds"] + f_015["shutdown_seconds"],
            design["limits"]["wall_seconds"],
        )

        # Verify RT-NEW-02 algebra on zero-spawn pure-iteration pilot:
        # 52 executions per case in pilot (2 blocks x 2 arms x 13 runs)
        zero_spawn_pilot = {
            "wall_seconds": len(phases["screen"]) * (13.0 / 3.0),
            "case_pilot_ns": {c["id"]: 1_000_000_000 for c in phases["screen"]},
            "baseline_build_wall_seconds": 0.0,
            "peak_memory_bytes": 64 * 1024 ** 2,
        }
        f_pure = research.forecast_native(["baseline", "prepared"], phases, design, zero_spawn_pilot, profile)
        prepared_cases = len(research._variant_cases("prepared", phases["screen"]))
        expected_exec_s = prepared_cases * (13.0 / 3.0) * (
            design["screen"]["blocks"] * (design["screen"]["iterations"] + 10) / 52.0
            + design["confirm"]["blocks"] * (design["confirm"]["iterations"] + 10) / 52.0
        )
        self.assertEqual(
            f_pure["wall_seconds"] - f_pure["finalist_service_gate_seconds_reserve"],
            math.ceil(2.0 * expected_exec_s),
        )

        # Verify RT-05: combined_finalist appended by _complete_finalist_selection is included in freeze_confirmation family_size
        selected = ["aae-direct", "rank-binary+rank-select"]
        joint = research.compose_finalist(selected)
        self.assertEqual(joint, "aae-direct+rank-binary+rank-select")
        selected_with_joint = [*selected, joint]
        confirmation_design = research.copy.deepcopy(design)
        confirmation_design["source_sha256"] = "0" * 64
        confirmation_design["confirm_assignments"] = {
            v: [c["id"] for c in research._variant_cases(v, phases["confirm"])]
            for v in selected_with_joint
        }
        admitted_pilot = {
            **pilot,
            "status": "COMPLETE",
            "phase": "pilot",
            "admitted": True,
            "precision_status": "PLANNED_NOT_GUARANTEED",
        }
        frozen = research.freeze_confirmation(
            confirmation_design,
            selected_with_joint,
            admitted_pilot,
            {split: research.fixture_inventory(phases[split], split) for split in ("screen", "confirm")},
        )
        self.assertEqual(frozen["family_size"], f_015["maximum_family"])
        self.assertEqual(frozen["family_size"], 892)

        full_rounds = [
            {
                "baseline": [100 + b * 7 + (i % 13) for i in range(design["confirm"]["iterations"])],
                "candidate": [98 + b * 7 + (i % 11) for i in range(design["confirm"]["iterations"])],
            }
            for b in range(design["confirm"]["blocks"])
        ]
        self.assertEqual(
            research.bootstrap_intervals(full_rounds, [0.5, 0.95, 0.99], 600, 938201, 0.25, batch_size=256),
            research.bootstrap_intervals(full_rounds, [0.5, 0.95, 0.99], 600, 938201, 0.25, batch_size=64),
        )


    def test_contenders_all_225_native_variants_preserve_valid_rust_imports(self):
        from cloud_experiments import contenders
        root = Path(__file__).resolve().parents[1]
        names = [
            "src/aae.rs",
            "src/rank.rs",
            "src/projection.rs",
            "src/table.rs",
            "cloud_experiments/core_probe.rs",
            "Cargo.toml",
            "Cargo.lock",
        ]
        source = {name: (root / name).read_bytes() for name in names}
        for lock_path in (root / "cloud_experiments/locks").glob("*.lock"):
            source[f"cloud_experiments/locks/{lock_path.name}"] = lock_path.read_bytes()
        for variant_id in research.experimental_design.native_design()["variants"]:
            out = contenders.transform(source, variant_id)
            aae_txt = out["src/aae.rs"].decode("utf-8")
            self.assertEqual(
                "BufWriter::" in aae_txt,
                "use std::io::{self, BufWriter, Write};" in aae_txt,
                variant_id,
            )
            rank_txt = out["src/rank.rs"].decode("utf-8")
            self.assertEqual(
                "HashMap<" in rank_txt,
                "use std::collections::{HashMap, HashSet};" in rank_txt,
                variant_id,
            )
        rank_bin_txt = contenders.transform(source, "rank-binary+rank-select")["src/rank.rs"].decode("utf-8")
        self.assertIn("if !(min_len..=max_len).contains(&word.len()) && word.is_ascii()", rank_bin_txt)
        self.assertIn("first_mask[(bytes[0] >> 6) as usize]", rank_bin_txt)
        self.assertIn("(terms.len() > 8 && chunks.len() > 1)", rank_bin_txt)
        probe_txt = (root / "cloud_experiments/core_probe.rs").read_text(encoding="utf-8")
        self.assertIn("fn parse_projection_and_table_cells(", probe_txt)
        self.assertIn("table_cells: Option<Vec<Value>>,", probe_txt)

    def test_service_study_positive_log_supports_subunit_and_subnormal_values(self):
        from cloud_experiments import service_study
        for value in (5e-324, 2.2250738585072014e-308, 1e-12, 0.1, 0.36787944117144233, 1.0, 1e9):
            logged = service_study._positive_log(value)
            self.assertGreater(logged, 0.0)
            self.assertTrue(math.isfinite(logged))
        sub_rounds = [
            {
                "baseline": [service_study._positive_log(0.05 * (b + 1)) for _ in range(4)],
                "candidate": [service_study._positive_log(0.10 * (b + 1)) for _ in range(4)],
            }
            for b in range(4)
        ]
        intervals = research.bootstrap_intervals(sub_rounds, [0.5, 0.95], 256, 42, 0.05)
        ratio = service_study._ratio_interval(intervals[0.5])
        self.assertAlmostEqual(ratio[0], 2.0, places=12)
        self.assertAlmostEqual(ratio[1], 2.0, places=12)

    def test_bootstrap_intervals_batch_size_256_matches_64_bit_for_bit(self):
        rounds = [
            {
                "baseline": [100 + b * 3 + i for i in range(12)],
                "candidate": [95 + b * 3 + (i % 5) for i in range(12)],
            }
            for b in range(6)
        ]
        b64 = research.bootstrap_intervals(rounds, [0.5, 0.95, 0.99], 2048, 938201, 0.025, batch_size=64)
        b256 = research.bootstrap_intervals(rounds, [0.5, 0.95, 0.99], 2048, 938201, 0.025, batch_size=256)
        self.assertEqual(b64, b256)

        for n_blocks, n_iters in ((2, 256), (2, 257), (3, 192), (4, 192), (6, 96), (8, 64), (24, 25)):
            shape_rounds = [
                {
                    "baseline": [100 + b * 17 + (i % 29) for i in range(n_iters)],
                    "candidate": [97 + b * 13 + (i % 31) for i in range(n_iters)],
                }
                for b in range(n_blocks)
            ]
            probs = [0.0, 0.5, 0.95, 0.99, 1.0]
            got_batches = list(research._bootstrap_batches(shape_rounds, probs, 64, 98765, 32))
            flat_got = {q: [x for batch in got_batches for x in batch[q]] for q in probs}
            w_rng = research.random.Random(98765)
            vb, gb = zip(*sorted((x, g) for g, b in enumerate(shape_rounds) for x in b["baseline"]))
            vc, gc = zip(*sorted((x, g) for g, b in enumerate(shape_rounds) for x in b["candidate"]))
            for draw_i in range(64):
                row = [0] * n_blocks
                for _ in range(n_blocks):
                    row[w_rng.randrange(n_blocks)] += 1
                qb = research.protocol._weighted_quantiles(vb, gb, row, probs)
                qc = research.protocol._weighted_quantiles(vc, gc, row, probs)
                for qi, q in enumerate(probs):
                    self.assertEqual(flat_got[q][draw_i], qc[qi] - qb[qi])

        research._WEIGHT_CACHE.clear()
        ref_a = list(research._bootstrap_batches(rounds, [0.5, 0.95], 64, 111, 16))
        ref_b = list(research._bootstrap_batches(rounds, [0.5, 0.95], 64, 222, 16))
        research._WEIGHT_CACHE.clear()
        gen_a = research._bootstrap_batches(rounds, [0.5, 0.95], 64, 111, 16)
        gen_b = research._bootstrap_batches(rounds, [0.5, 0.95], 64, 222, 16)
        interleaved_a, interleaved_b = [], []
        for _ in range(4):
            interleaved_a.append(next(gen_a))
            interleaved_b.append(next(gen_b))
        self.assertEqual(interleaved_a, ref_a)
        self.assertEqual(interleaved_b, ref_b)

        research._WEIGHT_CACHE.clear()
        gen_partial = research._bootstrap_batches(rounds, [0.5, 0.95], 24, 111, 16)
        gen_full = research._bootstrap_batches(rounds, [0.5, 0.95], 64, 111, 16)
        self.assertEqual(next(gen_partial), ref_a[0])
        self.assertEqual(next(gen_full), ref_a[0])
        _ = next(gen_partial)
        self.assertEqual([next(gen_full), next(gen_full), next(gen_full)], ref_a[1:])

        prof = research.profile_analysis(rounds, {"blocks": 4, "iterations": 8}, draws=4608)
        self.assertEqual(prof["draws_per_repeat"], 4608)
        self.assertGreaterEqual(prof["seconds_per_case_draw_max"], 0.0)


if __name__ == "__main__":
    unittest.main()
