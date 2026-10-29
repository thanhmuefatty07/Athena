import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from cloud_experiments import wceb_runner


class WcebRunnerTests(unittest.TestCase):
    def test_fixture_encoder_preserves_json_text_and_rejects_one_byte_over(self):
        html = ('quote=" slash=\\ newline=\n tab=\t nonascii=é🦄\x00' * 300)
        encoded = wceb_runner._fixture_json(html, "https://example.org/", chunk_chars=7)
        self.assertEqual(json.loads(encoded), {"html": html, "url": "https://example.org/"})
        with self.assertRaisesRegex(ValueError, "JSON admission"):
            wceb_runner._fixture_json(html, "https://example.org/", cap=len(encoded) - 1,
                                      chunk_chars=7)
        over_html_cap = "é" * (wceb_runner.CORE_HTML_BYTES // 2 + 1)
        with self.assertRaisesRegex(ValueError, "HTML exceeds"):
            wceb_runner._fixture_json(over_html_cap, "https://example.org/")

    def test_bootstrap_is_paired_by_domain_reproducible_and_counts_failed_rows(self):
        baseline, candidate = [], []
        for index in range(30):
            domain = f"site-{index}.example"
            baseline.append({"domain": domain, "with_rate": 0.5, "prediction_missing": False})
            candidate.append({"domain": domain, "with_rate": 0.0, "prediction_missing": True})
        first = wceb_runner.paired_cluster_bootstrap(
            baseline, candidate, metric="with_rate", comparison_count=3, seed=1729, draws=30_000)
        second = wceb_runner.paired_cluster_bootstrap(
            baseline, candidate, metric="with_rate", comparison_count=3, seed=1729, draws=30_000)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ESTIMATED")
        self.assertEqual(first["domain_count"], 30)
        self.assertEqual(first["pair_count"], 30)
        self.assertEqual(first["mean_domain_delta"], -0.5)
        self.assertEqual(first["simultaneous_ci"], [-0.5, -0.5])
        self.assertEqual(first["confidence_family_size"], 3)

    def test_public_test_freeze_binds_dev_report_and_exact_binary_hashes(self):
        report = {"schema": wceb_runner.SCHEMA, "split": "dev",
                  "decision_sufficiency": "REVIEW_REQUIRED",
                  "runtime": wceb_runner._runtime_identity(),
                  "source_identity": wceb_runner._source_identity(),
                  "dataset": {"commit": "62ff86d12ea72c80c31fb810ff1a724fad687bea",
                              "root_tree_sha": "361ac62da1d6ef90f709a98ff14155a12d06ca0a",
                              "page_count": 1},
                  "binaries": [{"id": "baseline", "sha256": "a" * 64, "bytes": 10},
                               {"id": "scraper", "sha256": "b" * 64, "bytes": 11},
                               {"id": "vector", "sha256": "c" * 64, "bytes": 12}],
                  "score_reports": {"baseline": {"page_count": 1},
                                    "scraper": {"page_count": 1},
                                    "vector": {"page_count": 1}},
                  "collection": {"rows_per_binary": 1}}
        frozen = wceb_runner._freeze_payload(report, ["scraper"])
        self.assertEqual([item["id"] for item in frozen["candidates"]], ["scraper"])
        self.assertFalse(frozen["public_test_is_independent_secret_holdout"])
        with tempfile.TemporaryDirectory(prefix="athena-wceb-freeze-") as temporary:
            root = Path(temporary)
            dev_path = root / "dev.json"
            freeze_path = root / "freeze.json"
            dev_path.write_text(json.dumps(report), encoding="utf-8")
            duplicate_path = root / "duplicate.json"
            duplicate_path.write_text('{"schema":"old","schema":"new"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate JSON object member"):
                wceb_runner._read_json(duplicate_path, 1024)
            dev_size = dev_path.stat().st_size
            self.assertEqual(wceb_runner._read_json(dev_path, dev_size)["schema"], wceb_runner.SCHEMA)
            with self.assertRaisesRegex(ValueError, "over its byte limit"):
                wceb_runner._read_json(dev_path, dev_size - 1)
            import tracemalloc
            tracemalloc.start()
            try:
                written = wceb_runner.freeze_candidates(str(dev_path), str(freeze_path), ["scraper"])
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 512 * 1024,
                            "Reading a small dev report must use bounded chunks instead of pre-allocating MAX_REPORT_BYTES")
            self.assertEqual(written, freeze_path)
            with self.assertRaises(FileExistsError):
                wceb_runner.freeze_candidates(str(dev_path), str(freeze_path), ["vector"])
            binaries = [dict(report["binaries"][0]), dict(report["binaries"][1])]
            checked = wceb_runner._validate_test_freeze(str(freeze_path), str(dev_path), binaries)
            self.assertEqual(checked["schema"], wceb_runner.FREEZE_SCHEMA)
            binaries[1]["sha256"] = "d" * 64
            with self.assertRaisesRegex(ValueError, "differ from the immutable dev freeze"):
                wceb_runner._validate_test_freeze(str(freeze_path), str(dev_path), binaries)

    def test_test_split_cannot_run_without_dev_freeze(self):
        with self.assertRaisesRegex(ValueError, "requires the immutable dev freeze"):
            wceb_runner.run_study("unused", "unused", split="test", baseline="unused",
                                  candidates=[], wall_seconds=1, probe_timeout_seconds=1,
                                  resume=False, freeze_from=None, dev_report=None)

    def test_disk_reserve_failure_aborts_the_study_instead_of_becoming_a_missing_page(self):
        case = wceb_runner.wceb.WcebCase(
            split="dev", file_id="0001", source_url="https://example.org/",
            domain="example.org", page_type="article", main_content="body",
            main_content_state="text", with_snippets=(), without_snippets=(),
            annotation_sha256="a" * 64, html_git_blob_sha1="b" * 40,
            html_sha256="c" * 64, html_bytes=1, compressed_bytes=1,
            html_relpath="dev/html/0001.html.gz")

        class Run:
            deadline = time.monotonic() + 30
            manifest = {"commands": []}

            def run(self, *args, **kwargs):
                raise RuntimeError("Supervised process exceeded DISK_RESERVE")

        with patch.object(wceb_runner.wceb, "load_html", return_value="<main>x</main>"):
            with self.assertRaisesRegex(RuntimeError, "DISK_RESERVE"):
                wceb_runner._execute_case(
                    Run(), {"id": "candidate", "path": "unused"}, None, case,
                    output_budget=wceb_runner.wceb.Limits().prediction_total_bytes,
                    timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
