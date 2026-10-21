"""Expanded research protocol; old native/1 evidence retains its original owner."""

import argparse
from contextlib import contextmanager
import copy
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import heapq
import itertools
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import time

from cloud_experiments import campaign, experimental_design, lifecycle
from kaggle_experiments import protocol, runner

PROTOCOL = "athena-core-research/2"
ROOT = Path(__file__).resolve().parent.parent
AUTHENTICITY = "NOT VERIFIED: runner and evidence share OS authority"


class ResearchRun(lifecycle.ManagedRun):
    pass


@contextmanager
def _analysis_lock(path):
    if path.is_symlink():
        raise ValueError("Analysis lock cannot be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Analysis already active for this output") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class EvidenceStream:
    def __init__(self, folder, total_limit, shard_limit=128 * 1024 ** 2):
        if type(total_limit) is not int or type(shard_limit) is not int or not 0 < shard_limit <= total_limit:
            raise ValueError("Invalid evidence byte bounds")
        self.folder, self.total_limit, self.shard_limit = Path(folder), total_limit, shard_limit
        self.total_bytes, self.shard_bytes, self.paths, self.stream = 0, 0, [], None
        self.closed = False

    def write(self, value):
        if self.closed:
            raise ValueError("Evidence stream is closed")
        data = value.encode("utf-8")
        if len(data) > self.shard_limit or len(data) > self.total_limit - self.total_bytes:
            raise ValueError("Evidence byte bound exceeded before write")
        if self.stream is None or self.shard_bytes + len(data) > self.shard_limit:
            self._close_shard()
            path = self.folder / ("samples-" + str(len(self.paths)).zfill(4) + ".jsonl")
            self.stream = path.open("xb")
            self.paths.append(path)
            self.shard_bytes = 0
        self.stream.write(data)
        self.total_bytes += len(data)
        self.shard_bytes += len(data)
        return len(value)

    def flush(self):
        if self.stream is not None:
            self.stream.flush()

    def _close_shard(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def close(self):
        self._close_shard()
        self.closed = True

    def __enter__(self):
        if self.closed:
            raise ValueError("Evidence stream is closed")
        return self

    def __exit__(self, *_):
        self.close()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def shortlist(records, required_cases, slots_per_owner=1):
    if type(slots_per_owner) is not int or slots_per_owner < 1 or not required_cases:
        raise ValueError("Selection requires explicit owner slots and cases")
    selected, excluded, rankings = [], {}, {}
    for name, record in records.items():
        if record.get("correct") is not True:
            excluded[name] = "ORACLE_REJECTED"
    for owner, cases in sorted(required_cases.items()):
        if not cases or len(set(cases)) != len(cases):
            raise ValueError("Empty or duplicate required cases")
        ranked = []
        for name, record in records.items():
            if owner not in record.get("owners", []) or name in excluded:
                continue
            ratios = record.get("ratios", {})
            if not set(cases).issubset(ratios) or any(not positive(ratios[c]) for c in cases):
                excluded[name] = "INCOMPLETE_OR_INVALID"
                continue
            values = [ratios[c] for c in cases]
            ranked.append((max(values), math.fsum(values) / len(values), name))
        rankings[owner] = [name for _, _, name in sorted(ranked)]
        for name in rankings[owner][:slots_per_owner]:
            if name not in selected:
                selected.append(name)
    for name in records:
        if name not in selected:
            excluded.setdefault(name, "UNCONFIRMED_SLOT_LIMIT")
    return {"selected": selected, "excluded": excluded, "rankings": rankings,
            "scope": "Exploratory worst-cohort ordering with owner slots; excluded does not mean technically inferior"}


def screen_schedule(assignments, blocks, seed):
    if type(blocks) is not int or blocks < 1 or not assignments:
        raise ValueError("Screening requires nonempty fixed macroblocks")
    for cases in assignments.values():
        if not cases or len(set(cases)) != len(cases):
            raise ValueError("Every contender requires distinct declared cases")
    rng, events = random.Random(experimental_design.derive_seed(seed, "screen-cell-order")), []
    for block in range(blocks):
        cohorts = sorted({case for cases in assignments.values() for case in cases})
        rng.shuffle(cohorts)
        for case in cohorts:
            cells = sorted(v for v, cases in assignments.items() if case in cases)
            rng.shuffle(cells)
            events.extend((block, case, variant) for variant in cells)
    return events


def freeze_confirmation(design, selected, pilot, fixtures):
    if (design.get("protocol") != PROTOCOL or not protocol.is_digest(design.get("source_sha256"))
            or not selected or len(set(selected)) != len(selected)
            or pilot.get("status") != "COMPLETE" or pilot.get("phase") != "pilot"
            or pilot.get("admitted") is not True or not positive(pilot.get("wall_seconds"))
            or pilot.get("precision_status") != "PLANNED_NOT_GUARANTEED"):
        raise ValueError("Frozen confirmation requires completed pilot and resource admission")
    split_hashes, split_lineages = {}, {}
    for split in ("screen", "confirm"):
        inputs = fixtures.get(split)
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("Missing input split")
        hashes, lineages, names = set(), set(), set()
        for item in inputs:
            if (not protocol.is_digest(item.get("sha256")) or not item.get("lineage")
                    or not item.get("id") or item["id"] in names or item["sha256"] in hashes):
                raise ValueError("Missing/duplicate input identity or lineage")
            names.add(item["id"])
            hashes.add(item["sha256"])
            lineages.add(item["lineage"])
        split_hashes[split], split_lineages[split] = hashes, lineages
    if split_hashes["screen"] & split_hashes["confirm"]:
        raise ValueError("Confirmation reuses exploratory input bytes")
    kind = design.get("holdout_kind")
    if kind not in ("independent_seed", "independent_domain"):
        raise ValueError("Declare holdout scope")
    if kind == "independent_domain" and split_lineages["screen"] & split_lineages["confirm"]:
        raise ValueError("Domain/template lineage leaks into claimed holdout")
    settings = design.get("confirm", {})
    if (type(settings.get("blocks")) is not int or settings["blocks"] < 20
            or type(settings.get("iterations")) is not int or not 1 <= settings["iterations"] <= 250
            or not positive(settings.get("alpha")) or settings["alpha"] > .05
            or not settings.get("metrics") or len(set(settings["metrics"])) != len(settings["metrics"])):
        raise ValueError("Invalid fixed confirmation settings")
    # All frozen candidate/cohort/metric intervals belong to the declared family.
    assignments = design.get("confirm_assignments", {name: [x["id"] for x in fixtures["confirm"]] for name in selected})
    known = {x["id"] for x in fixtures["confirm"]}
    if set(assignments) != set(selected) or any(not ids or len(set(ids)) != len(ids) or not set(ids) <= known for ids in assignments.values()):
        raise ValueError("Incomplete/invalid frozen candidate-cohort assignment")
    family = sum(map(len, assignments.values())) * len(settings["metrics"])
    tail = settings["alpha"] / (2 * family)
    frozen = {"protocol": PROTOCOL, "design": copy.deepcopy(design), "variants": list(selected),
              "fixtures": copy.deepcopy(fixtures), "pilot": copy.deepcopy(pilot), "assignments": copy.deepcopy(assignments),
              "family_size": family, "bootstrap_draws": max(30000, math.ceil(100 / tail)),
              "claim_scope": kind, "result_authenticity": AUTHENTICITY}
    frozen["freeze_id"] = identity(frozen)
    return frozen


def admit_resources(estimate, budget):
    reasons = []
    for field in ("price_verified", "credit_verified", "enforcement_verified"):
        if budget.get(field) is not True:
            reasons.append(field)
    try:
        money = {key: Decimal(str(budget[key])) for key in
                 ("remaining_usd", "ceiling_usd", "reserve_usd", "spent_usd", "hourly_usd", "storage_usd")}
        if any(not value.is_finite() or value < 0 for value in money.values()) or money["hourly_usd"] <= 0:
            raise ValueError("Invalid price/budget")
        duration = sum(estimate[k] for k in ("wall_seconds", "analysis_seconds", "export_seconds", "shutdown_seconds"))
        if (not positive(duration) or not positive(estimate["wall_seconds"])
                or type(estimate["replicas"]) is not int or estimate["replicas"] != 1
                or any(type(estimate[k]) is not int or estimate[k] < 0 for k in
                       ("evidence_bytes", "build_bytes", "metadata_bytes", "analysis_bytes", "export_bytes",
                        "export_seconds", "shutdown_seconds", "memory_reserve_bytes"))
                or type(estimate["peak_memory_bytes"]) is not int or estimate["peak_memory_bytes"] <= 0
                or type(estimate.get("fixture_bytes", 0)) is not int or estimate.get("fixture_bytes", 0) < 0):
            raise ValueError("Invalid workload bound")
        cost = (Decimal(str(duration)) * estimate["replicas"] * money["hourly_usd"] / 3600
                + money["storage_usd"])
        available = min(money["remaining_usd"], money["ceiling_usd"] - money["spent_usd"]) - money["reserve_usd"]
        if cost > available:
            reasons.append("dollar_budget")
        if not positive(budget.get("deadline_seconds")) or duration > budget["deadline_seconds"]:
            reasons.append("supervised_deadline")
        disk = sum(estimate[k] for k in ("evidence_bytes", "build_bytes", "metadata_bytes", "analysis_bytes", "export_bytes")) + estimate.get("fixture_bytes", 0)
        memory = estimate["peak_memory_bytes"] + estimate["memory_reserve_bytes"]
        if type(budget.get("free_bytes")) is not int or disk > budget["free_bytes"]:
            reasons.append("disk_capacity")
        if estimate.get("required_evidence_bytes", 0) > estimate["evidence_bytes"]:
            reasons.append("evidence_capacity")
        if (type(budget.get("memory_bytes")) is not int or type(budget.get("enforced_memory_bytes")) is not int
                or not memory <= budget["enforced_memory_bytes"] <= budget["memory_bytes"]):
            reasons.append("memory_capacity")
        return {"admitted": not reasons, "reasons": reasons,
                "estimated_usd": str(cost.quantize(Decimal(".000001"), rounding=ROUND_CEILING)),
                "available_work_usd": str(available), "wall_including_analysis_export_shutdown_seconds": duration,
                "disk_with_export_bytes": disk, "memory_with_reserve_bytes": memory,
                "limit": "Admission from supplied verified facts, not a billing cap or independent attestation"}
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        return {"admitted": False, "reasons": [*reasons, "invalid_estimate_or_budget"], "error": str(error)}


def paired_blocks(rows, *, study_id, variant, case, phase, blocks, iterations, order_offset=0):
    if (not protocol.is_digest(study_id) or not variant or not case
            or phase not in ("pilot", "screen", "confirm")
            or type(blocks) is not int or blocks < 1 or type(iterations) is not int or iterations < 1):
        raise ValueError("Invalid expected measurement grid")
    if any(row.get("protocol") != PROTOCOL or row.get("study_id") != study_id
           or row.get("variant") != variant for row in rows):
        raise ValueError("Observation belongs to another protocol/study/variant")
    result = campaign.paired_rows(rows, case, phase, blocks, iterations, order_offset=order_offset)
    for block in range(blocks):
        for arm in ("baseline", "candidate"):
            ordered = sorted((row for row in rows if row["block"] == block and row["arm"] == arm),
                             key=lambda row: row["iteration"])
            offsets = [row.get("offset_ns") for row in ordered]
            if (any(type(value) is not int or value < 0 for value in offsets)
                    or any(b <= a for a, b in zip(offsets, offsets[1:]))):
                raise ValueError("Non-monotonic or missing native offsets")
    return result


def decision_closure(required, evidence):
    if len(set(required)) != len(required):
        raise ValueError("Duplicate required decision")
    decisions, complete = {}, bool(required)
    for name in required:
        item = copy.deepcopy(evidence.get(name, {"status": "NOT_RUN", "remaining": ["No decision evidence"]}))
        performance = item.get("performance", {})
        decided = (item.get("status") == "DECIDED_WITHIN_SCOPE" and item.get("oracle") == "PASS"
                   and bool(item.get("scope")) and item.get("remaining") == []
                   and bool(performance) and all(v in ("PASS_PERFORMANCE", "NON_REGRESSION_WITHIN_SCOPE") for v in performance.values())
                   and any(v == "PASS_PERFORMANCE" for v in performance.values()))
        if not decided:
            complete = False
            if item.get("status") == "DECIDED_WITHIN_SCOPE":
                item["status"] = "HOLD"
        decisions[name] = item
    return {"overall_decision": "DECIDED_WITHIN_SCOPE" if complete else "HOLD", "decisions": decisions,
            "optimality": "NOT_PROVEN", "production_adoption": "NOT_AUTHORIZED", "result_authenticity": AUTHENTICITY}


def interaction_contrast(cells):
    if set(cells) != {"00", "10", "01", "11"}:
        raise ValueError("Interaction needs every cell including combined contender")
    sizes = {len(values) for values in cells.values()}
    if len(sizes) != 1 or not next(iter(sizes)) or any(not positive(x) for values in cells.values() for x in values):
        raise ValueError("Interaction cells require complete paired block values")
    values = [math.log(d) + math.log(a) - math.log(b) - math.log(c)
              for a, b, c, d in zip(cells["00"], cells["10"], cells["01"], cells["11"])]
    return math.fsum(values) / len(values)


def bounded_bootstrap(rounds, probabilities, draws, seed, batch_size=64):
    outputs = {q: [] for q in probabilities}
    for deltas in _bootstrap_batches(rounds, probabilities, draws, seed, batch_size):
        for q, values in deltas.items():
            outputs[q].extend(values)
    for values in outputs.values():
        values.sort()
    return outputs


def bootstrap_intervals(rounds, probabilities, draws, seed, tail, batch_size=64):
    if not positive(tail) or tail > .5 or type(draws) is not int or draws < 1:
        raise ValueError("Invalid bootstrap interval tail")
    lower_count = math.floor((draws - 1) * tail) + 1
    upper_count = draws - math.ceil((draws - 1) * (1 - tail))
    if not 1 <= min(lower_count, upper_count) <= max(lower_count, upper_count) <= 1024:
        raise ValueError("Bootstrap interval exceeds declared retained-tail bound")
    low, high = {q: [] for q in probabilities}, {q: [] for q in probabilities}
    for deltas in _bootstrap_batches(rounds, probabilities, draws, seed, batch_size):
        for q, values in deltas.items():
            for value in values:
                if len(low[q]) < lower_count:
                    heapq.heappush(low[q], -value)
                elif value < -low[q][0]:
                    heapq.heapreplace(low[q], -value)
                if len(high[q]) < upper_count:
                    heapq.heappush(high[q], value)
                elif value > high[q][0]:
                    heapq.heapreplace(high[q], value)
    return {q: [-low[q][0], high[q][0]] for q in probabilities}


def _bootstrap_batches(rounds, probabilities, draws, seed, batch_size):
    """Same paired-block estimand/seed as native/1, bounded temporary weight batches."""
    if (type(draws) is not int or draws < 1 or not 1 <= batch_size <= 256
            or len(rounds) < 2 or not probabilities or sorted(set(probabilities)) != probabilities
            or any(not 0 <= q <= 1 for q in probabilities)):
        raise ValueError("Invalid bootstrap grid")
    sizes = {len(pair.get(arm, [])) for pair in rounds for arm in ("baseline", "candidate")}
    if len(sizes) != 1 or not next(iter(sizes)):
        raise ValueError("Unbalanced bootstrap blocks")
    try:
        import numpy as np
    except ImportError:
        np = None
    count, rng = len(rounds), random.Random(seed)
    arrays = {}
    for arm in ("baseline", "candidate"):
        pairs = sorted((x, group) for group, block in enumerate(rounds) for x in block[arm])
        if any(not positive(x) for x, _ in pairs):
            raise ValueError("Invalid bootstrap observation")
        values, groups = zip(*pairs)
        if np is None:
            arrays[arm] = values, groups, None
        else:
            values, groups = np.asarray(values), np.asarray(groups)
            prefix = None
            if len(values) > 256:
                # Weighted order statistics need only group counts at queried ranks.
                # Avoid a draws x observations cumulative matrix for every batch.
                prefix = np.zeros((len(values) + 1, count), dtype=np.int32)
                prefix[np.arange(1, len(values) + 1), groups] = 1
                np.cumsum(prefix, axis=0, out=prefix)
            arrays[arm] = values, groups, prefix
    positions = [round(q * (len(rounds) * next(iter(sizes)) - 1)) for q in probabilities]
    for start in range(0, draws, batch_size):
        weights = []
        for _ in range(min(batch_size, draws - start)):
            row = [0] * count
            for _ in range(count):
                row[rng.randrange(count)] += 1
            weights.append(row)
        sampled = {}
        for arm, (values, groups, prefix) in arrays.items():
            if np is not None:
                weight_array = np.asarray(weights, dtype=np.int64)
                if prefix is None:
                    cumulative = weight_array[:, groups].cumsum(axis=1)
                    sampled[arm] = np.stack([values[(cumulative > p).argmax(axis=1)] for p in positions], axis=1).tolist()
                else:
                    low = np.zeros((len(weights), len(positions)), dtype=np.int64)
                    high = np.full_like(low, len(values) - 1)
                    while np.any(low < high):
                        middle = (low + high) // 2
                        mass = np.einsum("bqk,bk->bq", prefix[middle + 1], weight_array, dtype=np.int64)
                        found = mass > np.asarray(positions)
                        high = np.where(found, middle, high)
                        low = np.where(found, low, middle + 1)
                    sampled[arm] = values[low].tolist()
            else:
                sampled[arm] = [protocol._weighted_quantiles(values, groups, row, probabilities) for row in weights]
        deltas = {q: [] for q in probabilities}
        for baseline, candidate in zip(sampled["baseline"], sampled["candidate"]):
            for i, q in enumerate(probabilities):
                deltas[q].append(candidate[i] - baseline[i])
        yield deltas


def synthetic_cases(split):
    """Finite causal partitions, with fresh bytes but no domain-generalization claim."""
    if split not in ("screen", "confirm"):
        raise ValueError("Unknown fixture split")
    rng = random.Random({"screen": 458729, "confirm": 938201}[split])
    nonce = f"revision-{rng.randrange(100000, 1000000)}"
    vocab = [f"term{i}" for i in range(128)]
    article = "".join(f"<p>{nonce} {i} {' '.join(rng.sample(vocab, 12))} factual context</p>" for i in range(256))
    large = article * 6
    escaped = (f'<p>{nonce} &quot; \\ &lt; &amp; TÀI liệu 内容 日本語</p>' * 256)
    pages = {
        "tiny": f"<p>{nonce} term0 evidence</p>",
        "article": article,
        "large": large,
        "escaped_unicode": escaped,
        "unique_links": "".join(f"<p>{nonce} term0 <a href='/item/{i}'>evidence{i}</a></p>" for i in range(1024)),
        "duplicate_links": "".join(f"<p>{nonce} term0 <a href='/item/{i % 4}'>same</a></p>" for i in range(1024)),
        "deep": "<div>" * 512 + f"<p>{nonce} term0</p>" + "</div>" * 512,
        "repair": f"<table><p>{nonce} term0<tr><td>data</table><b><i>tail</b>repair</i><template>hidden</template>",
        "novelty": f"<p>{nonce} term0 same</p>" * 64 + "<p>term0 distinct</p>",
        "preformatted": f"<pre>{nonce}\n  term0\n\tcode &lt;x&gt;</pre>",
    }
    cases = []
    def add(name, stage, html, query, owners, **fields):
        fixture = {"html": html, "query": query, **fields}
        cases.append({"id": name, "stage": stage, "owners": owners, "fixture": fixture,
                      "lineage": "synthetic:" + name, "provenance": "project-authored synthetic causal partition"})
    for shape, html in pages.items():
        for mode, query in (("plain", None), ("q3", "term0 term1 term2")):
            add(f"pipeline-{shape}-{mode}", "pipeline", html, query, ["H01", "H02", "H04", "H05", "H10"])
            if shape in ("article", "large", "escaped_unicode"):
                add(f"export-{shape}-{mode}", "export", html, query, ["H01", "H02", "H10"])
    for terms in (1, 3, 8, 32, 128):
        for density, html in (("dense", article), ("sparse", pages["tiny"]), ("novelty", pages["novelty"])):
            query = " ".join(vocab[:terms])
            add(f"rank-{terms}-{density}", "rank", html, query, ["H02"])
            add(f"selection-{terms}-{density}", "highlight", html, query, ["H02"])
    for shape in ("article", "deep", "repair", "unique_links", "duplicate_links", "preformatted"):
        add(f"parse-{shape}", "parse", pages[shape], None, ["H04", "H05"])
    # Probe-specific workloads remain declared separately from full-path source contenders.
    for repeats in (1, 2, 8, 32):
        add(f"prepared-{repeats}", "prepared", article, "term0 term1 term2", ["H03"], queries=repeats)
    add("prepared-invalidation", "prepared", article, "term0 term1 term2", ["H03"], queries=8,
        invalidate_after=4, replacement_html=f"<p>{nonce} replacement term0 term2</p>", replacement_policy_revision="policy-2")
    for shape in ("article", "large", "escaped_unicode"):
        add(f"hash-{shape}", "hash", pages[shape], None, ["H01"])
        add(f"serialization-{shape}", "serialization", pages[shape], None, ["H01"])
    for size in (64 * 1024, 512 * 1024, 4 * 1024 ** 2):
        add(f"budget-{size}", "budget", pages["tiny"], None, ["H01"],
            oversized_text_bytes=size, limit=1024)
    for shape, rows, max_cols in (
        ("narrow", [{"group": 0, "cells": [{"text": nonce, "rowspan": 1, "colspan": 1, "is_header": False}]} for _ in range(256)], 1024),
        ("wide", [{"group": 0, "cells": [{"text": nonce, "rowspan": 1, "colspan": 1, "is_header": False} for _ in range(256)]} for _ in range(8)], 256),
        ("spans", [{"group": 0, "cells": [{"text": nonce, "rowspan": 0, "colspan": 2, "is_header": True}]},
                    {"group": 0, "cells": [{"text": "B", "rowspan": 1, "colspan": 1, "is_header": False}]},
                    {"group": 1, "cells": [{"text": "C", "rowspan": 1, "colspan": 1, "is_header": False}]}], 4),
    ):
        add("table-" + shape, "table", "", None, ["H06"], rows=rows, max_rows=512, max_cols=max_cols)
    from cloud_experiments import workloads
    cases.extend(workloads.native_cases(split))
    return cases


def fixture_inventory(cases, split):
    return [{"id": item["id"], "sha256": hashlib.sha256(canonical(item["fixture"])).hexdigest(),
             "lineage": item["lineage"], "split": split, "stage": item["stage"]} for item in cases]


def manifest_design():
    registry = lifecycle.read_json(ROOT / "cloud_experiments/core_decisions.json")
    audited = campaign.audit_decision_map(ROOT, registry)
    required_decisions = [item["id"] for item in registry["decisions"]]
    required_interactions = [item["id"] for item in registry["interactions"]]
    language_panels = registry["language_panels"]
    native = experimental_design.native_design()
    if (audited["mapping_status"] != "MAPPED"
            or set(required_decisions) != {f"H{i:02}" for i in range(1, 15)}
            or not set(native["factorial_panels"]).union({"I-NATIVE-CROSS-COVERAGE"}) <= set(required_interactions)
            or {panel["id"] for panel in language_panels} != {f"L{i:02}" for i in range(1, 7)}):
        raise ValueError("Authoritative decision map does not close the declared experiment scope")
    return {"protocol": PROTOCOL, "kind": "bounded-exploration-and-frozen-confirmation",
            "holdout_kind": "independent_seed", "required_decisions": required_decisions,
            "required_interactions": required_interactions, "native_design": native,
            "language_panels": language_panels,
            "randomization": {"schema": "athena-randomization/1", "seed": 81723091,
                              "arm_order": "randomized-start-alternating-blocks"},
            "screen": {"blocks": 6, "iterations": 30, "seed": 458729},
            "confirm": {"blocks": 24, "iterations": 250, "alpha": .05,
                        "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"]},
            "limits": {"wall_seconds": 120 * 86400, "command_seconds": 1800, "evidence_bytes": 12 * 1024 ** 3,
                       "build_bytes": 16 * 1024 ** 3, "minimum_free_bytes": 48 * 1024 ** 3,
                       "log_bytes": 64 * 1024 ** 2, "metadata_bytes": 64 * 1024 ** 2,
                       "analysis_bytes": 256 * 1024 ** 2, "command_output_bytes": 8 * 1024 ** 2,
                       "binary_bytes": 2 * 1024 ** 3,
                       "export_bytes": 16 * 1024 ** 3,
                       "block_bytes": 256 * 1024 ** 2,
                       "memory_reserve_bytes": 1024 ** 3,
                       "disk_reserve_bytes": 2 * 1024 ** 3,
                       "max_variants": experimental_design.MAX_VARIANTS,
                       "max_fixture_file_bytes": 8 * 1024 ** 2,
                       "max_fixture_html_bytes": 4 * 1024 ** 2,
                       "max_fixture_bytes": 16 * 1024 ** 2},
            "selection": {"slots_per_owner": 1, "method": "worst-cohort then mean, explicit owner slots; interaction cells retained independently",
                          "combined_finalist": "Union of compatible owner-selected components; conflicts stop; native and controlled-service gates before held-out confirmation"},
            "result_authenticity": AUTHENTICITY,
            "measurement_scope": "Native Rust boundaries on declared CPU; synthetic holdout seeds, not representative web-domain proof",
            "remaining": ["Representative licensed corpus/domain holdout", "Full generated-code sandbox", "Actual deployment SLO/demand envelope"]}


