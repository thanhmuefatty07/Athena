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
import tempfile
import time

from cloud_experiments import campaign, experimental_design, libraries, lifecycle, process_isolation
from kaggle_experiments import protocol, runner

PROTOCOL = "athena-core-research/2"
ROOT = Path(__file__).resolve().parent.parent
AUTHENTICITY = "NOT VERIFIED: runner and evidence share OS authority"
DECLARED_INTERACTION_IDS = frozenset({
    "I-AAE", "I-RANK", "I-PROJECT", "I-ACCESS", "I-AGENT", "I-SYSTEM",
    "I-HASH-LIBRARY", "I-PARSER-LIBRARY", "I-JSON-LIBRARY", "I-LIBRARY-SYSTEM",
    "I-JSON-CPU", "I-JSON-PROJECT", "I-QUERY-PROJECT", "I-COMPILER",
    "I-JSON-HASH-CPU", "I-NATIVE-CROSS-COVERAGE", "I-LANG-PARSE",
    "I-LANG-BROWSER", "I-LANG-AGENT", "I-LANG-BATCH", "I-LANG-SCHED",
    "I-LANG-MODEL", "I-HTML-TABLE", "I-HTML-ATTR-MEMBERSHIP",
})


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
        hashes, stage_hashes, lineages, names = set(), set(), set(), set()
        for item in inputs:
            stage_hash = (item.get("stage"), item.get("sha256"))
            if (not protocol.is_digest(item.get("sha256")) or not item.get("lineage")
                    or not item.get("id") or item["id"] in names or stage_hash in stage_hashes):
                raise ValueError("Missing/duplicate input identity or lineage")
            names.add(item["id"])
            hashes.add(item["sha256"])
            stage_hashes.add(stage_hash)
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
                or any(type(estimate.get(k, 0)) is not int or estimate.get(k, 0) < 0
                       for k in ("fixture_bytes", "retained_export_bytes"))):
            raise ValueError("Invalid workload bound")
        cost = (Decimal(str(duration)) * estimate["replicas"] * money["hourly_usd"] / 3600
                + money["storage_usd"])
        available = min(money["remaining_usd"], money["ceiling_usd"] - money["spent_usd"]) - money["reserve_usd"]
        deadline_seconds = budget.get("deadline_seconds")
        deadline_cost = None
        if cost > available:
            reasons.append("dollar_budget")
        if not positive(deadline_seconds):
            reasons.append("supervised_deadline")
        else:
            deadline_cost = (Decimal(str(deadline_seconds)) * money["hourly_usd"] / 3600
                             + money["storage_usd"])
            if duration > deadline_seconds:
                reasons.append("supervised_deadline")
            if deadline_cost > available:
                reasons.append("deadline_cost_ceiling")
        disk = (sum(estimate[k] for k in ("evidence_bytes", "build_bytes", "metadata_bytes", "analysis_bytes", "export_bytes"))
                + estimate.get("fixture_bytes", 0) + estimate.get("retained_export_bytes", 0))
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
                "deadline_cost_usd": (None if deadline_cost is None else str(deadline_cost.quantize(
                    Decimal(".000001"), rounding=ROUND_CEILING))),
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


def bounded_bootstrap(rounds, probabilities, draws, seed, batch_size=256):
    outputs = {q: [] for q in probabilities}
    for deltas in _bootstrap_batches(rounds, probabilities, draws, seed, batch_size):
        for q, values in deltas.items():
            outputs[q].extend(values)
    for values in outputs.values():
        values.sort()
    return outputs


def bootstrap_intervals(rounds, probabilities, draws, seed, tail, batch_size=256, *, deadline=None):
    if not positive(tail) or tail > .5 or type(draws) is not int or draws < 1:
        raise ValueError("Invalid bootstrap interval tail")
    lower_count = math.floor((draws - 1) * tail) + 1
    upper_count = draws - math.ceil((draws - 1) * (1 - tail))
    if not 1 <= min(lower_count, upper_count) <= max(lower_count, upper_count) <= 1024:
        raise ValueError("Bootstrap interval exceeds declared retained-tail bound")
    low, high = {q: [] for q in probabilities}, {q: [] for q in probabilities}
    for deltas in _bootstrap_batches(rounds, probabilities, draws, seed, batch_size, deadline):
        for q, values in deltas.items():
            low_q = low[q]
            high_q = high[q]
            for value in values:
                if len(low_q) < lower_count:
                    heapq.heappush(low_q, -value)
                elif value < -low_q[0]:
                    heapq.heapreplace(low_q, -value)
                if len(high_q) < upper_count:
                    heapq.heappush(high_q, value)
                elif value > high_q[0]:
                    heapq.heapreplace(high_q, value)
    return {q: [-low[q][0], high[q][0]] for q in probabilities}


def _bootstrap_batches(rounds, probabilities, draws, seed, batch_size, deadline=None):
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
    randrange = rng.randrange
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("Invalid bootstrap deadline")
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
    pos_array = np.asarray(positions, dtype=np.int32) if np is not None else None
    for start in range(0, draws, batch_size):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Bootstrap analysis exceeded its admitted wall deadline")
        weights = []
        for _ in range(min(batch_size, draws - start)):
            row = [0] * count
            for _ in range(count):
                row[randrange(count)] += 1
            weights.append(row)
        if np is not None:
            weight_array = np.asarray(weights, dtype=np.int32)
            sampled_np = {}
            for arm, (values, groups, prefix) in arrays.items():
                if prefix is None:
                    cumulative = weight_array[:, groups].cumsum(axis=1)
                    sampled_np[arm] = np.stack(
                        [values[(cumulative > p).argmax(axis=1)] for p in positions], axis=1
                    )
                else:
                    low = np.zeros((len(weights), len(positions)), dtype=np.int32)
                    high = np.full_like(low, len(values) - 1)
                    for _ in range((len(values) - 1).bit_length()):
                        middle = (low + high) >> 1
                        mass = np.einsum("bqk,bk->bq", prefix[middle + 1], weight_array)
                        found = mass > pos_array
                        high = np.where(found, middle, high)
                        low = np.where(found, low, middle + 1)
                    sampled_np[arm] = values[low]
            diff = sampled_np["candidate"] - sampled_np["baseline"]
            yield {q: diff[:, i].tolist() for i, q in enumerate(probabilities)}
        else:
            sampled = {
                arm: [protocol._weighted_quantiles(values, groups, row, probabilities) for row in weights]
                for arm, (values, groups, _) in arrays.items()
            }
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


def _interaction_scope(spec):
    """Defer conditional interactions only when no active panel also needs them."""
    interactions = [item["id"] for item in spec["interactions"]]
    deferred_candidates = set()
    required_by_active_panel = set()
    for panel in spec.get("language_panels", []):
        panel_interactions = set(panel.get("interactions", []))
        if panel.get("admission") == "CONDITIONAL" and panel.get("status") == "NOT_ADMITTED":
            deferred_candidates.update(panel_interactions)
        else:
            required_by_active_panel.update(panel_interactions)
    deferred_set = deferred_candidates - required_by_active_panel
    deferred = [identity for identity in interactions if identity in deferred_set]
    deferred_set = set(deferred)
    required = [identity for identity in interactions if identity not in deferred_set]
    return required, deferred


def _validated_wpt_limits(limits):
    names = ("wpt_report_bytes", "wpt_reports_total_bytes", "wpt_report_count")
    values = {name: limits.get(name) for name in names}
    if (any(type(value) is not int or value <= 0 for value in values.values())
            or values["wpt_reports_total_bytes"] < values["wpt_report_bytes"] * values["wpt_report_count"]):
        raise ValueError("WPT report count/byte bounds are invalid or cannot hold the declared panel")
    return values


