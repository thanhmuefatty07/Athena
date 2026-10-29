"""Focused contract tests for the bounded, offline WCEB adapter."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cloud_experiments.wceb import (
    Limits,
    _git_blob_sha1,
    _load_wceb_from_tree,
    _load_tree_manifest,
    aae_prediction_text,
    load_html,
    score_aae_stream,
    score_split,
)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _aae(source_url, text):
    block_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
    frames = [
        {"type": "meta", "version": "aae/1", "source_url": source_url,
         "content_hash": hashlib.sha256(b"content").hexdigest(), "total_blocks": 1},
        {"type": "block", "id": block_id, "role": "main", "text": text, "links": []},
        {"type": "end", "block_count": 1},
    ]
    return b"".join(_json_bytes(frame) + b"\n" for frame in frames)


def _fixture(root: Path, html: bytes | None = None, *, compressed_override: bytes | None = None,
             invalid_dev_url: bool = False, invalid_test_url: bool = False, unseen_test: bool = False,
             missing_test_main_content: bool = False, load: bool = True):
    html = html if html is not None else b"<main>shared html</main>"
    compressed = compressed_override if compressed_override is not None else gzip.compress(html, mtime=0)
    dev_url = "invalid source url" if invalid_dev_url else "https://example.com/dev"
    test_url = ("invalid source url" if invalid_test_url else
                "https://other.example/test" if unseen_test else "https://example.com/test")
    test_content = "test body" if invalid_test_url or unseen_test else None
    compressed_by_split = {"dev": compressed, "test": compressed}
    if unseen_test:
        compressed_by_split["test"] = gzip.compress(b"<main>test-only html</main>", mtime=0)
    page_data = {
        "dev": ("0001", dev_url, "a a é", ["A", "missing"], ["forbidden"]),
        "test": ("0002", test_url, test_content, [], []),
    }
    metadata = {
        "version": "1.0", "license": "CC-BY-4.0",
        "page_types": ["article", "forum", "product", "collection", "listing", "documentation", "service"],
        "splits": {split: {"count": 1} for split in page_data},
        "files": {file_id: {"split": split, "domain": "example.com", "page_type": "article"}
                  for split, (file_id, *_rest) in page_data.items()},
    }
    files = {"metadata.json": _json_bytes(metadata)}
    for split, (file_id, url, main_content, with_snippets, without_snippets) in page_data.items():
        annotation = {
            "schema_version": "2.0", "file_id": file_id, "url": url,
            "_internal": {"page_type": {"primary": "article"}},
            "ground_truth": {"main_content": main_content, "with": with_snippets,
                             "without": without_snippets},
        }
        if split == "test" and missing_test_main_content:
            annotation["ground_truth"].pop("main_content")
        if split == "test" and invalid_test_url:
            annotation.pop("file_id")
            annotation.pop("url")
        files[f"{split}/ground-truth/{file_id}.json"] = _json_bytes(annotation)
        files[f"{split}/html/{file_id}.html.gz"] = compressed_by_split[split]
    entries = []
    for relative, content in files.items():
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append({"path": relative, "git_blob_sha1": _git_blob_sha1(content),
                        "size_bytes": len(content)})
    tree = {"files": entries}
    dataset = _load_wceb_from_tree(root, tree) if load else None
    return dataset, files


class WcebAdapterContracts(unittest.TestCase):
    def test_committed_tree_manifest_matches_its_source_pin(self):
        manifest = _load_tree_manifest()
        self.assertEqual(manifest["root_tree_sha"], "361ac62da1d6ef90f709a98ff14155a12d06ca0a")
        self.assertEqual(len(manifest["files"]), 4017)

    def test_pinned_tree_pairing_leakage_and_scoring_oracles(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            root = Path(temporary)
            dataset, _ = _fixture(root)

            self.assertEqual(dataset.split_counts, {"dev": 1, "test": 1})
            self.assertEqual(dataset.leakage["overlap_domain_count"], 1)
            self.assertEqual(dataset.leakage["cross_split_html_duplicate_count"], 1)
            self.assertFalse(dataset.leakage["independent_secret_holdout"])
            self.assertEqual(load_html(dataset, dataset.cases_for("dev")[0]), "<main>shared html</main>")

            report = score_split(dataset, "dev", {("dev", "0001"): "A a a é"})
            self.assertEqual(report["publisher_declared_license"], "CC-BY-4.0")
            self.assertEqual(report["attribution"], "Murrough Foley, Web Content Extraction Benchmark v1.0")
            row = report["rows"][0]
            self.assertAlmostEqual(row["precision"], 0.75)
            self.assertEqual(row["recall"], 1.0)
            self.assertAlmostEqual(row["f1"], 6 / 7)
            self.assertEqual(row["with_rate"], 0.5)
            self.assertEqual(row["without_rate"], 0.0)

            null_report = score_split(dataset, "test", {})
            null_row = null_report["rows"][0]
            self.assertIsNone(null_row["f1"])
            self.assertEqual(null_row["official_compat_f1"], 1.0)
            self.assertEqual(null_report["nonnull_reference_count"], 0)

    def test_score_bounds_utf8_bytes_without_whole_text_encoding(self):
        class EncodeTrap(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("whole-string UTF-8 encoding must not run before admission")

        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            dataset, _ = _fixture(Path(temporary))
            case = dataset.cases_for("test")[0]
            prediction = EncodeTrap("é" * 40_000)
            exact = score_split(
                dataset, "test", {case.key: prediction},
                Limits(prediction_item_bytes=80_000, prediction_total_bytes=80_000),
            )
            self.assertEqual(exact["payload_bytes"], 80_000)
            with self.assertRaisesRegex(ValueError, "byte budget"):
                score_split(
                    dataset, "test", {case.key: EncodeTrap("éé")},
                    Limits(prediction_item_bytes=4, prediction_total_bytes=3),
                )

    def test_aae_stream_validates_contract_and_preserves_block_text(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            dataset, _ = _fixture(Path(temporary))
            case = dataset.cases_for("dev")[0]
            payload = _aae(case.source_url, "A a a é")
            self.assertEqual(aae_prediction_text(payload, case.source_url), "A a a é")
            report = score_aae_stream(dataset, "dev", iter(((case.key, payload),)))
            self.assertAlmostEqual(report["macro_f1_nonnull"], 6 / 7)
            self.assertEqual(report["payload_bytes"], len(payload))
            with self.assertRaisesRegex(ValueError, "byte budget"):
                score_aae_stream(
                    dataset, "dev", iter(((case.key, payload),)),
                    Limits(prediction_item_bytes=len(payload), prediction_total_bytes=len(payload) - 1),
                )
            with self.assertRaisesRegex(ValueError, "AAE/1"):
                aae_prediction_text(payload, "https://wrong.example/page")
            with self.assertRaisesRegex(ValueError, "frame-count"):
                aae_prediction_text(payload, case.source_url, Limits(prediction_frame_limit=2))
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                score_aae_stream(dataset, "dev", iter(((case.key, payload), (case.key, payload))))

    def test_invalid_source_url_keeps_content_row_outside_domain_metrics(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            dataset, _ = _fixture(Path(temporary), invalid_dev_url=True, invalid_test_url=True)
            case = dataset.cases_for("test")[0]
            self.assertIsNone(case.domain)
            report = score_split(dataset, "test", {})
            self.assertEqual(report["url_less_count"], 1)
            self.assertEqual(report["leakage_audit"]["overlap_domain_count"], 0)
            self.assertNotIn(None, report["domain_macro_f1_nonnull"])
            self.assertEqual(report["page_type_macro_f1_nonnull"], {"article": 0.0})
            aae_report = score_aae_stream(
                dataset, "test", iter(((case.key, _aae("https://example.com/test", "test body")),))
            )
            self.assertEqual(aae_report["rows"][0]["f1"], 1.0)
            self.assertIs(aae_report["rows"][0]["aae_source_identity_verified"], False)
            self.assertEqual(aae_report["aae_source_identity_unverified_count"], 1)

    def test_missing_main_content_is_distinct_from_null_and_excluded(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            dataset, _ = _fixture(Path(temporary), missing_test_main_content=True)
            report = score_split(dataset, "test", {})
            row = report["rows"][0]
            self.assertTrue(row["reference_is_missing"])
            self.assertFalse(row["reference_is_null"])
            self.assertIsNone(row["f1"])
            self.assertEqual(row["official_compat_f1"], 1.0)
            self.assertEqual(report["null_reference_count"], 0)
            self.assertEqual(report["missing_reference_count"], 1)
            self.assertEqual(report["nonnull_reference_count"], 0)

    def test_reports_strict_unseen_domain_subset_with_sample_counts(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            dataset, _ = _fixture(Path(temporary), unseen_test=True)
            case = dataset.cases_for("test")[0]
            report = score_split(dataset, "test", {case.key: "test body"})
            row = report["rows"][0]
            self.assertIs(row["domain_seen_in_dev"], False)
            self.assertIs(row["html_seen_in_dev"], False)
            self.assertIs(row["strict_nonoverlap_diagnostic"], True)
            self.assertEqual(report["strict_nonoverlap_page_count"], 1)
            self.assertEqual(report["strict_nonoverlap_macro_f1_nonnull"], 1.0)
            self.assertEqual(report["strict_nonoverlap_page_type_nonnull_count"], {"article": 1})
            self.assertEqual(report["domain_nonnull_count"], {"other.example": 1})

    def test_rejects_corrupt_expansion_mutation_and_raised_resource_limits(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            root = Path(temporary)
            dataset, _ = _fixture(root, html=b"x" * 33)
            with self.assertRaisesRegex(ValueError, "per-page limit"):
                _load_wceb_from_tree(root, {"files": [
                    {"path": entry["path"], "git_blob_sha1": entry["git_blob_sha1"],
                     "size_bytes": entry["size_bytes"]} for entry in _tree_entries(root)
                ]}, Limits(html_item_bytes=32))

            case = dataset.cases_for("dev")[0]
            html_path = root / Path(*case.html_relpath.split("/"))
            html_path.write_bytes(html_path.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "changed after index verification"):
                load_html(dataset, case)

        with self.assertRaisesRegex(ValueError, "hard ceiling"):
            Limits(html_item_bytes=32 * 1024 * 1024).validate()

    def test_rejects_gzip_with_invalid_utf8_after_matching_tree_identity(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            root = Path(temporary)
            _fixture(root, html=b"<main>\xff</main>", load=False)
            with self.assertRaisesRegex(ValueError, "valid UTF-8"):
                _load_wceb_from_tree(root, {"files": _tree_entries(root)})

        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            root = Path(temporary)
            _fixture(root, compressed_override=b"\x1f\x8btruncated", load=False)
            with self.assertRaisesRegex(ValueError, "gzip is corrupt"):
                _load_wceb_from_tree(root, {"files": _tree_entries(root)})

    def test_rejects_dataset_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="athena-wceb-") as temporary:
            base = Path(temporary)
            root = base / "dataset"
            _fixture(root, load=False)
            link = root / "dev" / "html" / "0001.html.gz"
            external = base / "outside.html.gz"
            external.write_bytes(link.read_bytes())
            link.unlink()
            try:
                link.symlink_to(external)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"host cannot create a filesystem symlink: {error}")
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                _load_wceb_from_tree(root, {"files": _tree_entries(root)})


def _tree_entries(root: Path):
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            entries.append({"path": path.relative_to(root).as_posix(),
                            "git_blob_sha1": _git_blob_sha1(data), "size_bytes": len(data)})
    return entries


if __name__ == "__main__":
    unittest.main()