def native_matrix():
    design = experimental_design.native_design()
    return design["variants"], design["factorial_panels"]


def verify_snapshot(source, design):
    source = Path(source).resolve(strict=True)
    document = {k: v for k, v in design.items() if k != "study_id"}
    if document.get("protocol") != PROTOCOL or identity(document) != design.get("study_id"):
        raise ValueError("Study identity/protocol mismatch")
    if not design.get("files"):
        raise ValueError("Empty source inventory")
    for name, digest in design["files"].items():
        path = source / name
        if ("\\" in name or Path(name).is_absolute() or ".." in Path(name).parts
                or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source)
                or not protocol.is_digest(digest)
                or hashlib.sha256(campaign.canonical_source_bytes(path.read_bytes())).hexdigest() != digest):
            raise ValueError("Frozen input missing/changed/escaped: " + name)
    return {name: campaign.canonical_source_bytes((source / name).read_bytes()) for name in design["files"]}


def _read_bound_json(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or linked launch input")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Launch input exceeds byte bound")
    return json.loads(data), hashlib.sha256(data).hexdigest()


def validate_launch_receipt(receipt_path, spec_path, budget_path, request):
    """Validate the operator-approved launch intent before creating a run.

    A receipt is authorization and binding evidence, not an independent
    attestation.  It must bind the exact frozen spec and reviewed budget to the
    requested mode/variants/deadline so a stale or partial cloud job cannot be
    started accidentally.
    """
    if not isinstance(request, dict) or set(request) != {"pilot_only", "variants"}:
        raise ValueError("Invalid launch request shape")
    if type(request["pilot_only"]) is not bool:
        raise ValueError("Invalid launch request mode")
    variants = request["variants"]
    if variants is not None and (not isinstance(variants, list)
                                 or not variants
                                 or any(not isinstance(value, str) or not value for value in variants)
                                 or len(set(variants)) != len(variants)):
        raise ValueError("Invalid launch request variants")
    receipt, receipt_sha256 = _read_bound_json(receipt_path, 256 * 1024)
    spec, spec_sha256 = _read_bound_json(spec_path, 16 * 1024 ** 2)
    budget, budget_sha256 = _read_bound_json(budget_path, 256 * 1024)
    if not isinstance(receipt, dict) or not isinstance(spec, dict) or not isinstance(budget, dict):
        raise ValueError("Launch receipt, spec and budget must be JSON objects")
    spec_document = {key: value for key, value in spec.items() if key != "study_id"}
    if spec.get("protocol") != PROTOCOL or not protocol.is_digest(spec.get("study_id")) \
            or identity(spec_document) != spec.get("study_id"):
        raise ValueError("Launch receipt spec identity is invalid")
    allowed = {"schema", "target", "operator_confirmed", "confirmed_at", "study_id",
               "spec_sha256", "budget_sha256", "request", "deadline_seconds", "command"}
    if set(receipt) != allowed:
        raise ValueError("Launch receipt has unknown or missing fields")
    if receipt["schema"] != "athena-launch-receipt/1":
        raise ValueError("Unsupported launch receipt schema")
    if receipt["target"] != "gce":
        raise ValueError("Launch receipt target must be gce")
    if receipt["operator_confirmed"] is not True:
        raise ValueError("Explicit operator confirmation is required")
    if (not isinstance(receipt["confirmed_at"], str) or not receipt["confirmed_at"].strip()
            or len(receipt["confirmed_at"]) > 128):
        raise ValueError("Launch receipt confirmation time is invalid")
    if receipt["study_id"] != spec.get("study_id") or not protocol.is_digest(receipt["study_id"]):
        raise ValueError("Launch receipt study identity mismatch")
    if spec_sha256 != receipt["spec_sha256"] or not protocol.is_digest(receipt["spec_sha256"]):
        raise ValueError("Launch receipt spec hash mismatch")
    if budget_sha256 != receipt["budget_sha256"] or not protocol.is_digest(receipt["budget_sha256"]):
        raise ValueError("Launch receipt budget hash mismatch")
    if receipt["request"] != request:
        raise ValueError("Launch receipt request mismatch")
    deadline = receipt["deadline_seconds"]
    if type(deadline) is not int or deadline <= 0 or budget.get("deadline_seconds") != deadline:
        raise ValueError("Launch receipt deadline mismatch")
    command = receipt["command"]
    if (not isinstance(command, list) or not command or
            any(not isinstance(item, str) or not item or len(item) > 512 for item in command)
            or "run-native" not in command):
        raise ValueError("Launch receipt command is invalid")
    if ("--pilot-only" in command) != request["pilot_only"]:
        raise ValueError("Launch receipt command/request mismatch")
    if variants is None:
        if "--variants" in command:
            raise ValueError("Launch receipt command/request mismatch")
    else:
        if command.count("--variants") != 1:
            raise ValueError("Launch receipt command/request mismatch")
        variant_index = command.index("--variants")
        if command[variant_index + 1:] != variants:
            raise ValueError("Launch receipt command/request mismatch")
    if any(any(secret in item.lower() for secret in ("api_key", "api-key", "token=", "password", "secret="))
           for item in command):
        raise ValueError("Launch receipt command must not contain credentials")
    return {**receipt, "receipt": receipt, "spec": spec, "budget": budget,
            "receipt_sha256": receipt_sha256, "spec_sha256": spec_sha256,
            "budget_sha256": budget_sha256}