def manifest_design():
    from cloud_experiments import service_study
    registry = lifecycle.read_json(ROOT / "cloud_experiments/core_decisions.json")
    audited = campaign.audit_decision_map(ROOT, registry)
    required_decisions = [item["id"] for item in registry["decisions"]]
    required_interactions, conditional_interaction_non_admissions = _interaction_scope(registry)
    declared_interactions = {item.get("id") for item in registry["interactions"]
                             if isinstance(item, dict)}
    language_panels = registry["language_panels"]
    native = experimental_design.native_design()
    if (audited["mapping_status"] != "MAPPED"
            or set(required_decisions) != {f"H{i:02}" for i in range(1, 15)}
            or declared_interactions != DECLARED_INTERACTION_IDS
            or not set(native["factorial_panels"]).union({"I-NATIVE-CROSS-COVERAGE"}) <= set(required_interactions)
            or {panel["id"] for panel in language_panels} != {f"L{i:02}" for i in range(1, 7)}):
        raise ValueError("Authoritative decision map does not close the declared experiment scope")
    return {"protocol": PROTOCOL, "kind": "bounded-exploration-and-frozen-confirmation",
            "holdout_kind": "independent_seed", "required_decisions": required_decisions,
            "required_interactions": required_interactions, "native_design": native,
            "service_study": service_study.make_design(),
            "conditional_interaction_non_admissions": conditional_interaction_non_admissions,
            "language_panels": language_panels,
            "randomization": {"schema": "athena-randomization/1", "seed": 81723091,
                              "arm_order": "randomized-start-alternating-blocks"},
            "screen": {"blocks": 6, "iterations": 30, "seed": 458729},
            "confirm": {"blocks": 24, "iterations": 250, "alpha": .05,
                        "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"]},
            "limits": {"wall_seconds": 120 * 86400, "command_seconds": 1800,
                       "build_bytes": 16 * 1024 ** 3, "minimum_free_bytes": 94 * 1024 ** 3,
                       "evidence_bytes": 16 * 1024 ** 3,
                       "log_bytes": 64 * 1024 ** 2, "metadata_bytes": 64 * 1024 ** 2,
                       "analysis_bytes": 256 * 1024 ** 2, "command_output_bytes": 8 * 1024 ** 2,
                       "binary_bytes": 2 * 1024 ** 3,
                       "export_bytes": 19 * 1024 ** 3,
                       "block_bytes": 256 * 1024 ** 2,
                       "memory_reserve_bytes": 1024 ** 3,
                       "disk_reserve_bytes": 2 * 1024 ** 3,
                       "max_variants": experimental_design.MAX_VARIANTS,
                       "max_fixture_file_bytes": 8 * 1024 ** 2,
                       "max_fixture_html_bytes": 4 * 1024 ** 2,
                       "max_fixture_bytes": 16 * 1024 ** 2,
                       "wpt_report_bytes": 2 * 1024 ** 2,
                       "wpt_reports_total_bytes": 24 * 1024 ** 2,
                       "wpt_report_count": len(native["factorial_panels"]["I-HTML-ATTR-MEMBERSHIP"]),
                       "service_study_block_bytes": 30 * 1024 ** 2,
                       "service_study_evidence_bytes": 768 * 1024 ** 2},
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
                or hashlib.sha256(campaign.canonical_source_bytes(path.read_bytes(), name)).hexdigest() != digest):
            raise ValueError("Frozen input missing/changed/escaped: " + name)
    return {name: campaign.canonical_source_bytes((source / name).read_bytes(), name)
            for name in design["files"]}


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
            "language_panel_records": {panel["id"]: records.get(panel["id"], {"status": panel["status"], "runnable_verified": False}) for panel in language_panels},
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

    def build(staging):
        files = campaign.source_files(ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
                                            "engineering-constitution.md", "CORE_RESEARCH_PLAN.md",
                                            *campaign.reviewed_cloud_experiment_sources(ROOT),
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
        (staging / "source.tgz").write_bytes(archive)
        design = manifest_design()
        if wall_seconds is not None:
            if type(wall_seconds) is not int or not 1 <= wall_seconds <= 120 * 86400:
                raise ValueError("Declare campaign duration before source/design freeze")
            design["limits"]["wall_seconds"] = wall_seconds
        design.update(source_sha256=hashlib.sha256(archive).hexdigest(),
                      files={k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
                      variants=contenders.definitions(),
                      fixtures={split: fixture_inventory(study_cases(files, split), split)
                                for split in ("screen", "confirm")},
                      library_dossier=libraries.dossier(ROOT),
                      corpus={"status": "NOT_PROVIDED"} if corpus is None else
                      {k: v for k, v in corpus.items() if k != "cases"})
        design["study_id"] = identity(design)
        campaign.write_json(staging / "spec.json", design)
        return {"study_id": design["study_id"], "bytes": len(archive), "files": len(files),
                "status": "PREPARED_NOT_LAUNCH_APPROVED"}

    return campaign.prepare_directory(destination, build)


def _materialize(workspace, files, variant, transformed=None):
    from cloud_experiments import contenders, parser_study
    transformed = contenders.transform(files, variant) if transformed is None else transformed
    factors = contenders.metadata(variant)["factors"]
    attr_lookup = factors.get("html_attr_lookup", "vector")
    if attr_lookup != "vector":
        version = "0.40.1" if factors.get("html_library") == "modern" else "0.39.0"
        parser_study.materialize_vendor(files, workspace, version, attr_lookup)
    for name, data in transformed.items():
        if name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml") or name.startswith(("src/", "tests/", "fixtures/")):
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("Work tree link/escape")
            target.write_bytes(data)
    for original in ("cloud_experiments/core_probe.rs", "cloud_experiments/mcp_sdk/task_inputs.json"):
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
    return process_isolation.command_argv(command, required=True)


def _assert_cargo_configuration_absent(workspace, cargo_home):
    """Reject ambient Cargo configuration before a locked dependency fetch/build."""
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Cargo workspace is not a regular directory")
    for directory in (root, *root.parents):
        config_dir = directory / ".cargo"
        if config_dir.is_symlink():
            raise ValueError("Cargo configuration directory cannot be linked")
        if config_dir.exists() and not config_dir.is_dir():
            raise ValueError("Cargo configuration path is not a directory")
        for name in ("config", "config.toml"):
            candidate = config_dir / name
            if candidate.exists() or candidate.is_symlink():
                raise ValueError("Ambient Cargo configuration is not admitted")
    home = Path(cargo_home).resolve(strict=True)
    for name in ("config", "config.toml", "credentials", "credentials.toml"):
        candidate = home / name
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("Cargo home configuration or credentials are not admitted")


@contextmanager
def _cargo_environment(build, target_cache):
    """Set one private, credential-free Cargo home and restore parent state."""
    previous = dict(os.environ)
    rustup_home = previous.get("RUSTUP_HOME") or str(Path.home() / ".rustup")
    try:
        for key in list(os.environ):
            if (key.startswith(("CARGO_", "ATHENA_RESEARCH_"))
                    or key in ("RUSTFLAGS", "RUSTC", "RUSTDOC", "RUSTC_WRAPPER",
                               "RUSTC_WORKSPACE_WRAPPER", "RUSTDOCFLAGS", "RUSTC_BOOTSTRAP",
                               "ATHENA_WPT_REPORT_ONLY")):
                del os.environ[key]
        os.environ.update(build["env"])
        if build["rustflags"]:
            os.environ["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(build["rustflags"])
        os.environ["CARGO_TARGET_DIR"] = str(target_cache)
        cargo_home = Path(target_cache) / "cargo-home"
        if cargo_home.is_symlink() or not cargo_home.resolve().is_relative_to(Path(target_cache).resolve()):
            raise ValueError("Cargo cache escaped the owned research cache")
        cargo_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (os.name == "posix" and (cargo_home.stat().st_uid != os.geteuid()
                or cargo_home.stat().st_mode & 0o077)):
            raise ValueError("Cargo cache must be private to the research account")
        os.environ["CARGO_HOME"] = str(cargo_home)
        os.environ["RUSTUP_HOME"] = rustup_home
        os.environ["HOME"] = str(cargo_home)
        yield cargo_home
    finally:
        os.environ.clear()
        os.environ.update(previous)

def _current_network_namespace_id():
    return os.readlink("/proc/self/ns/net")


def _current_pid_namespace_id():
    return os.readlink("/proc/self/ns/pid")


def _verify_build_network_isolation(run):
    if platform.system() != "Linux":
        raise RuntimeError("Native research children require Linux PID and network namespaces")
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("Native research dependencies and benchmarks are pinned to x86_64 Linux")
    host_netns = _current_network_namespace_id()
    host_pidns = _current_pid_namespace_id()
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
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.4)
        with socket.create_connection(listener.getsockname(), timeout=0.4):
            connection, _ = listener.accept()
            connection.close()
    checks["loopback"] = True
except OSError:
    checks["loopback"] = False
report = {"host_netns": sys.argv[1], "child_netns": os.readlink("/proc/self/ns/net"),
          "host_pidns": sys.argv[2], "child_pidns": os.readlink("/proc/self/ns/pid"),
          "metadata_tcp_blocked": checks["gce_metadata"], "public_tcp_blocked": checks["public_egress"],
          "loopback_available": checks["loopback"]}
print(json.dumps(report, sort_keys=True, separators=(",", ":")))
if (report["host_netns"] == report["child_netns"]
        or report["host_pidns"] == report["child_pidns"] or not all(checks.values())):
    raise SystemExit(41)
"""
    command = [sys.executable, "-B", "-c", probe, host_netns, host_pidns]
    if not getattr(run, "isolate_children", False):
        command = _network_namespace_command(command)
    raw = run.run(command, timeout=15, log_output=False)
    try:
        report = protocol.strict_json_loads(raw)
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise RuntimeError("Network isolation probe returned invalid evidence") from error
    if (not isinstance(report, dict) or report.get("host_netns") != host_netns
            or report.get("child_netns") == host_netns
            or report.get("host_pidns") != host_pidns
            or report.get("child_pidns") == host_pidns
            or report.get("metadata_tcp_blocked") is not True
            or report.get("public_tcp_blocked") is not True
            or report.get("loopback_available") is not True):
        raise RuntimeError("Child namespaces did not isolate PIDs and deny metadata/public egress")
    report.update(status="PROCESS_CHECKED_PID_AND_NETWORK_NAMESPACES",
                  authenticity="NOT_VERIFIED_INDEPENDENTLY")
    return report


def _run_isolated_cargo(run, command, *, cwd, timeout):
    if not getattr(run, "isolate_children", False):
        command = _network_namespace_command(command)
    return run.run(command, cwd=cwd, timeout=timeout, monitor_build_tree=True)


def _needs_wpt_report(variant, definition):
    return (variant == "baseline"
            or "html_attr_membership_stage_only" in definition["implementation_scopes"])


def _wpt_artifact(run, variant):
    relative = Path("wpt") / (variant + ".json")
    path = run.directory / relative
    if (relative.is_absolute() or ".." in relative.parts or path.parent.is_symlink() or path.is_symlink()
            or not path.resolve(strict=False).is_relative_to(run.directory.resolve())):
        raise lifecycle.IntegrityError("WPT report path escaped the owned run directory")
    return relative.as_posix(), path


def _load_wpt_report(run, variant, expected_receipt=None):
    from cloud_experiments import parser_study

    receipts = run.manifest.get("wpt_reports", {})
    receipt = receipts.get(variant)
    limits = _validated_wpt_limits(run.manifest["spec"]["limits"])
    expected_path, path = _wpt_artifact(run, variant)
    if (not isinstance(receipt, dict)
            or set(receipt) != {"artifact", "sha256", "bytes", "summary", "semantics"}
            or receipt.get("artifact") != expected_path
            or type(receipt.get("bytes")) is not int or receipt["bytes"] < 0
            or not protocol.is_digest(receipt.get("sha256"))
            or path.is_symlink() or not path.is_file()
            or path.stat().st_size != receipt.get("bytes")
            or receipt["bytes"] > limits["wpt_report_bytes"]
            or protocol.sha256_file(path) != receipt.get("sha256")
            or expected_receipt is not None and receipt != expected_receipt):
        raise lifecycle.IntegrityError("Resumed WPT report receipt or artifact is missing/changed")
    report = parser_study.validate_wpt_report(protocol.strict_json_loads(path.read_bytes()))
    summary = {name: report[name] for name in (
        "total_cases", "executed", "skipped_fragments", "skipped_script_off",
        "tree_mismatches", "annotation_count_deltas")}
    if receipt.get("summary") != summary:
        raise lifecycle.IntegrityError("WPT report summary differs from its case-level artifact")
    return report, receipt


def _run_wpt_report(run, workspace, variant, definition):
    from cloud_experiments import parser_study

    previous = os.environ.get("ATHENA_WPT_REPORT_ONLY")
    os.environ["ATHENA_WPT_REPORT_ONLY"] = "1"
    try:
        raw = _run_isolated_cargo(run, ["cargo", "--offline", "test", "--locked",
            "--test", "wpt_html_conformance", "-j", "2",
            *definition["build"]["cargo_feature_args"], "--", "--ignored", "--nocapture"],
            cwd=workspace, timeout=1200)
    finally:
        if previous is None:
            os.environ.pop("ATHENA_WPT_REPORT_ONLY", None)
        else:
            os.environ["ATHENA_WPT_REPORT_ONLY"] = previous
    report = parser_study.parse_wpt_report(raw)
    limits = _validated_wpt_limits(run.manifest["spec"]["limits"])
    relative, path = _wpt_artifact(run, variant)
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        existing = (parser_study.validate_wpt_report(protocol.strict_json_loads(path.read_bytes()))
                    if not path.is_symlink() else None)
        if existing != report:
            raise lifecycle.IntegrityError("Interrupted WPT artifact differs from the rerun parser report")
    else:
        if variant in run.manifest.get("wpt_reports", {}):
            raise lifecycle.IntegrityError("Recorded WPT report artifact disappeared before resume")
        campaign.write_json(path, report,
            max_bytes=limits["wpt_report_bytes"])
    entries = list(path.parent.iterdir())
    if any(item.is_symlink() or not item.is_file() or item.suffix != ".json" for item in entries):
        raise lifecycle.IntegrityError("WPT report directory contains an unexpected entry")
    size = sum(item.stat().st_size for item in entries)
    if path.stat().st_size > limits["wpt_report_bytes"]:
        raise RuntimeError("A WPT report exceeded the declared per-report byte bound")
    if size > limits["wpt_reports_total_bytes"] or len(entries) > limits["wpt_report_count"]:
        raise RuntimeError("Pinned WPT report artifacts exceeded the declared total byte bound")
    run.guard_storage()
    summary = {name: report[name] for name in (
        "total_cases", "executed", "skipped_fragments", "skipped_script_off",
        "tree_mismatches", "annotation_count_deltas")}
    receipt = {"artifact": relative, "sha256": protocol.sha256_file(path),
               "bytes": path.stat().st_size, "summary": summary,
               "semantics": "Normative WPT mismatches are counted, not called PASS; candidate comparison is same-parser-version exact tree/warning parity"}
    run.manifest.setdefault("wpt_reports", {})[variant] = receipt
    factors = definition["factors"]
    attr_lookup = factors.get("html_attr_lookup", "vector")
    reference = None
    if attr_lookup not in ("vector", None):
        reference = "html-attrs-modern-vector" if factors.get("html_library") == "modern" else "baseline"
    elif variant == "html-attrs-modern-vector":
        reference = "baseline"
    if reference is not None:
        reference_report, _ = _load_wpt_report(run, reference)
        comparison = parser_study.compare_wpt_cases(reference_report["cases"], report["cases"])
        comparison.update(reference_variant=reference,
                          mode="same_version_attribute_strategy" if attr_lookup not in ("vector", None)
                          else "parser_library_version_delta")
        run.manifest.setdefault("wpt_comparisons", {})[variant] = comparison
        if attr_lookup not in ("vector", None):
            run.manifest.setdefault("wpt_candidate_gates", {})[variant] = {
                "status": comparison["status"], "scope": "same pinned WPT document cases, exact serialized-tree digest and warning-count parity"}
            if comparison["status"] != "SEMANTICALLY_IDENTICAL":
                run.save()
                raise ValueError("HTML attribute-index candidate changed pinned WPT tree or warning behavior")
    run.save()
    return receipt


def _verify_resumed_wpt(run, variant, definition, snapshot):
    from cloud_experiments import parser_study

    report, receipt = _load_wpt_report(run, variant, snapshot.get("wpt_report_receipt"))
    attr_lookup = definition["factors"].get("html_attr_lookup", "vector")
    reference = None
    strict_gate = attr_lookup not in ("vector", None)
    if strict_gate:
        reference = ("html-attrs-modern-vector"
                     if definition["factors"].get("html_library") == "modern" else "baseline")
    elif variant == "html-attrs-modern-vector":
        reference = "baseline"
    if reference is None:
        if snapshot.get("wpt_comparison") is not None:
            raise lifecycle.IntegrityError("Unexpected WPT comparison on a non-parser-panel snapshot")
        return
    reference_report, _ = _load_wpt_report(run, reference)
    expected = parser_study.compare_wpt_cases(reference_report["cases"], report["cases"])
    expected.update(reference_variant=reference,
                    mode="same_version_attribute_strategy" if strict_gate
                    else "parser_library_version_delta")
    if (snapshot.get("wpt_comparison") != expected
            or run.manifest.get("wpt_comparisons", {}).get(variant) != expected):
        raise lifecycle.IntegrityError("Resumed WPT case differential differs from its recorded comparison")
    if strict_gate:
        gate = run.manifest.get("wpt_candidate_gates", {}).get(variant, {})
        if (expected["status"] != "SEMANTICALLY_IDENTICAL"
                or gate.get("status") != expected["status"]):
            raise lifecycle.IntegrityError("Resumed parser-index candidate lacks exact WPT semantic parity")


def _preflight_cargo_cache(run, files, variants, target_cache):
    """Prove every planned lock profile is available offline before measurement begins."""
    from cloud_experiments import contenders, libraries

    if (not isinstance(variants, (list, tuple)) or not variants
            or any(not isinstance(variant, str) or not variant for variant in variants)
            or len(set(variants)) != len(variants)):
        raise ValueError("Cargo cache preflight requires a non-empty unique variant list")
    if "Cargo.lock" not in files or "Cargo.toml" not in files:
        raise ValueError("Cargo cache preflight requires frozen Cargo build inputs")

    report = {"schema": "athena-offline-cargo-cache/1", "status": "RUNNING",
              "policy": process_isolation.CARGO_FETCH_POLICY,
              "fetch_argv": ["cargo", *process_isolation.CARGO_FETCH_ARGS],
              "target": "x86_64-unknown-linux-gnu", "planned_variants": list(variants),
              "verified_profiles": []}
    run.manifest["cargo_cache_preflight"] = report
    run.save()
    profiles = {}
    failed_variant = None
    try:
        with tempfile.TemporaryDirectory(prefix="athena-cargo-cache-preflight-",
                                         dir=run.directory) as temporary:
            scratch = Path(temporary).resolve(strict=True)
            if not scratch.is_relative_to(run.directory.resolve(strict=True)):
                raise ValueError("Cargo cache preflight workspace escaped the owned run")
            for index, variant in enumerate(variants):
                failed_variant = variant
                definition = contenders.metadata(variant)
                transformed = contenders.transform(files, variant)
                libraries.verify_build_inputs(files, transformed, definition["factors"])
                lock = transformed.get("Cargo.lock")
                if not isinstance(lock, bytes) or not lock:
                    raise ValueError("Cargo cache preflight received an empty candidate lockfile")
                lock_sha256 = hashlib.sha256(lock).hexdigest()
                profile = profiles.setdefault(lock_sha256, {"lock_sha256": lock_sha256,
                                                              "variants": [], "package_count": None,
                                                              "status": "RUNNING"})
                profile["variants"].append(variant)
                report["verified_profiles"] = list(profiles.values())
                run.save()
                with tempfile.TemporaryDirectory(prefix=f"profile-{index:03d}-", dir=scratch) as folder:
                    workspace = Path(folder).resolve(strict=True)
                    if not workspace.is_relative_to(scratch):
                        raise ValueError("Cargo cache profile workspace escaped its scratch root")
                    transformed = _materialize(workspace, files, variant, transformed)
                    if (workspace / "Cargo.lock").read_bytes() != lock:
                        raise lifecycle.IntegrityError("Cargo cache preflight materialized a different lockfile")
                    with _cargo_environment(definition["build"], target_cache) as cargo_home:
                        _assert_cargo_configuration_absent(workspace, cargo_home)
                        if profile["package_count"] is None:
                            run.run(["cargo", *process_isolation.CARGO_FETCH_ARGS], cwd=workspace,
                                    timeout=1200, network_mode=process_isolation.CARGO_FETCH_NETWORK_MODE)
                        graph = libraries.offline_dependency_metadata(
                            run, workspace, cargo_feature_args=definition["build"]["cargo_feature_args"])
                    if graph.get("lock_sha256") != lock_sha256:
                        raise lifecycle.IntegrityError("Offline Cargo metadata differs from the candidate lockfile")
                    libraries.verify_graph(files["Cargo.lock"], lock, graph, definition["factors"])
                    profile["package_count"] = len(graph.get("packages", {}))
                    profile["status"] = "OFFLINE_CACHE_READY"
                report["verified_profiles"] = list(profiles.values())
                run.save()
        if scratch.exists():
            raise RuntimeError("Cargo cache preflight scratch cleanup was not verified")
        report["status"] = "OFFLINE_CACHE_READY"
        report["failed_variant"] = None
        run.save()
        return report
    except Exception as error:
        report["status"] = "FAILED"
        report["failed_variant"] = failed_variant
        report["error_type"] = type(error).__name__
        run.save()
        raise


def _planned_cargo_cache_variants(design, *, variants=None, pilot_only=False):
    all_variants, interactions = native_matrix()
    if design.get("native_design") != experimental_design.native_design():
        raise ValueError("Frozen combination design differs from the actual native executor")
    if variants is not None:
        if not variants or len(set(variants)) != len(variants) or any(v not in all_variants for v in variants):
            raise ValueError("Unknown/duplicate selected experiment variant")
        all_variants = ["baseline", *[v for v in variants if v != "baseline"]]
    if len(all_variants) > design["limits"]["max_variants"]:
        raise ValueError("Variant panel exceeds declared resource bound")
    cargo_cache_variants = (["baseline"] if pilot_only else
                            list(dict.fromkeys([*all_variants, "lib-rustls-ring"])))
    return all_variants, interactions, cargo_cache_variants


def warm_cargo_cache(source, spec_path, cache_root, *, variants=None, pilot_only=False,
                     target=process_isolation.CARGO_FETCH_ARGS[-1], include_subprojects=True,
                     stage_sdk_artifacts=False, timeout=600, runner_fn=None):
    """Pre-populate and offline-verify the host Cargo cache before network-isolated execution."""
    from cloud_experiments import contenders, libraries

    if target != process_isolation.CARGO_FETCH_ARGS[-1]:
        raise ValueError("Unsupported pinned Linux Cargo cache target")
    if type(include_subprojects) is not bool:
        raise TypeError("Cargo cache subproject selector must be a boolean")
    if type(stage_sdk_artifacts) is not bool:
        raise TypeError("Cargo cache SDK artifact staging selector must be a boolean")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid Cargo cache warmup timeout")
    if (any(k in os.environ for k in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"))
            or any(k.startswith("CARGO_PROFILE_RELEASE_") for k in os.environ)):
        raise RuntimeError("Uncontrolled build/profile environment")
    source = Path(source).resolve(strict=True)
    design = protocol.strict_json_loads(Path(spec_path).read_text(encoding="utf-8"))
    files = verify_snapshot(source, design)
    if "Cargo.lock" not in files or "Cargo.toml" not in files:
        raise ValueError("Cargo cache warmup requires frozen Cargo build inputs")
    _, _, cargo_cache_variants = _planned_cargo_cache_variants(
        design, variants=variants, pilot_only=pilot_only)
    cache = Path(cache_root)
    if cache.is_symlink():
        raise ValueError("Cargo cache root cannot be a symlink")
    cache = cache.parent.resolve(strict=True) / cache.name
    if cache == source or cache.is_relative_to(source):
        raise ValueError("Cargo cache root must be outside the frozen source tree")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = cache.resolve(strict=True)

    class _WarmCacheRun:
        def run(self, command, *, cwd=None, timeout=timeout, log_output=False):
            del log_output
            argv = [str(item) for item in command]
            resolved_cwd = source if cwd is None else Path(cwd)
            if runner_fn is not None:
                raw = runner_fn(argv, cwd=resolved_cwd, timeout=timeout)
                if not isinstance(raw, bytes):
                    raise TypeError("Cargo cache warmup runner must return bytes")
                return raw
            completed = runner.run_bounded_process(
                argv, cwd=resolved_cwd, timeout=timeout,
                output_limit=runner.MAX_CAPTURED_COMMAND_OUTPUT_BYTES,
                env=lifecycle._research_child_environment())
            if completed.status in ("TIMEOUT", "TIMED_OUT"):
                raise TimeoutError("Cargo cache warmup command timed out: " + " ".join(argv))
            if completed.status != "EXITED" or completed.returncode != 0:
                raise subprocess.CalledProcessError(
                    completed.returncode or 1, argv, output=completed.stdout, stderr=completed.stderr)
            return completed.stdout

    warm_run = _WarmCacheRun()
    rust = warm_run.run(["rustc", "-Vv"], cwd=source, timeout=min(60, timeout)).decode()
    if not rust.startswith("rustc 1.99.1 "):
        raise RuntimeError("Pinned Rust1.99.1 required")

    profiles = {}
    subprojects = []
    with tempfile.TemporaryDirectory(prefix="athena-cargo-cache-warm-", dir=cache) as temporary:
        scratch = Path(temporary).resolve(strict=True)
        if not scratch.is_relative_to(cache):
            raise ValueError("Cargo cache warmup scratch directory escaped the owned cache root")
        for index, variant in enumerate(cargo_cache_variants):
            definition = contenders.metadata(variant)
            transformed = contenders.transform(files, variant)
            libraries.verify_build_inputs(files, transformed, definition["factors"])
            lock = transformed.get("Cargo.lock")
            if not isinstance(lock, bytes) or not lock:
                raise ValueError("Cargo cache warmup received an empty candidate lockfile")
            lock_sha256 = hashlib.sha256(lock).hexdigest()
            profile = profiles.setdefault(lock_sha256, {
                "lock_sha256": lock_sha256, "variants": [], "package_count": None, "status": "RUNNING",
            })
            profile["variants"].append(variant)
            with tempfile.TemporaryDirectory(prefix=f"profile-{index:03d}-", dir=scratch) as folder:
                workspace = Path(folder).resolve(strict=True)
                if not workspace.is_relative_to(scratch):
                    raise ValueError("Cargo cache warmup profile workspace escaped its scratch root")
                transformed = _materialize(workspace, files, variant, transformed)
                if (workspace / "Cargo.lock").read_bytes() != lock:
                    raise lifecycle.IntegrityError("Cargo cache warmup materialized a different lockfile")
                with _cargo_environment(definition["build"], cache) as cargo_home:
                    _assert_cargo_configuration_absent(workspace, cargo_home)
                    if profile["package_count"] is None:
                        warm_run.run(["cargo", "fetch", "--locked", "--target", target],
                                     cwd=workspace, timeout=timeout)
                        warm_run.run(["cargo", *process_isolation.CARGO_FETCH_ARGS],
                                     cwd=workspace, timeout=timeout)
                    graph = libraries.offline_dependency_metadata(
                        warm_run, workspace,
                        cargo_feature_args=definition["build"]["cargo_feature_args"],
                        target=target)
                if graph.get("lock_sha256") != lock_sha256:
                    raise lifecycle.IntegrityError("Offline Cargo metadata differs from the candidate lockfile")
                libraries.verify_graph(files["Cargo.lock"], lock, graph, definition["factors"])
                profile["package_count"] = len(graph.get("packages", {}))
                profile["status"] = "OFFLINE_CACHE_READY"
        if include_subprojects or stage_sdk_artifacts:
            for sub_index, prefix in enumerate(("cloud_experiments/mcp_sdk/rust",
                                                "cloud_experiments/lexbor_adapter")):
                manifest_rel = f"{prefix}/Cargo.toml"
                lock_rel = f"{prefix}/Cargo.lock"
                if manifest_rel not in files or lock_rel not in files:
                    raise ValueError("Frozen snapshot lacks required subproject Cargo inputs: " + prefix)
                sub_lock = files[lock_rel]
                sub_lock_sha256 = hashlib.sha256(sub_lock).hexdigest()
                with tempfile.TemporaryDirectory(prefix=f"subproject-{sub_index:02d}-", dir=scratch) as folder:
                    sub_workspace = Path(folder).resolve(strict=True)
                    if not sub_workspace.is_relative_to(scratch):
                        raise ValueError("Cargo cache subproject workspace escaped its scratch root")
                    _materialize(sub_workspace, files, "baseline")
                    prefix_slash = prefix + "/"
                    for name, data in files.items():
                        if name.startswith(prefix_slash):
                            dest = sub_workspace / name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if dest.is_symlink() or not dest.resolve().is_relative_to(sub_workspace):
                                raise ValueError("Subproject file escaped its scratch workspace")
                            dest.write_bytes(data)
                    sub_manifest_dir = (sub_workspace / prefix).resolve(strict=True)
                    if not sub_manifest_dir.is_relative_to(sub_workspace):
                        raise ValueError("Subproject manifest directory escaped its scratch workspace")
                    with _cargo_environment({"env": {}, "rustflags": []}, cache) as cargo_home:
                        _assert_cargo_configuration_absent(sub_manifest_dir, cargo_home)
                        warm_run.run(["cargo", "fetch", "--locked", "--target", target],
                                     cwd=sub_manifest_dir, timeout=timeout)
                        warm_run.run(["cargo", *process_isolation.CARGO_FETCH_ARGS],
                                     cwd=sub_manifest_dir, timeout=timeout)
                    subprojects.append({
                        "project": prefix,
                        "lock_sha256": sub_lock_sha256,
                        "status": "OFFLINE_CACHE_READY",
                    })
    if scratch.exists():
        raise RuntimeError("Cargo cache warmup scratch cleanup was not verified")
    staged_sdk_artifacts = None
    if stage_sdk_artifacts:
        baseline_build = contenders.metadata("baseline")["build"]
        mcp_rust_dir = source / "cloud_experiments" / "mcp_sdk" / "rust"
        mcp_rust_target = cache / "mcp-sdk-rust-target"
        if mcp_rust_target.is_symlink():
            raise ValueError("Rust MCP staging target cannot be a symlink")
        if mcp_rust_target.exists() and not mcp_rust_target.resolve().is_relative_to(cache):
            raise ValueError("Rust MCP staging target escaped the owned cache root")
        with _cargo_environment(baseline_build, cache) as cargo_home:
            _assert_cargo_configuration_absent(mcp_rust_dir, cargo_home)
            warm_run.run(
                ["cargo", "--offline", "build", "--locked", "--release",
                 *baseline_build["cargo_feature_args"], "--bin", "core_probe",
                 "--target-dir", str(cache)],
                cwd=source, timeout=timeout,
            )
            warm_run.run(
                ["cargo", "--offline", "build", "--locked", "--release",
                 "--manifest-path", str(mcp_rust_dir / "Cargo.toml"),
                 "--target-dir", str(mcp_rust_target)],
                cwd=mcp_rust_dir, timeout=timeout,
            )
        mcp_ts_root = cache / "mcp-sdk-ts"
        if mcp_ts_root.is_symlink():
            raise ValueError("TypeScript MCP staging root cannot be a symlink")
        if mcp_ts_root.exists():
            if not mcp_ts_root.resolve().is_relative_to(cache):
                raise ValueError("TypeScript MCP staging root escaped the owned cache root")
            shutil.rmtree(mcp_ts_root)
        mcp_ts_dir = mcp_ts_root / "typescript"
        mcp_ts_dir.mkdir(parents=True, exist_ok=True)
        for rel_src, dest_path in (
            ("cloud_experiments/mcp_sdk/protocol_contract.json", mcp_ts_root / "protocol_contract.json"),
            ("cloud_experiments/mcp_sdk/core_fixtures.json", mcp_ts_root / "core_fixtures.json"),
            ("cloud_experiments/mcp_sdk/typescript/package.json", mcp_ts_dir / "package.json"),
            ("cloud_experiments/mcp_sdk/typescript/package-lock.json", mcp_ts_dir / "package-lock.json"),
            ("cloud_experiments/mcp_sdk/typescript/server.mjs", mcp_ts_dir / "server.mjs"),
        ):
            if rel_src not in files:
                raise ValueError("Frozen snapshot lacks required TypeScript MCP input: " + rel_src)
            dest_path.write_bytes(files[rel_src])
        with _cargo_environment({"env": {}, "rustflags": []}, cache) as cargo_home:
            _assert_cargo_configuration_absent(mcp_ts_dir, cargo_home)
            warm_run.run(
                ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=mcp_ts_dir, timeout=timeout,
            )
            shutil.rmtree(cargo_home / ".npm", ignore_errors=True)
        node_modules_dir = mcp_ts_dir / "node_modules"
        if node_modules_dir.is_dir():
            for bin_dir in sorted(node_modules_dir.rglob(".bin"), reverse=True):
                if bin_dir.is_symlink() or bin_dir.is_file():
                    bin_dir.unlink()
                elif bin_dir.is_dir():
                    shutil.rmtree(bin_dir, ignore_errors=True)
            for entry in sorted(node_modules_dir.rglob("*"), reverse=True):
                if entry.is_symlink():
                    entry.unlink()
        lexbor_commit = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe"
        lexbor_root = cache / "lexbor-v3.0.0"
        lexbor_adapter_target = cache / "lexbor-adapter-target"
        if lexbor_adapter_target.is_symlink():
            raise ValueError("Lexbor adapter staging target cannot be a symlink")
        if lexbor_adapter_target.exists() and not lexbor_adapter_target.resolve().is_relative_to(cache):
            raise ValueError("Lexbor adapter staging target escaped the owned cache root")
        if lexbor_root.is_symlink():
            raise ValueError("Lexbor staging root cannot be a symlink")
        if lexbor_root.exists():
            if not lexbor_root.resolve().is_relative_to(cache):
                raise ValueError("Lexbor staging root escaped the owned cache root")
            shutil.rmtree(lexbor_root)
        if os.name != "nt" or runner_fn is not None:
            with _cargo_environment({
                "env": {
                    "ATHENA_LEXBOR_ARCHIVE_CACHE": str(cache / f"lexbor-{lexbor_commit}.zip"),
                    "ATHENA_LEXBOR_ADAPTER_TARGET_DIR": str(lexbor_adapter_target),
                },
                "rustflags": [],
            }, cache) as cargo_home:
                _assert_cargo_configuration_absent(
                    source / "cloud_experiments" / "lexbor_adapter", cargo_home)
                warm_run.run(
                    ["bash", str(source / "cloud_experiments" / "lexbor" / "build_and_smoke.sh"),
                     str(lexbor_root)],
                    cwd=source, timeout=max(timeout, 900),
                )
        lexbor_build_dir = lexbor_root / "build"
        lexbor_libs = sorted(lexbor_build_dir.rglob("liblexbor_static.a")) if lexbor_build_dir.is_dir() else []
        lexbor_lib_dir = lexbor_libs[0].parent if lexbor_libs else lexbor_build_dir
        staged_sdk_artifacts = {
            "status": "SDK_ARTIFACTS_STAGED",
            "core_probe": str(cache / "release" / ("core_probe.exe" if os.name == "nt" else "core_probe")),
            "rust_sdk_fixture": str(mcp_rust_target / "release"
                                    / ("athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture")),
            "typescript_server": str(mcp_ts_dir / "server.mjs"),
            "typescript_node_modules": str(mcp_ts_dir / "node_modules" / "@modelcontextprotocol" / "server" / "package.json"),
            "lexbor_work_root": str(lexbor_root),
            "lexbor_source_dir": str(lexbor_root / f"lexbor-{lexbor_commit}"),
            "lexbor_build_dir": str(lexbor_build_dir),
            "lexbor_lib_dir": str(lexbor_lib_dir),
            "lexbor_adapter_target": str(lexbor_adapter_target),
        }
    cache_bytes = lifecycle.tree_bytes(cache)
    return {
        "schema": "athena-warm-cargo-cache/1",
        "status": "OFFLINE_CACHE_READY",
        "cache_root": str(cache),
        "cargo_home": str(cache / "cargo-home"),
        "cache_bytes": cache_bytes,
        "target": target,
        "planned_variants": cargo_cache_variants,
        "verified_profiles": list(profiles.values()),
        "subprojects": subprojects,
        "staged_sdk_artifacts": staged_sdk_artifacts,
    }


def _prune_cargo_target_transients(target_cache):
    """Reclaim transient incremental caches and linked debug test executables while keeping .rlib/.rmeta."""
    root = Path(target_cache)
    if root.is_symlink() or not root.is_dir():
        return
    for profile in ("debug", "release"):
        prof_dir = root / profile
        if prof_dir.is_symlink() or not prof_dir.is_dir():
            continue
        inc = prof_dir / "incremental"
        if inc.is_dir() and not inc.is_symlink():
            shutil.rmtree(inc, ignore_errors=True)
    deps = root / "debug" / "deps"
    if deps.is_dir() and not deps.is_symlink():
        for entry in deps.iterdir():
            if (entry.is_file() and not entry.is_symlink()
                    and not entry.suffix and (entry.stat().st_mode & 0o111)):
                entry.unlink(missing_ok=True)


def _build(run, workspace, files, variant, target_cache):
    from cloud_experiments import contenders, libraries
    definition = contenders.metadata(variant)
    build = definition["build"]
    destination = run.directory / "bins" / variant
    previous = run.manifest["snapshots"].get(variant)
    if previous is not None:
        transformed = contenders.transform(files, variant)
        dependency_inputs = libraries.verify_build_inputs(files, transformed, definition["factors"])
        if previous.get("dependency_input_evidence") != dependency_inputs:
            raise lifecycle.IntegrityError("Resumed binary lacks matching pre-build dependency verification")
        _materialize(workspace, files, variant, transformed)
        with _cargo_environment(build, target_cache) as cargo_home:
            _assert_cargo_configuration_absent(workspace, cargo_home)
            graph = libraries.offline_dependency_metadata(
                run, workspace, cargo_feature_args=build["cargo_feature_args"])
        if graph["lock_sha256"] != previous.get("lock_sha256"):
            raise lifecycle.IntegrityError("Resumed dependency graph differs from the recorded lockfile")
        dependency_evidence = libraries.verify_graph(files["Cargo.lock"], transformed["Cargo.lock"],
                                                      graph, definition["factors"])
        if dependency_evidence != previous.get("dependency_evidence"):
            raise lifecycle.IntegrityError("Resumed target dependency features differ from the recorded provider profile")
        campaign.dependency_advisories(run, graph)
        isolation = _verify_build_network_isolation(run)
        prior_isolation = previous.get("build_network_isolation", {})
        if (prior_isolation.get("status") != isolation["status"]
                or prior_isolation.get("host_netns") == prior_isolation.get("child_netns")
                or prior_isolation.get("host_pidns") == prior_isolation.get("child_pidns")
                or prior_isolation.get("metadata_tcp_blocked") is not True
                or prior_isolation.get("public_tcp_blocked") is not True
                or prior_isolation.get("cargo_fetch_policy") != process_isolation.CARGO_FETCH_POLICY):
            raise lifecycle.IntegrityError("Resumed binary lacks matching PID/network isolation evidence")
        if (previous.get("definition") != definition or not previous.get("binaries")
                or destination.is_symlink()
                or any((destination / name).is_symlink() or not (destination / name).is_file()
                       or protocol.sha256_file(destination / name) != value for name, value in previous["binaries"].items())):
            raise lifecycle.IntegrityError("Resumed native binary or definition changed")
        if _needs_wpt_report(variant, definition):
            _verify_resumed_wpt(run, variant, definition, previous)
        return destination
    transformed = contenders.transform(files, variant)
    dependency_inputs = libraries.verify_build_inputs(files, transformed, definition["factors"])
    transformed = _materialize(workspace, files, variant, transformed)
    run.manifest.setdefault("build_inputs", {})[variant] = dependency_inputs
    network_isolation = _verify_build_network_isolation(run)
    network_isolation["cargo_fetch_policy"] = process_isolation.CARGO_FETCH_POLICY
    run.manifest.setdefault("build_network_isolation", {})[variant] = network_isolation
    run.save()
    build_started = time.monotonic()
    _prune_cargo_target_transients(target_cache)
    with _cargo_environment(build, target_cache) as cargo_home:
        _assert_cargo_configuration_absent(workspace, cargo_home)
        run.run(["cargo", *process_isolation.CARGO_FETCH_ARGS], cwd=workspace, timeout=1200,
                network_mode=process_isolation.CARGO_FETCH_NETWORK_MODE)
        prebuild_graph = libraries.offline_dependency_metadata(
            run, workspace, cargo_feature_args=build["cargo_feature_args"])
        if prebuild_graph["lock_sha256"] != dependency_inputs["candidate_lock_sha256"]:
            raise lifecycle.IntegrityError("Pre-build dependency graph differs from the reviewed lockfile")
        campaign.dependency_advisories(run, prebuild_graph)
        _run_isolated_cargo(run, ["cargo", "fmt", "--all"], cwd=workspace, timeout=300)
        _run_isolated_cargo(run, ["cargo", "fmt", "--all", "--", "--check"], cwd=workspace, timeout=300)
        _run_isolated_cargo(run, ["cargo", "--offline", "test", "--locked", "--all-targets", *build["cargo_feature_args"], "-j", "2"], cwd=workspace, timeout=1200)
        wpt_receipt = None
        if _needs_wpt_report(variant, definition):
            wpt_receipt = _run_wpt_report(run, workspace, variant, definition)
        _prune_cargo_target_transients(target_cache)
        _run_isolated_cargo(run, ["cargo", "--offline", "clippy", "--locked", "--all-targets", *build["cargo_feature_args"], "-j", "2", "--", "-D", "warnings"], cwd=workspace, timeout=1200)
        _prune_cargo_target_transients(target_cache)
        _run_isolated_cargo(run, ["cargo", "--offline", "build", "--locked", "--release", "--bins", *build["cargo_feature_args"], "-j", "2"], cwd=workspace, timeout=1800)
        _prune_cargo_target_transients(target_cache)
    if (workspace / "Cargo.lock").read_bytes() != transformed["Cargo.lock"]:
        raise ValueError("Locked dependency graph changed")
    with _cargo_environment(build, target_cache) as cargo_home:
        _assert_cargo_configuration_absent(workspace, cargo_home)
        graph = libraries.offline_dependency_metadata(
            run, workspace, cargo_feature_args=build["cargo_feature_args"])
    if graph["lock_sha256"] != prebuild_graph["lock_sha256"]:
        raise lifecycle.IntegrityError("Built dependency graph differs from the pre-build advisory profile")
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
    snapshot = {"definition": definition,
        "files": {name: protocol.sha256_file(workspace / name) for name in transformed
                  if name.endswith(".rs") and (workspace / name).is_file()},
        "lock_sha256": protocol.sha256_file(workspace / "Cargo.lock"),
        "manifest_sha256": protocol.sha256_file(workspace / "Cargo.toml"),
        "build_wall_seconds": time.monotonic() - build_started,
        "dependency_input_evidence": dependency_inputs,
        "build_network_isolation": network_isolation,
        "dependency_evidence": dependency_evidence,
        "binaries": {p.name: protocol.sha256_file(p) for p in destination.iterdir()}}
    if wpt_receipt is not None:
        snapshot["wpt_report_receipt"] = wpt_receipt
        if variant in run.manifest.get("wpt_comparisons", {}):
            snapshot["wpt_comparison"] = run.manifest["wpt_comparisons"][variant]
    run.manifest["snapshots"][variant] = snapshot
    run.save()
    return destination


def _invoke(run, binary, case, fixture, iterations, cpu):
    command = [binary / "core_probe", case["stage"], fixture, iterations]
    if cpu is not None:
        command = ["taskset", "-c", cpu, *command]
    raw = run.run(command, timeout=180, log_output=False)
    if iterations == 0:
        return raw
    packet = protocol.strict_json_loads(raw)
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
    if scopes == ["html_attr_membership_stage_only"]:
        return [c for c in cases if c["stage"] == "parser" and owners.intersection(c["owners"])]
    if "prepared_stage_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "prepared"]
    if "logical_grid_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "table"]
    if "html_table_stage_only" in scopes and len(scopes) == 1:
        return [c for c in cases if c["stage"] == "html-table"]
    if "service_provider_stage_only" in scopes and len(scopes) == 1:
        return []
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
    from cloud_experiments import contenders, service_lab, service_study
    owner_cases = {}
    variant_case_counts = []
    any_build_factor = False
    for variant in variants:
        if variant == "baseline":
            continue
        meta = contenders.metadata(variant)
        count = len(_variant_cases(variant, phases["confirm"]))
        owners = meta["decisions"]
        if count > 0 and owners:
            variant_case_counts.append(count)
            if "build_factor" in meta["implementation_scopes"]:
                any_build_factor = True
        for owner in owners:
            owner_cases[owner] = max(owner_cases.get(owner, 0), count)
    variant_case_counts.sort(reverse=True)
    max_individual_finalists = min(
        len(variant_case_counts),
        len(owner_cases) * design["selection"]["slots_per_owner"],
    )
    maximum_cases = min(
        sum(owner_cases.values()) * design["selection"]["slots_per_owner"],
        sum(variant_case_counts[:max_individual_finalists]),
        (len(variants) - 1) * len(phases["confirm"]),
    )
    # A predeclared combined finalist adds its affected path; bound by the union of active owners.
    if len(owner_cases) > 1 and len(variant_case_counts) > 1:
        if any_build_factor:
            joint_cases = len(phases["confirm"])
        else:
            all_owners = set(owner_cases)
            joint_cases = len([c for c in phases["confirm"] if all_owners.intersection(c["owners"])])
    else:
        joint_cases = 0
    maximum_cases += joint_cases
    if maximum_cases < 1 or profile.get("not_scientific_samples") is not True:
        raise ValueError("Resource forecast requires actual contender scopes and resource-only full-shape profile")
    maximum_family = maximum_cases * len(design["confirm"]["metrics"])
    draws = max(30000, math.ceil(100 * 2 * maximum_family / design["confirm"]["alpha"]))
    assignments = screen_plan(variants, native_matrix()[1], phases["screen"],
                              cross_cells=design.get("native_design", {}).get("cross_cells", []))["assignments"]
    screen_pairs = sum(len(ids) for v, ids in assignments.items() if v != "baseline")
    denominator = len(phases["screen"]) * 2 * 13
    case_pilot_ns = pilot.get("case_pilot_ns")
    if isinstance(case_pilot_ns, dict) and case_pilot_ns and positive(pilot.get("wall_seconds")):
        pilot_iter_s = {
            cid: max(0.0, float(ns)) * (13.0 / 3.0) / 1e9
            for cid, ns in case_pilot_ns.items()
        }
        spawn_4_s = max(0.0, float(pilot["wall_seconds"]) - sum(pilot_iter_s.values())) / max(1, len(phases["screen"]))
        screen_iter_mult = design["screen"]["blocks"] * (design["screen"]["iterations"] + 10) / 26.0
        screen_spawn_mult = design["screen"]["blocks"] / 2.0
        screen_wall_s = sum(
            pilot_iter_s.get(cid, 0.0) * screen_iter_mult + spawn_4_s * screen_spawn_mult
            for v, ids in assignments.items() if v != "baseline"
            for cid in ids
        )
        confirm_iter_mult = design["confirm"]["blocks"] * (design["confirm"]["iterations"] + 10) / 26.0
        confirm_spawn_mult = float(design["confirm"]["blocks"])
        candidate_confirm_costs = []
        for variant in variants:
            if variant == "baseline":
                continue
            meta = contenders.metadata(variant)
            if not meta["decisions"]:
                continue
            v_screen_cases = _variant_cases(variant, phases["screen"])
            if not v_screen_cases:
                continue
            v_cost = sum(
                pilot_iter_s.get(c["id"], 0.0) * confirm_iter_mult + spawn_4_s * confirm_spawn_mult
                for c in v_screen_cases
            )
            candidate_confirm_costs.append(v_cost)
        candidate_confirm_costs.sort(reverse=True)
        confirm_wall_s = sum(candidate_confirm_costs[:max_individual_finalists])
        if joint_cases > 0:
            if any_build_factor:
                joint_screen_cases = phases["screen"]
            else:
                all_owners = set(owner_cases)
                joint_screen_cases = [c for c in phases["screen"] if all_owners.intersection(c["owners"])]
            confirm_wall_s += sum(
                pilot_iter_s.get(c["id"], 0.0) * confirm_iter_mult + spawn_4_s * confirm_spawn_mult
                for c in joint_screen_cases
            )
        scale = (screen_wall_s + confirm_wall_s) / float(pilot["wall_seconds"])
    else:
        scale = (screen_pairs * design["screen"]["blocks"] * (design["screen"]["iterations"] + 10)
                 + maximum_cases * design["confirm"]["blocks"] * (design["confirm"]["iterations"] + 10)) / denominator

    limits = design["limits"]
    h07_profile = pilot.get("service_analysis_resource_profile")
    h07_reserve = (None if h07_profile is None else service_study.execution_reserve(
        design["service_study"], h07_profile,
        contract_scenarios=len(service_lab.correctness_scenarios()),
        maximum_block_bytes=limits["service_study_block_bytes"],
        maximum_evidence_bytes=limits["service_study_evidence_bytes"]))
    h07_seconds = 0 if h07_reserve is None else h07_reserve["total_wall_seconds"]
    h07_bytes = 0 if h07_reserve is None else h07_reserve["maximum_evidence_bytes"]
    h07_profile_seconds = (0 if h07_reserve is None else
                           math.ceil(pilot.get("service_analysis_profile_wall_seconds", 0)))
    largest_cases = max(len(_variant_cases(v, phases["confirm"])) for v in variants)
    screening_memory = screen_pairs * design["screen"]["blocks"] * design["screen"]["iterations"] * 2 * 64
    confirmation_memory = largest_cases * design["confirm"]["blocks"] * design["confirm"]["iterations"] * 2 * 1024
    h07_peak_memory = 0 if h07_reserve is None else h07_reserve["process_peak_rss_bytes"]
    if h07_reserve is not None and (type(h07_peak_memory) is not int or h07_peak_memory < 1):
        raise ValueError("H07 resource forecast requires measured process peak RSS")
    peak_memory = max(2 * max(pilot["peak_memory_bytes"], h07_peak_memory),
                      screening_memory, confirmation_memory)
    name_lengths = {}
    for name, definition in contenders.definitions().items():
        if definition["implementation_scope"] == "service_provider_stage_only":
            continue
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
    required_evidence = ((screen_rows + confirm_rows + pilot_rows) * row_bytes
                         + maximum_cases * design["confirm"]["blocks"] * 2 * rss_bytes + h07_bytes)
    metadata = limits["metadata_bytes"] + limits["log_bytes"]
    _, fixture_bytes = _fixture_payloads(phases, limits)
    wpt_report_bytes = _validated_wpt_limits(limits)["wpt_reports_total_bytes"]
    export = (limits["evidence_bytes"] + metadata + limits["analysis_bytes"]
              + limits["binary_bytes"] + fixture_bytes + wpt_report_bytes + 64 * 1024 ** 2)
    export_seconds = math.ceil(export / (1024 ** 2)) + 600
    maximum_finalists = min(len(variants) - 1, len(owner_cases) * design["selection"]["slots_per_owner"]) + bool(joint_cases)
    service_reserve = maximum_finalists * limits["command_seconds"]
    return {"wall_seconds": max(1, math.ceil(2 * (3 * pilot["wall_seconds"] * scale
                        + pilot["baseline_build_wall_seconds"] * (len(variants) - 1 + bool(joint_cases))))
                        + service_reserve + h07_seconds + h07_profile_seconds),
            "finalist_service_gate_seconds_reserve": service_reserve,
            "service_reserve_scope": "One configured command-time reserve per possible finalist; not measured service throughput or an independent hard deadline",
            "h07_service_study_reserve": h07_reserve,
            "h07_service_study_seconds_included": h07_seconds,
            "h07_resource_profile_seconds_included": h07_profile_seconds,
            "h07_service_study_evidence_bytes_included": h07_bytes,
            "analysis_seconds": max(1, math.ceil(2 * (profile["seconds_per_case_draw_max"] * draws * maximum_cases
                          + profile.get("setup_wall_seconds", 0) * len(owner_cases)))),
            "export_seconds": export_seconds, "shutdown_seconds": 600,
            "export_seconds_scope": "Planning reserve at 1 MiB/s encoded archive output plus ten-minute shutdown; replace with measured pilot throughput, not a guaranteed lower bound",
            "evidence_bytes": design["limits"]["evidence_bytes"], "build_bytes": design["limits"]["build_bytes"],
            "fixture_bytes": fixture_bytes, "wpt_report_bytes_included": wpt_report_bytes,
            "metadata_bytes": metadata, "analysis_bytes": limits["analysis_bytes"], "export_bytes": export,
            "retained_export_bytes": (lifecycle.MAX_SUPERVISOR_ATTEMPTS - 1) * limits["export_bytes"],
            "maximum_export_snapshots": lifecycle.MAX_SUPERVISOR_ATTEMPTS,
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


def _oracles(run, binary, baseline, cases, paths):
    from cloud_experiments import workloads
    for case in cases:
        actual = _invoke(run, binary, case, paths[case["id"]], 0, None)
        reference = _invoke(run, baseline, case, paths[case["id"]], 0, None)
        if actual != reference:
            raise ValueError("Canonical output parity failed: " + case["id"])
        errors = workloads.envelope_errors(actual, case["fixture"])
        if errors:
            raise ValueError("Independent contract oracle failed: " + case["id"] + ": " + "; ".join(errors))
        if case["stage"] in ("export", "pipeline") and "expected_error" not in case["fixture"]:
            protocol.validate_aae(
                actual,
                expected_source_url=case["fixture"].get("url") or "https://example.org/",
            )
    # Independent literal/content/math/denial oracles are distinct from baseline parity.
    control_case = {"id": "controls", "stage": "controls"}
    control = protocol.strict_json_loads(_invoke(run, binary, control_case, next(iter(paths.values())), 0, None))
    if control.get("status") != "PASS":
        raise ValueError("Independent native controls failed")
    runner.probe_oracles(run, binary)
    oracle_dir = run.directory / ("oracle-" + binary.name)
    oracle_dir.mkdir(exist_ok=True)
    runner.content_oracles(run, binary, oracle_dir, include_pre=True)
    return {"parity_cases": len(cases), "independent_native_controls": control,
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
    manifest = lifecycle.read_json(owned("manifest.json"), 16 * 1024 ** 2)
    frozen = lifecycle.read_json(owned("confirmation-freeze.json"))
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
    expected_rows = 2 * settings["blocks"] * settings["iterations"]
    rows = {case: [] for case in case_ids}
    rss = {case: {} for case in case_ids}
    shards = manifest.get("sample_shards", [])
    actual = (sorted(p.relative_to(directory).as_posix() for p in (directory / "collection").glob("*/samples.jsonl")
                     if not p.parent.name.startswith(".pending-"))
              if manifest.get("collection_layout") == "atomic-blocks/1" else sorted(p.name for p in directory.glob("samples-*.jsonl")))
    if not shards or len(set(shards)) != len(shards) or sorted(shards) != actual:
        raise ValueError("Raw shard inventory is missing, duplicate or altered")
    raw_inputs, receipt_inputs = {}, {}
    for name in shards:
        path = owned(name)
        raw_digest = hashlib.sha256()
        receipt = None
        if manifest.get("collection_layout") == "atomic-blocks/1":
            receipt_path = owned((path.parent / "receipt.json").relative_to(directory).as_posix())
            receipt_bytes = lifecycle.read_bytes(receipt_path)
            receipt = protocol.strict_json_loads(receipt_bytes)
            receipt_inputs[receipt_path.relative_to(directory).as_posix()] = hashlib.sha256(receipt_bytes).hexdigest()
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
                row = protocol.strict_json_loads(line)
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
                    if len(rows[case]) >= expected_rows:
                        raise ValueError("Excess native observations for frozen cohort: " + case)
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
                "raw_inputs": raw_inputs, "receipt_inputs": receipt_inputs, "binary_inputs": binary_inputs,
                "environment_binding": manifest.get("environment_binding"),
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
    protected_roots = (directory / "collection", directory / "bins")
    protected_names = {directory / "manifest.json", directory / "confirmation-freeze.json"}
    if any(path in protected_names or any(path.is_relative_to(root) for root in protected_roots)
           for path in (output, progress_path, lock_path)):
        raise ValueError("Analysis output would replace required evidence")
    with _analysis_lock(lock_path):
        groups, rss, evidence = load_confirmation(directory, variant)
        protected = {directory / name for name in ("manifest.json", "confirmation-freeze.json",
                     *evidence["raw_inputs"], *evidence["receipt_inputs"], *evidence["binary_inputs"])}
        if any(path in protected for path in (output, progress_path, lock_path)):
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
            chunks, size = [], 0
            with progress_path.open("rb") as stream:
                while size <= bound:
                    chunk = stream.read(min(65536, bound - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
            if size > bound:
                raise ValueError("Analysis checkpoint exceeds its byte bound")
            data = (chunks[0] if chunks else b"") if len(chunks) <= 1 else b"".join(chunks)
            progress = protocol.strict_json_loads(data)
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
            run.check("oracle:" + joint, lambda: _oracles(run, binaries[joint], binaries["baseline"],
                _variant_cases(joint, cases), paths))
        selected.append(joint)
    if joint is not None:
        chosen["combined_finalist"] = {"variant": joint, "rule": "Union of compatible owner-selected factors, fixed before confirmation; not an observed sum of speedups"}
    chosen["finalist_service_contracts"] = {}
    for variant in selected:
        scopes = contenders.metadata(variant)["implementation_scopes"]
        if scopes in (["prepared_stage_only"], ["logical_grid_only"], ["html_table_stage_only"],
                      ["service_provider_stage_only"]):
            chosen["finalist_service_contracts"][variant] = {"status": "NOT_APPLICABLE", "scope": scopes}
            continue
        def contracts():
            folder = run.directory / ("finalist-service-" + identity(variant)[:12] + "-" + run.session_id)
            report = execute_service(binaries[variant] / "service_probe", folder, deadline=run.deadline,
                                     isolate_process_tree=run.isolate_children)
            if report["status"] != "CONTRACTS_PASS_WITHIN_SCOPE":
                raise RuntimeError("Finalist service contract gate failed; see actual service-contracts.json")
            return {"status": report["status"], "scenarios": len(report["scenarios"]),
                    "artifact": folder.name + "/service-contracts.json",
                    "sha256": protocol.sha256_file(folder / "service-contracts.json")}
        key = "combined_service_contracts" if variant == joint else "finalist_service_contracts:" + variant
        chosen["finalist_service_contracts"][variant] = run.check(key, contracts)


def _native_disk_admission(output, design, budget, *, resume, execution_request, campaign_deadline_unix):
    limits = design["limits"]
    free_bytes = shutil.disk_usage(output.parent).free
    if type(budget.get("free_bytes")) is not int or budget["free_bytes"] < 0:
        raise ValueError("Invalid reviewed disk capacity")
    if free_bytes < limits["disk_reserve_bytes"]:
        raise lifecycle.StorageGuardError("DISK_RESERVE", "Research disk reserve exhausted before admission")
    previous, reusable = None, 0
    if resume and output.exists():
        previous = lifecycle.read_json(output / "manifest.json", 16 * 1024 ** 2)
        if (previous.get("bundle_id") != design["study_id"] or previous.get("spec") != design
                or previous.get("lifecycle") != "athena-managed-run/1"
                or previous.get("process_isolation_policy") != process_isolation.POLICY
                or previous.get("execution_request") != execution_request
                or previous.get("campaign_deadline_unix") != campaign_deadline_unix):
            raise ValueError("Resume disk ownership identity differs from the frozen run")
        deadline = time.monotonic() + min(limits["command_seconds"], campaign_deadline_unix - time.time())
        paths = [output, output.parent / "cargo-target"]
        exports = 0
        for path in output.parent.iterdir():
            if time.monotonic() >= deadline:
                raise TimeoutError("Resume disk admission exceeded its campaign deadline")
            if lifecycle._is_managed_export_name(output.name, path.name):
                if path.is_symlink() or not path.is_file() or path.stat().st_size > limits["export_bytes"]:
                    raise ValueError("Invalid retained managed export")
                exports += 1
                if exports >= lifecycle.MAX_SUPERVISOR_ATTEMPTS:
                    raise lifecycle.StorageGuardError("EXPORT_CAPACITY", "No export snapshot capacity remains for resume")
                paths.append(path)
        reusable = lifecycle.reusable_storage_bytes(paths, device=output.parent.stat().st_dev, deadline=deadline)
        owned_bound = (sum(limits[k] for k in ("build_bytes", "evidence_bytes", "metadata_bytes", "log_bytes",
                                             "analysis_bytes", "max_fixture_bytes", "wpt_reports_total_bytes"))
                       + lifecycle.MAX_SUPERVISOR_ATTEMPTS * limits["export_bytes"])
        if reusable > owned_bound:
            raise lifecycle.StorageGuardError("RESUME_CAPACITY", "Retained storage exceeds the owned workload envelope")
    if free_bytes + reusable < limits["minimum_free_bytes"]:
        raise RuntimeError("Insufficient free capacity for declared source/cache/evidence")
    return previous, {"physical_free_bytes": free_bytes, "reusable_allocated_bytes": reusable,
                      "admission_bytes": min(budget["free_bytes"],
                                             max(0, free_bytes - limits["disk_reserve_bytes"]) + reusable),
                      "scope": "Allocated single-link bytes in the identity-bound run, owned target cache and managed exports; no source or other-run credit"}


def execute_native(source, spec_path, output, *, budget_path=None, pilot_only=False, variants=None,
                   resume=False, campaign_deadline_unix=None, budget_bytes=None):
    if budget_path is None:
        raise ValueError("Native execution requires reviewed price/credit/supervisor admission facts")
    if budget_bytes is None:
        budget_bytes = lifecycle.read_bytes(budget_path)
    if not isinstance(budget_bytes, bytes):
        raise ValueError("Native execution budget snapshot must be bytes")
    budget = protocol.strict_json_loads(budget_bytes)
    budget_sha256 = hashlib.sha256(budget_bytes).hexdigest()
    if campaign_deadline_unix is None:
        seconds = budget.get("deadline_seconds")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Native execution requires a finite reviewed campaign deadline")
        campaign_deadline_unix = time.time() + seconds
    if type(campaign_deadline_unix) not in (int, float) or not math.isfinite(campaign_deadline_unix):
        raise ValueError("Invalid frozen campaign deadline")
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Run output cannot be a symlink")
    output = output.parent.resolve(strict=True) / output.name
    with _analysis_lock(output.with_name(output.name + ".execution.lock")):
        return _execute_native_locked(source, spec_path, output, budget=budget, budget_sha256=budget_sha256,
                                      pilot_only=pilot_only, variants=variants, resume=resume,
                                      campaign_deadline_unix=campaign_deadline_unix)


def _run_staged_language_panel_checks(run, source, output, cache, baseline_bin_dir):
    """Execute offline L01 and L03 language-panel preflights when staged artifacts are present."""
    technical = run.manifest.setdefault("technical_coverage", {})
    rust_name = "athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture"
    probe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
    rust_override = os.environ.get("ATHENA_MCP_RUST_BIN", "").strip()
    cached_rust_bin = cache / "mcp-sdk-rust-target" / "release" / rust_name
    if rust_override:
        rust_bin = Path(rust_override)
    elif cached_rust_bin.is_file() and not cached_rust_bin.is_symlink():
        rust_bin = cached_rust_bin
    else:
        rust_bin = source / "cloud_experiments" / "mcp_sdk" / "rust" / "target" / "release" / rust_name
    ts_override = os.environ.get("ATHENA_MCP_TS_SERVER", "").strip()
    cached_ts_server = cache / "mcp-sdk-ts" / "typescript" / "server.mjs"
    cached_ts_pkg = cached_ts_server.parent / "node_modules" / "@modelcontextprotocol" / "server" / "package.json"
    if ts_override:
        ts_server = Path(ts_override)
    elif (cached_ts_server.is_file() and not cached_ts_server.is_symlink()
          and cached_ts_pkg.is_file() and not cached_ts_pkg.is_symlink()):
        ts_server = cached_ts_server
    else:
        ts_server = source / "cloud_experiments" / "mcp_sdk" / "typescript" / "server.mjs"
    ts_pkg = ts_server.parent / "node_modules" / "@modelcontextprotocol" / "server" / "package.json"
    probe_override = os.environ.get("ATHENA_CORE_PROBE_BIN", "").strip()
    cached_probe = cache / "release" / probe_name
    baseline_probe = baseline_bin_dir / probe_name
    if probe_override:
        core_probe = Path(probe_override)
    elif cached_probe.is_file() and not cached_probe.is_symlink():
        core_probe = cached_probe
    elif baseline_probe.is_file() and not baseline_probe.is_symlink():
        core_probe = baseline_probe
    else:
        core_probe = source / "target" / "release" / probe_name
    node_exe = shutil.which("node")
    if (rust_bin.is_file() and not rust_bin.is_symlink()
            and ts_server.is_file() and not ts_server.is_symlink()
            and ts_pkg.is_file() and not ts_pkg.is_symlink()
            and core_probe.is_file() and not core_probe.is_symlink()
            and node_exe is not None):
        l03_out = output / "l03-mcp-sdk-preflight.json"

        def run_l03():
            if l03_out.exists() or l03_out.is_symlink():
                l03_out.unlink()
            run.run([
                sys.executable, "-B", "-m", "cloud_experiments.mcp_sdk.driver",
                "--rust-bin", str(rust_bin),
                "--node", str(node_exe),
                "--typescript-server", str(ts_server),
                "--core-probe", str(core_probe),
                "--out", str(l03_out),
            ], cwd=source, timeout=300, log_output=False)
            if l03_out.is_symlink() or not l03_out.is_file():
                raise lifecycle.IntegrityError("L03 MCP SDK preflight did not write its report")
            report = lifecycle.read_json(l03_out, 32 * 1024 * 1024)
            if report.get("status") != "PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT":
                raise RuntimeError("L03 MCP SDK preflight failed")
            return {
                "status": report["status"],
                "artifact": l03_out.name,
                "sha256": protocol.sha256_file(l03_out),
            }

        receipt = run.check("l03_mcp_sdk_preflight", run_l03)
        technical["L03"] = {
            "status": "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED",
            "runnable_verified": False,
            "meaning": "Offline 16-task L03 MCP SDK preflight executed across Rust and TypeScript over stdio and Streamable HTTP; full L03 comparative performance and live-agent obligation remains unverified",
            "artifact": receipt["artifact"],
            "sha256": receipt["sha256"],
        }
    else:
        technical["L03"] = {
            "status": "STAGED_ARTIFACTS_NOT_PRESENT_IN_RUN",
            "runnable_verified": False,
            "meaning": "Run warm-cache --stage-sdk-artifacts before run-native to stage L03 MCP SDK artifacts for full 16-task offline preflight",
        }

    lexbor_commit = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe"
    lexbor_root = cache / "lexbor-v3.0.0"
    src_override = os.environ.get("ATHENA_LEXBOR_SOURCE_DIR", "").strip()
    build_override = os.environ.get("ATHENA_LEXBOR_BUILD_DIR", "").strip()
    lib_override = os.environ.get("ATHENA_LEXBOR_LIB_DIR", "").strip()
    adapter_target_override = os.environ.get("ATHENA_LEXBOR_ADAPTER_TARGET_DIR", "").strip()
    lexbor_source = Path(src_override) if src_override else (lexbor_root / f"lexbor-{lexbor_commit}")
    lexbor_build = Path(build_override) if build_override else (lexbor_root / "build")
    lexbor_adapter_target = (
        Path(adapter_target_override)
        if adapter_target_override
        else (cache / "lexbor-adapter-target")
    )
    if lib_override:
        lexbor_lib = Path(lib_override)
    else:
        libs = sorted(lexbor_build.rglob("liblexbor_static.a")) if lexbor_build.is_dir() else []
        lexbor_lib = libs[0].parent if libs else lexbor_build
    if ((lexbor_source / "CMakeLists.txt").is_file()
            and not lexbor_source.is_symlink()
            and (lexbor_lib / "liblexbor_static.a").is_file()
            and not lexbor_lib.is_symlink()):
        l01_out = output / "l01-lexbor-semantic-parity.json"

        def run_l01():
            with _cargo_environment({
                "env": {
                    "ATHENA_LEXBOR_SOURCE_DIR": str(lexbor_source),
                    "ATHENA_LEXBOR_BUILD_DIR": str(lexbor_build),
                    "ATHENA_LEXBOR_LIB_DIR": str(lexbor_lib),
                    "ATHENA_LEXBOR_ADAPTER_TARGET_DIR": str(lexbor_adapter_target),
                },
                "rustflags": [],
            }, cache) as cargo_home:
                lexbor_adapter_dir = source / "cloud_experiments" / "lexbor_adapter"
                _assert_cargo_configuration_absent(
                    lexbor_adapter_dir if lexbor_adapter_dir.is_dir() else source, cargo_home)
                _run_isolated_cargo(run, [
                    "cargo", "--offline", "test", "--locked",
                    "--manifest-path", str(lexbor_adapter_dir / "Cargo.toml"),
                    "--target-dir", str(lexbor_adapter_target),
                    "--test", "semantic_parity",
                ], cwd=source, timeout=300)
            payload = {
                "schema": "athena-l01-lexbor-semantic-parity/1",
                "status": "PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY",
                "l01_status": "NOT_ADMITTED",
                "commit": lexbor_commit,
                "library_sha256": protocol.sha256_file(lexbor_lib / "liblexbor_static.a"),
            }
            campaign.write_json(l01_out, payload)
            return {
                "status": payload["status"],
                "artifact": l01_out.name,
                "sha256": protocol.sha256_file(l01_out),
            }

        receipt = run.check("l01_lexbor_semantic_parity", run_l01)
        technical["L01"] = {
            "status": "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED",
            "runnable_verified": False,
            "meaning": "Offline L01 Lexbor v3.0.0 6-case semantic parity test executed inside unshare -rn; full L01 native/2 integration and admission gates remain unverified",
            "artifact": receipt["artifact"],
            "sha256": receipt["sha256"],
        }
    else:
        technical["L01"] = {
            "status": "STAGED_ARTIFACTS_NOT_PRESENT_IN_RUN",
            "runnable_verified": False,
            "meaning": "Run warm-cache --stage-sdk-artifacts before run-native to stage Lexbor v3.0.0 C static library for offline L01 semantic parity",
        }
    run.save()
    return technical


def _update_technical_coverage(run):
    """Populate technical_coverage receipts for executed subsets while keeping full obligations fail-closed."""
    from cloud_experiments import contenders
    technical = run.manifest.setdefault("technical_coverage", {})

    def _load_optional_json(*names):
        if getattr(run, "directory", None) is None:
            return None, None
        for name in names:
            path = Path(run.directory) / name
            if path.is_file() and not path.is_symlink():
                return name, lifecycle.read_json(path, 16 * 1024 ** 2)
        return None, None

    def _record_subset(key, meaning, **fields):
        entry = technical.get(key)
        if not isinstance(entry, dict) or entry.get("status") != "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED":
            entry = {
                "status": "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED",
                "runnable_verified": False,
                "meaning": meaning,
            }
            technical[key] = entry
        entry["runnable_verified"] = False
        for k, v in fields.items():
            if v is not None:
                entry[k] = v
        return entry

    wpt_reports = run.manifest.get("wpt_reports") or {}
    wpt_comparisons = run.manifest.get("wpt_comparisons") or {}
    if wpt_reports:
        wpt_fields = {
            "wpt_report_variants": sorted(wpt_reports.keys()),
            "wpt_comparison_variants": sorted(wpt_comparisons.keys()),
        }
        _record_subset(
            "I-HTML-ATTR-MEMBERSHIP",
            "WPT tree-construction and attribute-membership differential reports recorded; full obligation requires held-out corpus and confirmation closure",
            **wpt_fields,
        )
        _record_subset(
            "H04",
            "WPT tree-construction and attribute-membership differential reports recorded; full obligation requires held-out corpus and confirmation closure",
            **wpt_fields,
        )

    l01_entry = technical.get("L01")
    if isinstance(l01_entry, dict) and l01_entry.get("status") == "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED":
        l01_fields = {
            "l01_artifact": l01_entry.get("artifact"),
            "l01_sha256": l01_entry.get("sha256"),
        }
        _record_subset(
            "I-LANG-PARSE",
            "Offline L01 Lexbor v3.0.0 semantic parity subset executed; full two-factor native/2 parser × ownership interaction remains unverified",
            **l01_fields,
        )
        _record_subset(
            "I-HTML-TABLE",
            "Offline L01 table/semantic parity subset executed; full stage-only and whole-path HTML table interaction closure remains unverified",
            **l01_fields,
        )
        for owner in ("H04", "H05", "H06", "H09", "H14"):
            _record_subset(
                owner,
                "Offline L01 Lexbor v3.0.0 semantic parity subset executed; full decision obligation remains unverified",
                **l01_fields,
            )

    l03_entry = technical.get("L03")
    if isinstance(l03_entry, dict) and l03_entry.get("status") == "SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED":
        l03_fields = {
            "l03_artifact": l03_entry.get("artifact"),
            "l03_sha256": l03_entry.get("sha256"),
        }
        _record_subset(
            "I-LANG-AGENT",
            "Offline 16-task L03 MCP SDK preflight executed across Rust and TypeScript over stdio and Streamable HTTP; live-agent comparative interaction remains unverified",
            **l03_fields,
        )
        _record_subset(
            "I-AGENT",
            "Offline 16-task L03 W2 synthetic task and compact-handle expansion preflight executed; live model routing and W1/W3 agent interaction remain unverified",
            **l03_fields,
        )
        for owner in ("H01", "H08", "H11", "H12", "H13", "H14"):
            _record_subset(
                owner,
                "Offline 16-task L03 MCP SDK preflight executed; full decision obligation remains unverified",
                **l03_fields,
            )

    interactions_artifact, interactions_doc = _load_optional_json(
        "screen-interactions.json", "interaction_panels.json",
    )
    if interactions_doc is None and isinstance(run.manifest.get("interaction_panels"), dict):
        interactions_doc = run.manifest["interaction_panels"]
    if isinstance(interactions_doc, dict):
        panels_map = (
            interactions_doc.get("panels")
            if isinstance(interactions_doc.get("panels"), dict)
            else interactions_doc
        )
        for panel_id, panel_data in panels_map.items():
            if not isinstance(panel_data, dict):
                continue
            if panel_data.get("status") == "COMPLETE_CELLS" or bool(panel_data.get("contrasts")):
                _record_subset(
                    panel_id,
                    "Exploratory screening factorial panel executed; full held-out interaction confirmation remains unverified",
                    panel_status=panel_data.get("status", "COMPLETE_CELLS"),
                    contrast_count=len(panel_data.get("contrasts") or ()),
                    cohorts=list(panel_data.get("cohorts") or ()),
                    interaction_artifact=interactions_artifact,
                )

    cross_artifact, cross_doc = _load_optional_json(
        "screen-cross-coverage.json", "cross-coverage.json",
    )
    if cross_doc is None and isinstance(run.manifest.get("cross_coverage"), dict):
        cross_doc = run.manifest["cross_coverage"]
    if isinstance(cross_doc, dict) and cross_doc.get("covered_tuples", 0) > 0:
        _record_subset(
            "I-NATIVE-CROSS-COVERAGE",
            "Mixed-strength covering array screened; full held-out confirmation and unconfounded interaction closure remain unverified",
            coverage_status=cross_doc.get("status"),
            covered_tuples=cross_doc.get("covered_tuples"),
            required_tuples=cross_doc.get("required_tuples"),
            requirements_sha256=cross_doc.get("requirements_sha256"),
            measurement_status=cross_doc.get("measurement_status"),
            artifact=cross_artifact,
        )

    screening_artifact, screening_doc = _load_optional_json("screening.json")
    if screening_doc is None and isinstance(run.manifest.get("screening"), dict):
        screening_doc = run.manifest["screening"]
    if isinstance(screening_doc, dict) and screening_doc:
        screened_by_owner = {}
        whole_path_variants = set()
        all_screened_variants = set()
        for variant, record in screening_doc.items():
            if not isinstance(record, dict):
                continue
            all_screened_variants.add(variant)
            owners = set(record.get("owners") or ())
            try:
                meta = contenders.metadata(variant)
            except ValueError:
                meta = None
            if meta is not None:
                owners.update(meta["decisions"])
                scopes = set(meta.get("implementation_scopes") or ())
                if "dependency_factor" in scopes:
                    owners.add("H09")
                if "html_table_stage_only" in scopes:
                    owners.add("H06")
                if "whole_path" in scopes:
                    whole_path_variants.add(variant)
                    owners.add("H14")
            for owner in sorted(owners):
                screened_by_owner.setdefault(owner, set()).add(variant)
        service_variants = set(run.manifest.get("service_study_variants") or ())
        if service_variants:
            screened_by_owner.setdefault("H07", set()).update(service_variants)
            screened_by_owner.setdefault("H08", set()).update(service_variants)
        fallback_variants = whole_path_variants or all_screened_variants
        if fallback_variants:
            for idx in range(1, 15):
                screened_by_owner.setdefault(f"H{idx:02}", set()).update(fallback_variants)
        for owner, variants_set in sorted(screened_by_owner.items()):
            entry = _record_subset(
                owner,
                "Native screening executed; full obligation requires held-out confirmation, representative corpus, and remaining scope closure",
                screening_artifact=screening_artifact,
            )
            existing = set(entry.get("screened_variants") or ())
            entry["screened_variants"] = sorted(existing.union(variants_set))

    selection_artifact, selection_doc = _load_optional_json("selection.json")
    if selection_doc is None and isinstance(run.manifest.get("selection"), dict):
        selection_doc = run.manifest["selection"]
    if isinstance(selection_doc, dict) and (
        selection_doc.get("selected")
        or selection_doc.get("confirmation_selected")
        or selection_doc.get("combined_finalist")
    ):
        selection_fields = {
            "owner_selected": list(selection_doc.get("owner_selected") or selection_doc.get("selected") or ()),
            "confirmation_selected": list(selection_doc.get("confirmation_selected") or selection_doc.get("selected") or ()),
            "combined_finalist": selection_doc.get("combined_finalist"),
            "selection_artifact": selection_artifact,
        }
        _record_subset(
            "H14",
            "Predeclared combined-finalist and native/service selection gates executed; full deployment envelope and agent-task SLO closure remain unverified",
            **selection_fields,
        )
        _record_subset(
            "I-SYSTEM",
            "Combined native finalist and controlled service contract gates executed; full multi-subsystem load and deployment interaction closure remain unverified",
            **selection_fields,
        )

    h07 = run.manifest.get("h07_service_study") or {}
    if h07.get("status") == "COMPLETE":
        _record_subset(
            "H07",
            "Scoped H07 TLS provider × lifetime study executed; full H07 production service/DNS/SLO obligation remains unverified",
            analysis_sha256=h07.get("analysis_sha256"),
            study_manifest_sha256=h07.get("study_manifest_sha256"),
            performance_decision=h07.get("performance_decision"),
        )
        _record_subset(
            "H08",
            "Controlled 17-scenario service admission, SingleFlight, cancellation, and H07 journey contracts executed; soak and production offered-load SLOs remain unverified",
            h07_study_manifest_sha256=h07.get("study_manifest_sha256"),
        )
        _record_subset(
            "I-ACCESS",
            "Controlled TLS pool/cache/revalidation and SingleFlight service contracts executed; full multi-tenant offered-load and production DNS/origin closure remain unverified",
            h07_analysis_sha256=h07.get("analysis_sha256"),
            h07_study_manifest_sha256=h07.get("study_manifest_sha256"),
        )
        _record_subset(
            "I-SYSTEM",
            "Scoped H07 TLS provider × lifetime study and system service gates executed; full system deployment interaction obligation remains unverified",
            h07_analysis_sha256=h07.get("analysis_sha256"),
            h07_study_manifest_sha256=h07.get("study_manifest_sha256"),
            h07_performance_decision=h07.get("performance_decision"),
        )

    candidate_decisions = run.manifest.get("candidate_decisions") or {}
    for variant, case_decisions in candidate_decisions.items():
        owners = set(contenders.metadata(variant)["decisions"])
        scopes = set(contenders.metadata(variant).get("implementation_scopes") or ())
        if "dependency_factor" in scopes:
            owners.add("H09")
        if "html_table_stage_only" in scopes:
            owners.add("H06")
        for owner in sorted(owners):
            entry = _record_subset(
                owner,
                "Native screening and held-out confirmation executed; full obligation requires representative corpus and remaining scope closure",
            )
            confirmed = entry.setdefault("confirmed_variants", {})
            confirmed[variant] = case_decisions
    return technical


def _execute_native_locked(source, spec_path, output, *, budget, budget_sha256, pilot_only=False, variants=None,
                           resume=False, campaign_deadline_unix):
    """Run real native comparators; incomplete service/model scopes remain explicit."""
    from cloud_experiments import contenders
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    design = protocol.strict_json_loads(Path(spec_path).read_text(encoding="utf-8"))
    files = verify_snapshot(source, design)
    if ROOT.resolve() != source:
        raise RuntimeError("Execute the delivered frozen harness from its own source root")
    if platform.system() != "Linux" or sys.version_info[:2] != (3, 13):
        raise RuntimeError("Native cloud execution requires Linux/Python3.13; preparation supports3.11+")
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("Native cloud execution requires NumPy in the selected Python 3.13 runtime") from error
    numpy_version = np.__version__
    if not isinstance(numpy_version, str) or not numpy_version:
        raise RuntimeError("Selected Python runtime returned an invalid NumPy version")
    previous_manifest, disk_admission = _native_disk_admission(
        output, design, budget, resume=resume,
        execution_request={"pilot_only": pilot_only, "variants": variants, "budget_sha256": budget_sha256},
        campaign_deadline_unix=campaign_deadline_unix)
    limits = design["limits"]
    _validated_wpt_limits(limits)
    physical_memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    enforced_memory = lifecycle.memory_limit_bytes()
    if (enforced_memory is None or budget.get("enforced_memory_bytes") != enforced_memory
            or type(budget.get("memory_bytes")) is not int or budget["memory_bytes"] > physical_memory):
        raise RuntimeError("Actual cgroup/host memory does not match reviewed admission facts")
    budget["free_bytes"] = disk_admission["admission_bytes"]
    pilot_estimate = {"wall_seconds": 3600, "analysis_seconds": 0, "replicas": 1,
                      "evidence_bytes": limits["evidence_bytes"], "build_bytes": limits["build_bytes"],
                      "metadata_bytes": limits["metadata_bytes"] + limits["log_bytes"], "analysis_bytes": limits["analysis_bytes"],
                      "export_bytes": limits["evidence_bytes"] + limits["binary_bytes"] + limits["analysis_bytes"]
                                      + limits["metadata_bytes"] + limits["log_bytes"]
                                      + limits["wpt_reports_total_bytes"] + 64 * 1024 ** 2,
                      "retained_export_bytes": (lifecycle.MAX_SUPERVISOR_ATTEMPTS - 1) * limits["export_bytes"],
                      "export_seconds": math.ceil((limits["evidence_bytes"] + limits["binary_bytes"]
                                      + limits["analysis_bytes"] + limits["metadata_bytes"] + limits["log_bytes"]
                                      + limits["wpt_reports_total_bytes"]
                                      + 64 * 1024 ** 2) / (1024 ** 2)) + 600,
                      "shutdown_seconds": 600,
                      "peak_memory_bytes": budget.get("pilot_memory_bound_bytes"),
                      "memory_reserve_bytes": limits["memory_reserve_bytes"]}
    admission = admit_resources(pilot_estimate, budget)
    if not admission["admitted"]:
        raise RuntimeError("Preparation workload not admitted: " + ",".join(admission["reasons"]))
    if any(k in os.environ for k in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")) or any(k.startswith("CARGO_PROFILE_RELEASE_") for k in os.environ):
        raise RuntimeError("Uncontrolled build/profile environment")
    reserved_seconds = pilot_estimate["export_seconds"] + pilot_estimate["shutdown_seconds"]
    if previous_manifest is not None:
        if previous_manifest.get("campaign_deadline_unix") != campaign_deadline_unix:
            raise ValueError("Resume cannot reset or replace its frozen campaign deadline")
        work_deadline_seconds = previous_manifest.get("deadline_duration_seconds")
        work_deadline_unix = previous_manifest.get("deadline_unix")
        if type(work_deadline_seconds) is not int or work_deadline_seconds < 1:
            raise ValueError("Resume has no frozen work deadline")
        if type(work_deadline_unix) not in (int, float) or not math.isfinite(work_deadline_unix):
            raise ValueError("Resume has no valid frozen work deadline")
    else:
        now = time.time()
        work_deadline_unix = min(now + limits["wall_seconds"], campaign_deadline_unix - reserved_seconds)
        work_deadline_seconds = math.floor(work_deadline_unix - now)
        if work_deadline_seconds < 1:
            raise RuntimeError("No supervised time remains after export and shutdown reserves")
    export_deadline_unix = min(work_deadline_unix + pilot_estimate["export_seconds"],
                               campaign_deadline_unix - pilot_estimate["shutdown_seconds"])
    if (type(export_deadline_unix) not in (int, float) or not math.isfinite(export_deadline_unix)
            or export_deadline_unix <= work_deadline_unix):
        raise RuntimeError("No finite export interval remains inside the campaign deadline")
    if (previous_manifest is not None
            and previous_manifest.get("export_deadline_unix") not in (None, export_deadline_unix)):
        raise ValueError("Resume cannot reset its frozen export deadline")
    run = ResearchRun(output, "expanded-native", design["study_id"], design, resume=resume,
                      isolate_children=True)
    run.sample_stream.close()
    run.sample_stream = EvidenceStream(output, design["limits"]["evidence_bytes"])
    try:
        run.manifest.update(campaign_deadline_unix=campaign_deadline_unix,
                            export_deadline_unix=export_deadline_unix,
                            shutdown_seconds=pilot_estimate["shutdown_seconds"], disk_admission=disk_admission)
        run.save()
        run.bind_deadline(work_deadline_seconds, deadline_unix=work_deadline_unix)
        isolation_evidence = run.check("child-process-isolation",
                                       lambda: _verify_build_network_isolation(run))
        run.manifest["child_process_isolation_evidence"] = isolation_evidence
        run.save()
        requested = {"pilot_only": pilot_only, "variants": variants, "budget_sha256": budget_sha256}
        if run.manifest.get("execution_request", requested) != requested:
            raise ValueError("Resume cannot change planned variants, pilot mode or budget facts")
        run.manifest["execution_request"] = requested
        run.manifest.update(protocol=PROTOCOL, result_authenticity=AUTHENTICITY,
                            current_phase="preflight", overall_decision="HOLD", technical_coverage={})
        rust = run.run(["rustc", "-Vv"]).decode()
        if not rust.startswith("rustc 1.99.1 "):
            raise RuntimeError("Pinned Rust1.99.1 required")
        cpu = str(sorted(os.sched_getaffinity(0))[-1])
        topology = run.run(["lscpu", "--json"], log_output=False).decode()
        stable_topology = [row for row in protocol.strict_json_loads(topology)["lscpu"]
                           if not any(word in row["field"].lower() for word in ("mhz", "bogomips"))]
        environment_binding = identity({"rust": rust, "python": sys.version, "numpy": numpy_version,
                                        "platform": platform.platform(),
                                        "topology": stable_topology, "affinity": sorted(os.sched_getaffinity(0)),
                                        "process_isolation_policy": run.process_isolation_policy})
        if run.manifest.get("environment_binding", environment_binding) != environment_binding:
            raise ValueError("Resume host/compiler/CPU topology changed; start an independent study")
        run.manifest["environment_binding"] = environment_binding
        run.manifest["environment"] = {"rust": rust, "python": sys.version, "numpy": numpy_version,
            "platform": platform.platform(),
            "lscpu": topology, "timing_cpu": cpu,
            "free_bytes_start": shutil.disk_usage(output.parent).free,
            "cpu_scope": "One declared vCPU; host/physical-core independence not inferred",
            "rss_scope": "Fresh process includes input/setup/warmup; not stage allocation accounting"}
        def _run_harness_controls():
            staged_cache = output.parent / "cargo-target"
            rust_name = "athena-mcp-rust-fixture.exe" if os.name == "nt" else "athena-mcp-rust-fixture"
            probe_name = "core_probe.exe" if os.name == "nt" else "core_probe"
            staged_env = {}
            for env_key, candidate in (
                ("ATHENA_MCP_RUST_BIN", staged_cache / "mcp-sdk-rust-target" / "release" / rust_name),
                ("ATHENA_MCP_TS_SERVER", staged_cache / "mcp-sdk-ts" / "typescript" / "server.mjs"),
                ("ATHENA_CORE_PROBE_BIN", staged_cache / "release" / probe_name),
            ):
                if not os.environ.get(env_key, "").strip() and candidate.is_file() and not candidate.is_symlink():
                    staged_env[env_key] = str(candidate)
            cargo_home = staged_cache / "cargo-home"
            if not os.environ.get("CARGO_HOME", "").strip() and cargo_home.is_dir() and not cargo_home.is_symlink():
                staged_env["CARGO_HOME"] = str(cargo_home)
            previous_env = {key: os.environ.get(key) for key in staged_env}
            try:
                os.environ.update(staged_env)
                return run.run([sys.executable, "-B", "-m", "unittest", "cloud_experiments.test_campaign",
                    "cloud_experiments.test_lifecycle", "kaggle_experiments.test_protocol", "model.test_models",
                    "cloud_experiments.service_lab", "cloud_experiments.test_wceb", "cloud_experiments.test_wceb_runner",
                    "cloud_experiments.mcp_sdk.test_driver", "-v"],
                    cwd=source, timeout=600).decode()
            finally:
                for key, old in previous_env.items():
                    if old is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old
        run.check("harness_controls", _run_harness_controls)
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
        all_variants, interactions, cargo_cache_variants = _planned_cargo_cache_variants(
            design, variants=variants, pilot_only=pilot_only)
        run.manifest["planned_variants"] = all_variants
        run.manifest["interaction_cells"] = interactions
        run.manifest["cargo_cache_preflight_variants"] = cargo_cache_variants
        run.manifest["current_phase"] = "cargo-cache-preflight"
        run.check("offline-cargo-cache", lambda: _preflight_cargo_cache(
            run, files, cargo_cache_variants, cache))
        run.manifest["current_phase"] = "build-baseline"
        start = time.monotonic()
        binaries = {"baseline": _build(run, workspace, files, "baseline", cache)}
        build_wall = run.manifest["snapshots"]["baseline"]["build_wall_seconds"]
        baseline_build = run.manifest["snapshots"]["baseline"]["definition"]["build"]
        with _cargo_environment(baseline_build, cache) as cargo_home:
            _assert_cargo_configuration_absent(workspace, cargo_home)
            graph = libraries.offline_dependency_metadata(
                run, workspace, cargo_feature_args=baseline_build["cargo_feature_args"])
        campaign.write_json(output / "dependency-graph.json", graph)
        run.manifest["dependency_advisories"] = campaign.dependency_advisories(run, graph)
        run.check("baseline_oracles", lambda: _oracles(run, binaries["baseline"], binaries["baseline"], phases["screen"], paths["screen"]))
        _run_staged_language_panel_checks(run, source, output, cache, binaries["baseline"])
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
            case_pilot_ns = {
                cid: sum(
                    int(val)
                    for block in blocks
                    for arm in ("baseline", "candidate")
                    for val in block.get(arm, ())
                )
                for cid, blocks in pilot_groups.items()
            }
            pilot = {"status": "COMPLETE", "phase": "pilot", "wall_seconds": pilot_wall,
                     "case_pilot_ns": case_pilot_ns,
                     "baseline_build_wall_seconds": build_wall, "analysis_resource_profile": analysis_profile,
                     "peak_memory_bytes": max(lifecycle.memory_peak_bytes() or 0, run.manifest.get("sampled_process_tree_rss_bytes", 0)),
                     "memory_scope": "Kernel cgroup peak when available plus sampled child tree and parent peak; sampling can miss brief spikes",
                     "precision_status": "PLANNED_NOT_GUARANTEED", "admitted": False,
                     "limits": "Two blocks/three iterations pilot estimates overhead only; not tail precision or host variance"}

        from cloud_experiments import service_study
        service_profile = pilot.get("service_analysis_resource_profile")
        if service_profile is None:
            profile_started = time.monotonic()
            profile_timeout = max(1, min(design["service_study"]["profile_timeout_seconds"],
                                         math.ceil(run.deadline - time.monotonic())))
            service_profile = run.check("h07_analysis_resource_profile", lambda: service_study.resource_profile(
                design["service_study"], max_wall_seconds=profile_timeout))
            pilot["service_analysis_profile_wall_seconds"] = time.monotonic() - profile_started
            pilot["service_analysis_resource_profile"] = service_profile
        else:
            pilot.setdefault("service_analysis_profile_wall_seconds", 0)
        if (service_profile.get("status") != "PROFILED_RESOURCE_ONLY"
                or service_profile.get("synthetic_values_not_measurements") is not True
                or service_profile.get("numpy_version") != numpy_version):
            raise ValueError("H07 resource forecast is not bound to the selected NumPy runtime")
        campaign.write_json(output / "pilot.json", pilot)
        if pilot_only:
            _update_technical_coverage(run)
            run.manifest.update(current_phase="pilot-finished", pilot=pilot,
                coverage=readiness(design, run.manifest["technical_coverage"]),
                stop_reason="Pilot only; no broad screening/confirmation/adoption")
            run.finish()
            return run.manifest
        estimate = forecast_native(all_variants, phases, design, pilot, analysis_profile)
        admission = admit_resources(estimate, budget)
        pilot.update(admitted=admission["admitted"], admission=admission, estimate=estimate)
        campaign.write_json(output / "pilot.json", pilot)
        run.manifest["pilot"] = pilot
        if not admission["admitted"]:
            raise RuntimeError("Expanded workload not admitted: " + ",".join(admission["reasons"]))
        run.manifest.update(current_phase="h07-provider-lifetime-preflight",
                            service_study_variants=["baseline", "lib-rustls-ring"])
        run.save()
        def build_ring_service_profile():
            _build(run, workspace, files, "lib-rustls-ring", cache)
            return {"variant": "lib-rustls-ring",
                    "snapshot_sha256": identity(run.manifest["snapshots"]["lib-rustls-ring"]),
                    "service_probe_sha256": run.manifest["snapshots"]["lib-rustls-ring"]["binaries"]["service_probe"]}
        run.check("h07:ring-build-and-feature-graph", build_ring_service_profile)
        for provider_variant in ("baseline", "lib-rustls-ring"):
            def run_provider_contract(variant=provider_variant):
                folder = output / ("h07-contract-" + variant + "-" + run.session_id)
                report = execute_service(binaries[variant] / "service_probe" if variant == "baseline"
                                         else output / "bins" / variant / "service_probe", folder,
                                         deadline=run.deadline,
                                         isolate_process_tree=run.isolate_children)
                if report["status"] != "CONTRACTS_PASS_WITHIN_SCOPE":
                    raise RuntimeError("H07 service contract gate failed for " + variant)
                return {"status": report["status"], "scenarios": len(report["scenarios"]),
                        "artifact": folder.name + "/service-contracts.json",
                        "sha256": protocol.sha256_file(folder / "service-contracts.json")}
            run.check("h07:service-contracts:" + provider_variant, run_provider_contract)
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
                run.check("oracle:" + variant, lambda: _oracles(run, binaries[variant], binaries["baseline"], cases, paths["screen"]))
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
            report = protocol.strict_json_loads(report_path.read_text(encoding="utf-8"))
            decisions[variant] = {name: item["decision"] for name, item in report["cases"].items()}
            run.manifest["candidate_decisions"] = decisions
            run.save()
        run.check("h07:provider-lifetime-study",
                  lambda: execute_service_study(run, resume=resume))
        _update_technical_coverage(run)
        run.manifest.update(current_phase="finished",
            coverage=readiness(design, run.manifest["technical_coverage"]),
            overall_decision="HOLD", stop_reason="Native panel and scoped H07 provider/lifetime study completed; remaining service/model/dependency/corpus decisions require their actual evidence",
            cleanup="Evidence and compatible cache retained on owned research disk; no production adoption")
    except BaseException as error:
        run.manifest.update(failed_phase=run.manifest.get("current_phase"), current_phase="failed",
                            failure_kind="TIMEOUT" if isinstance(error, TimeoutError) else "EXECUTION_ERROR",
                            overall_decision="HOLD")
        run.finish(error)
        raise
    run.finish()
    return run.manifest


def execute_service(binary, output, *, scenario_ids=None, deadline=None, isolate_process_tree=False):
    from cloud_experiments import service_lab
    if type(isolate_process_tree) is not bool:
        raise TypeError("Service process-isolation selector must be a boolean")
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
            timeout_s = 45
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    raise TimeoutError("Service contract gate reached its admitted wall deadline")
                timeout_s = min(timeout_s, remaining)
            result = service_lab.run_scenario(binary, scenario, output, timeout_s=timeout_s,
                                              isolate_process_tree=isolate_process_tree)
            report["scenarios"].append({"id": scenario["id"], **result})
            campaign.write_json(path, report)
        report["failure_cleanup_control"] = service_lab.cleanup_failure_control(output)
        if protocol.sha256_file(binary) != before:
            raise ValueError("Native executable changed during actual service checks")
        passed = all(x["oracle_passed"] for x in report["scenarios"]) and report["failure_cleanup_control"]["passed"]
        report["status"] = "CONTRACTS_PASS_WITHIN_SCOPE" if passed else "CONTRACT_FAILURE"
        report["scope"] = "Controlled real TLS plus native parse/export/SingleFlight/admission; production Engine success/DNS/TLS deployment remains NOT VERIFIED"
        report["process_isolation_policy"] = (process_isolation.POLICY if isolate_process_tree else "DISABLED")
    except BaseException as error:
        report.update(status="FAILED", error=type(error).__name__ + ": " + str(error))
        campaign.write_json(path, report)
        raise
    campaign.write_json(path, report)
    return report


def _h07_scenario(cell, design):
    from cloud_experiments import service_study
    requests = design["requests_per_cell"]
    return {"id": cell["id"],
            "input": {"protocol": "athena-core-research-service/1", "mode": "journey",
                      "variant": cell["strategy"], "queue_mode": "bounded_wait",
                      "arrivals": [{"task_id": f"t{index:03d}", "at_us": design["interarrival_us"],
                                    "source": "article", "tenant": "A",
                                    "query": "retrieval parser evidence latency",
                                    "epoch": 1, "cache_mode": "bypass"}
                                   for index in range(requests)],
                      "max_pending": 4, "max_origin_inflight": 1, "max_pool_entries": 1,
                      "max_cache_entries": 1, "max_body_bytes": 4096, "deadline_ms": 5000,
                      "pool_lifetime_ms": cell["pool_lifetime_ms"], "fresh_for_ms": 100,
                      "source_singleflight": True, "representation_singleflight": True},
            "expect": {"outcomes": {"completed": requests}, "origin_requests": requests,
                       "handshakes": cell["expected_handshakes"],
                       "client_builds": cell["expected_client_builds"],
                       "block_texts_by_source": {"article": list(service_study.ARTICLE_BLOCK_TEXTS)}}}


def _h07_snapshot(run, variant, provider):
    from cloud_experiments import contenders
    snapshot = run.manifest.get("snapshots", {}).get(variant)
    expected_definition = contenders.metadata(variant)
    if not isinstance(snapshot, dict) or snapshot.get("definition") != expected_definition:
        raise lifecycle.IntegrityError("H07 service binary lacks its exact frozen contender definition")
    provider_evidence = snapshot.get("dependency_evidence", {}).get("tls_provider", {})
    if provider_evidence.get("selected") != provider:
        raise lifecycle.IntegrityError("H07 service binary lacks matching resolved TLS provider evidence")
    binary_name = "service_probe" + (".exe" if os.name == "nt" else "")
    expected_hash = snapshot.get("binaries", {}).get(binary_name)
    binary = run.directory / "bins" / variant / binary_name
    if (not protocol.is_digest(expected_hash) or binary.is_symlink() or not binary.is_file()
            or not binary.resolve().is_relative_to(run.directory.resolve())
            or protocol.sha256_file(binary) != expected_hash):
        raise lifecycle.IntegrityError("H07 service binary differs from its recorded build snapshot")
    return binary, {"variant": variant, "provider": provider, "binary_sha256": expected_hash,
                    "snapshot_sha256": identity(snapshot), "lock_sha256": snapshot.get("lock_sha256"),
                    "dependency_evidence": snapshot.get("dependency_evidence")}


def _h07_validate_block(design, scheduled, block_record):
    from cloud_experiments import service_lab, service_study
    if (not isinstance(block_record, dict) or block_record.get("protocol") != service_study.PROTOCOL
            or block_record.get("block") != scheduled["block"]
            or block_record.get("order") != scheduled["order"]
            or block_record.get("process_isolation_policy") != process_isolation.POLICY
            or block_record.get("tls_lab_cleanup", {}).get("private_fixture_removed") is not True
            or block_record.get("tls_lab_cleanup", {}).get("server_thread_stopped") is not True
            or block_record.get("tls_lab_cleanup", {}).get("port_closed_after_shutdown") is not True):
        raise ValueError("H07 committed block lacks its exact schedule or cleanup proof")
    observations = block_record.get("observations")
    reports = block_record.get("cell_reports")
    if (not isinstance(observations, list) or not isinstance(reports, list)
            or [row.get("cell") for row in observations] != scheduled["order"]
            or [entry.get("cell") for entry in reports] != scheduled["order"]):
        raise ValueError("H07 block omitted or reordered a planned service cell")
    cells = {cell["id"]: cell for cell in design["cells"]}
    for observation, entry in zip(observations, reports):
        service_study.validate_cell_observation(design, observation)
        cell = cells[observation["cell"]]
        report = entry.get("report")
        if (entry.get("cell") != observation["cell"] or not isinstance(report, dict)
                or report.get("oracle_passed") is not True or report.get("process", {}).get("status") != "EXITED"
                or report.get("process", {}).get("exit_code") != 0
                or report.get("process", {}).get("stdout_sha256") != observation["stdout_sha256"]
                or report.get("process", {}).get("cpu_nanoseconds") != observation["process_cpu_ns"]
                or report.get("process", {}).get("peak_rss_kib") != observation["peak_rss_kib"]
                or report.get("origin", {}).get("handshakes") != cell["expected_handshakes"]
                or len(report.get("origin", {}).get("requests", [])) != design["requests_per_cell"]
                or report.get("origin", {}).get("known_body_write_bytes")
                   != service_study.expected_body_write_bytes(design)
                or report.get("origin", {}).get("known_body_write_bytes")
                   != observation.get("response_body_bytes")
                or any(request.get("body_bytes") !=
                       service_study.expected_body_write_bytes(design) // design["requests_per_cell"]
                       for request in report.get("origin", {}).get("requests", []))
                or report.get("probe", {}).get("counters", {}).get("client_builds") != cell["expected_client_builds"]):
            raise ValueError("H07 cell report does not support its measured observation")
        scenario = _h07_scenario(cell, design)
        errors = service_lab.verify_result(
            {**scenario, "lab_options": {"bodies": {"article": service_study.ARTICLE_HTML}}},
            report["probe"], report["origin"])
        if errors:
            raise ValueError("H07 saved service oracle failed: " + "; ".join(errors[:8]))
    return observations


def execute_service_study(run, *, resume=False):
    """Run the predeclared provider × lifetime study inside the admitted native run."""
    from cloud_experiments import service_lab, service_study
    if platform.system() != "Linux" or sys.version_info[:2] != (3, 13):
        raise RuntimeError("H07 service measurements require the admitted Linux/Python 3.13 runtime")
    design = service_study.validate_design(run.manifest.get("spec", {}).get("service_study"))
    limits = run.manifest["spec"]["limits"]
    block_limit = limits["service_study_block_bytes"]
    evidence_limit = limits["service_study_evidence_bytes"]
    if (type(block_limit) is not int or type(evidence_limit) is not int
            or design["blocks"] * block_limit + 32 * 1024 ** 2 > evidence_limit):
        raise ValueError("H07 declared block/evidence limits do not reserve checkpoint and analysis space")
    baseline, baseline_snapshot = _h07_snapshot(run, "baseline", "aws-lc")
    ring, ring_snapshot = _h07_snapshot(run, "lib-rustls-ring", "ring")
    time_binary = Path("/usr/bin/time")
    if time_binary.is_symlink() or not time_binary.is_file():
        raise RuntimeError("H07 requires the admitted GNU time resource collector at /usr/bin/time")
    version = runner.run_bounded_process([str(time_binary), "--version"], timeout=5,
                                         output_limit=64 * 1024)
    if (version.status != "EXITED" or version.returncode != 0
            or b"gnu time" not in (version.stdout + version.stderr).lower()):
        raise RuntimeError("H07 resource collector is not GNU time")
    binary_map = {"aws-lc": baseline, "ring": ring}
    build_evidence = {"aws-lc": baseline_snapshot, "ring": ring_snapshot}
    output = run.directory / "h07-service-study"
    if output.is_symlink() or not output.resolve().is_relative_to(run.directory.resolve()):
        raise ValueError("H07 output escaped the owned research run")
    output.parent.mkdir(parents=True, exist_ok=True)
    blocks_dir = output / "blocks"
    manifest_path = output / "study-manifest.json"
    binding = {"protocol": service_study.PROTOCOL, "design": design,
               "builds": build_evidence, "environment_binding": run.manifest.get("environment_binding"),
               "python": sys.version, "platform": platform.platform(),
               "process_isolation_policy": process_isolation.POLICY,
               "resource_collector_sha256": protocol.sha256_file(time_binary),
               "service_study_source_sha256": protocol.sha256_file(Path(__file__).with_name("service_study.py")),
               "service_lab_source_sha256": protocol.sha256_file(Path(__file__).with_name("service_lab.py")),
               "service_probe_source_sha256": hashlib.sha256(
                   (ROOT / "cloud_experiments" / "service_probe.rs").read_bytes()).hexdigest()}
    binding_sha = identity(binding)
    if not output.exists():
        if resume:
            raise ValueError("Cannot resume H07 study without its owned output directory")
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        blocks_dir.mkdir(mode=0o700)
        state = {"protocol": service_study.PROTOCOL, "binding": binding,
                 "binding_sha256": binding_sha, "status": "RUNNING", "completed_blocks": {},
                 "result_authenticity": service_study.make_design()["result_authenticity"]}
        campaign.write_json(manifest_path, state, max_bytes=16 * 1024 ** 2)
    else:
        if not resume or output.is_symlink() or not output.is_dir() or not blocks_dir.is_dir() or blocks_dir.is_symlink():
            raise ValueError("H07 output already exists; only an exact managed resume is permitted")
        state = lifecycle.read_json(manifest_path, 16 * 1024 ** 2)
        if (state.get("protocol") != service_study.PROTOCOL or state.get("binding") != binding
                or state.get("binding_sha256") != binding_sha
                or state.get("status") not in ("RUNNING", "FAILED", "COMPLETE")):
            raise lifecycle.IntegrityError("H07 resume inputs, binaries, resource tools or environment changed")
    schedule = service_study.block_schedule(design)
    completed = state.get("completed_blocks")
    if not isinstance(completed, dict):
        raise ValueError("H07 checkpoint has no valid block receipt map")
    expected_names = {f"block-{block['block']:03d}.json" for block in schedule}
    actual_names = {path.name for path in blocks_dir.iterdir()}
    if any(path.is_symlink() or not path.is_file() for path in blocks_dir.iterdir()):
        raise ValueError("H07 block directory contains a non-regular artifact")
    if actual_names - {receipt.get("file") for receipt in completed.values()}:
        raise ValueError("H07 has uncommitted block artifacts; preserve and inspect before resume")
    if any(receipt.get("file") not in expected_names for receipt in completed.values()):
        raise ValueError("H07 checkpoint names an unplanned block")
    observations = []
    evidence_bytes = manifest_path.stat().st_size
    for block in schedule:
        key = str(block["block"])
        receipt = completed.get(key)
        if receipt is None:
            continue
        path = blocks_dir / receipt.get("file", "")
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(blocks_dir.resolve())
                or path.stat().st_size != receipt.get("bytes")
                or protocol.sha256_file(path) != receipt.get("sha256")):
            raise lifecycle.IntegrityError("H07 committed block bytes differ from their checkpoint")
        record = lifecycle.read_json(path, block_limit)
        if record.get("binding_sha256") != binding_sha:
            raise lifecycle.IntegrityError("H07 block belongs to a different build or frozen design")
        observations.extend(_h07_validate_block(design, block, record))
        evidence_bytes += path.stat().st_size
    if evidence_bytes > evidence_limit - 32 * 1024 ** 2:
        raise ValueError("H07 existing blocks leave no bounded space for manifest and final analysis")
    if state.get("status") == "COMPLETE":
        analysis_path = output / "analysis.json"
        if (not analysis_path.is_file() or analysis_path.is_symlink()
                or protocol.sha256_file(analysis_path) != state.get("analysis_sha256")):
            raise lifecycle.IntegrityError("H07 completed analysis changed after its receipt")
        analysis = lifecycle.read_json(analysis_path, 16 * 1024 ** 2)
        return {"status": "COMPLETE", "blocks": design["blocks"], "cells": len(design["cells"]),
                "analysis_sha256": state["analysis_sha256"], "analysis": analysis,
                "study_manifest_sha256": protocol.sha256_file(manifest_path)}

    state.update(status="RUNNING", error=None)
    campaign.write_json(manifest_path, state, max_bytes=16 * 1024 ** 2)
    scratch = output / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    try:
        for block in schedule:
            key = str(block["block"])
            if key in completed:
                continue
            run.manifest.update(current_phase="h07-provider-lifetime", current_block=block["block"])
            run.save()
            cell_observations, cell_reports = [], []
            if not getattr(run, "isolate_children", False):
                raise RuntimeError("H07 requires the bound PID/network-isolated ManagedRun")
            items = []
            for cell_id in block["order"]:
                cell = next(item for item in design["cells"] if item["id"] == cell_id)
                items.append({"id": cell_id, "provider": cell["provider"],
                              "scenario": _h07_scenario(cell, design)})
            remaining = run.deadline - time.monotonic()
            if remaining < 1:
                raise TimeoutError("H07 study reached the persisted run deadline")
            worker_timeout = min(remaining, len(items) * 50 + 30)
            run.guard_storage()
            with tempfile.TemporaryDirectory(prefix=f"h07-worker-{block['block']:03d}-", dir=scratch) as folder:
                worker_dir = Path(folder).resolve(strict=True)
                input_path, result_path = worker_dir / "input.json", worker_dir / "result.json"
                worker_input = {"mode": "block", "binary_map": {name: str(path) for name, path in binary_map.items()},
                    "items": items, "scratch_root": str(scratch), "timeout_s": 45,
                    "time_binary": str(time_binary), "lab_options": {"delay_ms": 2,
                    "max_connections": 32, "max_requests": 512,
                    "bodies": {"article": service_study.ARTICLE_HTML}},
                    "result_path": str(result_path), "result_limit_bytes": block_limit}
                encoded_worker_input = json.dumps(worker_input, sort_keys=True, ensure_ascii=False,
                                                  allow_nan=False).encode("utf-8")
                if len(encoded_worker_input) > service_lab.MAX_WORKER_INPUT_BYTES:
                    raise ValueError("H07 worker input exceeds its declared byte cap")
                with input_path.open("xb") as stream:
                    stream.write(encoded_worker_input)
                run.run([sys.executable, "-B", "-m", "cloud_experiments.service_lab",
                         "--worker-input", str(input_path)], cwd=ROOT, timeout=worker_timeout,
                        log_output=False, monitor_build_tree=True)
                if (result_path.is_symlink() or not result_path.is_file()
                        or not result_path.resolve(strict=True).is_relative_to(worker_dir)
                        or result_path.stat().st_size > block_limit):
                    raise lifecycle.IntegrityError("H07 isolated worker did not produce its bounded block")
                worker_result = lifecycle.read_json(result_path, block_limit)
            if Path(folder).exists():
                raise RuntimeError("H07 isolated worker directory cleanup was not verified")
            if (worker_result.get("protocol") != service_lab.PROTOCOL
                    or worker_result.get("execution_isolation") != process_isolation.POLICY):
                raise lifecycle.IntegrityError("H07 isolated worker result has an unknown protocol or boundary")
            worker_reports = worker_result.get("reports")
            if (not isinstance(worker_reports, list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("report"), dict)
                           for item in worker_reports)
                    or [item.get("cell") for item in worker_reports] != block["order"]):
                raise ValueError("H07 isolated worker omitted or reordered its frozen cells")
            for order, entry in enumerate(worker_reports):
                cell_id, report = entry["cell"], entry["report"]
                if report.get("oracle_passed") is not True:
                    raise RuntimeError("H07 controlled service contract failed in " + cell_id +
                                       ": " + "; ".join(report.get("oracle_errors", [])[:8]))
                probe, origin, process = report["probe"], report["origin"], report["process"]
                rows = probe.get("rows", [])
                observation = {"block": block["block"], "order": order, "cell": cell_id,
                    "latencies_ns": [row["latency_ns"] for row in rows],
                    "process_cpu_ns": process.get("cpu_nanoseconds"),
                    "peak_rss_kib": process.get("peak_rss_kib"),
                    "completed": probe.get("counters", {}).get("completed"),
                    "origin_requests": len(origin.get("requests", [])),
                    "handshakes": origin.get("handshakes"),
                    "client_builds": probe.get("counters", {}).get("client_builds"),
                    "response_body_bytes": origin.get("known_body_write_bytes"),
                    "stdout_sha256": process.get("stdout_sha256")}
                service_study.validate_cell_observation(design, observation)
                cell_observations.append(observation)
                cell_reports.append({"cell": cell_id, "report": report})
            lab_cleanup = worker_result.get("tls_lab_cleanup", {})
            if (lab_cleanup.get("private_fixture_removed") is not True
                    or lab_cleanup.get("server_thread_stopped") is not True
                    or lab_cleanup.get("port_closed_after_shutdown") is not True):
                raise RuntimeError("H07 controlled TLS origin cleanup was not verified")
            block_record = {"protocol": service_study.PROTOCOL, "binding_sha256": binding_sha,
                            "block": block["block"], "order": block["order"],
                            "observations": cell_observations, "cell_reports": cell_reports,
                            "tls_lab_cleanup": lab_cleanup,
                            "process_isolation_policy": process_isolation.POLICY}
            _h07_validate_block(design, block, block_record)
            filename = f"block-{block['block']:03d}.json"
            path = blocks_dir / filename
            if path.is_symlink() or path.exists():
                raise ValueError("H07 block commit target is not new")
            run.guard_storage()
            block_bytes = len((json.dumps(block_record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
            if block_bytes > block_limit:
                raise ValueError("H07 block exceeds its declared evidence cap")
            free = shutil.disk_usage(output).free
            if free < (evidence_limit - evidence_bytes + limits["disk_reserve_bytes"]):
                raise RuntimeError("H07 remaining evidence reservation would breach the disk reserve")
            campaign.write_json(path, block_record, max_bytes=block_limit)
            block_size = path.stat().st_size
            evidence_bytes += block_size
            if evidence_bytes > evidence_limit - 32 * 1024 ** 2:
                raise ValueError("H07 blocks exceed their reserved evidence bound")
            completed[key] = {"file": filename, "bytes": block_size, "sha256": protocol.sha256_file(path)}
            state["completed_blocks"] = completed
            state["completed_count"] = len(completed)
            campaign.write_json(manifest_path, state, max_bytes=16 * 1024 ** 2)
            run.manifest.setdefault("h07_service_study", {}).update(
                status="RUNNING", completed_blocks=len(completed), total_blocks=design["blocks"],
                last_committed_block=block["block"], evidence_bytes=evidence_bytes)
            run.save()
            observations.extend(cell_observations)
        observations = []
        for block in schedule:
            record = lifecycle.read_json(blocks_dir / f"block-{block['block']:03d}.json", block_limit)
            observations.extend(_h07_validate_block(design, block, record))
        service_study.validate_observation_grid(design, schedule, observations)
        analysis_deadline = min(run.deadline, time.monotonic() + design["analysis_timeout_seconds"])
        analysis = service_study.analyze(design, schedule, observations, deadline=analysis_deadline)
        analysis_path = output / "analysis.json"
        analysis_bytes = len((json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
        if evidence_bytes + analysis_bytes > evidence_limit:
            raise ValueError("H07 final analysis exceeds its remaining evidence reservation")
        campaign.write_json(analysis_path, analysis, max_bytes=16 * 1024 ** 2)
        state.update(status="COMPLETE", analysis_sha256=protocol.sha256_file(analysis_path),
                     completed_count=len(completed), analysis_summary={
                         "performance_decision": analysis["performance_decision"],
                         "comparisons": {name: item["decision"] for name, item in analysis["comparisons"].items()},
                         "scope": analysis["tls_only_cpu"]})
        campaign.write_json(manifest_path, state, max_bytes=16 * 1024 ** 2)
        final_evidence_bytes = (manifest_path.stat().st_size + analysis_path.stat().st_size
                                + sum((blocks_dir / name).stat().st_size for name in expected_names))
        if final_evidence_bytes > evidence_limit:
            raise ValueError("H07 committed manifest, blocks and analysis exceed the evidence cap")
        run.manifest["h07_service_study"] = {"status": "COMPLETE", "completed_blocks": len(completed),
            "total_blocks": design["blocks"], "cells": len(design["cells"]),
            "evidence_bytes": final_evidence_bytes, "analysis_sha256": state["analysis_sha256"],
            "study_manifest_sha256": protocol.sha256_file(manifest_path),
            "comparison_decisions": state["analysis_summary"]["comparisons"],
            "performance_decision": analysis["performance_decision"]}
        run.save()
        return run.manifest["h07_service_study"]
    except BaseException as error:
        state.update(status="FAILED", error=type(error).__name__ + ": " + str(error)[:2048],
                     completed_count=len(completed), completed_blocks=completed)
        campaign.write_json(manifest_path, state, max_bytes=16 * 1024 ** 2)
        run.manifest["h07_service_study"] = {"status": "FAILED", "completed_blocks": len(completed),
                                             "total_blocks": design["blocks"], "error": state["error"]}
        run.save()
        raise


def execute_agent(binary, output, *, policy="W0", representation="full", model=None, request_cap=10,
                  task_ids=None, provider="nim", confirm_provider_no_charge=False):
    from datetime import datetime, timezone
    from cloud_experiments import agent_lab
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    producer = agent_lab.RustProbeProducer(binary, isolate_process_tree=True)
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
              "run_configuration": {
                  "provider": provider if policy == "W1" else None,
                  "model": model if policy == "W1" else None,
                  "policy": policy,
                  "representation": representation,
                  "request_cap": request_cap,
                  "selected_task_ids": [card["id"] for card in cards],
                  "producer_process_isolation_policy": producer.process_isolation_policy,
                  "task_corpus_sha256": protocol.sha256_file(
                      Path(agent_lab.__file__).with_name("agent_tasks.json")),
              },
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


def merge_agent_panels(input_paths, output, *, split):
    """Merge complete NIM agent-task shards while retaining task-level outcomes."""
    from cloud_experiments import agent_lab

    if split not in ("screen", "confirm"):
        raise ValueError("AGENT_PANEL_INVALID_SPLIT")
    expected_cards = [card for card in agent_lab.tasks() if card["split"] == split]
    expected_ids = [card["id"] for card in expected_cards]
    cards_by_id = {card["id"]: card for card in expected_cards}
    if not expected_ids:
        raise ValueError("AGENT_PANEL_EMPTY_EXPECTED_SPLIT")
    corpus_path = Path(agent_lab.__file__).with_name("agent_tasks.json")
    corpus_sha256 = protocol.sha256_file(corpus_path)
    paths = [Path(path) for path in input_paths]
    if len(paths) < 2 or len(paths) > len(expected_ids):
        raise ValueError("AGENT_PANEL_INVALID_BATCH_COUNT")
    output = Path(output)
    if output.is_symlink() or output.exists() or not output.parent.is_dir():
        raise ValueError("AGENT_PANEL_INVALID_OUTPUT")
    resolved_output = output.resolve()
    resolved_inputs = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("AGENT_PANEL_INVALID_INPUT_PATH")
        resolved = path.resolve(strict=True)
        if resolved == resolved_output or resolved in resolved_inputs:
            raise ValueError("AGENT_PANEL_DUPLICATE_INPUT")
        resolved_inputs.append(resolved)

    shards = []
    condition = None
    max_file_bytes = 10 * 1024 * 1024
    max_tasks_per_shard = 4
    max_turns_per_task = agent_lab._DEFAULT_LIMITS["max_model_turns"]
    for path in paths:
        if path.stat().st_size > max_file_bytes:
            raise ValueError("AGENT_PANEL_INPUT_TOO_LARGE")
        chunks, size = [], 0
        with path.open("rb") as stream:
            while size <= max_file_bytes:
                chunk = stream.read(min(65536, max_file_bytes - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        if size > max_file_bytes:
            raise ValueError("AGENT_PANEL_INPUT_TOO_LARGE")
        raw = (chunks[0] if chunks else b"") if len(chunks) <= 1 else b"".join(chunks)
        input_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            report = protocol.strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("AGENT_PANEL_INVALID_REPORT") from exc
        if (not isinstance(report, dict) or report.get("protocol") != PROTOCOL
                or report.get("kind") != "native-agent-task-panel"
                or report.get("status") != "PANEL_EXECUTED"
                or report.get("decision_sufficiency") != "NOT_EVALUATED"
                or report.get("result_authenticity") != AUTHENTICITY):
            raise ValueError("AGENT_PANEL_INVALID_REPORT")
        config = report.get("run_configuration")
        rows = report.get("tasks")
        if not isinstance(config, dict) or not isinstance(rows, list) or not rows:
            raise ValueError("AGENT_PANEL_INVALID_REPORT")
        task_ids = [row.get("task_id") if isinstance(row, dict) else None for row in rows]
        if (len(rows) > max_tasks_per_shard or any(task_id not in cards_by_id for task_id in task_ids)
                or task_ids != config.get("selected_task_ids")):
            raise ValueError("AGENT_PANEL_INVALID_SHARD_TASKS")
        expected_shard_ids = [task_id for task_id in expected_ids if task_id in set(task_ids)]
        if task_ids != expected_shard_ids:
            raise ValueError("AGENT_PANEL_INVALID_SHARD_TASKS")
        request_cap = config.get("request_cap")
        if (type(request_cap) is not int or request_cap < max_turns_per_task * len(rows)
                or request_cap > 50):
            raise ValueError("AGENT_PANEL_INSUFFICIENT_REQUEST_CAP")
        current_condition = (
            report.get("producer_sha256"), report.get("policy"), report.get("representation"),
            config.get("provider"), config.get("model"), config.get("policy"),
            config.get("representation"), config.get("task_corpus_sha256"),
        )
        if (report.get("policy") != "W1" or report.get("representation") not in ("full", "compact")
                or config.get("provider") != "nim" or not isinstance(config.get("model"), str)
                or not config["model"] or config.get("policy") != report.get("policy")
                or config.get("representation") != report.get("representation")
                or config.get("task_corpus_sha256") != corpus_sha256):
            raise ValueError("AGENT_PANEL_INVALID_CONDITION")
        if condition is None:
            condition = current_condition
        elif current_condition != condition:
            raise ValueError("AGENT_PANEL_MIXED_CONDITION")
        if (type(report.get("task_denominator")) is not int or report["task_denominator"] != len(rows)
                or type(report.get("attempted_tasks")) is not int or report["attempted_tasks"] != len(rows)):
            raise ValueError("AGENT_PANEL_INVALID_TASK_SUMMARY")
        task_successes = 0
        request_count = 0
        for row in rows:
            card = cards_by_id[row["task_id"]]
            usage = row.get("usage")
            model_requests = row.get("model_requests")
            quality = row.get("quality")
            if (row.get("family") != card["family"] or row.get("split") != split
                    or row.get("execution_status") != "COMPLETE"
                    or row.get("actual_model") is not True
                    or row.get("model_boundary") != "LIVE_NVIDIA_NIM"
                    or not isinstance(model_requests, list) or not model_requests
                    or any(not isinstance(req, dict) or req.get("model") != config["model"]
                           for req in model_requests)
                    or not isinstance(usage, dict) or type(usage.get("requests")) is not int
                    or usage["requests"] != len(model_requests)
                    or usage["requests"] < 1 or usage["requests"] > max_turns_per_task
                    or not isinstance(quality, dict)
                    or type(quality.get("contract_satisfied")) is not bool):
                raise ValueError("AGENT_PANEL_INCOMPLETE_TASK")
            request_count += usage["requests"]
            task_successes += quality["contract_satisfied"]
        if (request_count > request_cap or type(report.get("task_successes")) is not int
                or report["task_successes"] != task_successes):
            raise ValueError("AGENT_PANEL_INVALID_TASK_SUMMARY")
        shards.append({
            "path": path,
            "sha256": input_sha256,
            "task_ids": task_ids,
            "request_cap": request_cap,
            "request_count": request_count,
            "tasks": rows,
        })

    observed_ids = [task_id for shard in shards for task_id in shard["task_ids"]]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != set(expected_ids):
        raise ValueError("AGENT_PANEL_COVERAGE_MISMATCH")
    expected_index = {task_id: index for index, task_id in enumerate(expected_ids)}
    shards.sort(key=lambda shard: min(expected_index[task_id] for task_id in shard["task_ids"]))
    task_by_id = {row["task_id"]: row for shard in shards for row in shard["tasks"]}
    tasks = [task_by_id[task_id] for task_id in expected_ids]
    aggregate = {
        "protocol": PROTOCOL,
        "kind": "native-agent-task-panel-aggregate",
        "split": split,
        "producer_sha256": condition[0],
        "policy": condition[1],
        "representation": condition[2],
        "provider": condition[3],
        "model": condition[4],
        "task_corpus_sha256": condition[7],
        "status": "PANEL_EXECUTED",
        "decision_sufficiency": "NOT_EVALUATED",
        "result_authenticity": AUTHENTICITY,
        "expected_task_ids": expected_ids,
        "tasks": tasks,
        "task_successes": sum(row["quality"]["contract_satisfied"] for row in tasks),
        "task_denominator": len(expected_ids),
        "attempted_tasks": len(tasks),
        "request_count": sum(shard["request_count"] for shard in shards),
        "allocated_request_cap": sum(shard["request_cap"] for shard in shards),
        "batches": [{
            "file": shard["path"].name,
            "sha256": shard["sha256"],
            "task_ids": shard["task_ids"],
            "request_cap": shard["request_cap"],
            "request_count": shard["request_count"],
        } for shard in shards],
    }
    campaign.write_json(output, aggregate, max_bytes=32 * 1024 * 1024)
    return aggregate


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
    merge_agent_parser = commands.add_parser("merge-agent")
    merge_agent_parser.add_argument("output")
    merge_agent_parser.add_argument("inputs", nargs="+")
    merge_agent_parser.add_argument("--split", choices=("screen", "confirm"), required=True)
    warm_parser = commands.add_parser("warm-cache")
    for name in ("source", "spec", "cache"):
        warm_parser.add_argument(name)
    warm_parser.add_argument("--pilot-only", action="store_true")
    warm_parser.add_argument("--variants", nargs="+")
    warm_parser.add_argument("--skip-subprojects", action="store_true")
    warm_parser.add_argument("--stage-sdk-artifacts", action="store_true")
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
        report = execute_service(args.binary, args.output, scenario_ids=args.scenarios,
                                 isolate_process_tree=True)
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
    elif args.command == "merge-agent":
        report = merge_agent_panels(args.inputs, args.output, split=args.split)
        result = {
            "status": report["status"], "split": report["split"],
            "task_successes": report["task_successes"],
            "task_denominator": report["task_denominator"],
            "request_count": report["request_count"],
        }
    elif args.command == "warm-cache":
        result = warm_cargo_cache(args.source, args.spec, args.cache,
                                  variants=args.variants, pilot_only=args.pilot_only,
                                  include_subprojects=not args.skip_subprojects,
                                  stage_sdk_artifacts=args.stage_sdk_artifacts)
    else:
        report = execute_native(args.source, args.spec, args.output, budget_path=args.budget_file,
                                pilot_only=args.pilot_only, variants=args.variants, resume=args.resume)
        coverage = report.get("coverage")
        if not isinstance(coverage, dict):
            coverage = {}
        result = {
            "status": report["status"],
            "overall_decision": report["overall_decision"],
            "phase": report["current_phase"],
            "research_readiness": coverage.get("status", "NOT_RECORDED"),
            "missing_obligations": coverage.get("missing", []),
            "blocking_language_panels": coverage.get("blocking_language_panels", []),
            "conditional_language_non_admissions": coverage.get("conditional_language_non_admissions", []),
            "decision_sufficiency": coverage.get("decision_sufficiency", "NOT_EVALUATED"),
        }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
