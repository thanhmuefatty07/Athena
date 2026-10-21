"""Evidence contracts and paired statistics shared by generated Kaggle scripts."""
import hashlib
import json
import math
from pathlib import Path
import random

PROTOCOL_VERSION = "athena-research/2"
STUDY_REVISION = "2.1"
ANALYSIS_POLICY = "paired-rounds/2.1"
LEGACY_ANALYSIS_POLICY = "paired-rounds/2.0"
MAX_MEASUREMENT_BYTES = 512 * 1024 * 1024
MAX_MEASUREMENT_LINE_BYTES = 1024 * 1024
MAX_MEASUREMENT_RECORDS = 2_000_000
MAX_BOOTSTRAP_DRAWS = 1_000_000
C1_STAGES = ("rank", "table", "parse", "export", "export_no_query", "pipeline", "pipeline_no_query")
QUANTILES = (0.50, 0.95, 0.99)
REQUIRED_CHECKS = {
    "c1": ("environment", "source_integrity", "build_baseline", "build_candidate",
           "content_baseline", "content_candidate", "probe_baseline", "probe_candidate",
           "candidate_preformatted", "variant_identity", "byte_parity"),
    "c2": ("environment", "source_integrity", "build_baseline", "build_candidate",
           "candidate_content", "variant_identity", "deep_dom"),
    "c3": ("environment", "source_integrity", "build_candidate", "build_rng-upgrade",
           "core_oracles", "singleflight", "ip_classifier", "engine_resource_regressions", "rng_distribution"),
}


def declared_analysis_policy(spec):
    revision = spec.get("study_revision", "2.0")
    expected = {"2.0": LEGACY_ANALYSIS_POLICY, STUDY_REVISION: ANALYSIS_POLICY}.get(revision)
    if expected is None or spec.get("analysis_policy", LEGACY_ANALYSIS_POLICY) != expected:
        raise ValueError("Unknown or inconsistent predeclared analysis policy")
    return expected


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def quantile(values, probability):
    if not values or not 0 <= probability <= 1:
        raise ValueError("Quantile requires nonempty data and probability in [0,1]")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Nonfinite measurement")
    ordered = sorted(values)
    return ordered[round(probability * (len(ordered) - 1))]


def lag1_autocorrelation(values):
    if len(values) < 3:
        return None
    mean = math.fsum(values) / len(values)
    denominator = math.fsum((value - mean) ** 2 for value in values)
    if denominator == 0:
        return None
    return math.fsum((a - mean) * (b - mean)
                     for a, b in zip(values, values[1:])) / denominator


def interval_decision(intervals):
    if not intervals:
        return "HOLD"
    for lower, upper in intervals.values():
        if not (math.isfinite(lower) and math.isfinite(upper) and lower <= upper):
            raise ValueError("Invalid confidence interval")
    if any(lower > 0 for lower, _ in intervals.values()):
        return "REJECT"
    if all(upper <= 0 for _, upper in intervals.values()) and any(
            upper < 0 for _, upper in intervals.values()):
        return "PASS_PERFORMANCE"
    return "HOLD"