def readiness(design, records):
    """Explicit experiment gaps survive preparation and successful subset runs."""
    required = design["required_decisions"] + design["required_interactions"]
    missing = [name for name in required if records.get(name, {}).get("runnable_verified") is not True]
    language_panels = design.get("language_panels", [])
    blocking_language_panels = []
    conditional_language_non_admissions = []
    for panel in language_panels:
        identity = panel["id"]
        if panel["admission"] == "REQUIRED":
            if (panel["status"] == "NOT_IMPLEMENTED"
                    or records.get(identity, {}).get("runnable_verified") is not True):
                blocking_language_panels.append(identity)
        elif panel["status"] == "NOT_ADMITTED":
            conditional_language_non_admissions.append(identity)
        elif records.get(identity, {}).get("runnable_verified") is not True:
            blocking_language_panels.append(identity)
    missing.extend(blocking_language_panels)
    return {"status": "RECORDED_REQUIRES_EXECUTION_REVIEW" if not missing else "INCOMPLETE",
            "missing": missing, "decisions": {k: records.get(k, {"status": "NOT_VERIFIED", "meaning": "Full decision obligation is unverified; implemented subsets are described separately"}) for k in required},
            "language_panels": language_panels,
            "blocking_language_panels": blocking_language_panels,
            "conditional_language_non_admissions": conditional_language_non_admissions,
            "decision_sufficiency": "NOT_EVALUATED", "runnable_evidence": "NOT_ATTESTED_BY_THIS_REGISTRY_CHECK",
            "result_authenticity": AUTHENTICITY}


def study_cases(files, split):
    from cloud_experiments import workloads
    return synthetic_cases(split) + workloads.corpus_cases(files, split)


