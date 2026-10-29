"""Filesystem ownership checks for the offline Rust benchmark driver."""

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from model import benchmark


class BenchmarkFixtureOwnership(unittest.TestCase):
    def test_compare_preserves_neighbor_files_and_cleans_owned_fixtures(self):
        with tempfile.TemporaryDirectory(prefix="athena-benchmark-owner-") as folder:
            root = Path(folder)
            baseline, candidate = root / "baseline", root / "candidate"
            artifacts = ("athena-core-v2.exe", "bench_html.exe", "bench_core.exe", "Cargo.lock")
            for directory in (baseline, candidate):
                directory.mkdir()
                for name in artifacts:
                    (directory / name).write_bytes((directory.name + name).encode())

            neighbors = {
                root / "benchmark-fixture.html": b"owned by the user",
                root / "many-links.html": b"also owned by the user",
            }
            for path, content in neighbors.items():
                path.write_bytes(content)

            real_temporary_directory = tempfile.TemporaryDirectory
            scratch_roots = []

            def track_temporary_directory(*args, **kwargs):
                temporary = real_temporary_directory(*args, **kwargs)
                scratch_roots.append(Path(temporary.name))
                return temporary

            def fake_run(args, **kwargs):
                executable = Path(args[0]).stem
                if executable == "athena-core-v2":
                    return SimpleNamespace(stdout=b'{"equivalent":true}')
                if executable == "bench_core":
                    packet = {"output_size": 1, "p50_ms": 1.0, "p95_ms": 2.0}
                    return SimpleNamespace(stdout=json.dumps(packet))
                if executable == "bench_html":
                    packet = {"output_bytes": 1, "p50_ms": 1.0, "p95_ms": 2.0}
                    return SimpleNamespace(stdout=json.dumps(packet))
                if executable == "rustc":
                    return SimpleNamespace(stdout="rustc synthetic-version\n")
                if executable == "git":
                    return SimpleNamespace(stdout="synthetic-revision\n")
                raise AssertionError(f"unexpected child executable: {args[0]}")

            with patch.object(tempfile, "TemporaryDirectory",
                              side_effect=track_temporary_directory), \
                    patch.object(benchmark.platform, "system", return_value="Windows"), \
                    patch.object(benchmark.platform, "platform", return_value="synthetic-windows"), \
                    patch.object(benchmark.subprocess, "run", side_effect=fake_run):
                report = benchmark.compare(baseline, candidate, rounds=1)

            self.assertTrue(all(path.read_bytes() == content for path, content in neighbors.items()),
                            "comparison overwrote a neighboring user-owned fixture")
            self.assertEqual(len(scratch_roots), 1)
            self.assertFalse(scratch_roots[0].exists(), "successful run retained owned fixture scratch")
            self.assertEqual(set(report["fixtures"]), {"example.html", "benchmark-fixture.html", "many-links.html"})

            def failed_child(*_args, **_kwargs):
                raise OSError("controlled benchmark child failure")

            with patch.object(tempfile, "TemporaryDirectory",
                              side_effect=track_temporary_directory), \
                    patch.object(benchmark.platform, "system", return_value="Windows"), \
                    patch.object(benchmark.platform, "platform", return_value="synthetic-windows"), \
                    patch.object(benchmark.subprocess, "run", side_effect=failed_child):
                with self.assertRaisesRegex(OSError, "controlled benchmark child failure"):
                    benchmark.compare(baseline, candidate, rounds=1)

            self.assertEqual(len(scratch_roots), 2)
            self.assertFalse(scratch_roots[1].exists(), "failed run retained owned fixture scratch")
            self.assertTrue(all(path.read_bytes() == content for path, content in neighbors.items()),
                            "failed comparison changed a neighboring user-owned fixture")


if __name__ == "__main__":
    unittest.main()
