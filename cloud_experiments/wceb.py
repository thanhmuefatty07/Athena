"""Bounded, read-only adapter and scorer for the pinned public WCEB v1.0 tree.

The repository metadata license does not establish rights to the underlying
third-party page content. This module never downloads, copies, stages, or uploads
HTML; callers must complete the separate rights gate before cloud use.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import codecs
import gzip
import hashlib
import re
from pathlib import Path
import time
import unicodedata
from urllib.parse import urlsplit
import zlib

from kaggle_experiments.protocol import strict_json_loads, validate_aae
from cloud_experiments.workloads import _normalized_hostname


REPOSITORY = "Murrough-Foley/web-content-extraction-benchmark"
TAG = "v1.0"
COMMIT = "62ff86d12ea72c80c31fb810ff1a724fad687bea"
ROOT_TREE_SHA = "361ac62da1d6ef90f709a98ff14155a12d06ca0a"
TREE_MANIFEST_SHA256 = "ae210cc814f503987346fbc5c8d37662622e1039627f1a3c3fd2a76cee663516"
TREE_MANIFEST_PATH = Path(__file__).with_name("wceb_v1_tree_manifest.json")
_SPLITS = ("dev", "test")
_ID = re.compile(r"[0-9]{4}\Z")
_PAGE_TYPES = {"article", "forum", "product", "collection", "listing", "documentation", "service"}
MAX_AAE_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_AAE_FRAMES = 50_000
_UTF8_SIZE_CHUNK_CHARS = 16 * 1024


@dataclass(frozen=True)
class Limits:
    metadata_bytes: int = 512 * 1024
    annotation_bytes: int = 256 * 1024
    annotation_total_bytes: int = 64 * 1024 * 1024
    max_pages: int = 2008
    compressed_item_bytes: int = 1024 * 1024
    compressed_total_bytes: int = 128 * 1024 * 1024
    html_item_bytes: int = 16 * 1024 * 1024
    html_total_bytes: int = 8 * 1024 * 1024 * 1024
    prediction_item_bytes: int = MAX_AAE_OUTPUT_BYTES
    prediction_total_bytes: int = 512 * 1024 * 1024
    prediction_frame_limit: int = MAX_AAE_FRAMES
    wall_seconds: int = 1200
    chunk_bytes: int = 64 * 1024

    def validate(self):
        if any(type(value) is not int or value <= 0 for value in self.__dict__.values()):
            raise ValueError("WCEB resource limits must be positive integers")
        ceilings = {
            "metadata_bytes": 1024 * 1024,
            "annotation_bytes": 512 * 1024,
            "annotation_total_bytes": 128 * 1024 * 1024,
            "max_pages": 2008,
            "compressed_item_bytes": 1024 * 1024,
            "compressed_total_bytes": 128 * 1024 * 1024,
            "html_item_bytes": 16 * 1024 * 1024,
            "html_total_bytes": 8 * 1024 * 1024 * 1024,
            "prediction_item_bytes": MAX_AAE_OUTPUT_BYTES,
            "prediction_total_bytes": 512 * 1024 * 1024,
            "prediction_frame_limit": MAX_AAE_FRAMES,
            "wall_seconds": 1800,
            "chunk_bytes": 1024 * 1024,
        }
        if any(getattr(self, name) > cap for name, cap in ceilings.items()):
            raise ValueError("WCEB resource limit exceeds a hard ceiling")


@dataclass(frozen=True)
class WcebCase:
    split: str
    file_id: str
    source_url: str | None
    domain: str | None
    page_type: str
    main_content: str | None
    main_content_state: str
    with_snippets: tuple[str, ...]
    without_snippets: tuple[str, ...]
    annotation_sha256: str
    html_git_blob_sha1: str
    html_sha256: str
    html_bytes: int
    compressed_bytes: int
    html_relpath: str

    @property
    def key(self):
        return (self.split, self.file_id)


@dataclass(frozen=True)
class WcebDataset:
    root: Path
    metadata_sha256: str
    cases: tuple[WcebCase, ...]
    leakage: dict
    split_counts: dict
    public_splits: tuple[str, ...] = _SPLITS

    def cases_for(self, split: str):
        if split not in _SPLITS:
            raise ValueError("Unknown WCEB split")
        return tuple(case for case in self.cases if case.split == split)


def _git_blob_sha1(data: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    digest.update(data)
    return digest.hexdigest()


def _file_git_blob_sha1(path: Path, expected_size: int, limits: Limits, deadline: float) -> str:
    digest = hashlib.sha1()
    digest.update(b"blob " + str(expected_size).encode("ascii") + b"\0")
    total = 0
    with path.open("rb") as stream:
        while True:
            if time.monotonic() > deadline:
                raise ValueError("WCEB scan exceeded its wall-time budget")
            chunk = stream.read(limits.chunk_bytes)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise ValueError("WCEB file exceeds its pinned Git-tree size")
            digest.update(chunk)
    if total != expected_size:
        raise ValueError("WCEB file size differs from its pinned Git-tree size")
    return digest.hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("WCEB tree path is invalid")
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("WCEB tree path is invalid")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("WCEB inputs may not contain symbolic links")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("WCEB input path escapes its dataset root")
    return resolved


def _load_tree_manifest() -> dict:
    try:
        raw = TREE_MANIFEST_PATH.read_bytes()
    except OSError as error:
        raise ValueError("Pinned WCEB Git-tree manifest is unavailable") from error
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != TREE_MANIFEST_SHA256:
        raise ValueError("Pinned WCEB Git-tree manifest hash/size mismatch")
    try:
        tree = strict_json_loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("Pinned WCEB Git-tree manifest is malformed") from error
    if (not isinstance(tree, dict) or tree.get("schema") != "wceb-git-tree-manifest/1"
            or tree.get("repository") != REPOSITORY or tree.get("tag") != TAG
            or tree.get("commit") != COMMIT or tree.get("root_tree_sha") != ROOT_TREE_SHA
            or not isinstance(tree.get("files"), list)):
        raise ValueError("Pinned WCEB Git-tree manifest identity is invalid")
    _validate_tree_entries(tree["files"], pinned=True)
    return tree


def _validate_tree_entries(entries: list, *, pinned: bool):
    if not isinstance(entries, list) or len(entries) > 5000:
        raise ValueError("WCEB Git-tree manifest exceeds its entry bound")
    by_path = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"path", "git_blob_sha1", "size_bytes"}
                or not isinstance(entry["path"], str) or not isinstance(entry["git_blob_sha1"], str)
                or not re.fullmatch(r"[a-f0-9]{40}", entry["git_blob_sha1"])
                or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0):
            raise ValueError("WCEB Git-tree manifest entry is malformed")
        path = entry["path"]
        if (path in by_path or not (path == "metadata.json"
                or re.fullmatch(r"(?:dev|test)/html/[0-9]{4}\.html\.gz", path)
                or re.fullmatch(r"(?:dev|test)/ground-truth/[0-9]{4}\.json", path))):
            raise ValueError("WCEB Git-tree manifest has duplicate or unsupported paths")
        by_path[path] = entry
    if pinned:
        html = [entry for entry in entries if entry["path"].endswith(".html.gz")]
        annotations = [entry for entry in entries if "/ground-truth/" in entry["path"]]
        metadata = by_path.get("metadata.json")
        if len(html) != 2008 or len(annotations) != 2008 or metadata is None:
            raise ValueError("Pinned WCEB Git-tree inventory changed")
        if (sum(e["size_bytes"] for e in html) != 73_599_525
                or max(e["size_bytes"] for e in html) != 539_479
                or sum(e["size_bytes"] for e in annotations) != 37_888_479
                or max(e["size_bytes"] for e in annotations) != 144_772
                or metadata["size_bytes"] != 240_107):
            raise ValueError("Pinned WCEB Git-tree byte totals changed")
    return by_path


def _bounded_file_bytes(path: Path, cap: int, expected_size: int, limits: Limits, deadline: float) -> bytes:
    if expected_size > cap or path.stat().st_size != expected_size:
        raise ValueError("WCEB annotation exceeds its byte admission or pinned size")
    chunks = bytearray()
    with path.open("rb") as stream:
        while True:
            if time.monotonic() > deadline:
                raise ValueError("WCEB scan exceeded its wall-time budget")
            chunk = stream.read(min(limits.chunk_bytes, cap + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > cap:
                raise ValueError("WCEB annotation exceeds its byte admission")
    if len(chunks) != expected_size:
        raise ValueError("WCEB annotation changed while being read")
    return bytes(chunks)


def _scan_gzip(path: Path, limits: Limits, deadline: float, *, collect: bool):
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    output = bytearray() if collect else None
    total = 0
    try:
        with gzip.open(path, "rb") as stream:
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("WCEB scan exceeded its wall-time budget")
                chunk = stream.read(min(limits.chunk_bytes, limits.html_item_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limits.html_item_bytes:
                    raise ValueError("WCEB decompressed HTML exceeds its per-page limit")
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError as error:
                    raise ValueError("WCEB HTML is not valid UTF-8") from error
                digest.update(chunk)
                if output is not None:
                    output.extend(chunk)
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError("WCEB gzip is corrupt, truncated, or has an invalid trailer") from error
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as error:
        raise ValueError("WCEB HTML is not valid UTF-8") from error
    return digest.hexdigest(), total, bytes(output) if output is not None else None


def _page_type(annotation: dict) -> str:
    internal = annotation.get("_internal", {}) or {}
    if not isinstance(internal, dict):
        raise ValueError("WCEB annotation _internal value is invalid")
    value = internal.get("page_type", {})
    if isinstance(value, dict):
        page_type = value.get("primary", "article")
    elif isinstance(value, str):
        page_type = value
    else:
        page_type = "article"
    if page_type == "category":
        page_type = "collection"
    if not isinstance(page_type, str) or page_type not in _PAGE_TYPES:
        raise ValueError("WCEB annotation page type is unsupported")
    return page_type


def _url_host(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("WCEB source URL is missing or oversized")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("WCEB source URL is malformed") from error
    if parsed.scheme not in ("http", "https") or not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("WCEB source URL must be absolute HTTP(S) without credentials")
    return _normalized_hostname(host)


def _snippets(value, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("WCEB " + name + " snippets must be a string array or null")
    return tuple(value)


def load_wceb(root, limits: Limits | None = None) -> WcebDataset:
    """Verify a complete local v1.0 tree; no network or staging is performed."""
    return _load_wceb_from_tree(root, _load_tree_manifest(), limits, pinned=True)


def _load_wceb_from_tree(root, tree: dict, limits: Limits | None = None, *, pinned: bool = False) -> WcebDataset:
    """Internal injection point for tiny synthetic tests of the tree boundary."""
    limits = limits or Limits()
    limits.validate()
    root_input = Path(root)
    if root_input.is_symlink():
        raise ValueError("WCEB dataset root may not be a symbolic link")
    try:
        root = root_input.resolve(strict=True)
    except OSError as error:
        raise ValueError("WCEB dataset root is unavailable") from error
    if not root.is_dir():
        raise ValueError("WCEB dataset root must be a directory")
    if not isinstance(tree, dict) or not isinstance(tree.get("files"), list):
        raise ValueError("WCEB Git-tree manifest is invalid")
    expected = _validate_tree_entries(tree["files"], pinned=pinned)
    deadline = time.monotonic() + limits.wall_seconds
    metadata_path = _safe_path(root, "metadata.json")
    metadata_entry = expected["metadata.json"]
    if metadata_entry["size_bytes"] > limits.metadata_bytes:
        raise ValueError("WCEB metadata exceeds its byte admission")
    metadata_raw = _bounded_file_bytes(metadata_path, limits.metadata_bytes,
                                       metadata_entry["size_bytes"], limits, deadline)
    if _git_blob_sha1(metadata_raw) != metadata_entry["git_blob_sha1"]:
        raise ValueError("WCEB metadata differs from the pinned Git tree")
    metadata_sha256 = hashlib.sha256(metadata_raw).hexdigest()
    try:
        metadata = strict_json_loads(metadata_raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("WCEB metadata JSON is malformed") from error
    if (not isinstance(metadata, dict) or metadata.get("version") != "1.0"
            or metadata.get("license") != "CC-BY-4.0"
            or not isinstance(metadata.get("splits"), dict)
            or not isinstance(metadata.get("files"), dict)
            or not isinstance(metadata.get("page_types"), list)
            or any(not isinstance(value, str) for value in metadata["page_types"])
            or set(metadata["page_types"]) != _PAGE_TYPES):
        raise ValueError("WCEB metadata schema or declared collection license is unexpected")

    descriptors = []
    id_splits = defaultdict(set)
    annotation_total = compressed_total = html_total = 0
    expected_paths = {path for path in expected if path != "metadata.json"}
    actual_paths = set()
    for split in _SPLITS:
        split_info = metadata["splits"].get(split)
        if not isinstance(split_info, dict) or type(split_info.get("count")) is not int or split_info["count"] < 1:
            raise ValueError("WCEB metadata split count is invalid")
        gt_dir = _safe_path(root, split + "/ground-truth")
        html_dir = _safe_path(root, split + "/html")
        if not gt_dir.is_dir() or not html_dir.is_dir():
            raise ValueError("WCEB split directories are incomplete")
        gt_paths, html_paths = {}, {}
        for path in gt_dir.iterdir():
            if path.is_symlink():
                raise ValueError("WCEB inputs may not contain symbolic links")
            if not path.is_file() or not _ID.fullmatch(path.stem) or path.suffix != ".json":
                raise ValueError("WCEB ground-truth directory contains an unsupported entry")
            gt_paths[path.stem] = _safe_path(root, f"{split}/ground-truth/{path.name}")
            if len(gt_paths) > split_info["count"]:
                raise ValueError("WCEB ground-truth directory exceeds its declared page count")
        for path in html_dir.iterdir():
            file_id = path.name[:-len(".html.gz")] if path.name.endswith(".html.gz") else ""
            if path.is_symlink():
                raise ValueError("WCEB inputs may not contain symbolic links")
            if not path.is_file() or not _ID.fullmatch(file_id):
                raise ValueError("WCEB HTML directory contains an unsupported entry")
            html_paths[file_id] = _safe_path(root, f"{split}/html/{path.name}")
            if len(html_paths) > split_info["count"]:
                raise ValueError("WCEB HTML directory exceeds its declared page count")
        if set(gt_paths) != set(html_paths) or len(gt_paths) != split_info["count"]:
            raise ValueError("WCEB ground-truth/HTML pairing or declared split count differs")
        for file_id in sorted(gt_paths):
            gt_rel = f"{split}/ground-truth/{file_id}.json"
            html_rel = f"{split}/html/{file_id}.html.gz"
            actual_paths.update((gt_rel, html_rel))
            if gt_rel not in expected or html_rel not in expected:
                raise ValueError("WCEB file is absent from the pinned Git tree")
            gt_entry, html_entry = expected[gt_rel], expected[html_rel]
            if gt_entry["size_bytes"] > limits.annotation_bytes:
                raise ValueError("WCEB annotation exceeds its per-file limit")
            if html_entry["size_bytes"] > limits.compressed_item_bytes:
                raise ValueError("WCEB compressed HTML exceeds its per-file limit")
            annotation_total += gt_entry["size_bytes"]
            compressed_total += html_entry["size_bytes"]
            if annotation_total > limits.annotation_total_bytes or compressed_total > limits.compressed_total_bytes:
                raise ValueError("WCEB inputs exceed their aggregate byte limits")
            gt_raw = _bounded_file_bytes(gt_paths[file_id], limits.annotation_bytes,
                                         gt_entry["size_bytes"], limits, deadline)
            if _git_blob_sha1(gt_raw) != gt_entry["git_blob_sha1"]:
                raise ValueError("WCEB annotation differs from the pinned Git tree")
            annotation_sha256 = hashlib.sha256(gt_raw).hexdigest()
            try:
                annotation = strict_json_loads(gt_raw)
            except (ValueError, RecursionError) as error:
                raise ValueError("WCEB annotation JSON is malformed") from error
            if (not isinstance(annotation, dict) or annotation.get("schema_version") != "2.0"
                    or annotation.get("file_id", file_id) != file_id
                    or not isinstance(annotation.get("ground_truth"), dict)):
                raise ValueError(f"WCEB annotation identity/schema is invalid at {split}/{file_id}")
            gold = annotation["ground_truth"]
            if "main_content" not in gold:
                main_content, main_content_state = None, "missing"
            else:
                main_content = gold["main_content"]
                if main_content is not None and not isinstance(main_content, str):
                    raise ValueError(
                        f"WCEB main_content has invalid type {type(main_content).__name__} at {split}/{file_id}"
                    )
                main_content_state = "null" if main_content is None else "text"
            raw_source_url = annotation.get("url")
            try:
                host = _url_host(raw_source_url)
                source_url = raw_source_url
            except ValueError:
                # Preserve the row for content scoring, but do not invent a host
                # or pretend that AAE source identity can be checked for it.
                host = source_url = None
            page_type = _page_type(annotation)
            meta_file = metadata["files"].get(file_id)
            if not isinstance(meta_file, dict):
                raise ValueError("WCEB metadata lacks a file-index entry")
            domain = host
            with_snippets = _snippets(gold.get("with", []), "with")
            without_snippets = _snippets(gold.get("without", []), "without")
            compressed_path = html_paths[file_id]
            if (compressed_path.stat().st_size != html_entry["size_bytes"]
                    or _file_git_blob_sha1(compressed_path, html_entry["size_bytes"], limits, deadline)
                    != html_entry["git_blob_sha1"]):
                raise ValueError("WCEB compressed HTML differs from its pinned Git-tree object")
            html_sha256, html_bytes, _ = _scan_gzip(compressed_path, limits, deadline, collect=False)
            html_total += html_bytes
            if html_total > limits.html_total_bytes:
                raise ValueError("WCEB decompressed corpus exceeds its aggregate byte limit")
            descriptors.append(WcebCase(
                split=split, file_id=file_id, source_url=source_url, domain=domain, page_type=page_type,
                main_content=main_content, main_content_state=main_content_state, with_snippets=with_snippets,
                without_snippets=without_snippets, annotation_sha256=annotation_sha256,
                html_git_blob_sha1=html_entry["git_blob_sha1"], html_sha256=html_sha256,
                html_bytes=html_bytes, compressed_bytes=html_entry["size_bytes"], html_relpath=html_rel,
            ))
            id_splits[file_id].add(split)
            if len(descriptors) > limits.max_pages:
                raise ValueError("WCEB page count exceeds its hard maximum")
    if actual_paths != expected_paths:
        raise ValueError("WCEB local tree is incomplete or contains unpinned inputs")

    if set(metadata["files"]) != set(id_splits):
        raise ValueError("WCEB metadata file index does not cover the unique split-qualified IDs")
    for file_id, meta_file in metadata["files"].items():
        if not isinstance(file_id, str) or not _ID.fullmatch(file_id) or not isinstance(meta_file, dict):
            raise ValueError("WCEB metadata file index is malformed")
        if file_id not in id_splits or meta_file.get("split") not in id_splits[file_id]:
            raise ValueError("WCEB metadata file index contains an unpaired/mis-split id")

    dev = [case for case in descriptors if case.split == "dev"]
    test = [case for case in descriptors if case.split == "test"]
    dev_domains = {case.domain for case in dev if case.domain is not None}
    dev_html = {case.html_sha256 for case in dev}
    overlap_domains = sorted({case.domain for case in test} & dev_domains)
    duplicate_test = sorted(case.file_id for case in test if case.html_sha256 in dev_html)
    id_overlap = sorted({case.file_id for case in dev} & {case.file_id for case in test})
    diagnostic_ids = sorted(case.file_id for case in test
                            if case.domain is not None and case.domain not in dev_domains
                            and case.html_sha256 not in dev_html)
    leakage = {
        "public_test_split": True,
        "overlap_domain_count": len(overlap_domains),
        "overlap_domains": overlap_domains,
        "cross_split_html_duplicate_count": len(duplicate_test),
        "cross_split_html_duplicate_ids": duplicate_test,
        "cross_split_file_id_collision_count": len(id_overlap),
        "cross_split_file_id_collisions": id_overlap,
        "nonoverlap_diagnostic_page_ids": diagnostic_ids,
        "independent_secret_holdout": False,
    }
    return WcebDataset(root, metadata_sha256, tuple(descriptors), leakage,
                       {split: len([case for case in descriptors if case.split == split]) for split in _SPLITS})


def load_html(dataset: WcebDataset, case: WcebCase, limits: Limits | None = None) -> str:
    """Load one page on demand and reject post-index mutation or expansion."""
    limits = limits or Limits()
    limits.validate()
    if case not in dataset.cases or case.split not in _SPLITS:
        raise ValueError("WCEB case does not belong to this verified dataset index")
    path = _safe_path(dataset.root, case.html_relpath)
    if path.stat().st_size != case.compressed_bytes:
        raise ValueError("WCEB HTML changed after index verification")
    deadline = time.monotonic() + limits.wall_seconds
    if _file_git_blob_sha1(path, case.compressed_bytes, limits, deadline) != case.html_git_blob_sha1:
        raise ValueError("WCEB HTML changed after index verification")
    digest, size, raw = _scan_gzip(path, limits, deadline, collect=True)
    if digest != case.html_sha256 or size != case.html_bytes or raw is None:
        raise ValueError("WCEB decompressed HTML changed after index verification")
    return raw.decode("utf-8", errors="strict")


def _word_metrics(predicted: str, reference: str):
    predicted_counts = Counter()
    predicted_count = 0
    lowered_prediction = predicted.lower()
    for match in re.finditer(r"\w+", lowered_prediction):
        predicted_counts[match.group(0)] += 1
        predicted_count += 1
    del lowered_prediction

    reference_count = overlap = 0
    lowered_reference = reference.lower()
    for match in re.finditer(r"\w+", lowered_reference):
        token = match.group(0)
        available = predicted_counts.get(token, 0)
        if available:
            predicted_counts[token] = available - 1
            overlap += 1
        reference_count += 1
    if not reference_count:
        return (1.0, 1.0, 1.0) if not predicted_count else (0.0, 0.0, 0.0)
    if not predicted_count:
        return 0.0, 0.0, 0.0
    precision = overlap / predicted_count
    recall = overlap / reference_count
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _snippet_rate(text: str, snippets: tuple[str, ...]) -> float:
    if not snippets:
        return 1.0
    lower = text.lower()
    return sum(snippet.lower() in lower for snippet in snippets) / len(snippets)


def _mean(values):
    return sum(values) / len(values) if values else None


def _bounded_utf8_size(value: str, limit: int) -> int:
    """Count UTF-8 bytes with a fixed-size temporary instead of encoding all text."""
    length = str.__len__(value)
    total = 0
    offset = 0
    while offset < length:
        remaining = limit - total
        if remaining <= 0:
            raise ValueError("WCEB prediction text exceeds its byte budget")
        # At most 64 KiB of encoded data is materialized per chunk. Tighten the
        # character count when little budget remains so rejection is prompt.
        chars = min(_UTF8_SIZE_CHUNK_CHARS, max(1, remaining // 4))
        end = min(length, offset + chars)
        piece = str.__getitem__(value, slice(offset, end))
        try:
            encoded = str.encode(piece, "utf-8", "strict")
        except UnicodeEncodeError as error:
            raise ValueError("WCEB prediction text is not valid Unicode") from error
        total += len(encoded)
        if total > limit:
            raise ValueError("WCEB prediction text exceeds its byte budget")
        offset = end
    return total


def _score_case(case: WcebCase, predicted: str, prediction_missing: bool,
                aae_source_identity_verified: bool | None = None):
    reference = case.main_content
    if case.main_content_state != "text":
        precision = recall = f1 = None
        official_precision, official_recall, official_f1 = _word_metrics(predicted, "")
    else:
        precision, recall, f1 = _word_metrics(predicted, reference)
        official_precision, official_recall, official_f1 = precision, recall, f1
    return {
        "split": case.split, "file_id": case.file_id, "domain": case.domain, "page_type": case.page_type,
        "source_url_valid": case.source_url is not None,
        "annotation_sha256": case.annotation_sha256, "html_sha256": case.html_sha256,
        "aae_source_identity_verified": aae_source_identity_verified,
        "reference_is_null": case.main_content_state == "null",
        "reference_is_missing": case.main_content_state == "missing",
        "precision": precision, "recall": recall, "f1": f1,
        "official_compat_precision": official_precision,
        "official_compat_recall": official_recall, "official_compat_f1": official_f1,
        "with_rate": _snippet_rate(predicted, case.with_snippets),
        "without_rate": _snippet_rate(predicted, case.without_snippets),
        "prediction_missing": prediction_missing,
    }


def _score_report(dataset: WcebDataset, split: str, rows: list, extra: list, payload_bytes: int) -> dict:
    dev_cases = dataset.cases_for("dev")
    dev_domains = {case.domain for case in dev_cases if case.domain is not None}
    dev_html = {case.html_sha256 for case in dev_cases}
    for row in rows:
        if split == "test":
            row["domain_seen_in_dev"] = None if row["domain"] is None else row["domain"] in dev_domains
            row["html_seen_in_dev"] = row["html_sha256"] in dev_html
            row["strict_nonoverlap_diagnostic"] = (
                None if row["domain"] is None else
                not row["domain_seen_in_dev"] and not row["html_seen_in_dev"]
            )
        else:
            row["domain_seen_in_dev"] = None
            row["html_seen_in_dev"] = None
            row["strict_nonoverlap_diagnostic"] = None
    scored = [row for row in rows if row["f1"] is not None]
    by_type = defaultdict(list)
    by_domain = defaultdict(list)
    strict_nonoverlap = []
    for row in rows:
        if row["f1"] is not None:
            by_type[row["page_type"]].append(row["f1"])
            if row["domain"] is not None:
                by_domain[row["domain"]].append(row["f1"])
            if row["strict_nonoverlap_diagnostic"] is True:
                strict_nonoverlap.append(row)
    return {
        "schema": "wceb-score/1", "repository": REPOSITORY, "tag": TAG, "commit": COMMIT,
        "publisher_declared_license": "CC-BY-4.0",
        "attribution": "Murrough Foley, Web Content Extraction Benchmark v1.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "metadata_sha256": dataset.metadata_sha256, "split": split, "split_is_public": True,
        "independent_secret_holdout": False, "page_count": len(rows),
        "payload_bytes": payload_bytes,
        "leakage_audit": {key: value for key, value in dataset.leakage.items()
                           if key not in {"overlap_domains", "cross_split_html_duplicate_ids",
                                          "cross_split_file_id_collisions", "nonoverlap_diagnostic_page_ids"}},
        "nonnull_reference_count": len(scored),
        "null_reference_count": sum(row["reference_is_null"] for row in rows),
        "missing_reference_count": sum(row["reference_is_missing"] for row in rows),
        "unlabeled_reference_count": sum(row["reference_is_null"] or row["reference_is_missing"] for row in rows),
        "url_less_count": sum(row["domain"] is None for row in rows),
        "missing_prediction_count": sum(row["prediction_missing"] for row in rows),
        "aae_source_identity_unverified_count": sum(row["aae_source_identity_verified"] is False for row in rows),
        "extra_prediction_count": len(extra), "extra_prediction_keys": sorted(extra),
        "page_type_nonnull_count": {key: len(values) for key, values in sorted(by_type.items())},
        "domain_nonnull_count": {key: len(values) for key, values in sorted(by_domain.items())},
        "strict_nonoverlap_page_count": sum(row["strict_nonoverlap_diagnostic"] is True for row in rows),
        "strict_nonoverlap_nonnull_reference_count": len(strict_nonoverlap),
        "strict_nonoverlap_macro_f1_nonnull": _mean([row["f1"] for row in strict_nonoverlap]),
        "strict_nonoverlap_page_type_nonnull_count": {
            key: sum(row["page_type"] == key for row in strict_nonoverlap)
            for key in sorted({row["page_type"] for row in strict_nonoverlap})
        },
        "macro_precision_nonnull": _mean([row["precision"] for row in scored]),
        "macro_recall_nonnull": _mean([row["recall"] for row in scored]),
        "macro_f1_nonnull": _mean([row["f1"] for row in scored]),
        "official_compat_macro_f1_all": _mean([row["official_compat_f1"] for row in rows]),
        "macro_with_rate_all": _mean([row["with_rate"] for row in rows]),
        "macro_without_rate_all": _mean([row["without_rate"] for row in rows]),
        "page_type_macro_f1_nonnull": {key: _mean(values) for key, values in sorted(by_type.items())},
        "domain_macro_f1_nonnull": {key: _mean(values) for key, values in sorted(by_domain.items())},
        "python": __import__("platform").python_version(),
        "unicode_database": unicodedata.unidata_version,
        "rows": rows,
    }


def aae_prediction_text(payload: bytes, expected_source_url: str | None, limits: Limits | None = None) -> str:
    """Validate bounded AAE/1 output and, when known, bind it to its source URL."""
    limits = limits or Limits()
    limits.validate()
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= limits.prediction_item_bytes:
        raise ValueError("AAE prediction is empty or exceeds its byte admission")
    if payload.count(b"\n") > limits.prediction_frame_limit:
        raise ValueError("AAE prediction exceeds its frame-count limit")
    try:
        frames = validate_aae(payload, expected_source_url=expected_source_url)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValueError("WCEB prediction is not valid AAE/1 for its source URL") from error
    return "\n".join(frame["text"] for frame in frames if frame.get("type") == "block")


def score_aae_case(case: WcebCase, payload: bytes, limits: Limits | None = None) -> dict:
    """Validate and score one AAE result without retaining other page payloads."""
    text = aae_prediction_text(payload, case.source_url, limits)
    return _score_case(case, text, False, case.source_url is not None)


def score_missing_aae_case(case: WcebCase) -> dict:
    """Return the benchmark row for an explicitly failed or unadmitted page."""
    return _score_case(case, "", True)


def score_split(dataset: WcebDataset, split: str, predictions: dict, limits: Limits | None = None) -> dict:
    """Score extracted text keyed by (split, file_id), preserving null diagnostics."""
    limits = limits or Limits()
    limits.validate()
    cases = dataset.cases_for(split)
    if not isinstance(predictions, dict):
        raise ValueError("WCEB predictions must be a mapping keyed by (split, file_id)")
    if len(predictions) > limits.max_pages:
        raise ValueError("WCEB prediction mapping exceeds its result-count limit")
    known = {case.key: case for case in cases}
    seen, extra, prediction_text, total = set(), [], {}, 0
    for key, value in predictions.items():
        if (not isinstance(key, tuple) or len(key) != 2 or not all(isinstance(x, str) for x in key)
                or key[0] not in _SPLITS or not key[1] or len(key[1]) > 128
                or not isinstance(value, str)):
            raise ValueError("WCEB predictions require string tuple keys and string values")
        remaining = min(limits.prediction_item_bytes, limits.prediction_total_bytes - total)
        size = _bounded_utf8_size(value, remaining)
        total += size
        if key in seen:
            raise ValueError("Duplicate WCEB prediction key")
        seen.add(key)
        if key not in known:
            extra.append("/".join(key))
        else:
            prediction_text[key] = value
    rows = [_score_case(case, prediction_text.get(case.key, ""), case.key not in prediction_text)
            for case in cases]
    return _score_report(dataset, split, rows, extra, total)


def score_aae_stream(dataset: WcebDataset, split: str, results, limits: Limits | None = None) -> dict:
    """Score an iterable of `(split, file_id), AAE-jsonl-bytes` without retaining payloads."""
    limits = limits or Limits()
    limits.validate()
    cases = dataset.cases_for(split)
    known = {case.key: case for case in cases}
    rows_by_key, extra, seen = {}, [], set()
    total_bytes = item_count = 0
    for item in results:
        item_count += 1
        if item_count > limits.max_pages:
            raise ValueError("WCEB prediction stream exceeds its result-count limit")
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("WCEB AAE stream items must be (key, payload) tuples")
        key, payload = item
        if (not isinstance(key, tuple) or len(key) != 2 or not all(isinstance(x, str) for x in key)
                or key[0] not in _SPLITS or not key[1] or len(key[1]) > 128):
            raise ValueError("WCEB AAE stream key must be (split, file_id)")
        if key in seen:
            raise ValueError("Duplicate WCEB AAE stream key")
        seen.add(key)
        if not isinstance(payload, bytes):
            raise ValueError("WCEB AAE stream payload must be bytes")
        total_bytes += len(payload)
        if len(payload) > limits.prediction_item_bytes or total_bytes > limits.prediction_total_bytes:
            raise ValueError("WCEB AAE stream exceeds its byte budget")
        case = known.get(key)
        if case is None:
            extra.append("/".join(key))
            continue
        rows_by_key[key] = score_aae_case(case, payload, limits)
    rows = [rows_by_key[case.key] if case.key in rows_by_key else _score_case(case, "", True)
            for case in cases]
    return _score_report(dataset, split, rows, extra, total_bytes)