def prepare(destination, corpus_manifest=None, wall_seconds=None):
    from cloud_experiments import contenders, libraries, workloads
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    files = campaign.source_files(ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock", "deny.toml", "rust-toolchain.toml",
                                       "engineering-constitution.md", "CORE_RESEARCH_PLAN.md", "cloud_experiments",
                                       "kaggle_experiments/protocol.py", "kaggle_experiments/runner.py",
                                       "kaggle_experiments/probe.rs", "kaggle_experiments/test_protocol.py",
                                       "kaggle_experiments/pack_snapshots.py", "kaggle_experiments/generate_clusters.py",
                                       "kaggle_results/verify_statistics.py", "kaggle_results/pull_and_analyze.py", "model"])
    files = {k: v for k, v in files.items() if "__pycache__" not in Path(k).parts}
    corpus = None
    if corpus_manifest is not None:
        corpus = workloads.load_corpus(corpus_manifest)
        if any(name.startswith("cloud_experiments/corpus/") for name in files):
            raise ValueError("Do not mix an existing frozen corpus with a new intake")
        files.update(workloads.corpus_files(corpus))
    archive = campaign.pack_snapshots.archive_files(files)
    if len(archive) > 16 * 1024 ** 2 or sum(map(len, files.values())) > 16 * 1024 ** 2:
        raise ValueError("Source and corpus exceed bundle admission")
    campaign.write_bytes(destination / "source.tgz", archive,
                         max_bytes=campaign.pack_snapshots.GIT_ARCHIVE_OUTPUT_BYTES)
    design = manifest_design()
    if wall_seconds is not None:
        if type(wall_seconds) is not int or not 1 <= wall_seconds <= 120 * 86400:
            raise ValueError("Declare campaign duration before source/design freeze")
        design["limits"]["wall_seconds"] = wall_seconds
    design.update(source_sha256=hashlib.sha256(archive).hexdigest(), files={k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
                  variants=contenders.definitions(), fixtures={split: fixture_inventory(study_cases(files, split), split) for split in ("screen", "confirm")},
                  library_dossier=libraries.dossier(ROOT),
                  corpus={"status": "NOT_PROVIDED"} if corpus is None else {k: v for k, v in corpus.items() if k != "cases"})
    design["study_id"] = identity(design)
    campaign.write_json(destination / "spec.json", design)
    return {"study_id": design["study_id"], "bytes": len(archive), "files": len(files), "status": "PREPARED_NOT_LAUNCH_APPROVED"}


def _materialize(workspace, files, variant, transformed=None):
    from cloud_experiments import contenders
    transformed = contenders.transform(files, variant) if transformed is None else transformed
    for name, data in transformed.items():
        if name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml") or name.startswith(("src/", "tests/", "fixtures/")):
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("Work tree link/escape")
            target.write_bytes(data)
    original = "cloud_experiments/core_probe.rs"
    if original in transformed:
        target = workspace / original
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Research probe target escaped the frozen work tree")
        target.write_bytes(transformed[original])
    original = "cloud_experiments/service_probe.rs"
    if original in transformed:
        target = workspace / "src/bin/service_probe.rs"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Service probe target escaped the frozen work tree")
        target.write_bytes(transformed[original])
        transformed["src/bin/service_probe.rs"] = transformed[original]
    transformed["src/bin/research_probe.rs"] = files["kaggle_experiments/probe.rs"]
    (workspace / "src/bin/research_probe.rs").write_bytes(transformed["src/bin/research_probe.rs"])
    return transformed


def _network_namespace_command(command):
    return ["unshare", "--user", "--map-root-user", "--net", "--fork", "--", *map(str, command)]


def _current_network_namespace_id():
    return os.readlink("/proc/self/ns/net")


def _parse_verbose_toolchain(raw, command):
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("Toolchain identity is not bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Toolchain identity is not UTF-8") from error
    lines = text.splitlines()
    if not lines or not lines[0] or len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("Toolchain identity is empty or oversized")
    prefix = "rustc " if command == "rustc" else "cargo " if command == "cargo" else None
    if prefix is None or not lines[0].startswith(prefix):
        raise ValueError("Unexpected toolchain identity command")
    fields = {}
    for line in lines[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            if key and value and len(key) <= 64 and len(value) <= 1024:
                fields[key] = value
    required = {"release", "host"}
    if command == "rustc":
        required.add("LLVM version")
    if not required <= fields.keys():
        raise ValueError("Incomplete toolchain identity")
    return {"first_line": lines[0], "fields": fields,
            "stdout_sha256": hashlib.sha256(bytes(raw)).hexdigest()}


def _capture_build_receipt(run, workspace, build):
    rustc = _parse_verbose_toolchain(
        _run_isolated_cargo(run, ["rustc", "-Vv"], cwd=workspace, timeout=60), "rustc")
    cargo = _parse_verbose_toolchain(
        _run_isolated_cargo(run, ["cargo", "-Vv"], cwd=workspace, timeout=60), "cargo")
    toolchain_path = workspace / "rust-toolchain.toml"
    if toolchain_path.is_symlink() or not toolchain_path.is_file():
        raise ValueError("Missing pinned rust-toolchain.toml")
    try:
        import tomllib
        channel = tomllib.loads(toolchain_path.read_text(encoding="utf-8"))["toolchain"]["channel"]
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Invalid pinned rust-toolchain.toml") from error
    if not isinstance(channel, str) or not channel:
        raise ValueError("Invalid pinned Rust channel")
    release = rustc["fields"]["release"]
    if channel not in {"stable", "beta", "nightly"} and release != channel:
        raise RuntimeError("Compiler release does not match rust-toolchain.toml")
    return {"schema": "athena-build-receipt/1", "toolchain_channel": channel,
            "rustc": rustc, "cargo": cargo,
            "cargo_profile_env": dict(build.get("cargo_profile_env", {})),
            "probe_env": dict(build.get("probe_env", {})),
            "rustflags": list(build.get("rustflags", []))}


def _require_build_receipt(previous, current):
    if previous.get("build_receipt") != current:
        raise lifecycle.IntegrityError("Resumed binary lacks matching compiler/profile receipt")


def _verify_build_network_isolation(run):
    if platform.system() != "Linux":
        raise RuntimeError("Native dependency builds require a Linux network namespace")
    host_netns = _current_network_namespace_id()
    probe = """import json, os, socket, sys
checks = {}
for name, address, port in (("gce_metadata", "169.254.169.254", 80), ("public_egress", "1.1.1.1", 443)):
    try:
        connection = socket.create_connection((address, port), timeout=0.4)
    except OSError:
        checks[name] = True
    else:
        connection.close()
        checks[name] = False
report = {"host_netns": sys.argv[1], "build_netns": os.readlink("/proc/self/ns/net"),
          "metadata_tcp_blocked": checks["gce_metadata"], "public_tcp_blocked": checks["public_egress"]}
print(json.dumps(report, sort_keys=True, separators=(",", ":")))
if report["host_netns"] == report["build_netns"] or not all(checks.values()):
    raise SystemExit(41)
"""
    raw = run.run(_network_namespace_command([sys.executable, "-B", "-c", probe, host_netns]),
                  timeout=15, log_output=False)
    try:
        report = json.loads(raw)
    except (TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Network isolation probe returned invalid evidence") from error
    if (not isinstance(report, dict) or report.get("host_netns") != host_netns
            or report.get("build_netns") == host_netns
            or report.get("metadata_tcp_blocked") is not True
            or report.get("public_tcp_blocked") is not True):
        raise RuntimeError("Build network namespace did not deny metadata and public egress")
    report.update(status="PROCESS_CHECKED_NETWORK_NAMESPACE",
                  authenticity="NOT_VERIFIED_INDEPENDENTLY")
    return report


def _run_isolated_cargo(run, command, *, cwd, timeout):
    run.guard_storage()
    result = run.run(_network_namespace_command(command), cwd=cwd, timeout=timeout)
    run.guard_storage()
    return result


def _build(run, workspace, files, variant, target_cache):
    from cloud_experiments import contenders, libraries
    definition = contenders.metadata(variant)
    destination = run.directory / "bins" / variant
    previous = run.manifest["snapshots"].get(variant)
    if previous is not None:
        transformed = contenders.transform(files, variant)
        dependency_inputs = libraries.verify_build_inputs(files, transformed, definition["factors"])
        if previous.get("dependency_input_evidence") != dependency_inputs:
            raise lifecycle.IntegrityError("Resumed binary lacks matching pre-build dependency verification")
        _require_build_receipt(previous, _capture_build_receipt(run, workspace, definition["build"]))
        isolation = _verify_build_network_isolation(run)
        prior_isolation = previous.get("build_network_isolation", {})
        if (prior_isolation.get("status") != isolation["status"]
                or prior_isolation.get("host_netns") == prior_isolation.get("build_netns")
                or prior_isolation.get("metadata_tcp_blocked") is not True
                or prior_isolation.get("public_tcp_blocked") is not True):
            raise lifecycle.IntegrityError("Resumed binary lacks matching build network isolation evidence")
        if (previous.get("definition") != definition or not previous.get("binaries")
                or destination.is_symlink()
                or any((destination / name).is_symlink() or not (destination / name).is_file()
                       or protocol.sha256_file(destination / name) != value for name, value in previous["binaries"].items())):
            raise lifecycle.IntegrityError("Resumed native binary or definition changed")
        return destination
    transformed = contenders.transform(files, variant)
    dependency_inputs = libraries.verify_build_inputs(files, transformed, definition["factors"])
    transformed = _materialize(workspace, files, variant, transformed)
    run.manifest.setdefault("build_inputs", {})[variant] = dependency_inputs
    network_isolation = _verify_build_network_isolation(run)
    run.manifest.setdefault("build_network_isolation", {})[variant] = network_isolation
    run.save()
    build_started = time.monotonic()
    build = definition["build"]
    build_receipt = None
    existing = dict(os.environ)
    rustup_home = os.environ.get("RUSTUP_HOME") or str(Path.home() / ".rustup")
    try:
        for key in list(os.environ):
            if (key.startswith(("CARGO_", "ATHENA_RESEARCH_"))
                    or key in ("RUSTFLAGS", "RUSTC", "RUSTDOC", "RUSTC_WRAPPER",
                               "RUSTC_WORKSPACE_WRAPPER", "RUSTDOCFLAGS", "RUSTC_BOOTSTRAP")):
                del os.environ[key]
        os.environ.update(build["env"])
        if build["rustflags"]:
            os.environ["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(build["rustflags"])
        os.environ["CARGO_TARGET_DIR"] = str(target_cache)
        cargo_home = target_cache / "cargo-home"
        if cargo_home.is_symlink() or not cargo_home.resolve().is_relative_to(target_cache.resolve()):
            raise ValueError("Cargo cache escaped the owned research cache")
        cargo_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (os.name == "posix" and (cargo_home.stat().st_uid != os.geteuid()
                or cargo_home.stat().st_mode & 0o077)):
            raise ValueError("Cargo cache must be private to the research account")
        if any((cargo_home / name).exists() or (cargo_home / name).is_symlink()
               for name in ("config", "config.toml", "credentials", "credentials.toml")):
            raise ValueError("Isolated Cargo home must not contain configuration or credentials")
        os.environ["CARGO_HOME"] = str(cargo_home)
        os.environ["RUSTUP_HOME"] = rustup_home
        os.environ["HOME"] = str(cargo_home)
        build_receipt = _capture_build_receipt(run, workspace, build)
        run.run(["cargo", "fetch", "--locked"], cwd=workspace, timeout=1200)
        _run_isolated_cargo(run, ["cargo", "fmt", "--all"], cwd=workspace, timeout=300)
        _run_isolated_cargo(run, ["cargo", "fmt", "--all", "--", "--check"], cwd=workspace, timeout=300)
        _run_isolated_cargo(run, ["cargo", "--offline", "test", "--locked", "--all-targets", "--features", "research-probes", "-j", "2"], cwd=workspace, timeout=1200)
        _run_isolated_cargo(run, ["cargo", "--offline", "clippy", "--locked", "--all-targets", "--features", "research-probes", "-j", "2", "--", "-D", "warnings"], cwd=workspace, timeout=1200)
        _run_isolated_cargo(run, ["cargo", "--offline", "build", "--locked", "--release", "--bins", "--features", "research-probes", "-j", "2"], cwd=workspace, timeout=1800)
    finally:
        os.environ.clear()
        os.environ.update(existing)
    if (workspace / "Cargo.lock").read_bytes() != transformed["Cargo.lock"]:
        raise ValueError("Locked dependency graph changed")
    if build_receipt is None:
        raise RuntimeError("Build compiler/profile receipt was not captured")
    graph = runner.dependency_metadata(run, workspace)
    dependency_evidence = libraries.verify_graph(files["Cargo.lock"], transformed["Cargo.lock"], graph, definition["factors"])
    if destination.exists():
        if destination.is_symlink() or not destination.resolve().is_relative_to(run.directory.resolve()):
            raise ValueError("Partial binary destination escaped research root")
        for path in destination.iterdir():
            if path.is_symlink() or not path.is_file() or path.name not in {
                    "core_probe", "service_probe", "research_probe", "athena-core-v2", "bench_core",
                    "core_probe.exe", "service_probe.exe", "research_probe.exe", "athena-core-v2.exe", "bench_core.exe"}:
                raise ValueError("Unknown partial binary artifact")
            path.unlink()
        destination.rmdir()
    destination.mkdir(parents=True, exist_ok=False)
    ext = ".exe" if os.name == "nt" else ""
    for name in ("core_probe", "service_probe", "research_probe", "athena-core-v2", "bench_core"):
        origin = target_cache / "release" / (name + ext)
        if origin.is_file():
            shutil.copy2(origin, destination / (name + ext))
    run.manifest["snapshots"][variant] = {"definition": definition,
        "files": {name: protocol.sha256_file(workspace / name) for name in transformed
                  if name.endswith(".rs") and (workspace / name).is_file()},
        "lock_sha256": protocol.sha256_file(workspace / "Cargo.lock"),
        "manifest_sha256": protocol.sha256_file(workspace / "Cargo.toml"),
        "build_wall_seconds": time.monotonic() - build_started,
        "build_receipt": build_receipt,
        "dependency_input_evidence": dependency_inputs,
        "build_network_isolation": network_isolation,
        "dependency_evidence": dependency_evidence,
        "binaries": {p.name: protocol.sha256_file(p) for p in destination.iterdir()}}
    run.save()
    return destination


def _invoke(run, binary, case, fixture, iterations, cpu):
    command = [binary / "core_probe", case["stage"], fixture, iterations]
    if cpu is not None:
        command = ["taskset", "-c", cpu, *command]
    raw = run.run(command, timeout=180, log_output=False)
    if iterations == 0:
        return raw
    packet = json.loads(raw)
    if (packet.get("protocol") != PROTOCOL or packet.get("stage") != case["stage"]
            or packet.get("iterations") != iterations or packet.get("warmup") != 10
            or not protocol.is_digest(packet.get("fingerprint"))
            or len(packet.get("raw_ns", [])) != iterations or len(packet.get("offsets_ns", [])) != iterations
            or any(type(ns) is not int or ns <= 0 for ns in packet["raw_ns"])
            or any(type(ns) is not int or ns < 0 for ns in packet["offsets_ns"])
            or packet["offsets_ns"] != sorted(set(packet["offsets_ns"]))):
        raise ValueError("Invalid actual native packet")
    return packet


def _fixture_payloads(phases, limits, *, require_each=False):
    max_file_bytes = limits.get("max_fixture_file_bytes")
    max_html_bytes = limits.get("max_fixture_html_bytes")
    max_total_bytes = limits.get("max_fixture_bytes")
    if (type(max_file_bytes) is not int or max_file_bytes <= 0
            or type(max_html_bytes) is not int or max_html_bytes <= 0
            or type(max_total_bytes) is not int or max_total_bytes <= 0):
        raise ValueError("Invalid fixture resource bound")
    payloads, total_bytes = {}, 0
    for split, cases in phases.items():
        split_payloads = {}
        for case in cases:
            if "fixture" not in case:
                if require_each:
                    raise ValueError("Fixture resource bound requires a fixture for every case")
                continue
            case_id = case.get("id")
            safe_id = (isinstance(case_id, str) and 1 <= len(case_id) <= 128
                       and case_id.isascii() and case_id[0].isalnum()
                       and all(char.isalnum() or char in "._-" for char in case_id))
            if not safe_id or case_id in split_payloads:
                raise ValueError("Invalid or duplicate fixture identity")
            fixture = case["fixture"]
            if isinstance(fixture, dict):
                for field in ("html", "replacement_html"):
                    html = fixture.get(field)
                    if isinstance(html, str) and len(html.encode("utf-8")) > max_html_bytes:
                        raise ValueError("Fixture HTML field exceeds declared fixture resource bound")
            data = canonical(fixture)
            if len(data) > max_file_bytes:
                raise ValueError("Fixture file exceeds declared fixture resource bound")
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise ValueError("Aggregate fixture bytes exceed declared fixture resource bound")
            split_payloads[case_id] = data
        payloads[split] = split_payloads
    return payloads, total_bytes


def _write_fixtures(root, phases, limits):
    payloads, total_bytes = _fixture_payloads(phases, limits, require_each=True)
    if root.is_symlink():
        raise ValueError("Fixture root cannot be linked")
    paths, pending = {}, []
    for split, cases in phases.items():
        folder = root / ("fixtures-" + split)
        if folder.is_symlink():
            raise ValueError("Fixture directory cannot be linked")
        split_paths = {}
        for case in cases:
            case_id = case["id"]
            path = folder / (case_id + ".json")
            data = payloads[split][case_id]
            if path.is_symlink():
                raise ValueError("Fixture file cannot be linked")
            if path.exists():
                if path.stat().st_size != len(data) or path.read_bytes() != data:
                    raise ValueError("Resumed fixture changed")
            else:
                pending.append((path, data))
            split_paths[case_id] = path
        paths[split] = split_paths
    for split in phases:
        (root / ("fixtures-" + split)).mkdir(parents=True, exist_ok=True)
    for path, data in pending:
        path.write_bytes(data)
    return paths, total_bytes


def _variant_cases(variant, cases):
    from cloud_experiments import contenders
    metadata = contenders.metadata(variant)
    scopes = metadata["implementation_scopes"]
    owners = set(metadata["decisions"])
    if variant == "baseline" or "build_factor" in scopes:
        return cases
    if "prepared_stage_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "prepared"]
    if "logical_grid_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "table"]
    if "html_table_stage_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "html-table"]
    return [c for c in cases if owners.intersection(c["owners"])]


def screen_plan(variants, panels, cases, *, cross_cells=()):
    """Declare shared interaction cohorts before observing timings."""
    from cloud_experiments import contenders
    if not variants or len(set(variants)) != len(variants) or "baseline" not in variants:
        raise ValueError("Screen requires distinct variants including baseline")
    own = {v: {c["id"] for c in _variant_cases(v, cases)} for v in variants}
    assignments = {v: set(ids) for v, ids in own.items()}
    cohorts, missing = {}, {}
    for panel, cells in panels.items():
        missing[panel] = [v for v in cells if v not in own]
        if missing[panel]:
            continue
        local = [v for v in cells if v != "baseline"
                 and "build_factor" not in contenders.metadata(v)["implementation_scopes"]]
        scope = set().union(*(own[v] for v in (local or cells)))
        if not scope:
            raise ValueError("Interaction panel has no declared workload: " + panel)
        cohorts[panel] = sorted(scope)
        for cell in cells:
            assignments[cell].update(scope)
    cross_scope = sorted(c["id"] for c in cases if c["stage"] not in ("prepared", "table", "html-table"))
    if cross_cells and not cross_scope:
        raise ValueError("Cross-path coverage has no actual end-to-end workload")
    for cell in cross_cells:
        if cell in assignments:
            assignments[cell].update(cross_scope)
    return {"assignments": {v: sorted(ids) for v, ids in assignments.items()},
            "panel_cases": cohorts, "missing_cells": missing,
            "cross_cases": cross_scope if cross_cells else [],
            "missing_cross_cells": [v for v in cross_cells if v not in own],
            "policy": "Local-factor cohort union per complete panel; preserve each build factor's full independent scope"}


def profile_analysis(rounds, settings, draws=512):
    """Resource-only shape expansion; these values never enter scientific rows."""
    blocks, iterations = settings["blocks"], settings["iterations"]
    full = [{arm: [rounds[b % len(rounds)][arm][i % len(rounds[b % len(rounds)][arm])]
                   for i in range(iterations)] for arm in ("baseline", "candidate")} for b in range(blocks)]
    peaks = [{arm: [values[arm][0]] for arm in ("baseline", "candidate")} for values in full]
    setup_start = time.perf_counter()
    bootstrap_intervals(full, [.5, .95, .99], 2, 938201, .25)
    bootstrap_intervals(peaks, [.5], 2, 938201, .25)
    setup_seconds = time.perf_counter() - setup_start
    durations = []
    for repeat in range(3):
        start = time.perf_counter()
        bootstrap_intervals(full, [.5, .95, .99], draws, 938201 + repeat, .25)
        bootstrap_intervals(peaks, [.5], draws, 938201 + repeat, .25)
        durations.append(time.perf_counter() - start)
    return {"blocks": blocks, "iterations": iterations, "draws_per_repeat": draws,
            "setup_wall_seconds": setup_seconds, "repeat_wall_seconds": durations, "seconds_per_case_draw_max": max(durations) / draws,
            "interval_backend": "exact-order-statistic-tails/1", "resource_only_tail": .25,
            "not_scientific_samples": True,
            "scope": "Expanded pilot integers profile actual CPU+RSS analyzer shape only; no extra independent timing observations or precision proof"}


def forecast_native(variants, phases, design, pilot, profile):
    from cloud_experiments import contenders
    owner_cases = {}
    for variant in variants:
        if variant == "baseline":
            continue
        count = len(_variant_cases(variant, phases["confirm"]))
        for owner in contenders.metadata(variant)["decisions"]:
            owner_cases[owner] = max(owner_cases.get(owner, 0), count)
    maximum_cases = min(sum(owner_cases.values()) * design["selection"]["slots_per_owner"],
                        (len(variants) - 1) * len(phases["confirm"]))
    # A predeclared combined finalist adds a full affected path; do not budget only individual winners.
    joint_cases = len(phases["confirm"]) if len(owner_cases) > 1 else 0
    maximum_cases += joint_cases
    if maximum_cases < 1 or profile.get("not_scientific_samples") is not True:
        raise ValueError("Resource forecast requires actual contender scopes and resource-only full-shape profile")
    maximum_family = maximum_cases * len(design["confirm"]["metrics"])
    draws = max(30000, math.ceil(100 * 2 * maximum_family / design["confirm"]["alpha"]))
    assignments = screen_plan(variants, native_matrix()[1], phases["screen"],
                              cross_cells=design.get("native_design", {}).get("cross_cells", []))["assignments"]
    screen_pairs = sum(len(ids) for v, ids in assignments.items() if v != "baseline")
    denominator = len(phases["screen"]) * 2 * 13
    scale = (screen_pairs * design["screen"]["blocks"] * (design["screen"]["iterations"] + 10)
             + maximum_cases * design["confirm"]["blocks"] * (design["confirm"]["iterations"] + 10)) / denominator
    limits = design["limits"]
    largest_cases = max(len(_variant_cases(v, phases["confirm"])) for v in variants)
    screening_memory = screen_pairs * design["screen"]["blocks"] * design["screen"]["iterations"] * 2 * 64
    confirmation_memory = largest_cases * design["confirm"]["blocks"] * design["confirm"]["iterations"] * 2 * 1024
    peak_memory = max(2 * pilot["peak_memory_bytes"], screening_memory, confirmation_memory)
    name_lengths = {}
    for name, definition in contenders.definitions().items():
        for factor in definition["factors"]:
            name_lengths[factor] = max(name_lengths.get(factor, 0), len(name))
    longest_name = "x" * max(len("baseline"), sum(name_lengths.values()) + len(name_lengths) - 1)
    longest_case = max((case["id"] for cases in phases.values() for case in cases), key=lambda name: len(canonical(name)))
    row = {"protocol": PROTOCOL, "study_id": "a" * 64, "freeze_id": "b" * 64,
           "variant": longest_name, "case": longest_case, "phase": "confirm", "arm": "candidate",
           "block": max(design["confirm"]["blocks"], design["screen"]["blocks"]) - 1,
           "iteration": max(design["confirm"]["iterations"], design["screen"]["iterations"]) - 1,
           "order": 1, "ns": 2 ** 64 - 1, "offset_ns": 2 ** 64 - 1,
           "fingerprint": "c" * 64, "session_id": "d" * 32}
    row_bytes = len(canonical(row)) + 1
    rss_row = {key: value for key, value in row.items() if key not in ("iteration", "ns", "offset_ns", "fingerprint")}
    rss_row.update(kind="rss", value=2 ** 64 - 1, unit="KiB")
    rss_bytes = len(canonical(rss_row)) + 1
    screen_rows = sum(map(len, assignments.values())) * design["screen"]["blocks"] * design["screen"]["iterations"] * 2
    confirm_rows = maximum_cases * design["confirm"]["blocks"] * design["confirm"]["iterations"] * 2
    pilot_rows = len(phases["screen"]) * 2 * 3 * 2
    required_evidence = (screen_rows + confirm_rows + pilot_rows) * row_bytes + maximum_cases * design["confirm"]["blocks"] * 2 * rss_bytes
    metadata = limits["metadata_bytes"] + limits["log_bytes"]
    _, fixture_bytes = _fixture_payloads(phases, limits)
    export = limits["evidence_bytes"] + metadata + limits["analysis_bytes"] + limits["binary_bytes"] + fixture_bytes + 64 * 1024 ** 2
    export_seconds = math.ceil(export / (1024 ** 2)) + 600
    maximum_finalists = min(len(variants) - 1, len(owner_cases) * design["selection"]["slots_per_owner"]) + bool(joint_cases)
    service_reserve = maximum_finalists * limits["command_seconds"]
    return {"wall_seconds": max(1, math.ceil(2 * (3 * pilot["wall_seconds"] * scale
                        + pilot["baseline_build_wall_seconds"] * (len(variants) - 1 + bool(joint_cases)))) + service_reserve),
            "finalist_service_gate_seconds_reserve": service_reserve,
            "service_reserve_scope": "One configured command-time reserve per possible finalist; not measured service throughput or an independent hard deadline",
            "analysis_seconds": max(1, math.ceil(2 * (profile["seconds_per_case_draw_max"] * draws * maximum_cases
                          + profile.get("setup_wall_seconds", 0) * len(owner_cases)))),
            "export_seconds": export_seconds, "shutdown_seconds": 600,
            "export_seconds_scope": "Planning reserve at 1 MiB/s encoded archive output plus ten-minute shutdown; replace with measured pilot throughput, not a guaranteed lower bound",
            "evidence_bytes": design["limits"]["evidence_bytes"], "build_bytes": design["limits"]["build_bytes"],
            "fixture_bytes": fixture_bytes,
            "metadata_bytes": metadata, "analysis_bytes": limits["analysis_bytes"], "export_bytes": export,
            "peak_memory_bytes": peak_memory, "memory_reserve_bytes": limits["memory_reserve_bytes"],
            "required_evidence_bytes": required_evidence, "maximum_row_bytes": row_bytes,
            "evidence_scope": "Encoded row upper bound from declared grids, largest compatible factor-name bound, u64 timings and fixed session/digest widths",
            "memory_scope": "Measured pilot/kernel peak with 2x headroom plus conservative collector/analyzer object budgets; not a hard peak prediction",
            "replicas": 1, "maximum_confirm_cases": maximum_cases, "maximum_family": maximum_family,
            "maximum_bootstrap_draws": draws,
            "limit": "Conservative supplied-scope forecast, not a runtime guarantee; confirmation counts and analysis costs included, no precision claim"}


def compose_finalist(selected):
    from cloud_experiments import contenders
    for variant in selected:
        contenders.metadata(variant)
    parts = {part for variant in selected for part in variant.split("+") if part != "baseline"}
    name = "+".join(part for part in contenders.definitions() if part in parts)
    contenders.metadata(name)
    return name


def _oracles(run, binary, baseline, cases, paths, *, header_mode="explicit"):
    from cloud_experiments import workloads
    if header_mode not in ("explicit", "scope"):
        raise ValueError("Unsupported HTML header oracle mode")
    if header_mode == "scope" and any(case["stage"] != "html-table" for case in cases):
        raise ValueError("Scope header oracle is limited to HTML table cases")
    for case in cases:
        actual = _invoke(run, binary, case, paths[case["id"]], 0, None)
        reference = _invoke(run, baseline, case, paths[case["id"]], 0, None)
        if header_mode == "explicit" and actual != reference:
            raise ValueError("Canonical output parity failed: " + case["id"])
        errors = workloads.envelope_errors(actual, case["fixture"], header_mode=header_mode)
        if errors:
            raise ValueError("Independent contract oracle failed: " + case["id"] + ": " + "; ".join(errors))
        if case["stage"] in ("export", "pipeline") and "expected_error" not in case["fixture"]:
            protocol.validate_aae(actual)
    # Independent literal/content/math/denial oracles are distinct from baseline parity.
    control_case = {"id": "controls", "stage": "controls"}
    control = json.loads(_invoke(run, binary, control_case, next(iter(paths.values())), 0, None))
    if control.get("status") != "PASS":
        raise ValueError("Independent native controls failed")
    runner.probe_oracles(run, binary)
    oracle_dir = run.directory / ("oracle-" + binary.name)
    oracle_dir.mkdir(exist_ok=True)
    runner.content_oracles(run, binary, oracle_dir, include_pre=True)
    return {"parity_cases": len(cases) if header_mode == "explicit" else 0,
            "intentional_output_difference_cases": len(cases) if header_mode == "scope" else 0,
            "independent_native_controls": control,
            "limit": "Finite independent oracles and differential outputs; no exhaustive correctness claim"}


def _collection(run):
    if run.collection is None:
        context = {"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"],
                   "environment": run.manifest.get("environment_binding"),
                   "source_sha256": run.manifest["spec"].get("source_sha256")}
        limit = run.manifest["spec"].get("limits", {}).get("evidence_bytes", 4 * 1024 ** 3)
        run.collection = lifecycle.CollectionJournal(run.directory, context, limit, run.session_id,
            metadata_limit=run.manifest["spec"].get("limits", {}).get("metadata_bytes", 64 * 1024 ** 2),
            block_limit=run.manifest["spec"].get("limits", {}).get("block_bytes", 256 * 1024 ** 2))
    return run.collection


def _collection_binding(binaries, cases, settings):
    return {"binaries": {name: protocol.sha256_file(path / "core_probe") for name, path in binaries.items()},
            "fixtures": {case["id"]: identity(case) for case in cases}, "settings": settings}


def _publish_collection(run):
    run.manifest.update(collection_layout="atomic-blocks/1", sample_shards=run.collection.shards,
                        sample_bytes=run.collection.total_bytes)
    run.save()


def _measure(run, binaries, variant, cases, paths, phase, blocks, iterations, cpu, freeze_id=None):
    journal = _collection(run)
    design = run.manifest["spec"]
    binding = _collection_binding({v: binaries[v] for v in {"baseline", variant}}, cases,
                                  {"blocks": blocks, "iterations": iterations, "cpu": cpu, "freeze_id": freeze_id,
                                   "randomization": design.get("randomization")})
    groups = {c["id"]: [{"baseline": [], "candidate": []} for _ in range(blocks)] for c in cases}
    rss, fingerprints = {}, {}
    policy = design.get("randomization")
    order_seed = (experimental_design.derive_seed(policy["seed"], "measure-case-order", phase) if policy
                  else 458729 if phase != "confirm" else 938201)
    order_rng = random.Random(order_seed)
    for block in range(blocks):
        ordered = list(cases)
        order_rng.shuffle(ordered)
        def produce():
            for case in ordered:
                for order in range(2):
                    start = experimental_design.arm_start(design, variant, case["id"], phase)
                    arm = ("baseline", "candidate")[order ^ (block % 2) ^ start]
                    binary = binaries["baseline" if arm == "baseline" else variant]
                    packet = _invoke(run, binary, case, paths[case["id"]], iterations, cpu)
                    for i, ns in enumerate(packet["raw_ns"]):
                        yield {"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"], "freeze_id": freeze_id,
                               "variant": variant, "case": case["id"], "phase": phase, "arm": arm,
                               "block": block, "iteration": i, "order": order, "ns": ns,
                               "offset_ns": packet["offsets_ns"][i], "fingerprint": packet["fingerprint"]}
                    if phase == "confirm":
                        rss_path = run.directory / "rss-current.txt"
                        run.run(["/usr/bin/time", "-f", "%M", "-o", rss_path, "taskset", "-c", cpu,
                                 binary / "core_probe", case["stage"], paths[case["id"]], 1], timeout=180, log_output=False)
                        yield {"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"], "freeze_id": freeze_id,
                               "variant": variant, "case": case["id"], "phase": phase, "kind": "rss", "block": block,
                               "arm": arm, "order": order, "value": protocol.parse_peak_rss(rss_path.read_text()), "unit": "KiB"}
        descriptor = {"channel": phase + ":" + variant, "binding": binding, "block": block}
        offsets = {}
        for row in journal.collect(descriptor, produce):
            case_id, arm = row["case"], row["arm"]
            if row.get("kind") == "rss":
                rss.setdefault(case_id, {}).setdefault(block, {})[arm] = row["value"]
                continue
            key = (case_id, arm)
            slot = groups[case_id][block][arm]
            start = experimental_design.arm_start(design, variant, case_id, phase)
            if (row["block"] != block or row["iteration"] != len(slot) or row["order"] != ((0 if arm == "baseline" else 1) ^ (block % 2) ^ start)
                    or not positive(row["ns"]) or row["offset_ns"] <= offsets.get(key, -1)):
                raise ValueError("Invalid committed balanced block")
            if fingerprints.setdefault(case_id, row["fingerprint"]) != row["fingerprint"]:
                raise ValueError("Balanced block consumed different outputs")
            offsets[key] = row["offset_ns"]
            slot.append(row["ns"])
        _publish_collection(run)
    if any(len(pair[arm]) != iterations for rounds in groups.values() for pair in rounds for arm in ("baseline", "candidate")):
        raise ValueError("Incomplete balanced collection")
    return groups, rss


def _interleaved_screen(run, binaries, assignments, cases, paths, cpu):
    design = run.manifest["spec"]
    settings = design["screen"]
    events = screen_schedule(assignments, settings["blocks"], settings["seed"])
    campaign.write_json(run.directory / "screen-schedule.json", events)
    lookup = {c["id"]: c for c in cases}
    groups = {v: {c: [{"baseline": [], "candidate": []} for _ in range(settings["blocks"])] for c in ids}
              for v, ids in assignments.items()}
    fingerprints = {}
    journal = _collection(run)
    binding = _collection_binding(binaries, cases, {"settings": settings, "assignments": assignments, "cpu": cpu,
                                                    "randomization": design.get("randomization")})
    for block in range(settings["blocks"]):
        def produce():
            for _, case_id, variant in (event for event in events if event[0] == block):
                for order in range(2):
                    start = experimental_design.arm_start(design, variant, case_id, "screen")
                    arm = ("baseline", "candidate")[order ^ (block % 2) ^ start]
                    packet = _invoke(run, binaries["baseline" if arm == "baseline" else variant], lookup[case_id],
                                     paths[case_id], settings["iterations"], cpu)
                    for i, ns in enumerate(packet["raw_ns"]):
                        yield {"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"], "variant": variant,
                               "case": case_id, "phase": "screen", "arm": arm, "block": block, "iteration": i,
                               "order": order, "ns": ns, "offset_ns": packet["offsets_ns"][i], "fingerprint": packet["fingerprint"]}
        descriptor = {"channel": "screen:all-cells", "binding": binding, "block": block}
        offsets = {}
        for row in journal.collect(descriptor, produce):
            variant, case_id, arm = row.get("variant"), row.get("case"), row.get("arm")
            if (variant not in groups or case_id not in groups[variant] or arm not in ("baseline", "candidate")
                    or row.get("protocol") != PROTOCOL or row.get("study_id") != run.manifest["bundle_id"]
                    or row.get("phase") != "screen"):
                raise ValueError("Undeclared screening cell or arm")
            key = (variant, case_id)
            if (not protocol.is_digest(row.get("fingerprint"))
                    or fingerprints.setdefault(key, row["fingerprint"]) != row["fingerprint"]):
                raise ValueError("Interleaved cell changed its consumed output")
            slot = groups[variant][case_id][block][arm]
            start = experimental_design.arm_start(design, variant, case_id, "screen")
            offset_key = (variant, case_id, arm)
            if (type(row.get("iteration")) is not int or row["iteration"] != len(slot) or row.get("block") != block
                    or row.get("order") != ((0 if arm == "baseline" else 1) ^ (block % 2) ^ start)
                    or type(row.get("ns")) is not int or row["ns"] <= 0
                    or type(row.get("offset_ns")) is not int or row["offset_ns"] <= offsets.get(offset_key, -1)):
                raise ValueError("Invalid screening order, timing or native offsets")
            offsets[offset_key] = row["offset_ns"]
            slot.append(row["ns"])
        run.manifest.update(current_phase="interleaved-screen", current_macroblock=block)
        _publish_collection(run)
    if any(len(pair[arm]) != settings["iterations"] for variants in groups.values() for rounds in variants.values()
           for pair in rounds for arm in ("baseline", "candidate")):
        raise ValueError("Incomplete interleaved cell panel")
    return groups


