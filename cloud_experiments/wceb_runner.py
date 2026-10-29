"""Bounded, resumable WCEB-to-Rust AAE evaluation; never fetches dataset content."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time

from cloud_experiments import campaign, lifecycle, process_isolation, wceb, workloads
from kaggle_experiments import protocol, runner


SCHEMA = "athena-wceb-core-study/1"
FREEZE_SCHEMA = "athena-wceb-dev-freeze/1"
CORE_HTML_BYTES = 4 * 1024 * 1024
CORE_FIXTURE_BYTES = 8 * 1024 * 1024
MAX_BINARIES_DEV = 16
MAX_BINARIES_TEST = 3
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
MAX_COLLECTION_METADATA_BYTES = 64 * 1024 * 1024
MAX_COMMAND_METADATA_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_WALL_SECONDS = 18_000
MAX_DATASET_SCAN_SECONDS = 1800
MAX_PROBE_SECONDS = 60
BOOTSTRAP_DRAWS = 50_000
PRIMARY_METRICS = ("f1", "with_rate", "without_rate")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha256_file(path: Path, max_bytes: int) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native probe must be a regular, unlinked file")
    size = path.stat().st_size
    if size < 1 or size > max_bytes:
        raise ValueError("Native probe executable exceeds its byte admission")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Native probe changed while it was hashed")
            digest.update(chunk)
    if total != size:
        raise ValueError("Native probe changed while it was hashed")
    return digest.hexdigest(), total


def _runtime_identity() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "process_isolation": ("required-deny-network" if os.name == "posix"
                                  else "DISABLED-on-this-platform")}


def _source_identity() -> dict:
    modules = {"wceb_runner": sys.modules[__name__], "wceb": wceb,
               "lifecycle": lifecycle, "campaign": campaign,
               "process_isolation": process_isolation, "workloads": workloads,
               "protocol": protocol, "runner": runner}
    result = {}
    for name, module in modules.items():
        source = Path(module.__file__)
        if source.is_symlink() or not source.is_file():
            raise ValueError("A required WCEB runner source is not a regular file")
        digest, size = _sha256_file(source, 8 * 1024 * 1024)
        result[name] = {"sha256": digest, "bytes": size}
    return result


def _fixture_json(html: str, url: str, *, cap: int = CORE_FIXTURE_BYTES,
                  chunk_chars: int = 32 * 1024) -> bytes:
    """Encode one fixture with a hard output bound and without a full escaped copy."""
    if not isinstance(html, str) or not isinstance(url, str):
        raise ValueError("WCEB fixture fields must be text")
    if type(cap) is not int or not 1 <= cap <= CORE_FIXTURE_BYTES:
        raise ValueError("WCEB fixture cap is outside the native probe limit")
    if type(chunk_chars) is not int or not 1 <= chunk_chars <= 64 * 1024:
        raise ValueError("WCEB fixture chunk size is invalid")
    if len(url) > 8192:
        raise ValueError("WCEB URL exceeds the native probe admission")
    raw_bytes = 0
    for offset in range(0, len(html), chunk_chars):
        raw_bytes += len(html[offset:offset + chunk_chars].encode("utf-8"))
        if raw_bytes > CORE_HTML_BYTES:
            raise ValueError("WCEB HTML exceeds the native core's current 4 MiB admission")
    prefix = b'{"html":"'
    suffix = b'","url":' + json.dumps(url, ensure_ascii=False).encode("utf-8") + b'}'
    output = bytearray(prefix)
    if len(output) + len(suffix) > cap:
        raise ValueError("WCEB fixture exceeds the native probe JSON admission")
    for offset in range(0, len(html), chunk_chars):
        escaped = json.dumps(html[offset:offset + chunk_chars], ensure_ascii=False)[1:-1].encode("utf-8")
        if len(escaped) > cap - len(output) - len(suffix):
            raise ValueError("WCEB fixture exceeds the native probe JSON admission")
        output.extend(escaped)
    output.extend(suffix)
    return bytes(output)


def _binary_arguments(baseline: str, candidates: list[str], split: str) -> list[dict]:
    limit = MAX_BINARIES_DEV if split == "dev" else MAX_BINARIES_TEST
    if not isinstance(candidates, list) or len(candidates) > limit - 1:
        raise ValueError(f"{split} study accepts at most {limit} binaries including baseline")
    specifications = [("baseline", baseline)]
    for item in candidates:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError("Each candidate must be ID=PATH")
        identifier, path = item.split("=", 1)
        if identifier == "baseline" or not _IDENTIFIER.fullmatch(identifier) or not path:
            raise ValueError("Candidate IDs must be unique bounded identifiers")
        specifications.append((identifier, path))
    if len({identifier for identifier, _ in specifications}) != len(specifications):
        raise ValueError("Native probe IDs must be unique")
    resolved = []
    for identifier, raw_path in specifications:
        source = Path(raw_path)
        if source.is_symlink():
            raise ValueError("Native probe path cannot be a symbolic link")
        path = source.resolve(strict=True)
        digest, size = _sha256_file(path, 256 * 1024 * 1024)
        resolved.append({"id": identifier, "path": str(path), "sha256": digest, "bytes": size})
    if len({item["sha256"] for item in resolved}) != len(resolved):
        raise ValueError("Duplicate native probe bytes would repeat the same comparison")
    return resolved


def _case_identity(case: wceb.WcebCase) -> dict:
    return {"split": case.split, "file_id": case.file_id, "domain": case.domain,
            "page_type": case.page_type, "annotation_sha256": case.annotation_sha256,
            "html_sha256": case.html_sha256}


def _execute_case(run: lifecycle.ManagedRun, binary: dict, dataset: wceb.WcebDataset,
                  case: wceb.WcebCase, output_budget: int, timeout_seconds: int) -> dict:
    base = _case_identity(case)
    if time.monotonic() >= run.deadline:
        raise TimeoutError("WCEB study wall-clock budget exhausted")
    if case.html_bytes > CORE_HTML_BYTES:
        row = wceb.score_missing_aae_case(case)
        row.update(base, candidate_id=binary["id"], core_execution_state="NOT_ADMITTED_HTML_CAP",
                   output_sha256=None, output_bytes=0, output_budget_bytes=output_budget,
                   command_wall_ns=None, sampled_process_tree_rss_bytes=None)
        return row

    html = wceb.load_html(dataset, case)
    url = case.source_url or "https://wceb.invalid/"
    fixture = _fixture_json(html, url)
    started = time.monotonic_ns()
    try:
        payload = run.run([binary["path"], "parse", "-", "0"], input_bytes=fixture,
                          timeout=min(timeout_seconds, MAX_PROBE_SECONDS), log_output=False,
                          redact_output=True)
    except subprocess.CalledProcessError as error:
        row = wceb.score_missing_aae_case(case)
        state = "FAILED_EXIT"
        exit_code = error.returncode
        payload = error.stdout if isinstance(error.stdout, bytes) else b""
        payload_bytes = len(payload)
        payload_digest = hashlib.sha256(payload).hexdigest() if payload else None
    except (TimeoutError, RuntimeError) as error:
        message = str(error).lower().replace("_", " ")
        fatal_storage = any(marker in message for marker in
                            ("disk reserve", "metadata capacity", "storage guard", "collection capacity"))
        if time.monotonic() >= run.deadline or fatal_storage:
            raise
        row = wceb.score_missing_aae_case(case)
        state = ("FAILED_TIMEOUT" if "timed out" in message else
                 "FAILED_OUTPUT_LIMIT" if "output_limit" in message else "FAILED_RUNNER")
        exit_code = None
        payload_bytes = 0
        payload_digest = None
    else:
        payload_bytes = len(payload)
        payload_digest = hashlib.sha256(payload).hexdigest()
        try:
            row = wceb.score_aae_case(case, payload)
            state, exit_code = "SCORED", 0
        except ValueError:
            row = wceb.score_missing_aae_case(case)
            state, exit_code = "INVALID_AAE", 0
    command = run.manifest.get("commands", [])[-1] if run.manifest.get("commands") else {}
    row.update(base, candidate_id=binary["id"], core_execution_state=state,
               output_sha256=payload_digest, output_bytes=payload_bytes,
               output_budget_bytes=output_budget, exit_code=exit_code,
               command_wall_ns=command.get("wall_ns", time.monotonic_ns() - started),
               sampled_process_tree_rss_bytes=command.get("sampled_process_tree_rss_bytes"))
    return row


def _domain_metric_values(rows: list[dict], metric: str) -> dict[str, float]:
    grouped = defaultdict(list)
    for row in rows:
        value = row.get(metric)
        if row.get("domain") is not None and value is not None:
            grouped[row["domain"]].append(float(value))
    return {domain: math.fsum(values) / len(values) for domain, values in grouped.items() if values}


def paired_cluster_bootstrap(baseline_rows: list[dict], candidate_rows: list[dict], *,
                             metric: str, comparison_count: int, seed: int,
                             draws: int = BOOTSTRAP_DRAWS, deadline: float | None = None) -> dict:
    if metric not in PRIMARY_METRICS or type(comparison_count) is not int or comparison_count < 1:
        raise ValueError("Invalid paired cluster comparison design")
    if type(draws) is not int or draws < 30_000 or draws > 200_000:
        raise ValueError("Bootstrap draws must be between 30,000 and 200,000")
    baseline = _domain_metric_values(baseline_rows, metric)
    candidate = _domain_metric_values(candidate_rows, metric)
    domains = sorted(set(baseline) & set(candidate))
    if not domains:
        return {"metric": metric, "status": "NO_PAIRED_DOMAINS", "domain_count": 0,
                "pair_count": 0, "mean_domain_delta": None, "simultaneous_ci": None,
                "confidence_family_size": comparison_count, "bootstrap_draws": draws, "seed": seed}
    deltas = [candidate[domain] - baseline[domain] for domain in domains]
    estimate = math.fsum(deltas) / len(deltas)
    rng = random.Random(seed)
    bootstrap = []
    n = len(deltas)
    for draw in range(draws):
        if draw % 1024 == 0 and deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("WCEB bootstrap exceeded its study wall-clock budget")
        bootstrap.append(math.fsum(deltas[rng.randrange(n)] for _ in range(n)) / n)
    bootstrap.sort()
    tail = 0.05 / (2 * comparison_count)
    low_index = max(0, min(draws - 1, math.floor(tail * (draws - 1))))
    high_index = max(0, min(draws - 1, math.ceil((1 - tail) * (draws - 1))))
    return {"metric": metric, "status": "ESTIMATED" if n >= 30 else "TOO_FEW_DOMAINS",
            "domain_count": n,
            "pair_count": sum(1 for row in candidate_rows
                              if row.get("domain") in domains and row.get(metric) is not None),
            "mean_domain_delta": estimate,
            "simultaneous_ci": [bootstrap[low_index], bootstrap[high_index]],
            "confidence_level_familywise": 0.95,
            "confidence_family_size": comparison_count, "bootstrap_draws": draws, "seed": seed,
            "higher_is_better": metric != "without_rate",
            "estimand": "unweighted mean of paired per-domain metric differences"}


def _summary_report(dataset: wceb.WcebDataset, split: str, rows: list[dict], output_bytes: int) -> dict:
    aggregate_rows = []
    for row in rows:
        aggregate_rows.append({key: row.get(key) for key in (
            "split", "file_id", "domain", "page_type", "source_url_valid", "annotation_sha256",
            "html_sha256", "aae_source_identity_verified", "reference_is_null", "reference_is_missing",
            "precision", "recall", "f1", "official_compat_precision", "official_compat_recall",
            "official_compat_f1", "with_rate", "without_rate", "prediction_missing",
            "domain_seen_in_dev", "html_seen_in_dev", "strict_nonoverlap_diagnostic")})
    return wceb._score_report(dataset, split, aggregate_rows, [], output_bytes)


def _freeze_payload(dev_report: dict, candidate_ids: list[str]) -> dict:
    if dev_report.get("schema") != SCHEMA or dev_report.get("split") != "dev":
        raise ValueError("Only a completed dev WCEB report can freeze candidates")
    binaries = dev_report.get("binaries")
    if (not isinstance(binaries, list) or not binaries or not isinstance(binaries[0], dict)
            or binaries[0].get("id") != "baseline"):
        raise ValueError("Dev report is missing the baseline binary")
    for binary in binaries:
        if (not isinstance(binary, dict) or set(binary) != {"id", "sha256", "bytes"}
                or not isinstance(binary.get("id"), str) or not _IDENTIFIER.fullmatch(binary["id"])
                or not isinstance(binary.get("sha256"), str) or not _SHA256.fullmatch(binary["sha256"])
                or type(binary.get("bytes")) is not int or not 1 <= binary["bytes"] <= 256 * 1024 * 1024):
            raise ValueError("Dev report contains malformed native binary identity")
    dataset = dev_report.get("dataset")
    score_reports = dev_report.get("score_reports")
    collection = dev_report.get("collection")
    binary_ids = {binary["id"] for binary in binaries}
    page_count = dataset.get("page_count") if isinstance(dataset, dict) else None
    if (not isinstance(dataset, dict) or dataset.get("commit") != wceb.COMMIT
            or dataset.get("root_tree_sha") != wceb.ROOT_TREE_SHA
            or type(page_count) is not int or page_count < 1
            or not isinstance(score_reports, dict) or set(score_reports) != binary_ids
            or any(not isinstance(score_reports[identifier], dict)
                   or score_reports[identifier].get("page_count") != page_count
                   for identifier in binary_ids)
            or not isinstance(collection, dict) or collection.get("rows_per_binary") != page_count):
        raise ValueError("Dev report is incomplete or does not cover the pinned WCEB split")
    if not isinstance(candidate_ids, list) or not 1 <= len(candidate_ids) <= 2:
        raise ValueError("Freeze one or two dev candidates for the public test pass")
    if len(set(candidate_ids)) != len(candidate_ids) or "baseline" in candidate_ids:
        raise ValueError("Frozen candidate IDs must be distinct from baseline")
    indexed = {binary["id"]: binary for binary in binaries}
    if any(identifier not in indexed for identifier in ["baseline", *candidate_ids]):
        raise ValueError("Freeze names a binary not present in the dev report")
    if dev_report.get("decision_sufficiency") != "REVIEW_REQUIRED":
        raise ValueError("Dev report is not eligible for a manual freeze")
    runtime = dev_report.get("runtime")
    if not isinstance(runtime, dict) or runtime != _runtime_identity():
        raise ValueError("Dev report runtime does not match the current machine")
    source_identity = dev_report.get("source_identity")
    if not isinstance(source_identity, dict) or source_identity != _source_identity():
        raise ValueError("Dev report runner source has changed")
    return {"schema": FREEZE_SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
            "dev_report_sha256": hashlib.sha256(_canonical(dev_report)).hexdigest(),
            "dataset_commit": wceb.COMMIT, "dataset_root_tree_sha": wceb.ROOT_TREE_SHA,
            "baseline": indexed["baseline"],
            "candidates": [indexed[identifier] for identifier in candidate_ids],
            "runtime": runtime,
            "source_identity": source_identity,
            "decision": "OPERATOR_SELECTED_AFTER_DEV_REVIEW",
            "public_test_is_independent_secret_holdout": False}


def _read_json(path: Path, max_bytes: int) -> dict:
    if type(max_bytes) is not int or max_bytes < 1 or path.is_symlink() or not path.is_file():
        raise ValueError("Study input file is missing, linked, or over its byte limit")
    chunks, size = [], 0
    with path.open("rb") as stream:
        while size <= max_bytes:
            chunk = stream.read(min(65536, max_bytes - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    if size > max_bytes:
        raise ValueError("Study input file is missing, linked, or over its byte limit")
    raw = (chunks[0] if chunks else b"") if len(chunks) <= 1 else b"".join(chunks)
    value = protocol.strict_json_loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Study input must be a JSON object")
    return value


def freeze_candidates(dev_report_path: str, output_path: str, candidate_ids: list[str]) -> Path:
    source = Path(dev_report_path)
    report = _read_json(source, MAX_REPORT_BYTES)
    frozen = _freeze_payload(report, candidate_ids)
    output = Path(output_path)
    if output.is_symlink():
        raise ValueError("Freeze output cannot be linked")
    parent = output.parent.resolve(strict=True)
    destination = parent / output.name
    if destination.exists():
        raise FileExistsError("Candidate freeze is immutable and already exists")
    data = json.dumps(frozen, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(data) > 1024 * 1024:
        raise ValueError("Candidate freeze exceeds its byte limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        campaign.sync_directory(parent)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _validate_test_freeze(freeze_path: str, dev_report_path: str, binaries: list[dict]) -> dict:
    freeze = _read_json(Path(freeze_path), 1024 * 1024)
    dev_path = Path(dev_report_path)
    dev_report = _read_json(dev_path, MAX_REPORT_BYTES)
    if (freeze.get("schema") != FREEZE_SCHEMA
            or freeze.get("dev_report_sha256") != hashlib.sha256(_canonical(dev_report)).hexdigest()
            or dev_report.get("schema") != SCHEMA or dev_report.get("split") != "dev"
            or freeze.get("dataset_commit") != wceb.COMMIT
            or freeze.get("dataset_root_tree_sha") != wceb.ROOT_TREE_SHA
            or freeze.get("runtime") != dev_report.get("runtime")
            or freeze.get("runtime") != _runtime_identity()
            or freeze.get("source_identity") != dev_report.get("source_identity")
            or freeze.get("source_identity") != _source_identity()
            or freeze.get("public_test_is_independent_secret_holdout") is not False):
        raise ValueError("Dev freeze does not bind to the supplied dev report and pinned WCEB tree")
    expected = [freeze.get("baseline"), *(freeze.get("candidates") or [])]
    if len(expected) != len(binaries) or {item.get("id"): item.get("sha256") for item in expected} != {
            item["id"]: item["sha256"] for item in binaries}:
        raise ValueError("Public test binaries differ from the immutable dev freeze")
    return freeze


def run_study(dataset_root: str, output_directory: str, *, split: str, baseline: str,
              candidates: list[str], wall_seconds: int, probe_timeout_seconds: int,
              resume: bool, freeze_from: str | None, dev_report: str | None) -> Path:
    if split not in ("dev", "test"):
        raise ValueError("WCEB split must be dev or test")
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= MAX_WALL_SECONDS:
        raise ValueError("WCEB run wall time must be from 1 to 18,000 seconds")
    if type(probe_timeout_seconds) is not int or not 1 <= probe_timeout_seconds <= MAX_PROBE_SECONDS:
        raise ValueError("Per-page probe timeout must be from 1 to 60 seconds")
    if split == "dev" and (freeze_from is not None or dev_report is not None):
        raise ValueError("Dev pass cannot consume a candidate freeze")
    if split == "test" and (not freeze_from or not dev_report):
        raise ValueError("Public test pass requires the immutable dev freeze and its source report")

    raw_dataset_path = Path(dataset_root)
    if raw_dataset_path.is_symlink():
        raise ValueError("Dataset root cannot be a symbolic link")
    dataset_path = raw_dataset_path.resolve(strict=True)
    binaries = _binary_arguments(baseline, candidates, split)
    limits = wceb.Limits(wall_seconds=min(wall_seconds, MAX_DATASET_SCAN_SECONDS))
    scan_started = time.monotonic()
    dataset = wceb.load_wceb(dataset_path, limits)
    scan_seconds = time.monotonic() - scan_started
    cases = dataset.cases_for(split)
    if not cases:
        raise ValueError("Pinned WCEB split contains no pages")
    freeze = None
    if split == "test":
        freeze = _validate_test_freeze(freeze_from, dev_report, binaries)

    destination = Path(output_directory)
    if destination.is_symlink():
        raise ValueError("Study output cannot be a link")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if not resume and destination.exists():
        raise FileExistsError("Study output already exists; choose a new root or pass --resume")
    output_free_bytes = shutil.disk_usage(parent).free
    if output_free_bytes < 1024 * 1024 * 1024:
        raise RuntimeError("Less than 1 GiB remains on the output volume")

    spec = {"schema": SCHEMA, "split": split, "dataset": {"repository": wceb.REPOSITORY,
            "tag": wceb.TAG, "commit": wceb.COMMIT, "root_tree_sha": wceb.ROOT_TREE_SHA,
            "metadata_sha256": dataset.metadata_sha256, "page_count": len(cases)},
            "runtime": _runtime_identity(),
            "source_identity": _source_identity(),
            "binaries": [{key: item[key] for key in ("id", "sha256", "bytes")} for item in binaries],
            "limits": {"wall_seconds": wall_seconds, "per_page_timeout_seconds": probe_timeout_seconds,
                       "native_html_bytes": CORE_HTML_BYTES, "native_fixture_json_bytes": CORE_FIXTURE_BYTES,
                       "aae_item_bytes": wceb.MAX_AAE_OUTPUT_BYTES,
                       "aae_total_bytes_per_binary": wceb.Limits().prediction_total_bytes,
                       "command_output_bytes": wceb.MAX_AAE_OUTPUT_BYTES,
                        "metadata_bytes": MAX_COMMAND_METADATA_BYTES,
                        "log_bytes": MAX_COMMAND_METADATA_BYTES,
                       "collection_bytes": MAX_COLLECTION_BYTES,
                       "collection_block_bytes": 4 * 1024 * 1024,
                       "collection_block_cases": 16,
                       "collection_metadata_bytes": MAX_COLLECTION_METADATA_BYTES,
                       "report_bytes": MAX_REPORT_BYTES,
                       "disk_reserve_bytes": 1024 * 1024 * 1024},
            "freeze_sha256": hashlib.sha256(_canonical(freeze)).hexdigest() if freeze else None}
    bundle_id = hashlib.sha256(_canonical({"spec": spec, "source": "wceb_runner/1"})).hexdigest()
    run = lifecycle.ManagedRun(destination, "wceb-" + split, bundle_id, spec,
                               resume=resume, isolate_children=(os.name == "posix"))
    failed = None
    try:
        run.bind_deadline(wall_seconds)
        run.guard_storage()
        rows_by_binary = {}
        output_bytes_by_binary = {}
        journal = lifecycle.CollectionJournal(
            run.directory, {"study": bundle_id, "dataset_tree": wceb.ROOT_TREE_SHA,
                             "split": split, "limits": spec["limits"]}, MAX_COLLECTION_BYTES,
            run.session_id, metadata_limit=MAX_COLLECTION_METADATA_BYTES,
            block_limit=4 * 1024 * 1024)
        block_size = spec["limits"]["collection_block_cases"]
        for binary in binaries:
            all_rows, candidate_output_bytes = [], 0
            observed_binary_hash, _ = _sha256_file(Path(binary["path"]), 256 * 1024 * 1024)
            if observed_binary_hash != binary["sha256"]:
                raise ValueError("Native probe changed after study admission")
            binary_binding = {"binary_sha256": binary["sha256"], "split": split,
                              "dataset_tree_sha": wceb.ROOT_TREE_SHA,
                              "fixture": "html+url, UTF-8 JSON, native probe parse/0",
                              "limits": spec["limits"]}
            block_count = (len(cases) + block_size - 1) // block_size
            for block_index in range(block_count):
                start = block_index * block_size
                block_cases = cases[start:start + block_size]
                descriptor = {"channel": "wceb:" + split + ":" + binary["id"],
                              "binding": binary_binding,
                              "block": {"index": block_index, "start": start,
                                        "end": start + len(block_cases),
                                        "case_keys_sha256": hashlib.sha256(_canonical(
                                            [list(case.key) for case in block_cases])).hexdigest()}}

                def produce():
                    nonlocal candidate_output_bytes
                    for case in block_cases:
                        if candidate_output_bytes >= wceb.Limits().prediction_total_bytes:
                            row = wceb.score_missing_aae_case(case)
                            row.update(_case_identity(case), candidate_id=binary["id"],
                                       core_execution_state="NOT_ADMITTED_TOTAL_OUTPUT_CAP",
                                       output_sha256=None, output_bytes=0,
                                       output_budget_bytes=wceb.Limits().prediction_total_bytes,
                                       command_wall_ns=None, sampled_process_tree_rss_bytes=None)
                        else:
                            row = _execute_case(run, binary, dataset, case,
                                                wceb.Limits().prediction_total_bytes,
                                                probe_timeout_seconds)
                            attempted_bytes = row["output_bytes"]
                            if candidate_output_bytes + attempted_bytes > wceb.Limits().prediction_total_bytes:
                                missing = wceb.score_missing_aae_case(case)
                                missing.update({key: value for key, value in row.items()
                                                if key not in missing})
                                missing.update(precision=0.0 if case.main_content_state == "text" else None,
                                               recall=0.0 if case.main_content_state == "text" else None,
                                               f1=0.0 if case.main_content_state == "text" else None,
                                               with_rate=0.0, without_rate=0.0,
                                               prediction_missing=True,
                                               core_execution_state="NOT_SCORED_TOTAL_OUTPUT_CAP")
                                row = missing
                            candidate_output_bytes += attempted_bytes
                        yield row

                all_rows.extend(journal.collect(descriptor, produce))
                candidate_output_bytes = sum(row["output_bytes"] for row in all_rows)
                run.manifest["wceb_progress"] = {"binary_id": binary["id"],
                    "completed_blocks": block_index + 1, "total_blocks": block_count,
                    "completed_pages": min(start + len(block_cases), len(cases)),
                    "total_pages": len(cases)}
                run.save()
            rows_by_binary[binary["id"]] = all_rows
            output_bytes_by_binary[binary["id"]] = sum(row["output_bytes"] for row in all_rows)
            observed_binary_hash, _ = _sha256_file(Path(binary["path"]), 256 * 1024 * 1024)
            if observed_binary_hash != binary["sha256"]:
                raise ValueError("Native probe changed during its WCEB pass")

        aggregate = {identifier: _summary_report(dataset, split, rows, output_bytes_by_binary[identifier])
                     for identifier, rows in rows_by_binary.items()}
        comparisons = []
        baseline_rows = rows_by_binary["baseline"]
        comparison_count = (len(binaries) - 1) * len(PRIMARY_METRICS)
        for binary in binaries[1:]:
            candidate_rows = rows_by_binary[binary["id"]]
            baseline_by_key = {row["file_id"]: row for row in baseline_rows}
            candidate_by_key = {row["file_id"]: row for row in candidate_rows}
            if set(baseline_by_key) != set(candidate_by_key):
                raise ValueError("Candidate and baseline do not have identical WCEB case keys")
            if any(baseline_by_key[key]["html_sha256"] != candidate_by_key[key]["html_sha256"]
                   or baseline_by_key[key]["annotation_sha256"] != candidate_by_key[key]["annotation_sha256"]
                   for key in baseline_by_key):
                raise ValueError("Paired candidate case contents differ")
            metrics = []
            for metric_index, metric in enumerate(PRIMARY_METRICS):
                seed_bytes = hashlib.sha256(_canonical({"dataset": wceb.ROOT_TREE_SHA,
                    "split": split, "baseline": next(x["sha256"] for x in binaries if x["id"] == "baseline"),
                    "candidate": binary["sha256"], "metric": metric,
                    "draws": BOOTSTRAP_DRAWS})).digest()[:8]
                metrics.append(paired_cluster_bootstrap(
                    baseline_rows, candidate_rows, metric=metric,
                    comparison_count=max(1, comparison_count), seed=int.from_bytes(seed_bytes, "big"),
                    deadline=run.deadline))
            comparisons.append({"candidate_id": binary["id"], "metrics": metrics})

        report = {"schema": SCHEMA, "study_id": bundle_id, "split": split,
                  "split_is_public": True, "independent_secret_holdout": False,
                  "decision_sufficiency": "REVIEW_REQUIRED",
                  "dataset": spec["dataset"], "dataset_integrity_scan_seconds": scan_seconds,
                  "runtime": spec["runtime"], "source_identity": spec["source_identity"],
                  "binaries": spec["binaries"], "resource_limits": spec["limits"],
                  "freeze_sha256": spec["freeze_sha256"],
                  "runner": {"python": sys.version.split()[0], "platform": sys.platform,
                             "process_isolation": spec["runtime"]["process_isolation"],
                             "content_transfer": "NOT_PERFORMED",
                             "third_party_page_content_rights": "NOT_VERIFIED",
                             "output_volume_free_bytes_at_start": output_free_bytes,
                             "results_authenticity": "NOT_VERIFIED: runner and collector share the measured host OS authority",
                             "execution_scope": "offline WCEB text extraction quality; no ranking, search, proxy, concurrency, agent, or cloud service evaluation"},
                  "score_reports": {identifier: {key: value for key, value in score.items() if key != "rows"}
                                    for identifier, score in aggregate.items()},
                  "comparisons": comparisons,
                  "collection": {"path": "collection/", "rows_per_binary": len(cases),
                                 "block_size_cases": block_size,
                                 "sha256_receipts": [journal.commits[key]["sha256"]
                                                      for key in sorted(journal.commits)]},
                  "limitations": ["Three WCEB pages larger than the core probe's 4 MiB HTML admission are counted as missing predictions.",
                                  "WCEB test split is public and is not a secret holdout.",
                                  "Third-party page-content rights for cloud transfer require a separate documented review.",
                                  "Timing and RSS are subprocess-level diagnostics, not isolated parser measurements."]}
        campaign.write_json(run.directory / "report.json", report, max_bytes=MAX_REPORT_BYTES)
        run.manifest["wceb_report_sha256"] = hashlib.sha256((run.directory / "report.json").read_bytes()).hexdigest()
        run.finish()
        return run.directory / "report.json"
    except BaseException as error:
        failed = f"{type(error).__name__}: {error}"
        raise
    finally:
        if failed is not None:
            run.finish(error=failed)
        run.sample_stream.close()


def _parse_candidate(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise argparse.ArgumentTypeError("Candidate IDs must be short alphanumeric labels")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run one pinned public WCEB split against Rust AAE probes")
    run_parser.add_argument("--dataset", required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--split", choices=("dev", "test"), required=True)
    run_parser.add_argument("--baseline", required=True)
    run_parser.add_argument("--candidate", action="append", default=[])
    run_parser.add_argument("--wall-seconds", type=int, required=True)
    run_parser.add_argument("--probe-timeout-seconds", type=int, default=30)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--freeze-from")
    run_parser.add_argument("--dev-report")
    freeze_parser = subparsers.add_parser("freeze", help="Record a human-reviewed dev candidate choice")
    freeze_parser.add_argument("--dev-report", required=True)
    freeze_parser.add_argument("--out", required=True)
    freeze_parser.add_argument("--candidate", action="append", required=True, type=_parse_candidate)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_candidates(args.dev_report, args.out, args.candidate)
        else:
            result = run_study(args.dataset, args.out, split=args.split, baseline=args.baseline,
                               candidates=args.candidate, wall_seconds=args.wall_seconds,
                               probe_timeout_seconds=args.probe_timeout_seconds, resume=args.resume,
                               freeze_from=args.freeze_from, dev_report=args.dev_report)
        print(str(result))
        return 0
    except Exception as error:
        print(f"WCEB study failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
