"""Regression checks for research evidence, without running a benchmark or Kaggle."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tarfile
import base64
import hashlib
import unittest
from unittest.mock import patch

from kaggle_results import verify_statistics
from kaggle_experiments import protocol
from kaggle_experiments import runner
from kaggle_experiments import generate_clusters
from kaggle_experiments import pack_snapshots
from kaggle_results import pull_and_analyze


def v2_fixture(directory):
    path = Path(directory) / "samples.jsonl"
    case = {"id": "parse", "stage": "parse", "rounds": 2, "iterations": 1,
            "unit": "ms", "size": 1, "quantiles": [0.5]}
    records = [{"protocol": protocol.PROTOCOL_VERSION, "bundle_id": "a" * 64,
                "case_id": "parse", "stage": "parse", "candidate": arm,
                "round": r, "iteration": 0, "order_in_round": index ^ (r % 2),
                "value": 1.0, "unit": "ms", "size": 1, "status": "PASS",
                "duration_ns": 1000000, "measurement_offset_ns": 1, "output_fingerprint": "d" * 64}
               for r in range(2) for index, arm in enumerate(("baseline", "candidate"))]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    manifest = {"protocol": protocol.PROTOCOL_VERSION, "cluster": "c1",
                "bundle_id": "a" * 64, "status": "COMPLETE",
                "checks": {name: {"status": "PASS"} for name in protocol.REQUIRED_CHECKS["c1"]},
                "snapshots": {arm: {"binaries": {name: "b" * 64 for name in
                             ("athena-core-v2", "bench_core", "research_probe")},
                             "dependency_graph": {"lock_sha256": "c" * 64}}
                              for arm in ("baseline", "candidate")},
                "spec": {"run_kind": "confirm", "cases": [case], "family_alpha": 0.05,
                         "simultaneous_family_size": 54, "drift_threshold": 0.5},
                "artifacts": {path.name: protocol.sha256_file(path)}}
    return path, manifest, records


def dependency_fixture(root):
    packages = {
        root: {"name": "athena-core-v2", "version": "0.1.0", "source": None},
        "rng": {"name": "rand", "version": "0.9.4", "source": "registry+official"},
        "distr": {"name": "rand_distr", "version": "0.5.1", "source": "registry+official"},
        "leaf": {"name": "rng_leaf", "version": "1.0.0", "source": "registry+official"},
        "parser": {"name": "parser", "version": "1.0.0", "source": "registry+official"},
    }
    edges = {root: ["rng", "distr", "parser"], "rng": ["leaf"], "distr": ["rng"],
             "leaf": [], "parser": []}
    return {"root": root, "packages": packages,
            "direct": {packages[key]["name"]: {**packages[key], "id": key}
                       for key in edges[root]},
            "nodes": [{"id": key, "features": [], "deps": [
                {"name": packages[dep]["name"], "pkg": dep,
                 "dep_kinds": [{"kind": None, "target": None}]} for dep in deps]}
                      for key, deps in edges.items()]}


class BoundedProcessControls(unittest.TestCase):
    def test_implicit_child_environment_excludes_provider_cloud_and_proxy_credentials(self):
        with patch.dict(os.environ, {
            "NVIDIA_API_KEY": "synthetic-nim-key",
            "OPENROUTER_API_KEY": "synthetic-router-key",
            "GOOGLE_APPLICATION_CREDENTIALS": "synthetic-gcp-path",
            "KAGGLE_KEY": "synthetic-kaggle-key",
            "HTTPS_PROXY": "http://synthetic-proxy.invalid",
            "PATH": os.environ.get("PATH", ""),
        }, clear=False):
            code = "import json,os; print(json.dumps({k:os.environ.get(k) for k in (\"NVIDIA_API_KEY\", \"OPENROUTER_API_KEY\", \"GOOGLE_APPLICATION_CREDENTIALS\", \"KAGGLE_KEY\", \"HTTPS_PROXY\", \"PATH\")}))"
            result = runner.run_bounded_process([sys.executable, "-B", "-c", code], timeout=2, output_limit=4096)
            self.assertEqual(result.status, "EXITED")
            observed = json.loads(result.stdout)
        for key in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                    "KAGGLE_KEY", "HTTPS_PROXY"):
            self.assertIsNone(observed[key])
        self.assertTrue(observed["PATH"])

    def test_experiment_spec_cannot_raise_the_combined_output_hard_cap(self):
        with tempfile.TemporaryDirectory(prefix="athena-output-hard-cap-") as directory:
            root = Path(directory)
            run = runner.EvidenceRun(root / "run", "c1", "a" * 64,
                                     {"limits": {"command_output_bytes": 8 * 1024 * 1024 + 1}})
            marker = root / "child-ran.txt"
            script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')"
            try:
                with self.assertRaisesRegex(ValueError, "output limit"):
                    run.run([sys.executable, "-B", "-c", script, str(marker)], timeout=2)
                self.assertFalse(marker.exists(), "invalid budget must be rejected before child creation")
                self.assertEqual(run.manifest["commands"], [])
            finally:
                if not run.sample_stream.closed:
                    run.finish(error="controlled output-budget test cleanup")

    def test_command_output_limit_stops_both_streams_and_reaps_the_child(self):
        with tempfile.TemporaryDirectory(prefix="athena-output-bound-") as directory:
            root = Path(directory)
            run = runner.EvidenceRun(root / "run", "c1", "a" * 64,
                                     {"limits": {"command_output_bytes": 4096}})
            pid_file = root / "child.pid"
            script = (
                "import os,sys,time; "
                "open(sys.argv[1], 'w').write(str(os.getpid())); "
                "sys.stdout.buffer.write(b'o'*2048); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'e'*2049); sys.stderr.flush(); "
                "time.sleep(30)"
            )
            try:
                error = None
                try:
                    run.run([sys.executable, "-B", "-c", script, str(pid_file)], timeout=2,
                            log_output=False)
                except Exception as caught:
                    error = caught
                self.assertIsInstance(error, RuntimeError, f"Expected output-limit rejection, got {error!r}")
                self.assertIn("OUTPUT_LIMIT", str(error))
                record = run.manifest["commands"][-1]
                self.assertEqual(record["status"], "OUTPUT_LIMIT")
                self.assertEqual(record["captured_output_bytes"], 4096)
                pid = int(pid_file.read_text(encoding="ascii"))
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    open_process = kernel32.OpenProcess
                    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                    open_process.restype = wintypes.HANDLE
                    wait = kernel32.WaitForSingleObject
                    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                    wait.restype = wintypes.DWORD
                    close = kernel32.CloseHandle
                    close.argtypes = (wintypes.HANDLE,)
                    handle = open_process(0x00100000 | 0x1000, False, pid)
                    if handle:
                        try:
                            self.assertNotEqual(wait(handle, 0), 0x00000102,
                                                "child remained live after output-limit termination")
                        finally:
                            close(handle)
                    else:
                        self.assertEqual(ctypes.get_last_error(), 87,
                                         "could not establish that the child process had exited")
                else:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if not run.sample_stream.closed:
                    run.finish(error="controlled process-output test cleanup")

    def test_bounded_process_timeout_reaps_a_real_child(self):
        with tempfile.TemporaryDirectory(prefix="athena-process-timeout-") as directory:
            result = runner.run_bounded_process(
                [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                cwd=directory, timeout=0.2, output_limit=1024)
            self.assertEqual(result.status, "TIMEOUT")
            self.assertIsInstance(result.returncode, int)
            self.assertLessEqual(result.captured_output_bytes, 1024)


class RustupBootstrapControls(unittest.TestCase):
    def test_toolchain_preparation_rejects_unpinned_bootstrap_before_execution(self):
        from io import BytesIO
        from types import SimpleNamespace

        payload = b"synthetic untrusted rustup installer"
        commands = []

        def controlled_run(command, **kwargs):
            commands.append(command)
            if command[0] == "curl":
                return payload
            raise AssertionError("unchecked rustup installer reached the execution boundary")

        with tempfile.TemporaryDirectory(prefix="athena-kaggle-rustup-") as folder:
            run = SimpleNamespace(directory=Path(folder), run=controlled_run)
            with patch.dict(os.environ, {"PATH": "", "RUSTUP_TOOLCHAIN": "test"}), \
                    patch.object(runner.shutil, "which", return_value=None), \
                    patch.object(runner, "urlopen", return_value=BytesIO(payload), create=True), \
                    patch.object(runner.platform, "machine", return_value="x86_64"):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    runner.prepare_toolchain(run)
            self.assertEqual(commands, [])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_matching_pin_executes_only_verified_temporary_installer(self):
        import hashlib
        from io import BytesIO
        from types import SimpleNamespace

        target = "x86_64-unknown-linux-gnu"
        payload = b"synthetic verified rustup installer"
        digest = hashlib.sha256(payload).hexdigest()
        expected_url = ("https://static.rust-lang.org/rustup/archive/"
                        f"{runner.RUSTUP_VERSION}/{target}/rustup-init")

        with tempfile.TemporaryDirectory(prefix="athena-kaggle-rustup-match-") as folder:
            def inspect_execution(command, **kwargs):
                self.assertEqual(Path(command[0]).read_bytes(), payload)
                self.assertEqual(command[1:], ["-y", "--profile", "minimal", "--default-toolchain",
                                               runner.RUSTUP_TOOLCHAIN, "--no-modify-path"])
                self.assertEqual(kwargs, {"timeout": 600, "log_output": False})
                if os.name == "posix":
                    self.assertTrue(Path(command[0]).stat().st_mode & 0o111)
                return b""

            run = SimpleNamespace(directory=Path(folder), run=inspect_execution)
            with patch.dict(runner.PINNED_RUSTUP_SHA256, {target: digest}), \
                    patch.object(runner, "urlopen", return_value=BytesIO(payload)) as download:
                runner.install_pinned_rustup(run, target=target)
            download.assert_called_once_with(expected_url, timeout=60)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_unsupported_architecture_fails_before_download(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="athena-kaggle-rustup-arch-") as folder:
            run = SimpleNamespace(directory=Path(folder), run=lambda *args, **kwargs: self.fail("unexpected install"))
            with patch.object(runner, "urlopen") as download:
                with self.assertRaisesRegex(ValueError, "Unsupported Linux architecture"):
                    runner.install_pinned_rustup(run, machine="mips64")
            download.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])


class StatisticalRegressions(unittest.TestCase):
    def test_relocated_root_does_not_look_like_an_unrelated_dependency_change(self):
        before, after = dependency_fixture("old-root"), dependency_fixture("new-root")
        self.assertEqual(runner.verify_rng_dependency_changes(before, after), {"removed": [], "added": []})
        after["packages"]["rng"]["version"] = "0.10.3"
        self.assertTrue(runner.verify_rng_dependency_changes(before, after)["added"])

    def test_rng_guard_still_rejects_unrelated_resolution_changes(self):
        before = dependency_fixture("old-root")
        for field, value in (("version", "2.0.0"), ("source", "registry+other")):
            after = dependency_fixture("old-root")
            after["packages"]["parser"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner.verify_rng_dependency_changes(before, after)
        for mutation in ("features", "edges", "root"):
            after = dependency_fixture("old-root")
            if mutation == "features":
                after["nodes"][-1]["features"] = ["unexpected"]
            elif mutation == "edges":
                after["nodes"][-1]["deps"] = copy.deepcopy(after["nodes"][1]["deps"])
            else:
                after["packages"][after["root"]]["version"] = "0.2.0"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runner.verify_rng_dependency_changes(before, after)

    def test_rng_guard_ignores_metadata_order_but_not_a_missing_rng_root(self):
        before, after = dependency_fixture("old-root"), dependency_fixture("new-root")
        after["nodes"].reverse()
        after["nodes"][-1]["deps"].reverse()
        self.assertEqual(runner.verify_rng_dependency_changes(before, after), {"removed": [], "added": []})
        del after["direct"]["rand"]
        with self.assertRaises(ValueError):
            runner.verify_rng_dependency_changes(before, after)

    def test_rng_guard_normalizes_only_the_root_path(self):
        before, after = dependency_fixture("old-root"), dependency_fixture("new-root")
        for graph in (before, after):
            graph["packages"]["parser"]["source"] = None
        after["packages"]["relocated-parser"] = after["packages"].pop("parser")
        for node in after["nodes"]:
            if node["id"] == "parser":
                node["id"] = "relocated-parser"
            for dep in node["deps"]:
                if dep["pkg"] == "parser":
                    dep["pkg"] = "relocated-parser"
        with self.assertRaises(ValueError):
            runner.verify_rng_dependency_changes(before, after)

    def test_missing_gnu_time_is_prepared_before_the_rss_self_check(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            binary, calls = Path(directory) / "time", []
            def execute(argv, **kwargs):
                calls.append([str(value) for value in argv])
                if argv[:2] == ["apt-get", "install"]:
                    binary.write_bytes(b"test tool identity")
                if "--version" in argv:
                    return b"time (GNU Time) 1.9\n"
                if "-o" in argv:
                    Path(argv[argv.index("-o") + 1]).write_text("1024\n", encoding="ascii")
                return b""
            run = SimpleNamespace(run=execute, directory=Path(directory))
            with patch.object(runner.platform, "system", return_value="Linux"), \
                    patch.object(runner.os, "geteuid", return_value=0, create=True), \
                    patch.object(runner.shutil, "which", return_value="apt-get"):
                receipt = runner.prepare_rss_tool(run, binary)
                self.assertEqual(receipt["self_check_peak_rss_kib"], 1024)
                self.assertEqual(calls[:2], [["apt-get", "update"],
                                 ["apt-get", "install", "--yes", "--no-install-recommends", "--no-upgrade", "time"]])
                calls.clear()
                (Path(directory) / "rss-tool-self-check.txt").unlink()
                runner.prepare_rss_tool(run, binary)
                self.assertFalse(any("apt-get" in call for call in calls))

    def test_missing_or_non_gnu_rss_tool_fails_without_fake_measurements(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "time"
            run = SimpleNamespace(run=lambda *args, **kwargs: b"BSD time", directory=Path(directory))
            with patch.object(runner.os, "geteuid", return_value=1000, create=True):
                with self.assertRaises(RuntimeError):
                    runner.prepare_rss_tool(run, binary)
            binary.write_bytes(b"not GNU")
            with self.assertRaises(RuntimeError):
                runner.prepare_rss_tool(run, binary)
            self.assertFalse((Path(directory) / "rss-tool-self-check.txt").exists())

    def test_rss_setup_failure_stops_before_version_check_or_measurement(self):
        from types import SimpleNamespace
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            binary, calls = Path(directory) / "time", []
            def execute(argv, **kwargs):
                calls.append(argv)
                raise subprocess.CalledProcessError(7, argv)
            run = SimpleNamespace(run=execute, directory=Path(directory))
            with patch.object(runner.platform, "system", return_value="Linux"), \
                    patch.object(runner.os, "geteuid", return_value=0, create=True), \
                    patch.object(runner.shutil, "which", return_value="apt-get"):
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.prepare_rss_tool(run, binary)
            self.assertEqual(calls, [["apt-get", "update"]])
            self.assertFalse((Path(directory) / "rss-tool-self-check.txt").exists())

    def test_v21_resamples_within_block_dependence_but_still_holds_between_round_drift(self):
        rounds = [{"baseline": [10.0 + i / 100 for i in range(100)],
                   "candidate": [5.0 + i / 200 for i in range(100)]} for _ in range(20)]
        case = {"rounds": rounds, "spec": {"unit": "ms"}, "quantiles": (0.5,), "family_size": 57,
                "analysis_policy": "paired-rounds/2.1"}
        old = protocol.analyze_measurements({"export_no_query": {**case, "analysis_policy": "paired-rounds/2.0"}})
        new = protocol.analyze_measurements({"export_no_query": case})
        self.assertEqual(old["cases"]["export_no_query"]["decision"], "HOLD")
        self.assertEqual(new["cases"]["export_no_query"]["decision"], "PASS_PERFORMANCE")
        self.assertTrue(new["cases"]["export_no_query"]["diagnostics"])
        drift = [{arm: [value + round_id for value in block[arm]] for arm in block}
                 for round_id, block in enumerate(rounds)]
        held = protocol.analyze_measurements({"export_no_query": {**case, "rounds": drift}})
        self.assertEqual(held["cases"]["export_no_query"]["decision"], "HOLD")
        slower = [{"baseline": block["baseline"], "candidate": [value + 10 for value in block["candidate"]]}
                  for block in rounds]
        rejected = protocol.analyze_measurements({"export_no_query": {**case, "rounds": slower}})
        self.assertEqual(rejected["cases"]["export_no_query"]["decision"], "REJECT")

    def test_v21_cannot_relabel_the_old_six_stage_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, _ = v2_fixture(directory)
            manifest["spec"].update(study_revision="2.1", analysis_policy="paired-rounds/2.1",
                                    simultaneous_family_size=57)
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)

    def test_v21_complete_manifest_selects_only_its_declared_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, templates = v2_fixture(directory)
            manifest["spec"] = generate_clusters.specifications(screen=True)["c1"]
            rows = [{**template, "case_id": case["id"], "stage": case["stage"], "size": case["size"],
                     "iteration": i} for case in manifest["spec"]["cases"]
                    for template in templates for i in range(case["iterations"])]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            manifest["artifacts"][path.name] = protocol.sha256_file(path)
            cases = protocol.load_measurements(path, manifest)
            self.assertEqual(set(cases), set(protocol.C1_STAGES))
            self.assertTrue(all(case["analysis_policy"] == protocol.ANALYSIS_POLICY for case in cases.values()))
            manifest["spec"]["analysis_policy"] = "paired-rounds/unknown"
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)

    def test_v21_requires_the_new_environment_and_dependency_gates(self):
        for cluster, gate in (("c2", "rss_tool"), ("c3", "rng_dependencies")):
            with self.subTest(cluster=cluster), tempfile.TemporaryDirectory() as directory:
                path, manifest, _ = v2_fixture(directory)
                manifest["cluster"] = cluster
                manifest["checks"] = {name: {"status": "PASS"} for name in protocol.REQUIRED_CHECKS[cluster]}
                manifest["spec"].update(study_revision="2.1", analysis_policy="paired-rounds/2.1",
                                        simultaneous_family_size=57)
                if cluster == "c3":
                    manifest["snapshots"]["rng-upgrade"] = manifest["snapshots"].pop("baseline")
                with self.assertRaises(ValueError):
                    protocol.load_measurements(path, manifest)
                manifest["checks"][gate] = {"status": "PASS"}
                self.assertTrue(protocol.load_measurements(path, manifest))

    def test_analysis_and_combine_keep_raw_identity_and_scoped_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = []
            for cluster in ("c1", "c2", "c3"):
                folder = Path(directory) / cluster
                folder.mkdir()
                raw, manifest, _ = v2_fixture(folder)
                manifest["cluster"] = cluster
                manifest["checks"] = {name: {"status": "PASS"} for name in protocol.REQUIRED_CHECKS[cluster]}
                if cluster == "c3":
                    manifest["snapshots"]["rng-upgrade"] = manifest["snapshots"].pop("baseline")
                manifest_path, output = folder / "manifest.json", folder / "analysis.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    verify_statistics.main(["--input", str(raw), "--manifest", str(manifest_path),
                                            "--output", str(output)])
                outputs.append(output)
            combined = verify_statistics.combine_reports(outputs)
            self.assertEqual(combined["overall_decision"], "HOLD")
            self.assertEqual(combined["decision_matrix"]["cpu_core"]["decision"], "HOLD")
            saved = outputs[1].read_bytes()
            mismatched = json.loads(saved)
            mismatched["analysis_policy"] = "paired-rounds/2.1"
            outputs[1].write_text(json.dumps(mismatched), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_statistics.combine_reports(outputs)
            outputs[1].write_bytes(saved)
            first_raw, first_manifest = Path(directory) / "c1/samples.jsonl", Path(directory) / "c1/manifest.json"
            rows = [json.loads(line) for line in first_raw.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                if row["candidate"] == "candidate":
                    row.update(value=2.0, duration_ns=2000000)
            first_raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            changed = json.loads(first_manifest.read_text(encoding="utf-8"))
            changed["artifacts"][first_raw.name] = protocol.sha256_file(first_raw)
            first_manifest.write_text(json.dumps(changed), encoding="utf-8")
            outputs[0] = Path(directory) / "c1/rejected-analysis.json"
            with contextlib.redirect_stdout(io.StringIO()):
                verify_statistics.main(["--input", str(first_raw), "--manifest", str(first_manifest),
                                        "--output", str(outputs[0])])
            self.assertEqual(verify_statistics.combine_reports(outputs)["decision_matrix"]["cpu_core"]["decision"],
                             "REJECT")
            raw.write_text(raw.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_statistics.combine_reports(outputs)

    def test_screening_does_not_become_acceptance_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, _ = v2_fixture(directory)
            manifest["spec"]["run_kind"] = "screen"
            report = protocol.analyze_measurements(protocol.load_measurements(path, manifest), draws=30000)
            self.assertEqual(report["cases"]["parse"]["decision"], "HOLD")
            self.assertIn("Screening experiment is not confirmatory evidence",
                          report["cases"]["parse"]["warnings"])

    def test_changed_raw_data_or_output_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, records = v2_fixture(directory)
            records[0]["output_fingerprint"] = "e" * 64
            path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)
            manifest["artifacts"][path.name] = protocol.sha256_file(path)
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)

    def test_bundle_wide_family_covers_all_three_clusters(self):
        specs = generate_clusters.specifications()
        family = sum(len(case["quantiles"]) for spec in specs.values() for case in spec["cases"])
        self.assertEqual(family, 57)
        self.assertEqual([case["stage"] for case in specs["c1"]["cases"]].count("export_no_query"), 1)
        for spec in specs.values():
            self.assertEqual(spec.get("simultaneous_family_size"), family)
            self.assertEqual(spec.get("study_revision"), "2.1")
            self.assertEqual(spec.get("analysis_policy"), "paired-rounds/2.1")
            self.assertEqual(spec.get("limits", {}).get("command_output_bytes"), 8 * 1024 * 1024)

    def test_manifest_family_cannot_be_reduced_to_reported_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, _ = v2_fixture(directory)
            cases = protocol.load_measurements(path, manifest)
            report = protocol.analyze_measurements(cases, draws=30000)
            self.assertEqual(report["family_size"], 54)

    def test_combine_cannot_accept_empty_case_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for cluster in ("c1", "c2", "c3"):
                path = Path(directory) / (cluster + ".json")
                path.write_text(json.dumps({"cluster": cluster, "protocol": protocol.PROTOCOL_VERSION,
                                            "bundle_id": "a" * 64, "cases": {}}), encoding="utf-8")
                paths.append(path)
            with self.assertRaises(ValueError):
                verify_statistics.combine_reports(paths)

    def test_complete_manifest_cannot_rename_the_measured_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest, records = v2_fixture(directory)
            records[0]["stage"] = "export"
            path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            manifest["artifacts"][path.name] = protocol.sha256_file(path)
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)

    def test_complete_label_without_correctness_gates_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            records = [{"protocol": protocol.PROTOCOL_VERSION, "bundle_id": "a" * 64,
                        "case_id": "parse", "candidate": arm, "round": r, "iteration": 0,
                        "order_in_round": (0 if arm == "baseline" else 1) ^ (r % 2),
                        "value": 1.0, "unit": "ms", "size": 1, "status": "PASS"}
                       for r in range(2) for arm in ("baseline", "candidate")]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            manifest = {"protocol": protocol.PROTOCOL_VERSION, "cluster": "c1",
                        "bundle_id": "a" * 64, "status": "COMPLETE",
                        "spec": {"cases": [{"id": "parse", "rounds": 2, "iterations": 1,
                                             "unit": "ms", "size": 1}]},
                        "artifacts": {path.name: protocol.sha256_file(path)}}
            with self.assertRaises(ValueError):
                protocol.load_measurements(path, manifest)

    def test_missing_samples_are_not_zero_latency(self):
        with self.assertRaises(ValueError):
            verify_statistics.quantile([], 0.5)

    def test_fast_median_does_not_accept_a_regressed_tail(self):
        with tempfile.TemporaryDirectory(prefix="athena-protocol-") as directory:
            root = Path(directory)
            (root / "c1").mkdir()
            samples = root / "c1" / "samples.jsonl"
            with samples.open("w", encoding="utf-8") as stream:
                for round_id in range(2):
                    for arm, values in (("baseline", [10.0] * 100),
                                        ("candidate", [5.0] * 97 + [30.0] * 3)):
                        for iteration, duration in enumerate(values):
                            stream.write(json.dumps({
                                "stage": "parse", "size": 1000, "round": round_id,
                                "candidate": arm, "iteration": iteration,
                                "duration_ms": duration,
                            }) + "\n")
            output = root / "audit_synthesis_report.json"
            args = ["verify_statistics", "--input", str(samples),
                    "--output", str(output), "--legacy", "--bootstrap", "2000"]
            with patch.object(verify_statistics, "ROOT", root), patch.object(sys, "argv", args):
                with contextlib.redirect_stdout(io.StringIO()):
                    verify_statistics.main()
            report = json.loads(output.read_text(encoding="utf-8"))
            stages = report.get("cases", report)
            verdicts = [value["decision"] for value in stages.values()
                        if isinstance(value, dict) and "decision" in value]
            self.assertTrue(verdicts)
            self.assertTrue(all(not verdict.startswith("ACCEPT") for verdict in verdicts))

    def test_every_reported_quantile_counts_in_family(self):
        rounds = [{"baseline": [10.0] * 5, "candidate": [5.0] * 5} for _ in range(2)]
        cases = {name: {"rounds": rounds, "spec": None, "quantiles": protocol.QUANTILES}
                 for name in ("parse", "rank")}
        report = protocol.analyze_measurements(cases, draws=3000, legacy=True)
        self.assertEqual(report["family_size"], 6)
        self.assertEqual(report["overall_decision"], "HOLD")

    def test_numpy_and_independent_expanded_bootstrap_agree(self):
        import random
        rounds = [{"baseline": [1.0, 3.0], "candidate": [2.0, 6.0]},
                  {"baseline": [8.0, 9.0], "candidate": [7.0, 10.0]}]
        rng = random.Random(19)
        expected = {q: [] for q in protocol.QUANTILES}
        for _ in range(50):
            selected = [rng.randrange(2) for _ in range(2)]
            base = [value for group in selected for value in rounds[group]["baseline"]]
            cand = [value for group in selected for value in rounds[group]["candidate"]]
            for q in expected:
                expected[q].append(protocol.quantile(cand, q) - protocol.quantile(base, q))
        expected = {q: sorted(values) for q, values in expected.items()}
        for backend in ("python", "auto"):
            actual, _ = protocol.bootstrap_deltas(rounds, protocol.QUANTILES, 50, 19, backend)
            self.assertEqual(actual, expected)

    def test_malformed_or_incomplete_raw_data_is_rejected(self):
        records = [{"stage": "parse", "size": 1, "round": r, "candidate": arm,
                    "iteration": 0, "duration_ms": 1.0}
                   for r in range(2) for arm in ("baseline", "candidate")]
        corruptions = [records[:-1], records + [records[0]],
                       [dict(record, duration_ms=float("nan")) for record in records]]
        for corruption in corruptions:
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "samples.jsonl"
                path.write_text("".join(json.dumps(record) + "\n" for record in corruption))
                with self.assertRaises(ValueError):
                    protocol.load_measurements(path)


class CorrectnessRegressions(unittest.TestCase):
    def test_duplicate_binary_cannot_pass_source_variant_control(self):
        from types import SimpleNamespace
        frames = [json.loads(line) for line in self.envelope().splitlines()]
        frames[1].update(role="preformatted", text="if ready:\n    run()\n    stop()")
        payload = "".join(json.dumps(frame) + "\n" for frame in frames).encode()
        fake = SimpleNamespace(run=lambda *args, **kwargs: payload)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "did not distinguish"):
                runner.verify_variant_identity(fake, {"baseline": Path("same"), "candidate": Path("same")},
                                               Path(directory))

    def test_beta_central_variance_bound_matches_exact_rational_moments(self):
        from fractions import Fraction
        for a, b in ((1, 1), (2, 5), (1, 100), (1000000, 1000000)):
            moments = [Fraction(1)]
            for k in range(4):
                moments.append(moments[-1] * Fraction(a + k, a + b + k))
            mean = moments[1]
            variance = moments[2] - mean ** 2
            central4 = moments[4] - 4 * mean * moments[3] + 6 * mean ** 2 * moments[2] - 3 * mean ** 4
            bound = runner.beta_variance_bound(a, b, 100000, 2240)
            self.assertAlmostEqual(bound["expected"], float(variance), places=15)
            self.assertAlmostEqual(bound["excess_kurtosis"], float(central4 / variance ** 2 - 3), places=12)
            self.assertLess(bound["failure_bound"], 0.001 / 2240)

    def test_rng_analytic_diagnostics_accept_the_declared_moments(self):
        packet = {"alpha": 1, "beta": 1, "n": 100000, "seed": 1, "duration_ns": 100000,
                  "bandit_consumer": True, "sum": 50000, "sum_sq": 100000 / 3,
                  "cutoffs": [0.1, 0.25, 0.5, 0.9], "cdf_counts": [10000, 25000, 50000, 90000],
                  "central_second_block_means": [1 / 12] * 20}
        diagnostic = runner.validate_rng_packet(packet, 1, 1, 100000, 1, 2240)
        self.assertEqual(diagnostic["family_checks"], 2240)

    def test_preparation_archives_are_reproducible_and_content_addressed(self):
        files = {"src/lib.rs": b"pub fn example() {}\n", "Cargo.lock": b"lock\n"}
        raw = pack_snapshots.archive_files(files)
        self.assertEqual(raw, pack_snapshots.archive_files(dict(reversed(list(files.items())))))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
            runner.unpack_snapshot(base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(),
                                   hashes, destination)
            self.assertEqual((destination / "src/lib.rs").read_bytes(), files["src/lib.rs"])

    def test_malformed_timing_packet_cannot_be_recorded(self):
        from types import SimpleNamespace
        packet = {"protocol": protocol.PROTOCOL_VERSION, "stage": "parse", "size": 2,
                  "iterations": 2, "raw_ns": [100], "sample_offsets_ns": [0, 100],
                  "fingerprint": "f" * 64}
        fake = SimpleNamespace(run=lambda *args, **kwargs: json.dumps(packet).encode())
        with self.assertRaises(ValueError):
            runner.bench_result(fake, Path("not-executed"), "parse", 2, 2)

    def test_download_failure_is_not_swallowed(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            failure = subprocess.CalledProcessError(7, ["kaggle"])
            with patch.object(pull_and_analyze.subprocess, "run", side_effect=failure) as execute:
                with self.assertRaises(subprocess.CalledProcessError):
                    pull_and_analyze.pull_kernel_output("owner/example", Path(directory) / "new")
                self.assertTrue(execute.call_args.kwargs["check"])
        self.assertEqual(pull_and_analyze.normalized_status({"status": "KernelWorkerStatus.ERROR"}), "ERROR")

    def envelope(self, text="Visible"):
        frames = [
            {"type": "meta", "version": "aae/1", "source_url": "https://example.org/",
             "content_hash": "0" * 64, "total_blocks": 1},
            {"type": "block", "id": "1" * 64, "role": "paragraph", "text": text, "links": []},
            {"type": "end", "block_count": 1},
        ]
        return "".join(json.dumps(frame) + "\n" for frame in frames).encode()

    def test_success_exit_cannot_replace_content_oracle(self):
        expected = {"blocks": [("paragraph", "Visible", [])]}
        protocol.validate_aae(self.envelope(), expected)
        with self.assertRaises(ValueError):
            protocol.validate_aae(self.envelope("Wrong but valid JSON"), expected)

    def test_parity_divergence_and_missing_end_are_fatal(self):
        with self.assertRaises(ValueError):
            protocol.require_parity(self.envelope(), self.envelope("Different"))
        with self.assertRaises(ValueError):
            protocol.validate_aae(b"\n".join(self.envelope().splitlines()[:-1]) + b"\n")

    def test_missing_rss_is_not_a_zero_measurement(self):
        for invalid in ("", "0", "-1", "not available", "nan"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                protocol.parse_peak_rss(invalid)
        self.assertEqual(protocol.parse_peak_rss("1024\n"), 1024)

    def test_archive_cannot_escape_source_workspace(self):
        content = b"not source"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("../escape")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        raw = buffer.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with self.assertRaises(ValueError):
                runner.unpack_snapshot(base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(),
                                       {"../escape": hashlib.sha256(content).hexdigest()}, destination)
            self.assertFalse((Path(directory) / "escape").exists())

    def test_failed_process_preserves_exit_and_diagnostic(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            run = runner.EvidenceRun(Path(directory) / "run", "unit", "a" * 64, {})
            source = Path(directory) / "scratch-rng"
            (source / "src/bin").mkdir(parents=True)
            for name in ("Cargo.toml", "Cargo.lock", "src/bin/research_probe.rs"):
                (source / name).write_text("retained diagnostic source", encoding="utf-8")
            run.manifest["snapshots"]["candidate"] = {
                "archive_sha256": "b" * 64, "source_files": {"Cargo.toml": "old", "Cargo.lock": "old"}}
            with self.assertRaises(subprocess.CalledProcessError):
                try:
                    run.run([sys.executable, "-c", "import sys; print('actual failure'); sys.exit(7)"])
                finally:
                    runner.preserve_rng_attempt(run, source)
            run.finish(RuntimeError("expected failure for regression"))
            self.assertEqual(run.manifest["status"], "FAILED")
            self.assertEqual(run.manifest["commands"][0]["exit_code"], 7)
            self.assertIn("actual failure", run.log_path.read_text())
            self.assertEqual((run.directory / "rng-upgrade-Cargo.lock").read_bytes(), (source / "Cargo.lock").read_bytes())
            self.assertEqual(run.manifest["snapshots"]["rng-upgrade"]["source_files"]["Cargo.toml"],
                             protocol.sha256_file(source / "Cargo.toml"))

    def test_missing_distribution_checks_cannot_pass(self):
        packet = {"alpha": 2, "beta": 5, "n": 100000, "seed": 1,
                  "bandit_consumer": True, "sum": 28571.4, "sum_sq": 10714.3,
                  "cutoffs": [], "cdf_counts": []}
        with self.assertRaises(ValueError):
            runner.validate_rng_packet(packet, 2, 5, 100000, 1, 100)

    def test_concentrated_beta_cannot_hide_collapsed_variance(self):
        packet = {"alpha": 1000000, "beta": 1000000, "n": 100000, "seed": 1,
                  "bandit_consumer": True, "sum": 50000, "sum_sq": 25000.0125,
                  "duration_ns": 100000, "cutoffs": [0.1, 0.25, 0.5, 0.9],
                  "cdf_counts": [0, 0, 50000, 100000], "central_second_block_means": [0.0] * 20}
        with self.assertRaises(ValueError):
            runner.validate_rng_packet(packet, 1000000, 1000000, 100000, 1, 1920)


if __name__ == "__main__":
    unittest.main()