def interaction_report(groups, panels, case_scopes=None):
    from cloud_experiments import contenders
    reference = contenders.metadata("baseline")["factors"]
    report = {"protocol": PROTOCOL, "phase": "screen", "decision": "EXPLORATORY_ONLY", "panels": {},
              "scope": "Common-macroblock log quantile contrasts, including three-way terms where cells exist; no confirmatory interval, significance or adoption claim"}
    for panel, cells in panels.items():
        missing = [cell for cell in cells if cell not in groups]
        record = {"status": "INCOMPLETE" if missing else "COMPLETE_CELLS", "missing_cells": missing, "contrasts": []}
        report["panels"][panel] = record
        if missing:
            continue
        factors = {cell: contenders.metadata(cell)["factors"] for cell in cells}
        axes = sorted(k for k in reference if len({factors[cell][k] for cell in cells}) > 1)
        levels = {axis: sorted({factors[cell][axis] for cell in cells}) for axis in axes}
        lookup = {tuple(factors[cell][k] for k in axes): cell for cell in cells}
        if len(lookup) != math.prod(len(levels[k]) for k in axes):
            raise ValueError("Declared interaction panel is not a complete factorial")
        if case_scopes is None:
            cases = sorted(set().union(*(set(groups[cell]) for cell in cells if cell != "baseline")))
        else:
            cases = case_scopes.get(panel)
            if not cases or len(set(cases)) != len(cases):
                raise ValueError("Missing/duplicate predeclared interaction cohorts: " + panel)
        record["cohorts"] = list(cases)
        if any(case not in groups[cell] for cell in cells for case in cases):
            raise ValueError("Interaction cells do not share the required cohorts")
        first_order_only = len(axes) == 1
        for order in range(1 if first_order_only else 2, len(axes) + 1):
            for changed in itertools.combinations(axes, order):
                others = [k for k in axes if k not in changed]
                for other_values in itertools.product(*(levels[k] for k in others)):
                    for new_values in itertools.product(*([v for v in levels[k] if v != reference[k]] for k in changed)):
                        corners = []
                        for bits in itertools.product((0, 1), repeat=order):
                            point = {**dict(zip(others, other_values)), **{k: new if bit else reference[k] for k, new, bit in zip(changed, new_values, bits)}}
                            corners.append((lookup[tuple(point[k] for k in axes)], (-1) ** (order - sum(bits))))
                        for case in cases:
                            count = len(groups[corners[0][0]][case])
                            if count < 2 or any(len(groups[cell][case]) != count for cell, _ in corners):
                                raise ValueError("Interaction cells lack common macroblocks")
                            for q in (.5, .95, .99):
                                values = []
                                for block in range(count):
                                    costs = [(protocol.quantile(groups[cell][case][block]["candidate"], q), sign) for cell, sign in corners]
                                    if any(not positive(value) for value, _ in costs):
                                        raise ValueError("Invalid interaction observation")
                                    values.append(math.fsum(sign * math.log(value) for value, sign in costs))
                                contrast = {"kind": "main_effect" if order == 1 else "interaction",
                                    "axes": list(changed), "levels": dict(zip(changed, new_values)),
                                    "stratum": dict(zip(others, other_values)), "case": case, "quantile": q,
                                    "cells": [cell for cell, _ in corners], "block_log_contrasts": values,
                                    "mean_log_contrast": math.fsum(values) / count}
                                if order >= 2:
                                    contrast["mean_log_interaction"] = contrast["mean_log_contrast"]
                                record["contrasts"].append(contrast)
    return report


