"""Regression checks for research evidence, without running a benchmark or Kaggle."""
import contextlib
import io
import json
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


class StatisticalRegressions(unittest.TestCase):
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
        self.assertEqual(family, 54)
        for spec in specs.values():
            self.assertEqual(spec.get("simultaneous_family_size"), family)

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
