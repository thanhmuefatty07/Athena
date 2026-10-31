"""Frozen paired provider × client-lifetime study for the real Rust service probe.

The study uses a controlled loopback TLS origin. Its results characterize the
locked Reqwest/Rustls path and do not establish production DNS or deployment
behavior. Randomized macroblocks are the independent sampling unit.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time

from cloud_experiments import experimental_design
from kaggle_experiments import protocol

PROTOCOL = "athena-tls-provider-study/1"
SEED = 81723091
BLOCKS = 24
REQUESTS_PER_CELL = 256
INTERARRIVAL_US = 20_000
SHORT_POOL_LIFETIME_MS = 10
LONG_POOL_LIFETIME_MS = 30_000
ARTICLE_HTML = (
    "<article><h1>Athena retrieval engineering</h1>"
    "<p>Athena retrieves evidence, checks freshness, parses article pages, and ranks results."
    " The retrieval pipeline keeps source order, bounded memory, and exact output contracts.</p>"
    "<p>Search ranking uses the query terms retrieval, parser, evidence, and latency."
    " Reusing a validated connection can reduce repeated TLS handshakes and client setup.</p>"
    "<p>Each response is parsed, projected into the bounded arena, ranked, and serialized"
    " as an AAE result. Provider choice and connection lifetime are measured together.</p>"
    "</article>"
)
ARTICLE_BLOCK_TEXTS = (
    "Athena retrieval engineering",
    "Athena retrieves evidence, checks freshness, parses article pages, and ranks results."
    " The retrieval pipeline keeps source order, bounded memory, and exact output contracts.",
    "Search ranking uses the query terms retrieval, parser, evidence, and latency."
    " Reusing a validated connection can reduce repeated TLS handshakes and client setup.",
    "Each response is parsed, projected into the bounded arena, ranked, and serialized"
    " as an AAE result. Provider choice and connection lifetime are measured together.",
)
ALPHA = 0.05
METRICS = ("latency_p50", "latency_p95", "latency_p99", "process_cpu_per_request", "peak_rss")
QUANTILES = {"latency_p50": 0.5, "latency_p95": 0.95, "latency_p99": 0.99,
             "process_cpu_per_request": 0.5, "peak_rss": 0.5}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _cells(requests_per_cell=REQUESTS_PER_CELL):
    cells = []
    for provider in ("aws-lc", "ring"):
        for strategy, lifetime, expected_reuse in (
                ("client_per_fetch", LONG_POOL_LIFETIME_MS, False),
                ("policy_pool", SHORT_POOL_LIFETIME_MS, False),
                ("policy_pool", LONG_POOL_LIFETIME_MS, True)):
            suffix = {"client_per_fetch": "per-fetch",
                      ("policy_pool", SHORT_POOL_LIFETIME_MS): "pool-short",
                      ("policy_pool", LONG_POOL_LIFETIME_MS): "pool-long"}
            label = suffix[strategy] if strategy == "client_per_fetch" else suffix[(strategy, lifetime)]
            cells.append({"id": f"{provider}-{label}", "provider": provider,
                          "strategy": strategy, "pool_lifetime_ms": lifetime,
                          "expected_client_builds": 1 if expected_reuse else requests_per_cell,
                          "expected_handshakes": 1 if expected_reuse else requests_per_cell,
                          "requests_per_cell": requests_per_cell})
    return cells


def make_design(*, seed=SEED):
    if type(seed) is not int or seed < 0:
        raise ValueError("TLS study requires a fixed nonnegative seed")
    cells = _cells()
    family_size = len(METRICS) * 6
    draws = max(30_000, math.ceil(100 * 2 * family_size / ALPHA))
    design = {
        "protocol": PROTOCOL,
        "schema": 1,
        "seed": seed,
        "blocks": BLOCKS,
        "requests_per_cell": REQUESTS_PER_CELL,
        "interarrival_us": INTERARRIVAL_US,
        "short_pool_lifetime_ms": SHORT_POOL_LIFETIME_MS,
        "long_pool_lifetime_ms": LONG_POOL_LIFETIME_MS,
        "article_fixture_bytes": len(ARTICLE_HTML.encode("utf-8")),
        "article_fixture_sha256": hashlib.sha256(ARTICLE_HTML.encode("utf-8")).hexdigest(),
        "cells": cells,
        "comparisons": [
            {"id": "provider-per-fetch", "baseline": "aws-lc-per-fetch", "candidate": "ring-per-fetch"},
            {"id": "provider-pool-short", "baseline": "aws-lc-pool-short", "candidate": "ring-pool-short"},
            {"id": "provider-pool-long", "baseline": "aws-lc-pool-long", "candidate": "ring-pool-long"},
            {"id": "reuse-aws-lc", "baseline": "aws-lc-per-fetch", "candidate": "aws-lc-pool-long"},
            {"id": "reuse-ring", "baseline": "ring-per-fetch", "candidate": "ring-pool-long"},
            {"id": "provider-by-reuse-interaction", "baseline": "aws-lc-per-fetch", "candidate": "ring-per-fetch",
             "interaction": {"baseline_other": "aws-lc-pool-long", "candidate_other": "ring-pool-long"}},
        ],
        "metrics": list(METRICS),
        "family_size": family_size,
        "alpha": ALPHA,
        "bootstrap_draws": draws,
        "bootstrap_seed_namespace": "athena-h07-service-confirmation",
        "profile_draws": 30_000,
        "profile_repeats": 3,
        "profile_timeout_seconds": 600,
        "analysis_timeout_seconds": 900,
        "tail_samples_per_arm_minimum": 50,
        "result_authenticity": "NOT VERIFIED: runner and origin observer share OS authority",
        "scope": "Same-host synthetic loopback TLS/HTTP1.1, exact Reqwest/Rustls feature profiles, full bounded parse+Projection+AAE path; not production DNS, distributed load, HTTP2/3, or TLS-only CPU",
    }
    design["design_sha256"] = digest(design)
    return design


def validate_design(design):
    if not isinstance(design, dict) or design.get("protocol") != PROTOCOL:
        raise ValueError("Unknown H07 service study protocol")
    expected = make_design(seed=design.get("seed"))
    if design != expected:
        raise ValueError("H07 study differs from its frozen complete factorial design")
    return expected


def block_schedule(design):
    design = validate_design(design)
    cell_ids = [cell["id"] for cell in design["cells"]]
    rng = random.Random(experimental_design.derive_seed(design["seed"], "h07-cell-order"))
    blocks = []
    # Each six-block group puts every cell in every run position once. A seeded
    # random base order varies the groups while keeping period effects balanced.
    for group in range(design["blocks"] // len(cell_ids)):
        order = cell_ids[:]
        rng.shuffle(order)
        if rng.randrange(2):
            order.reverse()
        for rotation in range(len(cell_ids)):
            sequence = order[rotation:] + order[:rotation]
            blocks.append({"block": group * len(cell_ids) + rotation, "order": sequence})
    if len(blocks) != design["blocks"]:
        raise ValueError("H07 design blocks must balance all cell positions")
    return blocks


def validate_schedule(design, schedule):
    expected = block_schedule(design)
    if schedule != expected:
        raise ValueError("H07 schedule is not the complete factorial randomized block schedule")
    return expected


def validate_cell_observation(design, observation):
    design = validate_design(design)
    if not isinstance(observation, dict):
        raise ValueError("H07 observation must be an object")
    cell = next((item for item in design["cells"] if item["id"] == observation.get("cell")), None)
    if cell is None:
        raise ValueError("Unknown H07 service cell")
    count = design["requests_per_cell"]
    latencies = observation.get("latencies_ns")
    if (type(observation.get("block")) is not int or observation["block"] < 0
            or type(observation.get("order")) is not int or observation["order"] < 0
            or not isinstance(latencies, list) or len(latencies) != count
            or any(type(value) is not int or value <= 0 for value in latencies)
            or type(observation.get("process_cpu_ns")) is not int or observation["process_cpu_ns"] <= 0
            or type(observation.get("peak_rss_kib")) is not int or observation["peak_rss_kib"] <= 0
            or observation.get("completed") != count
            or observation.get("origin_requests") != count
            or observation.get("handshakes") != cell["expected_handshakes"]
            or observation.get("client_builds") != cell["expected_client_builds"]
            or type(observation.get("response_body_bytes")) is not int
            or observation["response_body_bytes"] != expected_body_write_bytes(design)
            or not protocol.is_digest(observation.get("stdout_sha256"))):
        raise ValueError("H07 observation violates its task, resource, output or handshake contract")
    return observation


def validate_observation_grid(design, schedule, observations):
    design = validate_design(design)
    schedule = validate_schedule(design, schedule)
    if not isinstance(observations, list):
        raise ValueError("H07 observation grid must be a list")
    expected = {(block["block"], position, cell)
                for block in schedule for position, cell in enumerate(block["order"])}
    observed = set()
    for row in observations:
        validate_cell_observation(design, row)
        key = (row["block"], row["order"], row["cell"])
        if key in observed:
            raise ValueError("H07 observation grid has missing, duplicate, or unplanned cells")
        observed.add(key)
    if observed != expected:
        raise ValueError("H07 observation grid has missing, duplicate, or unplanned cells")
    return []


def _positive_log(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("H07 paired analysis requires positive finite measurements")
    return math.log(value) + 1024.0


def _quantile(values, probability):
    return protocol.quantile(values, probability)


def _ratio_interval(log_interval):
    lower, upper = log_interval
    return [math.exp(lower), math.exp(upper)]


def analyze(design, schedule, observations, *, draws=None, deadline=None):
    design = validate_design(design)
    validate_observation_grid(design, schedule, observations)
    draws = design["bootstrap_draws"] if draws is None else draws
    if type(draws) is not int or draws < max(30_000, math.ceil(100 * 2 * design["family_size"] / design["alpha"])):
        raise ValueError("H07 bootstrap draw count cannot resolve the frozen Bonferroni tail")
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("H07 analysis requires NumPy; the slow pure-Python fallback is not admitted") from error
    by_key = {(row["block"], row["cell"]): row for row in observations}
    cells = {cell["id"]: cell for cell in design["cells"]}
    comparisons = []
    for block in range(design["blocks"]):
        cell_values = {name: by_key[(block, name)] for name in cells}
        comparisons.append(cell_values)
    results, all_intervals = {}, {}
    from cloud_experiments import research
    tail = design["alpha"] / (2 * design["family_size"])
    for comparison in design["comparisons"]:
        baseline, candidate = comparison["baseline"], comparison["candidate"]
        metrics, drift_warnings = {}, []
        comparison_interaction = comparison.get("interaction")
        shared_latency_rounds = None
        shared_latency_intervals = None
        if not comparison_interaction:
            shared_latency_rounds = [{
                "baseline": [_positive_log(value) for value in block[baseline]["latencies_ns"]],
                "candidate": [_positive_log(value) for value in block[candidate]["latencies_ns"]],
            } for block in comparisons]
            shared_latency_intervals = research.bootstrap_intervals(
                shared_latency_rounds, [0.5, 0.95, 0.99], draws,
                experimental_design.derive_seed(design["seed"], design["bootstrap_seed_namespace"],
                                                comparison["id"], "latency-family"), tail,
                deadline=deadline)
        for metric in METRICS:
            interaction = comparison_interaction
            if interaction:
                rounds, block_effects = [], []
                probability = QUANTILES[metric]
                for block in comparisons:
                    if metric.startswith("latency_"):
                        base_effect = (_quantile([math.log(value) for value in block[baseline]["latencies_ns"]], probability)
                                       - _quantile([math.log(value) for value in block[interaction["baseline_other"]]["latencies_ns"]], probability))
                        candidate_effect = (_quantile([math.log(value) for value in block[candidate]["latencies_ns"]], probability)
                                            - _quantile([math.log(value) for value in block[interaction["candidate_other"]]["latencies_ns"]], probability))
                    else:
                        def raw_value(cell_id):
                            row = block[cell_id]
                            if metric == "process_cpu_per_request":
                                return row["process_cpu_ns"] / row["completed"]
                            return row["peak_rss_kib"]
                        base_effect = math.log(raw_value(baseline)) - math.log(raw_value(interaction["baseline_other"]))
                        candidate_effect = math.log(raw_value(candidate)) - math.log(raw_value(interaction["candidate_other"]))
                    block_effects.append((base_effect, candidate_effect))
                shift = max(0.0, 1.0 - min(min(pair) for pair in block_effects))
                rounds = [{"baseline": [base + shift], "candidate": [new + shift]}
                          for base, new in block_effects]
                probabilities = [0.5]
            elif metric.startswith("latency_"):
                probability = QUANTILES[metric]
                rounds = shared_latency_rounds
                probabilities = [probability]
            else:
                def value(block, cell_id):
                    row = block[cell_id]
                    if metric == "process_cpu_per_request":
                        return row["process_cpu_ns"] / row["completed"]
                    return row["peak_rss_kib"]
                rounds = [{"baseline": [_positive_log(value(block, baseline))],
                           "candidate": [_positive_log(value(block, candidate))]}
                          for block in comparisons]
                probabilities = [0.5]
            intervals = (shared_latency_intervals if metric.startswith("latency_") and not interaction
                         else research.bootstrap_intervals(rounds, probabilities, draws,
                              experimental_design.derive_seed(design["seed"], design["bootstrap_seed_namespace"],
                                                              comparison["id"], metric), tail,
                              deadline=deadline))
            log_interval = intervals[probabilities[0]]
            ratio_interval = _ratio_interval(log_interval)
            key = comparison["id"] + ":" + metric
            all_intervals[key] = log_interval
            if interaction:
                effects = [math.exp(candidate_value - base_value)
                           for base_value, candidate_value in block_effects]
                point = protocol.quantile(effects, 0.5)
                lag_values = [new - base for base, new in block_effects]
            elif metric.startswith("latency_"):
                all_baseline = [value for block in comparisons for value in block[baseline]["latencies_ns"]]
                all_candidate = [value for block in comparisons for value in block[candidate]["latencies_ns"]]
                point = _quantile(all_candidate, probabilities[0]) / _quantile(all_baseline, probabilities[0])
                lag_values = [(_quantile([math.log(x) for x in block[candidate]["latencies_ns"]], probabilities[0])
                               - _quantile([math.log(x) for x in block[baseline]["latencies_ns"]], probabilities[0]))
                              for block in comparisons]
            else:
                value = lambda block, cell_id: (block[cell_id]["process_cpu_ns"] / block[cell_id]["completed"]
                                                if metric == "process_cpu_per_request"
                                                else block[cell_id]["peak_rss_kib"])
                base_values = [value(block, baseline) for block in comparisons]
                cand_values = [value(block, candidate) for block in comparisons]
                point = protocol.quantile(cand_values, 0.5) / protocol.quantile(base_values, 0.5)
                lag_values = [math.log(value(block, candidate)) - math.log(value(block, baseline))
                              for block in comparisons]
            lag = protocol.lag1_autocorrelation(lag_values)
            if lag is not None and abs(lag) >= .5:
                drift_warnings.append(metric + ": paired block drift")
            metrics[metric] = {"point_ratio": point, "ratio_interval": ratio_interval,
                               "log_ratio_interval": log_interval,
                               "decision": protocol.interval_decision({metric: log_interval}),
                               "estimand": ("pooled task quantile under block-cluster resampling"
                                            if metric.startswith("latency_") and not interaction else
                                            "paired median block ratio; interaction is ratio-of-ratios"),
                               "paired_block_delta_lag1": lag}
        decisions = [item["decision"] for item in metrics.values()]
        comparison_decision = ("REJECT" if "REJECT" in decisions else
                               "PASS_PERFORMANCE" if all(value == "PASS_PERFORMANCE" for value in decisions)
                               else "HOLD")
        if drift_warnings and comparison_decision != "REJECT":
            comparison_decision = "HOLD"
        results[comparison["id"]] = {"baseline": baseline, "candidate": candidate,
                                     "interaction": comparison.get("interaction"),
                                     "decision": comparison_decision, "metrics": metrics,
                                     "warnings": drift_warnings,
                                     "scope": "Finite same-host synthetic service workload; no optimality or production claim"}
    return {"protocol": PROTOCOL, "design_sha256": design["design_sha256"],
            "family_size": design["family_size"], "alpha": design["alpha"], "draws": draws,
            "tail_probability": tail, "comparisons": results,
            "performance_decision": "PER_COMPARISON_ONLY_NO_GLOBAL_WINNER",
            "tls_only_cpu": "NOT_MEASURED; process CPU includes runtime, TLS, parsing and AAE export",
            "result_authenticity": design["result_authenticity"]}


def resource_profile(design, *, draws=None, repeats=None, max_wall_seconds=None):
    """Profile one exact large cluster contrast; values and repeats are resource-only."""
    design = validate_design(design)
    draws = design["profile_draws"] if draws is None else draws
    repeats = design["profile_repeats"] if repeats is None else repeats
    max_wall_seconds = design["profile_timeout_seconds"] if max_wall_seconds is None else max_wall_seconds
    if (type(draws) is not int or draws < 1000 or type(repeats) is not int or not 1 <= repeats <= 5
            or type(max_wall_seconds) not in (int, float) or not math.isfinite(max_wall_seconds)
            or not 1 <= max_wall_seconds <= 1800):
        raise ValueError("H07 analysis profile must use bounded draws and repeats")
    from cloud_experiments import research
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("H07 resource profile requires NumPy; a pure-Python profile would misstate campaign cost") from error
    baseline, candidate = [], []
    cpu_baseline, cpu_candidate, rss_baseline, rss_candidate = [], [], [], []
    for block in range(design["blocks"]):
        baseline.append([_positive_log(1_000_000 + block * 137 + index % 101)
                         for index in range(design["requests_per_cell"])])
        candidate.append([_positive_log(1_000_029 + block * 139 + index % 103)
                          for index in range(design["requests_per_cell"])])
        cpu_baseline.append(_positive_log(10_000_000 + block * 10_003))
        cpu_candidate.append(_positive_log(10_000_997 + block * 10_007))
        rss_baseline.append(_positive_log(80_000 + block * 13))
        rss_candidate.append(_positive_log(80_007 + block * 17))
    rounds = [{"baseline": baseline[block], "candidate": candidate[block]}
              for block in range(design["blocks"])]
    resource_rounds = [
        {"baseline": [cpu_baseline[block]], "candidate": [cpu_candidate[block]]}
        for block in range(design["blocks"])], [
        {"baseline": [rss_baseline[block]], "candidate": [rss_candidate[block]]}
        for block in range(design["blocks"])],
    tail = design["alpha"] / (2 * design["family_size"])
    durations = []
    deadline = time.monotonic() + max_wall_seconds
    for repeat in range(repeats):
        started = time.perf_counter()
        seed = experimental_design.derive_seed(design["seed"], "h07-resource-profile", repeat)
        research.bootstrap_intervals(rounds, [0.5, 0.95, 0.99], draws, seed, tail, deadline=deadline)
        research.bootstrap_intervals(resource_rounds[0], [0.5], draws, seed + 1, tail, deadline=deadline)
        research.bootstrap_intervals(resource_rounds[1], [0.5], draws, seed + 2, tail, deadline=deadline)
        durations.append(time.perf_counter() - started)
    estimate = math.ceil(max(durations) * 40)
    try:
        import resource
        raw_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        process_peak = raw_peak * (1024 if sys.platform.startswith("linux") else 1)
    except ImportError:
        process_peak = None
    return {"status": "PROFILED_RESOURCE_ONLY", "repeat_wall_seconds": durations,
            "profile_draws": draws, "estimated_full_analysis_seconds": estimate,
            "process_peak_rss_bytes": process_peak,
            "numpy_version": np.__version__,
            "profiled_quantiles": [0.5, 0.95, 0.99], "blocks": design["blocks"],
            "requests_per_cell": design["requests_per_cell"],
            "synthetic_values_not_measurements": True,
            "scope": "One representative direct latency contrast plus whole-process CPU/RSS, repeated three times; scales 40x for five contrasts, interactions, full draws and margin"}


def expected_body_write_bytes(design):
    design = validate_design(design)
    prefix = b"<p>athena-lab tenant=A epoch=1 source=article version=1</p>\n"
    return design["requests_per_cell"] * (len(prefix) + design["article_fixture_bytes"])


def execution_reserve(design, analysis_profile, *, contract_scenarios,
                     maximum_block_bytes=30 * 1024 ** 2,
                     maximum_evidence_bytes=768 * 1024 ** 2):
    """Conservative supervised-time/evidence reserve; not a runtime guarantee."""
    design = validate_design(design)
    durations = (analysis_profile.get("repeat_wall_seconds")
                 if isinstance(analysis_profile, dict) else None)
    if (not isinstance(analysis_profile, dict)
            or type(contract_scenarios) is not int or contract_scenarios < 1
            or type(maximum_block_bytes) is not int or maximum_block_bytes < 1
            or type(maximum_evidence_bytes) is not int or maximum_evidence_bytes < 32 * 1024 ** 2
            or design["blocks"] * maximum_block_bytes + 32 * 1024 ** 2 > maximum_evidence_bytes
            or analysis_profile.get("status") != "PROFILED_RESOURCE_ONLY"
            or analysis_profile.get("profile_draws") != design["profile_draws"]
            or not isinstance(durations, list) or len(durations) != design["profile_repeats"]
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                   for value in durations)
            or analysis_profile.get("synthetic_values_not_measurements") is not True
            or type(analysis_profile.get("estimated_full_analysis_seconds")) is not int
            or analysis_profile["estimated_full_analysis_seconds"] <= 0
            or analysis_profile["estimated_full_analysis_seconds"] != math.ceil(max(durations) * 40)
            or 2 * analysis_profile["estimated_full_analysis_seconds"] > design["analysis_timeout_seconds"]
            or not isinstance(analysis_profile.get("numpy_version"), str)):
        raise ValueError("H07 reserve requires the exact bounded resource-only profile and actual contract scenario count")
    cell_count = len(design["cells"])
    lab_setup_timeout = 45  # version(5s) + four bounded certificate commands(10s each)
    probe_timeout = 45
    measurement_seconds = (design["blocks"] * cell_count * probe_timeout
                           + design["blocks"] * lab_setup_timeout)
    contract_seconds = 2 * (probe_timeout + max(0, contract_scenarios - 1)
                            * (probe_timeout + lab_setup_timeout))
    ring_build_timeout = 6000  # fetch, fmt, tests, clippy, release build, each using fixed command caps
    analysis_seconds = math.ceil(2 * analysis_profile["estimated_full_analysis_seconds"])
    return {"measurement_seconds": measurement_seconds,
            "provider_contract_seconds": contract_seconds,
            "ring_build_seconds": ring_build_timeout,
            "analysis_seconds": analysis_seconds,
            "total_wall_seconds": measurement_seconds + contract_seconds + ring_build_timeout + analysis_seconds,
            "maximum_evidence_bytes": maximum_evidence_bytes,
            "maximum_block_bytes": maximum_block_bytes,
             "process_peak_rss_bytes": analysis_profile["process_peak_rss_bytes"],
            "limits_scope": "Worst-case per-command timeouts and output caps; excludes cloud setup and cannot guarantee process scheduling",
            "analysis_profile": analysis_profile}