def analyze_groups(groups, rss, *, family, draws, alpha, seed):
    """Reuse frozen decision/drift rules; only replace the unbounded bootstrap allocator."""
    result = {"protocol": PROTOCOL, "family_size": family, "family_alpha": alpha, "bootstrap_draws": draws,
              "analysis_policy": "paired-rounds/2.1-batched-memory", "seed": seed, "cases": {},
              "result_authenticity": AUTHENTICITY, "overall_decision": "HOLD"}
    count = sum(3 + (case in rss) for case in groups)
    if family < count or draws < max(30000, math.ceil(100 * 2 * family / alpha)):
        raise ValueError("Undeclared family or inadequate Monte Carlo tail resolution")
    every_interval, all_warnings = {}, []
    for case, rounds in groups.items():
        targets = [(case, rounds, [.5, .95, .99], "ns")]
        if case in rss:
            peaks = rss[case]
            if sorted(peaks) != list(range(len(rounds))) or any(set(v) != {"baseline", "candidate"} for v in peaks.values()):
                raise ValueError("Incomplete process-RSS pairing")
            targets.append((case + "/rss", [{a: [peaks[b][a]] for a in ("baseline", "candidate")} for b in range(len(rounds))], [.5], "KiB"))
        for name, values, quantiles, unit in targets:
            summaries = {}
            warnings = []
            for arm in ("baseline", "candidate"):
                flat = [x for pair in values for x in pair[arm]]
                medians = [protocol.quantile(pair[arm], .5) for pair in values]
                lag = protocol.lag1_autocorrelation(medians)
                summaries[arm] = {"samples": len(flat), "maximum_observed": max(flat),
                                  "quantiles": {str(q): protocol.quantile(flat, q) for q in quantiles}, "round_median_lag1": lag}
                if lag is not None and abs(lag) >= .5:
                    warnings.append(arm + ": block drift")
            for q in quantiles:
                lag = protocol.lag1_autocorrelation([protocol.quantile(pair["candidate"], q) - protocol.quantile(pair["baseline"], q) for pair in values])
                if lag is not None and abs(lag) >= .5:
                    warnings.append(f"paired delta q={q}: block drift")
            if len(values) < 20 or (.99 in quantiles and summaries["baseline"]["samples"] * .01 < 50):
                warnings.append("Insufficient declared block/tail minimum")
            tail = alpha / (2 * family)
            intervals = {str(q): bounds for q, bounds in bootstrap_intervals(values, quantiles, draws, seed, tail).items()}
            verdict = protocol.interval_decision(intervals)
            if unit == "KiB" and summaries["candidate"]["maximum_observed"] > summaries["baseline"]["maximum_observed"]:
                warnings.append("Adverse observed maximum RSS")
            if warnings and verdict != "REJECT":
                verdict = "HOLD"
            result["cases"][name] = {"decision": verdict, "unit": unit, **summaries, "delta_intervals": intervals, "warnings": warnings}
            every_interval.update({name + ":" + q: value for q, value in intervals.items()})
            all_warnings.extend(warnings)
    performance = protocol.interval_decision(every_interval)
    if all_warnings and performance != "REJECT":
        performance = "HOLD"
    result["performance_decision"] = performance
    return result


def load_confirmation(directory, variant):
    directory = Path(directory).resolve(strict=True)
    def owned(name):
        path = directory / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name or path.is_symlink()
                or not path.is_file() or not path.resolve().is_relative_to(directory)):
            raise ValueError("Missing/escaped evidence input: " + name)
        return path
    manifest = json.loads(owned("manifest.json").read_text(encoding="utf-8"))
    frozen = json.loads(owned("confirmation-freeze.json").read_text(encoding="utf-8"))
    spec = manifest.get("spec", {})
    freeze_id = frozen.get("freeze_id")
    frozen_design = frozen.get("design", {})
    if (not isinstance(frozen_design, dict) or any(frozen_design.get(key) != value for key, value in spec.items())
            or set(frozen_design) - set(spec) - {"confirm_assignments", "confirm_binaries", "confirm_definitions"}
            or frozen.get("fixtures") != spec.get("fixtures")):
        raise ValueError("Confirmation changed the predeclared study design or fixture inventory")
    if (manifest.get("protocol") != PROTOCOL or spec.get("protocol") != PROTOCOL
            or not protocol.is_digest(manifest.get("bundle_id")) or spec.get("study_id") != manifest["bundle_id"]
            or identity({k: v for k, v in spec.items() if k != "study_id"}) != manifest["bundle_id"]
            or identity({k: v for k, v in frozen.items() if k != "freeze_id"}) != freeze_id
            or frozen.get("design", {}).get("study_id") != manifest["bundle_id"]
            or frozen["design"].get("source_sha256") != spec.get("source_sha256")
            or variant not in frozen.get("variants", [])):
        raise ValueError("Frozen source/study/confirmation identity mismatch")
    rebuilt = freeze_confirmation(frozen["design"], frozen["variants"], frozen["pilot"], frozen["fixtures"])
    if rebuilt != frozen:
        raise ValueError("Frozen family/settings/assignments were altered")
    binary_inputs = {}
    for name in ("baseline", variant):
        hashes = frozen["design"].get("confirm_binaries", {}).get(name)
        if not isinstance(hashes, dict):
            raise ValueError("Missing frozen native binary identity: " + name)
        key = "core_probe" if "core_probe" in hashes else "core_probe.exe"
        expected = hashes.get(key)
        path = owned("bins/" + name + "/" + key)
        if not protocol.is_digest(expected) or protocol.sha256_file(path) != expected:
            raise ValueError("Actual native binary identity changed: " + name)
        binary_inputs[path.relative_to(directory).as_posix()] = expected
    case_ids = frozen["assignments"][variant]
    settings = frozen["design"]["confirm"]
    rows = {case: [] for case in case_ids}
    rss = {case: {} for case in case_ids}
    shards = manifest.get("sample_shards", [])
    actual = (sorted(p.relative_to(directory).as_posix() for p in (directory / "collection").glob("*/samples.jsonl")
                     if not p.parent.name.startswith(".pending-"))
              if manifest.get("collection_layout") == "atomic-blocks/1" else sorted(p.name for p in directory.glob("samples-*.jsonl")))
    if not shards or len(set(shards)) != len(shards) or sorted(shards) != actual:
        raise ValueError("Raw shard inventory is missing, duplicate or altered")
    raw_inputs = {}
    for name in shards:
        path = owned(name)
        raw_digest = hashlib.sha256()
        receipt = None
        if manifest.get("collection_layout") == "atomic-blocks/1":
            receipt = lifecycle.read_json(path.parent / "receipt.json")
            descriptor = receipt.get("descriptor", {})
            expected_context = {"protocol": PROTOCOL, "study_id": manifest["bundle_id"],
                                "environment": manifest.get("environment_binding"), "source_sha256": spec.get("source_sha256")}
            if (receipt.get("context") != expected_context or identity(descriptor) != path.parent.name
                    or receipt.get("version") != "athena-collection-block/1"):
                raise ValueError("Raw collection receipt identity changed")
        row_count = 0
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536 or not line.endswith(b"\n"):
                    raise ValueError("Truncated/oversized raw observation")
                raw_digest.update(line)
                row = json.loads(line)
                row_count += 1
                if receipt is not None and (row.get("session_id") != receipt.get("session_id")
                        or row.get("block") != descriptor.get("block")):
                    raise ValueError("Raw balanced block changed its session or macroblock")
                if row.get("protocol") != PROTOCOL or row.get("study_id") != manifest["bundle_id"]:
                    raise ValueError("Raw observation came from another study/protocol")
                if row.get("variant") != variant or row.get("phase") != "confirm":
                    continue
                case = row.get("case")
                if row.get("freeze_id") != freeze_id or case not in rows:
                    raise ValueError("Wrong freeze or undeclared confirmation cohort")
                if row.get("kind") is None:
                    rows[case].append(row)
                elif row.get("kind") == "rss":
                    block, arm = row.get("block"), row.get("arm")
                    if (type(block) is not int or not 0 <= block < settings["blocks"]
                            or arm not in ("baseline", "candidate") or row.get("unit") != "KiB"
                            or type(row.get("value")) is not int or row["value"] <= 0
                            or row.get("order") != ((0 if arm == "baseline" else 1) ^ (block % 2)
                                ^ experimental_design.arm_start(frozen_design, variant, case, "confirm"))
                            or arm in rss[case].get(block, {})):
                        raise ValueError("Invalid/duplicate process RSS observation")
                    rss[case].setdefault(block, {})[arm] = row["value"]
                else:
                    raise ValueError("Unknown confirmation metric row")
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Raw observations changed during analysis input loading")
        raw_inputs[name] = raw_digest.hexdigest()
        if receipt is not None and (receipt.get("sha256") != raw_inputs[name] or receipt.get("bytes") != before.st_size
                                    or receipt.get("rows") != row_count):
            raise ValueError("Raw collection bytes do not match their commit receipt")
    groups = {case: paired_blocks(values, study_id=manifest["bundle_id"], variant=variant, case=case,
        phase="confirm", blocks=settings["blocks"], iterations=settings["iterations"],
        order_offset=experimental_design.arm_start(frozen_design, variant, case, "confirm")) for case, values in rows.items()}
    if any(sorted(peaks) != list(range(settings["blocks"])) or any(set(p) != {"baseline", "candidate"} for p in peaks.values()) for peaks in rss.values()):
        raise ValueError("Missing process RSS observations")
    evidence = {"study_id": manifest["bundle_id"], "freeze_id": freeze_id, "variant": variant,
                "family": frozen["family_size"], "draws": frozen["bootstrap_draws"], "alpha": settings["alpha"],
                "raw_inputs": raw_inputs, "binary_inputs": binary_inputs,
                "confirmation_freeze_sha256": protocol.sha256_file(owned("confirmation-freeze.json")),
                "result_authenticity": AUTHENTICITY}
    policy = frozen_design.get("randomization")
    evidence["bootstrap_seed"] = (experimental_design.derive_seed(policy["seed"], "confirmation-bootstrap")
                                  if policy else 938201)
    return groups, rss, evidence


def analyze_confirmation(directory, variant, output):
    directory = Path(directory).resolve(strict=True)
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Analysis output cannot be a symlink")
    output = output.parent.resolve(strict=True) / output.name
    progress_path = output.with_name(output.stem + ".analysis-progress.json")
    lock_path = output.with_name(output.stem + ".analysis.lock")
    with _analysis_lock(lock_path):
        groups, rss, evidence = load_confirmation(directory, variant)
        protected = {directory / name for name in ("manifest.json", "confirmation-freeze.json",
                     *evidence["raw_inputs"], *evidence["binary_inputs"])}
        if output in protected or progress_path in protected:
            raise ValueError("Analysis output would replace required evidence")
        try:
            import numpy as np
        except ImportError:
            np = None
        context = {"version": "athena-confirmation-analysis/1", "evidence": evidence,
                   "case_order": list(groups), "seed": evidence["bootstrap_seed"],
                   "sources": {"research.py": protocol.sha256_file(__file__),
                               "experimental_design.py": protocol.sha256_file(experimental_design.__file__),
                               "campaign.py": protocol.sha256_file(campaign.__file__),
                               "protocol.py": protocol.sha256_file(protocol.__file__)},
                   "runtime": {"python": sys.version, "platform": platform.platform(),
                               "numpy": None if np is None else np.__version__}}
        binding = identity(context)
        bound = 8 * 1024 ** 2
        progress = {"version": context["version"], "binding": binding, "context": context,
                    "status": "RUNNING", "completed_cases": [], "results": {}}
        if progress_path.is_symlink():
            raise ValueError("Analysis checkpoint cannot be a symlink")
        if progress_path.exists():
            with progress_path.open("rb") as stream:
                data = stream.read(bound + 1)
            if len(data) > bound:
                raise ValueError("Analysis checkpoint exceeds its byte bound")
            progress = json.loads(data)
            if (not isinstance(progress, dict) or progress.get("version") != context["version"]
                    or progress.get("binding") != binding or progress.get("context") != context
                    or progress.get("status") not in ("RUNNING", "COMPLETE")):
                raise ValueError("Analysis checkpoint does not match raw inputs, frozen design, code or runtime")
            completed = progress.get("completed_cases")
            results = progress.get("results")
            if (not isinstance(completed, list) or completed != list(groups)[:len(completed)]
                    or not isinstance(results, dict) or set(results) != set(completed)
                    or (progress["status"] == "COMPLETE" and completed != list(groups))):
                raise ValueError("Analysis checkpoint has incomplete or unordered case results")
            for case, entry in results.items():
                names = {case, case + "/rss"} if case in rss else {case}
                if (not isinstance(entry, dict) or not isinstance(entry.get("cases"), dict)
                        or set(entry["cases"]) != names or entry.get("digest") != identity(entry["cases"])):
                    raise ValueError("Analysis checkpoint result digest or case scope mismatch")
        settings = {"family": evidence["family"], "draws": evidence["draws"],
                    "alpha": evidence["alpha"], "seed": context["seed"]}
        result = analyze_groups({}, {}, **settings)
        for case, rounds in groups.items():
            if case not in progress["results"]:
                partial = analyze_groups({case: rounds}, {case: rss[case]} if case in rss else {}, **settings)
                progress["results"][case] = {"cases": partial["cases"], "digest": identity(partial["cases"])}
                progress["completed_cases"].append(case)
                campaign.write_json(progress_path, progress, max_bytes=bound)
            result["cases"].update(progress["results"][case]["cases"])
        intervals = {name + ":" + q: bounds for name, item in result["cases"].items()
                     for q, bounds in item["delta_intervals"].items()}
        performance = protocol.interval_decision(intervals)
        if any(item["warnings"] for item in result["cases"].values()) and performance != "REJECT":
            performance = "HOLD"
        result.update(performance_decision=performance, evidence=evidence, analysis_context=context)
        campaign.write_json(output, result)
        progress["status"] = "COMPLETE"
        campaign.write_json(progress_path, progress, max_bytes=bound)
        return result