def load_measurements(path, manifest=None):
    """Reject incomplete pairs, missing/duplicate iterations, and stale evidence."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Measurement artifact is not a regular file")
    maximum_bytes = MAX_MEASUREMENT_BYTES
    if manifest is not None:
        declared = manifest.get("spec", {}).get("limits", {}).get("measurement_bytes", maximum_bytes)
        if type(declared) is not int or not 1 <= declared <= MAX_MEASUREMENT_BYTES:
            raise ValueError("Invalid measurement byte limit")
        maximum_bytes = declared
    if path.stat().st_size > maximum_bytes:
        raise ValueError("Measurement artifact exceeds byte limit")
    specifications = None
    policy = LEGACY_ANALYSIS_POLICY
    if manifest is not None:
        if manifest.get("protocol") != PROTOCOL_VERSION or manifest.get("status") != "COMPLETE":
            raise ValueError("Manifest is not a complete v2 experiment")
        policy = declared_analysis_policy(manifest.get("spec", {}))
        required = REQUIRED_CHECKS.get(manifest.get("cluster"))
        if required and policy == ANALYSIS_POLICY:
            required += {"c1": (), "c2": ("rss_tool",), "c3": ("rng_dependencies",)}[manifest["cluster"]]
        if not required or any(manifest.get("checks", {}).get(name, {}).get("status") != "PASS"
                               for name in required):
            raise ValueError("Required provenance/correctness gates did not pass")
        if any(check.get("status") == "FAIL" for check in manifest["checks"].values()):
            raise ValueError("A failed check cannot be overridden by COMPLETE")
        if (not is_digest(manifest.get("bundle_id"))
                or manifest.get("spec", {}).get("run_kind") not in ("screen", "confirm")):
            raise ValueError("Missing bundle identity or experiment kind")
        built_arms = ("candidate", "rng-upgrade") if manifest.get("cluster") == "c3" else ("baseline", "candidate")
        if any(arm not in manifest.get("snapshots", {}) for arm in built_arms):
            raise ValueError("Missing compiled source variants")
        for arm in built_arms:
            snapshot = manifest["snapshots"][arm]
            binaries = snapshot.get("binaries", {})
            if (set(binaries) != {"athena-core-v2", "bench_core", "research_probe"}
                    or any(not is_digest(value) for value in binaries.values())
                    or not is_digest(snapshot.get("dependency_graph", {}).get("lock_sha256"))):
                raise ValueError("Missing compiled binary/lock identity")
        if not manifest.get("snapshots"):
            raise ValueError("Missing source provenance")
        if manifest.get("artifacts", {}).get(path.name) != sha256_file(path):
            raise ValueError("Raw data hash does not match manifest")
        specifications = {case["id"]: case for case in manifest["spec"]["cases"]}
        if len(specifications) != len(manifest["spec"]["cases"]):
            raise ValueError("Duplicate case specification")
        family = manifest["spec"].get("simultaneous_family_size")
        if policy == ANALYSIS_POLICY and (family != 57 or (manifest["cluster"] == "c1" and (
                set(specifications) != set(C1_STAGES)
                or any(case["id"] != case["stage"] for case in specifications.values())))):
            raise ValueError("Study 2.1 requires its complete predeclared family, including export_no_query")
        if (type(family) is not int or family < sum(len(case["quantiles"]) for case in specifications.values())
                or manifest["spec"].get("family_alpha") != 0.05
                or manifest["spec"].get("drift_threshold") != 0.5):
            raise ValueError("Missing or inconsistent predeclared statistical family")
    groups, identifiers, records, fingerprints = {}, set(), 0, {}
    with path.open("rb") as stream:
        line_number = 0
        while True:
            raw_line = stream.readline(MAX_MEASUREMENT_LINE_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            if line_number > MAX_MEASUREMENT_RECORDS:
                raise ValueError("Measurement artifact exceeds record limit")
            if len(raw_line) > MAX_MEASUREMENT_LINE_BYTES:
                raise ValueError("Measurement record exceeds line limit")
            line = raw_line.decode("utf-8")
            record = json.loads(line)
            case_id = record.get("case_id", record.get("stage"))
            arm = record.get("candidate")
            round_id, iteration = record.get("round"), record.get("iteration")
            if (not isinstance(case_id, str) or arm not in ("baseline", "candidate")
                    or type(round_id) is not int or round_id < 0
                    or type(iteration) is not int or iteration < 0):
                raise ValueError(f"Invalid sample identity at line {line_number}")
            value = record.get("value", record.get("duration_ms"))
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid measurement at line {line_number}")
            if record.get("status", "PASS" if manifest is None else None) != "PASS":
                raise ValueError(f"Failed or unclassified sample at line {line_number}")
            if manifest is not None:
                if (record.get("protocol") != PROTOCOL_VERSION
                        or record.get("bundle_id") != manifest.get("bundle_id")
                        or case_id not in specifications):
                    raise ValueError("Sample provenance or case does not match manifest")
                expected_case = specifications[case_id]
                if record.get("unit") != expected_case["unit"]:
                    raise ValueError("Measurement unit does not match case")
                if record.get("size") != expected_case.get("size"):
                    raise ValueError("Workload size does not match case")
                if record.get("stage") != expected_case["stage"]:
                    raise ValueError("Measured stage does not match case")
                if expected_case["unit"] == "ms":
                    duration, offset = record.get("duration_ns"), record.get("measurement_offset_ns")
                    if (type(duration) is not int or duration <= 0 or value != duration / 1e6
                            or type(offset) is not int or offset < 0):
                        raise ValueError("Missing Rust timing evidence or inconsistent ns-to-ms conversion")
                elif expected_case["unit"] == "KiB":
                    if type(value) is not int:
                        raise ValueError("Peak RSS must be integer KiB")
                elif expected_case["unit"] == "ns/draw":
                    packet = record.get("diagnostics", {})
                    if (packet.get("n") != expected_case["size"]
                            or type(packet.get("duration_ns")) is not int or packet["duration_ns"] <= 0
                            or value != packet["duration_ns"] / packet["n"]
                            or record.get("seed") != 1009 + round_id):
                        raise ValueError("RNG block timing or seed does not match experiment")
                else:
                    raise ValueError("Unknown measurement unit")
                if expected_case["stage"] != "rng":
                    fingerprint = record.get("output_fingerprint")
                    if not is_digest(fingerprint) or fingerprints.setdefault(case_id, fingerprint) != fingerprint:
                        raise ValueError("Missing or divergent output fingerprint")
                expected_order = (0 if arm == "baseline" else 1) ^ (round_id % 2)
                if record.get("order_in_round") != expected_order:
                    raise ValueError("Paired order does not match specification")
            identity = (case_id, arm, round_id, iteration)
            if identity in identifiers:
                raise ValueError(f"Duplicate sample at line {line_number}")
            identifiers.add(identity)
            groups.setdefault(case_id, {}).setdefault(round_id, {}).setdefault(arm, {})[iteration] = value
            records += 1
    if not records:
        raise ValueError("No samples")
    if specifications is not None and set(groups) != set(specifications):
        raise ValueError("Missing or unexpected experimental cases")
    result = {}
    for case_id, rounds in groups.items():
        keys = sorted(rounds)
        if keys != list(range(len(keys))) or len(keys) < 2:
            raise ValueError("Missing rounds or fewer than two paired blocks")
        spec = specifications[case_id] if specifications is not None else None
        if spec and len(keys) != spec["rounds"]:
            raise ValueError("Round count does not match specification")
        normalized, count = [], None
        for round_id in keys:
            pair = rounds[round_id]
            if set(pair) != {"baseline", "candidate"}:
                raise ValueError("Unpaired round")
            sizes = {len(values) for values in pair.values()}
            if len(sizes) != 1:
                raise ValueError("Unequal arm sizes")
            current_count = sizes.pop()
            if count is None:
                count = current_count
            if current_count != count or any(sorted(values) != list(range(count)) for values in pair.values()):
                raise ValueError("Missing iterations or unequal block sizes")
            if spec and count != spec["iterations"]:
                raise ValueError("Iteration count does not match specification")
            normalized.append({arm: [pair[arm][i] for i in range(count)] for arm in pair})
        result[case_id] = {"rounds": normalized, "spec": spec,
                            "analysis_policy": policy,
                           "quantiles": tuple(spec.get("quantiles", QUANTILES)) if spec else QUANTILES,
                           "family_size": manifest["spec"]["simultaneous_family_size"] if manifest else 0,
                           "screen": manifest is not None and manifest["spec"]["run_kind"] == "screen"}
    return result


def _weighted_quantiles(ordered, group_ids, weights, probabilities):
    size = sum(weights[group] for group in group_ids)
    positions = [round(probability * (size - 1)) for probability in probabilities]
    results, cumulative, target = [], 0, 0
    for value, group in zip(ordered, group_ids):
        cumulative += weights[group]
        while target < len(positions) and cumulative > positions[target]:
            results.append(value)
            target += 1
        if target == len(positions):
            return results
    raise ValueError("Invalid bootstrap weights")


def bootstrap_deltas(rounds, probabilities, draws, seed, backend="auto"):
    """Resample whole paired blocks; sort data once instead of every draw."""
    if draws < 1 or len(rounds) < 2:
        raise ValueError("Insufficient bootstrap draws or paired blocks")
    rng, count, weights = random.Random(seed), len(rounds), []
    for _ in range(draws):
        row = [0] * count
        for _ in range(count):
            row[rng.randrange(count)] += 1
        weights.append(row)
    arms = {}
    for arm in ("baseline", "candidate"):
        pairs = sorted((value, group) for group, block in enumerate(rounds) for value in block[arm])
        arms[arm] = ([value for value, _ in pairs], [group for _, group in pairs])
    np = None
    if backend != "python":
        try:
            import numpy as np
        except ImportError:
            if backend == "numpy":
                raise
    outputs = {}
    if np is not None:
        size = sum(len(block["baseline"]) for block in rounds)
        positions = [round(probability * (size - 1)) for probability in probabilities]
        for arm, (values, group_ids) in arms.items():
            values, group_ids = np.asarray(values), np.asarray(group_ids)
            outputs[arm] = []
            for start in range(0, draws, 64):
                counts = np.asarray(weights[start:start + 64], dtype=np.int64)
                cumulative = counts[:, group_ids].cumsum(axis=1)
                outputs[arm].extend(np.stack(
                    [values[(cumulative > position).argmax(axis=1)] for position in positions], axis=1
                ).tolist())
        implementation = "numpy-weighted-paired-blocks/" + np.__version__
    else:
        for arm, (values, group_ids) in arms.items():
            outputs[arm] = [_weighted_quantiles(values, group_ids, row, probabilities) for row in weights]
        implementation = "stdlib-weighted-paired-blocks"
    deltas = {q: sorted(candidate[i] - baseline[i]
                        for baseline, candidate in zip(outputs["baseline"], outputs["candidate"]))
              for i, q in enumerate(probabilities)}
    return deltas, implementation


def analyze_measurements(cases, draws=30000, seed=42, alpha=0.05, legacy=False, backend="auto"):
    if type(draws) is not int or not 1 <= draws <= MAX_BOOTSTRAP_DRAWS:
        raise ValueError("Invalid bootstrap draw limit")
    policies = {case.get("analysis_policy", LEGACY_ANALYSIS_POLICY) for case in cases.values()}
    if len(policies) != 1 or not policies <= {LEGACY_ANALYSIS_POLICY, ANALYSIS_POLICY}:
        raise ValueError("Unknown or mixed analysis policies")
    policy = next(iter(policies))
    reported = sum(len(case["quantiles"]) for case in cases.values())
    families = {case.get("family_size", 0) for case in cases.values()}
    if len(families) != 1:
        raise ValueError("Inconsistent interval families")
    family_size = max(reported, next(iter(families)))
    if not family_size or not 0 < alpha < 1 or draws * alpha / (2 * family_size) < 10:
        raise ValueError("Too few bootstrap draws for the simultaneous interval family")
    tail = alpha / (2 * family_size)
    lower_index = math.floor((draws - 1) * tail)
    upper_index = math.ceil((draws - 1) * (1 - tail))
    report = {"protocol": PROTOCOL_VERSION, "analysis_policy": policy,
               "family_size": family_size, "reported_intervals": reported,
              "family_alpha": alpha, "interval": "two-sided percentile paired-cluster bootstrap, Bonferroni",
              "quantile_method": "nearest, Python round ties to even", "bootstrap_draws": draws,
              "seed": seed, "legacy_input": legacy, "cases": {},
              "overall_decision": "HOLD", "scope": "Performance evidence; not core/library deployment approval"}
    for case_id, case in sorted(cases.items()):
        rounds, probabilities = case["rounds"], case["quantiles"]
        if sorted(probabilities) != list(probabilities) or len(set(probabilities)) != len(probabilities):
            raise ValueError("Quantiles must be unique and ascending")
        summaries, warnings, diagnostics = {}, [], []
        for arm in ("baseline", "candidate"):
            flat = [value for block in rounds for value in block[arm]]
            medians = [quantile(block[arm], 0.5) for block in rounds]
            within = [lag1_autocorrelation(block[arm]) for block in rounds]
            within = [value for value in within if value is not None]
            correlation = lag1_autocorrelation(medians)
            summaries[arm] = {"samples": len(flat), "mean": math.fsum(flat) / len(flat),
                              "maximum_observed": max(flat),
                              "quantiles": {str(q): quantile(flat, q) for q in probabilities},
                              "round_median_lag1": correlation,
                              "within_block_lag1_median": quantile(within, 0.5) if within else None}
            if ((correlation is not None and abs(correlation) >= 0.5)
                    or (policy == LEGACY_ANALYSIS_POLICY and within and abs(quantile(within, 0.5)) >= 0.5)):
                warnings.append(arm + ": dependence/drift screening threshold exceeded")
            if policy == ANALYSIS_POLICY and within and abs(quantile(within, 0.5)) >= 0.5:
                diagnostics.append(arm + ": within-block dependence retained by whole paired-block resampling")
        paired_lags = {}
        if policy == ANALYSIS_POLICY:
            for q in probabilities:
                deltas = [quantile(block["candidate"], q) - quantile(block["baseline"], q) for block in rounds]
                paired_lags[str(q)] = lag1_autocorrelation(deltas)
                if paired_lags[str(q)] is not None and abs(paired_lags[str(q)]) >= 0.5:
                    warnings.append(f"Paired round delta q={q}: dependence/drift screening threshold exceeded")
        distributions, implementation = bootstrap_deltas(rounds, probabilities, draws, seed, backend)
        intervals = {str(q): [values[lower_index], values[upper_index]] for q, values in distributions.items()}
        decision = interval_decision(intervals)
        if legacy:
            warnings.append("Legacy evidence lacks v2 provenance/correctness gates")
        if case.get("screen"):
            warnings.append("Screening experiment is not confirmatory evidence")
        if len(rounds) < 20:
            warnings.append("Fewer than 20 paired blocks")
        if 0.99 in probabilities and summaries["baseline"]["samples"] * 0.01 < 50:
            warnings.append("Fewer than 50 expected observations in the p99 tail")
        if (case["spec"] or {}).get("unit") == "KiB" and (
                summaries["candidate"]["maximum_observed"] > summaries["baseline"]["maximum_observed"]):
            warnings.append("Candidate maximum observed process peak RSS exceeds baseline")
        if warnings and decision != "REJECT":
            decision = "HOLD"
        report["cases"][case_id] = {"decision": decision, "unit": (case["spec"] or {}).get("unit", "ms"),
                                     "baseline": summaries["baseline"], "candidate": summaries["candidate"],
                                     "delta_intervals": intervals, "warnings": warnings,
                                      "diagnostics": diagnostics, "paired_round_delta_lag1": paired_lags,
                                     "bootstrap_backend": implementation}
    return report


def validate_aae(payload, expected=None):
    if not payload.endswith(b"\n"):
        raise ValueError("Missing final JSONL newline")
    frames = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    if len(frames) < 2 or frames[0].get("type") != "meta" or frames[-1].get("type") != "end":
        raise ValueError("Missing meta/end framing")
    meta = frames[0]
    if meta.get("version") != "aae/1" or meta.get("source_url") != "https://example.org/":
        raise ValueError("Unexpected envelope identity")
    if not is_digest(meta.get("content_hash")):
        raise ValueError("Missing content hash")
    blocks, links, seen_link = [], [], False
    for frame in frames[1:-1]:
        if frame.get("type") == "block" and not seen_link:
            if (not isinstance(frame.get("text"), str) or not isinstance(frame.get("links"), list)
                    or not is_digest(frame.get("id")) or not isinstance(frame.get("role"), str)):
                raise ValueError("Malformed content block")
            blocks.append(frame)
        elif frame.get("type") == "link":
            seen_link = True
            if frame.get("id") != len(links) or not isinstance(frame.get("url"), str):
                raise ValueError("Invalid link catalog identity")
            links.append(frame["url"])
        else:
            raise ValueError("Unexpected, duplicate or out-of-order frame")
    if meta.get("total_blocks") != len(blocks) or frames[-1].get("block_count") != len(blocks):
        raise ValueError("Content count mismatch")
    if len({block.get("id") for block in blocks}) != len(blocks):
        raise ValueError("Duplicate block identifiers")
    for block in blocks:
        if any(type(link) is not int or not 0 <= link < len(links) for link in block["links"]):
            raise ValueError("Dangling link identifier")
        if len(set(block["links"])) != len(block["links"]):
            raise ValueError("Repeated link within owner")
    if expected is not None:
        actual = [(block.get("role"), block["text"], block["links"]) for block in blocks]
        if actual != expected["blocks"] or links != expected.get("links", []):
            raise ValueError("Independent content oracle mismatch")
    return frames


def require_parity(baseline, candidate):
    validate_aae(baseline)
    validate_aae(candidate)
    if baseline != candidate:
        raise ValueError("AAE byte parity failed; timing comparison forbidden")


def parse_peak_rss(text):
    value = text.strip()
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError("Missing/invalid kernel peak RSS; never substitute zero")
    return int(value)

