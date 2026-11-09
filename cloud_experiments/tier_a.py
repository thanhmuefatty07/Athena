"""Tier-A baselines: Athena on WCXB v1.0, WebMainBench-545 and Natural Questions dev.

All inputs are local, verified files (``data_fetch.py`` receipts); nothing is
downloaded here. Every page runs through the native ``core_probe pipeline``
stage (parse + AAE/1 export, links off) in a child process; on macOS the child
is wrapped in ``sandbox-exec`` with outbound network denied, on Linux in the
repository's namespace isolation. Release policy: internal-only (owner Q1).

Reported for each benchmark: n per type, point estimate, and a 95% interval
(percentile bootstrap; clustered by domain for WCXB; Wilson for proportions).

Honest-measurement rules applied:
- WCXB: the public test split is not domain-disjoint (154 shared domains); test
  results are reported for the seen-domain and unseen-domain subsets separately,
  null/missing references are excluded from F1 (the official evaluator scores
  empty-vs-empty as 1.0), and policy choice must be made on dev only. The GT was
  revised against incumbent extractors (README step 4) - disclosed, not corrected.
- WebMainBench: ``cc-select`` and ``mark-selected`` label channels are stripped
  (``leakage.strip_cc_select``) and audited per page before Athena sees the HTML;
  GT is Markdown, so the chunks->Markdown renderer here is part of the measured
  system; the scorer is deterministic (no LLM).
- NQ: gold long answers are HTML byte spans; Athena chunks carry no offsets, so
  matching is by normalized token overlap (criterion recorded in the report).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import html as html_lib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time

from cloud_experiments import leakage, wceb

CORE_HTML_BYTES = 4 * 1024 * 1024
FIXTURE_JSON_BYTES = 8 * 1024 * 1024
POLICIES = {"full_dom": None, "main_content_v1": "main_content_v1", "cetd_entropy_v1": "cetd_entropy_v1"}
_SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"
_WORD = re.compile(r"\w+")


# ----------------------------------------------------------------------------- native execution

def isolation_mode() -> str:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return "macos-sandbox-exec-deny-network"
    if sys.platform.startswith("linux"):
        return "linux-namespaces"
    return "NONE"


def core_argv(binary: str) -> list[str]:
    argv = [binary, "pipeline", "-", "0"]
    mode = isolation_mode()
    if mode.startswith("macos"):
        return ["/usr/bin/sandbox-exec", "-p", _SANDBOX_PROFILE, *argv]
    if mode == "linux-namespaces":
        from cloud_experiments import process_isolation
        return process_isolation.command_argv(argv)
    return argv


def run_pipeline(binary: str, fixture: dict, timeout: float = 60.0) -> tuple[str, bytes, str]:
    """Return (state, stdout, error) for one native pipeline execution."""
    payload = json.dumps(fixture, ensure_ascii=False).encode("utf-8")
    if len(payload) > FIXTURE_JSON_BYTES:
        return "NOT_ADMITTED_FIXTURE_CAP", b"", ""
    try:
        proc = subprocess.run(core_argv(binary), input=payload, capture_output=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        return "FAILED_TIMEOUT", b"", "timeout"
    if proc.returncode != 0:
        return "FAILED_EXIT", b"", proc.stderr[-300:].decode("utf-8", "replace")
    return "OK", proc.stdout, ""


def aae_blocks(payload: bytes) -> tuple[list[dict], str | None]:
    """Parse AAE/1 frames (structure validated by wceb/protocol rules) into blocks."""
    from kaggle_experiments.protocol import validate_aae
    frames = validate_aae(payload, expected_source_url=None)
    return [frame for frame in frames if frame.get("type") == "block"], frames[0].get("source_url")


def fixture_for(html: str, url: str, policy: str, **extra) -> dict:
    fixture = {"html": html, "url": url, "link_mode": "none", **extra}
    if POLICIES[policy]:
        fixture["boilerplate"] = POLICIES[policy]
    return fixture


# ----------------------------------------------------------------------------- statistics

def mean(values):
    values = [v for v in values if v is not None]
    return math.fsum(values) / len(values) if values else None


def bootstrap_ci(values, clusters=None, *, draws: int = 2000, seed: int = 20261109, level: float = 0.95):
    """Percentile bootstrap CI of the mean; resamples whole clusters when given."""
    pairs = [(v, c) for v, c in zip(values, clusters or range(len(values))) if v is not None]
    if len(pairs) < 2:
        return None
    groups = defaultdict(list)
    for value, cluster in pairs:
        groups[cluster].append(value)
    keys = sorted(groups, key=str)
    sums = [math.fsum(groups[k]) for k in keys]
    counts = [len(groups[k]) for k in keys]
    rng = random.Random(seed)
    estimates = []
    n = len(keys)
    for _ in range(draws):
        total = count = 0.0
        for _ in range(n):
            index = rng.randrange(n)
            total += sums[index]
            count += counts[index]
        estimates.append(total / count)
    estimates.sort()
    tail = (1 - level) / 2
    return [round(estimates[int(tail * (draws - 1))], 6), round(estimates[int(math.ceil((1 - tail) * (draws - 1)))], 6)]


def wilson(successes: int, n: int, z: float = 1.959964):
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(centre - half, 6), round(centre + half, 6)]


def summarize(rows, metric: str, *, cluster_key: str | None = None, by: str | None = None) -> dict:
    def block(subset):
        values = [row.get(metric) for row in subset]
        clusters = [row.get(cluster_key) for row in subset] if cluster_key else None
        scored = [v for v in values if v is not None]
        return {"n": len(scored), "mean": None if not scored else round(mean(scored), 6),
                "ci95": bootstrap_ci(values, clusters),
                "clusters": len({row.get(cluster_key) for row in subset if row.get(metric) is not None})
                if cluster_key else None}

    result = {"all": block(rows)}
    if by:
        groups = defaultdict(list)
        for row in rows:
            for key in (row.get(by) if isinstance(row.get(by), list) else [row.get(by)]):
                groups[str(key)].append(row)
        result["by_" + by] = {key: block(group) for key, group in sorted(groups.items())}
    return result


def word_prf(predicted: str, reference: str):
    return wceb._word_metrics(predicted, reference)


def write_rows(path: Path, rows: list[dict]):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def binary_identity(binary: str) -> dict:
    data = Path(binary).read_bytes()
    return {"path": str(binary), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def map_ordered(function, items, workers: int):
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        yield from pool.map(function, items)


# ----------------------------------------------------------------------------- WCXB

def run_wcxb(dataset_root: str, binary: str, out: Path, *, policies, workers: int) -> dict:
    dataset = wceb.load_wceb(dataset_root)
    dev_domains = {case.domain for case in dataset.cases_for("dev") if case.domain}
    dev_html = {case.html_sha256 for case in dataset.cases_for("dev")}
    report = {"benchmark": "WCXB v1.0", "commit": wceb.COMMIT, "metadata_sha256": dataset.metadata_sha256,
              "leakage": {k: v for k, v in dataset.leakage.items() if not isinstance(v, list)},
              "policies": {}}
    for policy in policies:
        policy_report = {}
        for split in ("dev", "test"):
            cases = dataset.cases_for(split)

            def execute(case):
                base = {"split": split, "file_id": case.file_id, "domain": case.domain,
                        "page_type": case.page_type, "reference_state": case.main_content_state}
                if case.html_bytes > CORE_HTML_BYTES:
                    row = wceb.score_missing_aae_case(case)
                    row.update(base, state="NOT_ADMITTED_HTML_CAP")
                    return row
                html = wceb.load_html(dataset, case)
                url = case.source_url or "https://wceb.invalid/"
                state, payload, error = run_pipeline(binary, fixture_for(html, url, policy))
                if state != "OK":
                    row = wceb.score_missing_aae_case(case)
                    row.update(base, state=state, error=error)
                    return row
                try:
                    row = wceb.score_aae_case(case, payload)
                    row.update(base, state="SCORED", url_identity="exact")
                except ValueError:
                    # Url::parse may canonicalize the URL; score text, record the identity miss.
                    try:
                        text = wceb.aae_prediction_text(payload, None)
                        row = wceb._score_case(case, text, False, False)
                        row.update(base, state="SCORED", url_identity="canonicalized")
                    except ValueError as error:
                        row = wceb.score_missing_aae_case(case)
                        row.update(base, state="INVALID_AAE", error=str(error)[:200])
                return row

            rows = list(map_ordered(execute, cases, workers))
            for row in rows:
                if split == "test":
                    row["domain_seen_in_dev"] = None if row["domain"] is None else row["domain"] in dev_domains
                    row["unseen_domain"] = (row["domain"] is not None and row["domain"] not in dev_domains
                                            and row["html_sha256"] not in dev_html)
            write_rows(out / f"wcxb-{policy}-{split}.rows.jsonl.gz", rows)
            states = defaultdict(int)
            for row in rows:
                states[row["state"]] += 1
            entry = {"pages": len(rows), "states": dict(states),
                     "null_or_missing_reference_excluded": sum(r["f1"] is None for r in rows),
                     "f1": summarize(rows, "f1", cluster_key="domain", by="page_type"),
                     "precision": summarize(rows, "precision", cluster_key="domain"),
                     "recall": summarize(rows, "recall", cluster_key="domain"),
                     "with_rate": summarize(rows, "with_rate", cluster_key="domain"),
                     "without_rate": summarize(rows, "without_rate", cluster_key="domain")}
            if split == "test":
                seen = [r for r in rows if r.get("domain_seen_in_dev")]
                unseen = [r for r in rows if r.get("unseen_domain")]
                entry["seen_domain_subset"] = {"pages": len(seen),
                                               "f1": summarize(seen, "f1", cluster_key="domain", by="page_type")}
                entry["unseen_domain_subset"] = {"pages": len(unseen),
                                                 "f1": summarize(unseen, "f1", cluster_key="domain",
                                                                 by="page_type")}
            policy_report[split] = entry
        report["policies"][policy] = policy_report
    return report


# ----------------------------------------------------------------------------- WebMainBench

_MD_SYNTAX = re.compile(r"(^|\n)\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)|```[^\n]*|\|")


def render_markdown(blocks: list[dict]) -> str:
    """Athena chunks -> Markdown (part of the measured system)."""
    parts = []
    for block in blocks:
        text = block["text"].strip()
        if not text:
            continue
        role = block.get("role")
        if role == "heading":
            parts.append("## " + text)
        elif role == "list_item":
            parts.append("- " + text)
        elif role == "preformatted":
            parts.append("```\n" + text + "\n```")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def markdown_text(markdown: str) -> str:
    return _MD_SYNTAX.sub(" ", markdown or "")


_TEX_SPAN = re.compile(r"\$\$(.+?)\$\$|\$(?!\s)([^$\n]+?)\$", re.S)
_CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def formula_scores(predicted_md: str, gold_md: str):
    gold = [a or b for a, b in _TEX_SPAN.findall(gold_md or "")]
    if not gold:
        return None, None, 0
    predicted = [a or b for a, b in _TEX_SPAN.findall(predicted_md or "")]
    best = []
    for formula in gold:
        best.append(max((leakage.tex_similarity(p, formula) for p in predicted), default=0.0))
    return mean(best), sum(score >= 0.9 for score in best) / len(best), len(gold)


def code_score(predicted_md: str, gold_md: str):
    gold = "\n".join(_CODE_FENCE.findall(gold_md or ""))
    if not gold.strip():
        return None
    predicted = "\n".join(_CODE_FENCE.findall(predicted_md or ""))
    return word_prf(predicted, gold)[2]


def wmb_types(meta: dict) -> list[str]:
    types = []
    for key in ("equation", "table", "code"):
        values = meta.get(key) or []
        if values:
            types.append(key)
            types.extend(f"{key}:{value}" for value in sorted(set(values)))
    return types or ["plain"]


def _safe_host(url) -> str | None:
    try:
        return wceb._url_host(url)
    except (ValueError, TypeError):
        return None


def run_webmainbench(path: str, binary: str, out: Path, *, policies, workers: int) -> dict:
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = {"benchmark": "WebMainBench_545", "records": len(records), "policies": {},
              "input_sanitizer": "leakage.strip_cc_select (cc-select attributes + mark-selected class)"}
    for policy in policies:
        def execute(record):
            meta = record.get("meta") or {}
            base = {"track_id": record["track_id"], "types": wmb_types(meta),
                    "level": meta.get("level"), "language": meta.get("language") or "unknown",
                    "domain": _safe_host(record.get("url"))}
            stripped = leakage.strip_cc_select(record["html"] or "")
            audit = leakage.cc_select_attribute_count(stripped.html) + leakage.label_class_count(stripped.html)
            base.update(labels_removed=stripped.attributes_removed + stripped.label_class_tokens_removed,
                        labels_after_strip=audit)
            if audit:
                raise AssertionError(f"label leak survived in {record['track_id']}")
            if len(stripped.html.encode("utf-8")) > CORE_HTML_BYTES:
                return {**base, "state": "NOT_ADMITTED_HTML_CAP", "f1": 0.0, "precision": 0.0, "recall": 0.0}
            url = record["url"] if str(record.get("url", "")).startswith("http") else "https://wmb.invalid/"
            state, payload, error = run_pipeline(binary, fixture_for(stripped.html, url, policy))
            predicted_md = ""
            if state == "OK":
                try:
                    blocks, _ = aae_blocks(payload)
                    predicted_md = render_markdown(blocks)
                except ValueError as err:
                    state, error = "INVALID_AAE", str(err)[:200]
            gold = record.get("groundtruth_content") or ""
            precision, recall, f1 = word_prf(markdown_text(predicted_md), markdown_text(gold))
            formula_mean, formula_hit, formula_n = formula_scores(predicted_md, gold)
            return {**base, "state": state, "error": error, "precision": precision, "recall": recall, "f1": f1,
                    "formula_similarity": formula_mean, "formula_hit_at_0_9": formula_hit,
                    "gold_formulas": formula_n, "code_f1": code_score(predicted_md, gold)}

        rows = list(map_ordered(execute, records, workers))
        write_rows(out / f"webmainbench-{policy}.rows.jsonl.gz", rows)
        states = defaultdict(int)
        for row in rows:
            states[row["state"]] += 1
        report["policies"][policy] = {
            "states": dict(states),
            "labels_removed_total": sum(r["labels_removed"] for r in rows),
            "labels_after_strip_total": sum(r["labels_after_strip"] for r in rows),
            "text_f1": summarize(rows, "f1", cluster_key="domain", by="types"),
            "text_f1_by_level": summarize(rows, "f1", by="level")["by_level"],
            "text_f1_by_language": summarize(rows, "f1", by="language")["by_language"],
            "precision": summarize(rows, "precision", cluster_key="domain"),
            "recall": summarize(rows, "recall", cluster_key="domain"),
            "formula_similarity": summarize(rows, "formula_similarity"),
            "formula_hit_at_0_9": summarize(rows, "formula_hit_at_0_9"),
            "code_f1": summarize(rows, "code_f1"),
            "table_teds": "NOT_MEASURED (needs a table-structure renderer; text F1 by type only)",
        }
    return report


# ----------------------------------------------------------------------------- Natural Questions

def nq_prepare(parquet_dir: str, output: str) -> dict:
    """Parquet -> compact JSONL.gz (needs pyarrow; run once in a venv)."""
    import pyarrow.parquet as pq
    paths = sorted(Path(parquet_dir).glob("validation-*.parquet"))
    written = 0
    with gzip.open(output, "wt", encoding="utf-8") as stream:
        for path in paths:
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=32, columns=[
                    "id", "document.title", "document.url", "document.html", "question.text",
                    "annotations"]):
                for row in batch.to_pylist():
                    spans = []
                    for answer in (row["annotations"] or {}).get("long_answer") or []:
                        if answer and answer.get("start_byte", -1) >= 0 and answer.get("end_byte", -1) > answer["start_byte"]:
                            spans.append([answer["start_byte"], answer["end_byte"]])
                    stream.write(json.dumps({
                        "id": row["id"], "title": row["document"]["title"], "url": row["document"]["url"],
                        "question": row["question"]["text"], "html": row["document"]["html"],
                        "long_answer_spans": spans}, ensure_ascii=False) + "\n")
                    written += 1
    return {"written": written, "files": [p.name for p in paths]}


_TAG = re.compile(r"<[^>]*>")


def span_text(html_bytes: bytes, start: int, end: int) -> str:
    fragment = html_bytes[start:end].decode("utf-8", "replace")
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", fragment))).strip()


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def block_matches_gold(block_text: str, gold_texts: list[str]) -> bool:
    """A block hits when it holds >=50% of a gold answer's tokens, or is >=8 tokens of
    which >=80% belong to that gold answer (Athena may split one answer across chunks)."""
    block = _tokens(block_text)
    if not block:
        return False
    for gold_text in gold_texts:
        gold = _tokens(gold_text)
        if not gold:
            continue
        gold_counts = defaultdict(int)
        for token in gold:
            gold_counts[token] += 1
        overlap = 0
        for token in block:
            if gold_counts[token] > 0:
                gold_counts[token] -= 1
                overlap += 1
        if overlap / len(gold) >= 0.5 or (len(block) >= 8 and overlap / len(block) >= 0.8):
            return True
    return False


def run_nq(compact_path: str, binary: str, out: Path, *, policies, workers: int, k: int = 5,
           limit: int | None = None) -> dict:
    def examples():
        with gzip.open(compact_path, "rt", encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                if limit is not None and index >= limit:
                    break
                yield json.loads(line)

    report = {"benchmark": "Natural Questions v1.0 dev (HF validation, 7,830)", "k": k, "policies": {},
              "match_rule": block_matches_gold.__doc__.strip()}
    for policy in policies:
        def execute(example):
            html = example["html"]
            html_bytes = html.encode("utf-8")
            golds = [span_text(html_bytes, s, e) for s, e in example["long_answer_spans"]]
            golds = [g for g in golds if g]
            base = {"id": example["id"], "has_long_answer": bool(golds), "html_bytes": len(html_bytes)}
            if len(html_bytes) > CORE_HTML_BYTES:
                return {**base, "state": "NOT_ADMITTED_HTML_CAP", "rank": None, "extracted": False}
            url = example["url"] if str(example.get("url", "")).startswith("http") else "https://nq.invalid/"
            state, payload, error = run_pipeline(binary, fixture_for(html, url, policy,
                                                                     query=example["question"],
                                                                     highlight_limit=k))
            if state != "OK":
                return {**base, "state": state, "error": error, "rank": None, "extracted": False}
            try:
                blocks, _ = aae_blocks(payload)
            except ValueError as err:
                return {**base, "state": "INVALID_AAE", "error": str(err)[:200], "rank": None, "extracted": False}
            highlighted = [b for b in blocks if b.get("highlight")][:k]
            rank = next((i + 1 for i, b in enumerate(highlighted) if block_matches_gold(b["text"], golds)), None)
            extracted = any(block_matches_gold(b["text"], golds) for b in blocks) if golds else False
            return {**base, "state": "SCORED", "rank": rank, "extracted": extracted,
                    "highlighted": len(highlighted), "blocks": len(blocks)}

        rows = list(map_ordered(execute, examples(), workers))
        write_rows(out / f"nq-{policy}.rows.jsonl.gz", rows)
        answerable = [r for r in rows if r["has_long_answer"]]
        n = len(answerable)
        states = defaultdict(int)
        for row in rows:
            states[row["state"]] += 1
        hits1 = sum(1 for r in answerable if r["rank"] == 1)
        hitsk = sum(1 for r in answerable if r["rank"] is not None)
        extracted = sum(1 for r in answerable if r["extracted"])
        rr = [1.0 / r["rank"] if r["rank"] else 0.0 for r in answerable]
        report["policies"][policy] = {
            "examples": len(rows), "answerable": n, "no_long_answer": len(rows) - n, "states": dict(states),
            "hit_at_1": {"value": round(hits1 / n, 6) if n else None, "ci95": wilson(hits1, n)},
            f"hit_at_{k}": {"value": round(hitsk / n, 6) if n else None, "ci95": wilson(hitsk, n)},
            f"mrr_at_{k}": {"value": round(mean(rr), 6) if rr else None, "ci95": bootstrap_ci(rr)},
            "answer_extracted_any_block": {"value": round(extracted / n, 6) if n else None,
                                           "ci95": wilson(extracted, n)},
            "conditional_hit_at_1_given_extracted": round(hits1 / extracted, 6) if extracted else None,
        }
    return report


# ----------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("nq-prepare")
    prep.add_argument("--parquet-dir", required=True)
    prep.add_argument("--out", required=True)
    run = sub.add_parser("run")
    run.add_argument("--binary", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--wcxb")
    run.add_argument("--webmainbench")
    run.add_argument("--nq")
    run.add_argument("--nq-limit", type=int)
    run.add_argument("--policies", default="full_dom,main_content_v1,cetd_entropy_v1")
    run.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if args.command == "nq-prepare":
        print(json.dumps(nq_prepare(args.parquet_dir, args.out)))
        return 0
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass
    policies = [p for p in args.policies.split(",") if p]
    unknown = [p for p in policies if p not in POLICIES]
    if unknown:
        parser.error(f"unknown policies {unknown}")
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    report = {"schema": "athena-tier-a-baseline/1", "binary": binary_identity(args.binary),
              "isolation": isolation_mode(), "python": sys.version.split()[0], "platform": sys.platform,
              "release_policy": "internal-only (owner decision Q1); page rights not verified",
              "results": {}}
    if args.wcxb:
        report["results"]["wcxb"] = run_wcxb(args.wcxb, args.binary, out, policies=policies, workers=args.workers)
    if args.webmainbench:
        report["results"]["webmainbench"] = run_webmainbench(args.webmainbench, args.binary, out,
                                                             policies=policies, workers=args.workers)
    if args.nq:
        report["results"]["nq"] = run_nq(args.nq, args.binary, out, policies=policies, workers=args.workers,
                                         limit=args.nq_limit)
    report["elapsed_s"] = round(time.time() - started, 1)
    (out / "tier_a_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(out / "tier_a_report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