def _complete_finalist_selection(run, selected, chosen, binaries, *, workspace, files, cache, cases, paths):
    from cloud_experiments import contenders
    joint = None
    try:
        joint = compose_finalist(selected)
    except ValueError as error:
        chosen["combined_finalist"] = {"status": "INCOMPATIBLE_FACTORS", "detail": str(error)[:1024]}
    if joint is not None and joint not in selected:
        run.manifest.update(current_phase="build-combined-finalist", current_variant=joint)
        if joint not in binaries:
            run.manifest["planned_variants"].append(joint)
            binaries[joint] = _build(run, workspace, files, joint, cache)
            joint_header_mode = "scope" if contenders.metadata(joint)["factors"].get("html_headers") == "scope" else "explicit"
            run.check("oracle:" + joint, lambda: _oracles(run, binaries[joint], binaries["baseline"],
                _variant_cases(joint, cases), paths, header_mode=joint_header_mode))
        selected.append(joint)
    if joint is not None:
        chosen["combined_finalist"] = {"variant": joint, "rule": "Union of compatible owner-selected factors, fixed before confirmation; not an observed sum of speedups"}
    chosen["finalist_service_contracts"] = {}
    for variant in selected:
        scopes = contenders.metadata(variant)["implementation_scopes"]
        if scopes in (["prepared_stage_only"], ["logical_grid_only"], ["html_table_stage_only"]):
            chosen["finalist_service_contracts"][variant] = {"status": "NOT_APPLICABLE", "scope": scopes}
            continue
        def contracts():
            folder = run.directory / ("finalist-service-" + identity(variant)[:12] + "-" + run.session_id)
            report = execute_service(binaries[variant] / "service_probe", folder)
            if report["status"] != "CONTRACTS_PASS_WITHIN_SCOPE":
                raise RuntimeError("Finalist service contract gate failed; see actual service-contracts.json")
            return {"status": report["status"], "scenarios": len(report["scenarios"]),
                    "artifact": folder.name + "/service-contracts.json",
                    "sha256": protocol.sha256_file(folder / "service-contracts.json")}
        key = "combined_service_contracts" if variant == joint else "finalist_service_contracts:" + variant
        chosen["finalist_service_contracts"][variant] = run.check(key, contracts)


def execute_native(source, spec_path, output, *, budget_path=None, launch_receipt=None,
                   pilot_only=False, variants=None, resume=False):
    if launch_receipt is None:
        raise ValueError("Explicit launch receipt is required before native execution")
    if budget_path is None:
        raise ValueError("Reviewed budget is required before native execution")
    request = {"pilot_only": pilot_only, "variants": variants}
    # Fail closed before creating the execution lock or touching the output
    # directory.  The locked path validates again to cover file replacement
    # between this preflight and the actual run.
    validate_launch_receipt(launch_receipt, spec_path, budget_path, request)
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Run output cannot be a symlink")
    output = output.parent.resolve(strict=True) / output.name
    with _analysis_lock(output.with_name(output.name + ".execution.lock")):
        return _execute_native_locked(source, spec_path, output, budget_path=budget_path,
                                      launch_receipt=launch_receipt, pilot_only=pilot_only,
                                      variants=variants, resume=resume)


def _execute_native_locked(source, spec_path, output, *, budget_path=None, launch_receipt=None,
                           pilot_only=False, variants=None, resume=False):
    """Run real native comparators; incomplete service/model scopes remain explicit."""
    from cloud_experiments import contenders
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if budget_path is None:
        raise ValueError("Pilot and expanded execution require reviewed price/credit/supervisor admission facts")
    request = {"pilot_only": pilot_only, "variants": variants}
    binding = validate_launch_receipt(launch_receipt, spec_path, budget_path, request)
    design = binding["spec"]
    files = verify_snapshot(source, design)
    if ROOT.resolve() != source:
        raise RuntimeError("Execute the delivered frozen harness from its own source root")
    if platform.system() != "Linux" or sys.version_info[:2] != (3, 13):
        raise RuntimeError("Native cloud execution requires Linux/Python3.13; preparation supports3.11+")
    if shutil.disk_usage(output.parent).free < design["limits"]["minimum_free_bytes"]:
        raise RuntimeError("Insufficient free capacity for declared source/cache/evidence")
    receipt_sha256 = binding["receipt_sha256"]
    budget = binding["budget"]
    limits = design["limits"]
    physical_memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    enforced_memory = lifecycle.memory_limit_bytes()
    if (enforced_memory is None or budget.get("enforced_memory_bytes") != enforced_memory
            or type(budget.get("memory_bytes")) is not int or budget["memory_bytes"] > physical_memory):
        raise RuntimeError("Actual cgroup/host memory does not match reviewed admission facts")
    budget["free_bytes"] = min(budget.get("free_bytes", 0),
                               max(0, shutil.disk_usage(output.parent).free - limits["disk_reserve_bytes"]))
    pilot_estimate = {"wall_seconds": 3600, "analysis_seconds": 0, "replicas": 1,
                      "evidence_bytes": limits["evidence_bytes"], "build_bytes": limits["build_bytes"],
                      "metadata_bytes": limits["metadata_bytes"] + limits["log_bytes"], "analysis_bytes": limits["analysis_bytes"],
                      "export_bytes": limits["evidence_bytes"] + limits["binary_bytes"] + limits["analysis_bytes"]
                                      + limits["metadata_bytes"] + limits["log_bytes"] + 64 * 1024 ** 2,
                      "export_seconds": math.ceil((limits["evidence_bytes"] + limits["binary_bytes"]
                                      + limits["analysis_bytes"] + limits["metadata_bytes"] + limits["log_bytes"]
                                      + 64 * 1024 ** 2) / (1024 ** 2)) + 600,
                      "shutdown_seconds": 600,
                      "peak_memory_bytes": budget.get("pilot_memory_bound_bytes"),
                      "memory_reserve_bytes": limits["memory_reserve_bytes"]}
    admission = admit_resources(pilot_estimate, budget)
    if not admission["admitted"]:
        raise RuntimeError("Preparation workload not admitted: " + ",".join(admission["reasons"]))
    if any(k in os.environ for k in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")) or any(k.startswith("CARGO_PROFILE_RELEASE_") for k in os.environ):
        raise RuntimeError("Uncontrolled build/profile environment")
    run = ResearchRun(output, "expanded-native", design["study_id"], design, resume=resume)
    run.sample_stream.close()
    run.sample_stream = EvidenceStream(output, design["limits"]["evidence_bytes"])
    reserved_seconds = pilot_estimate["export_seconds"] + pilot_estimate["shutdown_seconds"]
    work_deadline = budget["deadline_seconds"] - reserved_seconds
    if work_deadline < 1:
        raise RuntimeError("No supervised time remains after export and shutdown reserves")
    run.bind_deadline(min(limits["wall_seconds"], work_deadline))
    requested = {**request, "budget_sha256": binding["budget_sha256"],
                 "launch_receipt_sha256": receipt_sha256}
    if run.manifest.get("execution_request", requested) != requested:
        raise ValueError("Resume cannot change planned variants, pilot mode or budget facts")
    run.manifest["execution_request"] = requested
    run.manifest.update(protocol=PROTOCOL, result_authenticity=AUTHENTICITY,
                        current_phase="preflight", overall_decision="HOLD", technical_coverage={})
    try:
        rust = run.run(["rustc", "-Vv"]).decode()
        if not rust.startswith("rustc 1.99.1 "):
            raise RuntimeError("Pinned Rust1.99.1 required")
        cpu = str(sorted(os.sched_getaffinity(0))[-1])
        topology = run.run(["lscpu", "--json"], log_output=False).decode()
        stable_topology = [row for row in json.loads(topology)["lscpu"]
                           if not any(word in row["field"].lower() for word in ("mhz", "bogomips"))]
        environment_binding = identity({"rust": rust, "python": sys.version, "platform": platform.platform(),
                                        "topology": stable_topology, "affinity": sorted(os.sched_getaffinity(0))})
        if run.manifest.get("environment_binding", environment_binding) != environment_binding:
            raise ValueError("Resume host/compiler/CPU topology changed; start an independent study")
        run.manifest["environment_binding"] = environment_binding
        run.manifest["environment"] = {"rust": rust, "python": sys.version, "platform": platform.platform(),
            "lscpu": topology, "timing_cpu": cpu,
            "free_bytes_start": shutil.disk_usage(output.parent).free,
            "cpu_scope": "One declared vCPU; host/physical-core independence not inferred",
            "rss_scope": "Fresh process includes input/setup/warmup; not stage allocation accounting"}
        run.check("harness_controls", lambda: run.run([sys.executable, "-B", "-m", "unittest", "cloud_experiments.test_campaign",
                 "cloud_experiments.test_lifecycle", "kaggle_experiments.test_protocol", "model.test_models",
                 "cloud_experiments.service_lab", "-v"], cwd=source, timeout=600).decode())
        run.check("catalog_controls", lambda: run.run([sys.executable, "-B", "-m", "cloud_experiments.contenders"], cwd=source, timeout=60).decode())
        workspace = output / "work"
        workspace.mkdir(exist_ok=True)
        cache = output.parent / "cargo-target"
        cache.mkdir(exist_ok=True)
        phases = {split: study_cases(files, split) for split in ("screen", "confirm")}
        paths, _ = _write_fixtures(output, phases, limits)
        for split, cases in phases.items():
            if fixture_inventory(cases, split) != design["fixtures"][split]:
                raise ValueError("Frozen corpus generation differs from spec")
        all_variants, interactions = native_matrix()
        if design.get("native_design") != experimental_design.native_design():
            raise ValueError("Frozen combination design differs from the actual native executor")
        if variants is not None:
            if not variants or len(set(variants)) != len(variants) or any(v not in all_variants for v in variants):
                raise ValueError("Unknown/duplicate selected experiment variant")
            all_variants = ["baseline", *[v for v in variants if v != "baseline"]]
        if len(all_variants) > design["limits"]["max_variants"]:
            raise ValueError("Variant panel exceeds declared resource bound")
        run.manifest["planned_variants"] = all_variants
        run.manifest["interaction_cells"] = interactions
        run.manifest["current_phase"] = "build-baseline"
        start = time.monotonic()
        binaries = {"baseline": _build(run, workspace, files, "baseline", cache)}
        build_wall = run.manifest["snapshots"]["baseline"]["build_wall_seconds"]
        graph = runner.dependency_metadata(run, workspace)
        campaign.write_json(output / "dependency-graph.json", graph)
        run.manifest["dependency_advisories"] = campaign.dependency_advisories(run, graph)
        run.check("baseline_oracles", lambda: _oracles(run, binaries["baseline"], binaries["baseline"], phases["screen"], paths["screen"]))
        run.manifest["current_phase"] = "pilot"
        if (output / "pilot.json").exists():
            pilot = lifecycle.read_json(output / "pilot.json")
            analysis_profile = pilot["analysis_resource_profile"]
        else:
            start = time.monotonic()
            pilot_groups, _ = _measure(run, binaries, "baseline", phases["screen"], paths["screen"], "pilot", 2, 3, cpu)
            pilot_wall = sum(receipt["collection_wall_seconds"] for receipt in run.collection.commits.values()
                             if receipt["descriptor"]["channel"] == "pilot:baseline")
            analysis_profile = profile_analysis(next(iter(pilot_groups.values())), design["confirm"])
            pilot = {"status": "COMPLETE", "phase": "pilot", "wall_seconds": pilot_wall,
                     "baseline_build_wall_seconds": build_wall, "analysis_resource_profile": analysis_profile,
                     "peak_memory_bytes": max(lifecycle.memory_peak_bytes() or 0, run.manifest.get("sampled_process_tree_rss_bytes", 0)),
                     "memory_scope": "Kernel cgroup peak when available plus sampled child tree and parent peak; sampling can miss brief spikes",
                     "precision_status": "PLANNED_NOT_GUARANTEED", "admitted": False,
                     "limits": "Two blocks/three iterations pilot estimates overhead only; not tail precision or host variance"}
        campaign.write_json(output / "pilot.json", pilot)
        if pilot_only:
            run.manifest.update(current_phase="pilot-finished", pilot=pilot,
                coverage=readiness(design, {}), stop_reason="Pilot only; no broad screening/confirmation/adoption")
            run.finish()
            return run.manifest
        estimate = forecast_native(all_variants, phases, design, pilot, analysis_profile)
        admission = admit_resources(estimate, budget)
        pilot.update(admitted=admission["admitted"], admission=admission, estimate=estimate)
        campaign.write_json(output / "pilot.json", pilot)
        run.manifest["pilot"] = pilot
        if not admission["admitted"]:
            raise RuntimeError("Expanded workload not admitted: " + ",".join(admission["reasons"]))
        cross_cells = design["native_design"]["cross_cells"]
        plan = screen_plan(all_variants, interactions, phases["screen"], cross_cells=cross_cells)
        campaign.write_json(output / "screen-plan.json", plan)
        records, required_cases, assignments = {}, {}, plan["assignments"]
        for variant in all_variants[1:]:
            run.manifest.update(current_phase="build-and-oracle", current_variant=variant)
            run.save()
            cases = [c for c in phases["screen"] if c["id"] in assignments[variant]]
            if not cases:
                raise ValueError("Contender has no real matching workload: " + variant)
            previous = run.manifest.setdefault("rejected_variants", {}).get(variant)
            if previous is not None:
                records[variant] = {"owners": contenders.metadata(variant)["decisions"], "correct": False, "reason": previous}
                continue
            try:
                binaries[variant] = _build(run, workspace, files, variant, cache)
                header_mode = "scope" if contenders.metadata(variant)["factors"].get("html_headers") == "scope" else "explicit"
                run.check("oracle:" + variant, lambda: _oracles(run, binaries[variant], binaries["baseline"], cases, paths["screen"], header_mode=header_mode))
            except lifecycle.IntegrityError:
                raise
            except (subprocess.CalledProcessError, ValueError) as error:
                reason = {"status": "REJECTED_WITHIN_SCOPE", "error_type": type(error).__name__, "detail": str(error)[:2048]}
                run.manifest["rejected_variants"][variant] = reason
                records[variant] = {"owners": contenders.metadata(variant)["decisions"], "correct": False, "reason": reason}
                binaries.pop(variant, None)
                run.save()
        assignments = {v: ids for v, ids in assignments.items() if v in binaries}
        screen_groups = _interleaved_screen(run, binaries, assignments, phases["screen"], paths["screen"], cpu)
        cross_coverage = experimental_design.coverage([v for v in cross_cells if v in screen_groups])
        cross_coverage["measurement_status"] = "SCREEN_ONLY_NO_CONFIRMATION"
        campaign.write_json(output / "screen-cross-coverage.json", cross_coverage)
        run.manifest["cross_coverage"] = cross_coverage
        campaign.write_json(output / "screen-interactions.json", interaction_report(screen_groups, interactions, plan["panel_cases"]))
        for variant in all_variants[1:]:
            if variant not in binaries:
                continue
            cases = _variant_cases(variant, phases["screen"])
            groups = screen_groups[variant]
            owners = contenders.metadata(variant)["decisions"]
            ratios = {name: max(protocol.quantile([x for block in blocks for x in block["candidate"]], q)
                               / protocol.quantile([x for block in blocks for x in block["baseline"]], q) for q in (.5, .95, .99))
                      for name, blocks in groups.items()}
            records[variant] = {"owners": owners, "correct": True, "ratios": ratios}
            for owner in owners:
                whole_build = owner == "H10" and "build_factor" in contenders.metadata(variant)["implementation_scopes"]
                owner_cases = sorted(c["id"] for c in cases if owner in c["owners"] or whole_build)
                required_cases.setdefault(owner, owner_cases)
            campaign.write_json(output / "screening.json", records)
        chosen = shortlist(records, required_cases, design["selection"]["slots_per_owner"])
        # Freeze exactly the scoped confirm cases for each finalist, without hiding adverse cases.
        selected = list(chosen["selected"])
        chosen["owner_selected"] = list(selected)
        chosen["exclusion_scope"] = "Owner shortlist only; confirmation_selected may additionally contain the predeclared combined finalist"
        if not selected:
            raise RuntimeError("No complete contract-passing contender available for confirmation")
        _complete_finalist_selection(run, selected, chosen, binaries, workspace=workspace, files=files,
                                    cache=cache, cases=phases["screen"], paths=paths["screen"])
        chosen["confirmation_selected"] = selected
        campaign.write_json(output / "selection.json", chosen)
        confirmation_design = copy.deepcopy(design)
        confirmation_design["confirm_assignments"] = {v: [c["id"] for c in _variant_cases(v, phases["confirm"])] for v in selected}
        confirmation_design["confirm_binaries"] = {v: run.manifest["snapshots"][v]["binaries"] for v in ["baseline", *selected]}
        frozen = freeze_confirmation(confirmation_design, selected, pilot, design["fixtures"])
        if (output / "confirmation-freeze.json").exists() and lifecycle.read_json(output / "confirmation-freeze.json") != frozen:
            raise ValueError("Resumed confirmation freeze changed")
        campaign.write_json(output / "confirmation-freeze.json", frozen)
        run.manifest.update(current_phase="confirm", confirmation_freeze=frozen["freeze_id"])
        decisions = {}
        for variant in selected:
            cases = _variant_cases(variant, phases["confirm"])
            for case in cases:
                actual = _invoke(run, binaries[variant], case, paths["confirm"][case["id"]], 0, None)
                expected = _invoke(run, binaries["baseline"], case, paths["confirm"][case["id"]], 0, None)
                if actual != expected:
                    raise ValueError("Held-out canonical parity failed")
            _measure(run, binaries, variant, cases, paths["confirm"], "confirm",
                design["confirm"]["blocks"], design["confirm"]["iterations"], cpu, frozen["freeze_id"])
        run.sample_stream.close()
        run.manifest["current_phase"] = "analysis"
        run.save()
        for variant in selected:
            report_path = output / ("confirmation-" + variant + ".json")
            run.run([sys.executable, "-B", "-m", "cloud_experiments.research", "analyze", output, variant, report_path], cwd=source,
                    timeout=design["limits"]["wall_seconds"], log_output=False)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            decisions[variant] = {name: item["decision"] for name, item in report["cases"].items()}
            run.manifest["candidate_decisions"] = decisions
            run.save()
        run.manifest.update(current_phase="finished", coverage=readiness(design, {}),
            overall_decision="HOLD", stop_reason="Native panel completed; remaining service/model/dependency/corpus decisions require their actual evidence",
            cleanup="Evidence and compatible cache retained on owned research disk; no production adoption")
    except BaseException as error:
        run.manifest.update(failed_phase=run.manifest.get("current_phase"), current_phase="failed",
                            failure_kind="TIMEOUT" if isinstance(error, TimeoutError) else "EXECUTION_ERROR",
                            overall_decision="HOLD")
        run.finish(error)
        raise
    run.finish()
    return run.manifest


def execute_service(binary, output, *, scenario_ids=None):
    from cloud_experiments import service_lab
    binary = Path(binary).resolve(strict=True)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    before = protocol.sha256_file(binary)
    scenarios = service_lab.correctness_scenarios()
    if scenario_ids is not None:
        known = {x["id"] for x in scenarios}
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids) or not set(scenario_ids) <= known:
            raise ValueError("Unknown/duplicate actual service scenario")
        scenarios = [x for x in scenarios if x["id"] in scenario_ids]
    report = {"protocol": PROTOCOL, "kind": "controlled-service-contracts", "binary_sha256": before,
              "environment": {"python": sys.version, "platform": platform.platform()}, "status": "RUNNING",
              "decision_sufficiency": "NOT_EVALUATED", "scenarios": [], "result_authenticity": AUTHENTICITY}
    path = output / "service-contracts.json"
    try:
        for scenario in scenarios:
            result = service_lab.run_scenario(binary, scenario, output, timeout_s=45)
            report["scenarios"].append({"id": scenario["id"], **result})
            campaign.write_json(path, report)
        report["failure_cleanup_control"] = service_lab.cleanup_failure_control(output)
        if protocol.sha256_file(binary) != before:
            raise ValueError("Native executable changed during actual service checks")
        passed = all(x["oracle_passed"] for x in report["scenarios"]) and report["failure_cleanup_control"]["passed"]
        report["status"] = "CONTRACTS_PASS_WITHIN_SCOPE" if passed else "CONTRACT_FAILURE"
        report["scope"] = "Controlled real TLS plus native parse/export/SingleFlight/admission; production Engine success/DNS/TLS deployment remains NOT VERIFIED"
    except BaseException as error:
        report.update(status="FAILED", error=type(error).__name__ + ": " + str(error))
        campaign.write_json(path, report)
        raise
    campaign.write_json(path, report)
    return report


def execute_agent(binary, output, *, policy="W0", representation="full", model=None, request_cap=10,
                  task_ids=None, provider="nim", confirm_provider_no_charge=False):
    from datetime import datetime, timezone
    from cloud_experiments import agent_lab
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    producer = agent_lab.RustProbeProducer(binary)
    cards = agent_lab.tasks()
    if task_ids is not None:
        known = {x["id"] for x in cards}
        if not task_ids or len(set(task_ids)) != len(task_ids) or not set(task_ids) <= known:
            raise ValueError("Unknown/duplicate actual agent task")
        cards = [x for x in cards if x["id"] in task_ids]
    client = None
    report = {"protocol": PROTOCOL, "kind": "native-agent-task-panel", "producer_sha256": producer.sha256,
              "policy": policy, "representation": representation, "status": "RUNNING", "tasks": [],
              "decision_sufficiency": "NOT_EVALUATED", "result_authenticity": AUTHENTICITY,
              "limits": "Synthetic task corpus; actual free API only with explicit W1/model; no model ranking from a capability probe"}
    path = output / "agent-trials.json"
    try:
        if policy == "W1":
            if not confirm_provider_no_charge:
                raise RuntimeError("W1 requires explicit no-charge confirmation after independent price verification")
            if not model:
                raise RuntimeError("Explicit hosted-trial model required; no model discovery/fallback")
            if provider == "nim":
                credential = os.environ.get("NVIDIA_API_KEY")
                resource = os.environ.get("NVIDIA_API_KEY_SECRET_RESOURCE")
                if not credential and resource:
                    from cloud_experiments.gcp_secrets import access_secret_resource
                    credential = access_secret_resource(resource)
            elif provider == "openrouter":
                credential = os.environ.get("OPENROUTER_API_KEY")
            else:
                raise ValueError("Unknown explicit model provider")
            if not credential:
                raise RuntimeError("Explicit hosted-trial provider credential or Secret Manager resource required")
            if provider == "nim":
                from cloud_experiments.nim import NimClient, NimConfig, credential_rate_file
                client = NimClient(NimConfig(model=model, api_key=credential, allow_live=True,
                    trial_without_billing_confirmed=confirm_provider_no_charge,
                    request_cap=request_cap, rate_file=str(credential_rate_file(credential))))
            else:
                client = agent_lab.OpenRouterClient(agent_lab.OpenRouterConfig(model=model, api_key=credential,
                    allow_live=confirm_provider_no_charge,
                    operator_no_charge_acknowledged=confirm_provider_no_charge,
                    request_cap=request_cap, quota={"utc_date": datetime.now(timezone.utc).date().isoformat(),
                    "remaining_requests": request_cap, "source": "Explicit per-process campaign allocation; provider remaining UNKNOWN"}))
        for card in cards:
            if protocol.sha256_file(producer.executable) != producer.sha256:
                raise ValueError("Native evidence producer changed during tasks")
            result = agent_lab.run_task(card, policy, representation, client, producer=producer)
            report["tasks"].append(result)
            if client is not None:
                report["provider_allocation"] = client.budget_state()
            campaign.write_json(path, report)
            if client is not None and (client.blocked or client.requests_used >= request_cap):
                report["stop_reason"] = client.blocked or "ALLOCATED_REQUEST_CAP_REACHED"
                break
        report["status"] = "PANEL_EXECUTED" if len(report["tasks"]) == len(cards) else "PARTIAL_PROVIDER_LIMIT"
        report["task_successes"] = sum(x["quality"]["contract_satisfied"] for x in report["tasks"])
        report["task_denominator"] = len(cards)
        report["attempted_tasks"] = len(report["tasks"])
    except BaseException as error:
        # API clients sanitize provider content; never persist exception text that might contain headers/secrets.
        report.update(status="FAILED", error_type=type(error).__name__)
        campaign.write_json(path, report)
        raise
    campaign.write_json(path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("destination")
    prepare_parser.add_argument("--corpus-manifest")
    prepare_parser.add_argument("--wall-seconds", type=int)
    commands.add_parser("inventory")
    run_parser = commands.add_parser("run-native")
    for name in ("source", "spec", "output"):
        run_parser.add_argument(name)
    run_parser.add_argument("--pilot-only", action="store_true")
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--budget-file")
    run_parser.add_argument("--launch-receipt", required=True,
                            help="Operator-approved athena-launch-receipt/1 bound to this spec, budget and request")
    run_parser.add_argument("--variants", nargs="+")
    analyzer = commands.add_parser("analyze")
    analyzer.add_argument("directory")
    analyzer.add_argument("variant")
    analyzer.add_argument("output")
    service_parser = commands.add_parser("run-service")
    service_parser.add_argument("binary")
    service_parser.add_argument("output")
    service_parser.add_argument("--scenarios", nargs="+")
    agent_parser = commands.add_parser("run-agent")
    agent_parser.add_argument("binary")
    agent_parser.add_argument("output")
    agent_parser.add_argument("--policy", choices=["W0", "W1", "W2", "W3"], default="W0")
    agent_parser.add_argument("--representation", choices=["full", "compact"], default="full")
    agent_parser.add_argument("--model")
    agent_parser.add_argument("--provider", choices=("nim", "openrouter"), default="nim")
    agent_parser.add_argument("--request-cap", type=int, default=10)
    agent_parser.add_argument("--confirm-provider-no-charge", action="store_true",
                              help="Acknowledge independent zero-price eligibility review; provider billing remains unverified here")
    agent_parser.add_argument("--tasks", nargs="+")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.destination, corpus_manifest=args.corpus_manifest, wall_seconds=args.wall_seconds)
    elif args.command == "inventory":
        from cloud_experiments import contenders
        variants, panels = native_matrix()
        result = {"protocol": PROTOCOL, "variants": {v: contenders.metadata(v) for v in variants},
                  "interactions": panels, "cases_per_split": len(synthetic_cases("screen")),
                  "combination_design": experimental_design.native_design(),
                  "coverage": readiness(manifest_design(), {})}
    elif args.command == "analyze":
        result = analyze_confirmation(args.directory, args.variant, args.output)
        result = {"status": "ANALYZED", "overall_decision": result["overall_decision"]}
    elif args.command == "run-service":
        report = execute_service(args.binary, args.output, scenario_ids=args.scenarios)
        result = {"status": report["status"], "cases": len(report["scenarios"])}
        if report["status"] != "CONTRACTS_PASS_WITHIN_SCOPE":
            print(json.dumps(result))
            return 2
    elif args.command == "run-agent":
        report = execute_agent(args.binary, args.output, policy=args.policy, representation=args.representation,
                               model=args.model, request_cap=args.request_cap, task_ids=args.tasks, provider=args.provider,
                               confirm_provider_no_charge=args.confirm_provider_no_charge)
        result = {"status": report["status"], "task_successes": report["task_successes"], "task_denominator": report["task_denominator"]}
        if report["task_successes"] != report["task_denominator"]:
            print(json.dumps(result))
            return 2
    else:
        result = execute_native(args.source, args.spec, args.output, budget_path=args.budget_file,
                                launch_receipt=args.launch_receipt, pilot_only=args.pilot_only,
                                variants=args.variants, resume=args.resume)
        result = {"status": result["status"], "overall_decision": result["overall_decision"], "phase": result["current_phase"]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
