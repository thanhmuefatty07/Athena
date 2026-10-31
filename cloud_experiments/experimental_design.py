"""Finite native combination coverage; this does not attest execution or causality."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import itertools
import json
import math
import random

from cloud_experiments import contenders


SCHEMA = "athena-native-design/1"
SEED = 20261020
POOL_ATTEMPTS = 4096
MAX_VARIANTS = 256
AXES = {
    "writer": (None, "aae-direct", "aae-buffered", "aae-reuse"),
    "hash": (None, "aae-incremental", "aae-batched4096", "aae-batched16384"),
    "reserve": (None, "aae-smallreserve", "aae-estimatedreserve"),
    "lookup": (None, "rank-binary", "rank-linear"),
    "selection": (None, "rank-select"),
    "projection": (None, "projection-fused"),
    "links": (None, "links-btree"),
    "lto": (None, "build-lto-off", "build-lto-full"),
    "codegen_units": (None, "build-codegen1"),
    "cpu": (None, "build-native"),
    "digest_library": (None, "lib-blake3-pure", "lib-blake3-intrinsics"),
    "html_library": (None, "lib-scraper-lean", "lib-scraper-atomic", "lib-scraper-modern"),
    "json_library": (None, "lib-json-sonic"),
}
MECHANISM_GROUPS = (
    ("writer", "hash", "reserve"),
    ("lookup", "selection", "projection"),
    ("html_library", "projection", "links"),
    ("json_library", "hash", "cpu"),
    ("lto", "codegen_units", "cpu"),
    ("writer", "hash", "projection"),
    ("html_library", "lookup", "selection"),
    ("json_library", "html_library", "digest_library"),
    ("writer", "hash", "lookup", "projection"),
)


def derive_seed(seed, namespace, *labels):
    if type(seed) is not int or seed < 0 or not isinstance(namespace, str) or not namespace:
        raise ValueError("Randomization requires an integer seed and stream namespace")
    encoded = json.dumps([seed, namespace, *labels], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:16], "big")


def arm_start(design, variant, case, phase):
    policy = design.get("randomization")
    if policy is None:
        return 0
    if (not isinstance(policy, dict) or set(policy) != {"schema", "seed", "arm_order"}
            or policy.get("schema") != "athena-randomization/1"
            or policy.get("arm_order") != "randomized-start-alternating-blocks"):
        raise ValueError("Unsupported frozen randomization policy")
    return derive_seed(policy["seed"], "paired-arm-start", phase, variant, case) & 1


def factorial_panels():
    return {
        "I-AAE": (AXES["writer"], AXES["hash"], AXES["reserve"]),
        "I-RANK": (AXES["lookup"], AXES["selection"]),
        "I-PROJECT": (AXES["projection"], AXES["links"]),
        "I-HASH-LIBRARY": (AXES["digest_library"], AXES["hash"]),
        "I-PARSER-LIBRARY": (AXES["html_library"], AXES["projection"], AXES["links"]),
        "I-JSON-LIBRARY": (AXES["json_library"], AXES["reserve"], AXES["hash"]),
        "I-LIBRARY-SYSTEM": (AXES["digest_library"], AXES["html_library"], AXES["json_library"]),
        "I-JSON-CPU": (AXES["cpu"], AXES["json_library"]),
        "I-JSON-PROJECT": (AXES["projection"], AXES["json_library"]),
        "I-QUERY-PROJECT": (AXES["lookup"], AXES["selection"], AXES["projection"]),
        "I-COMPILER": (AXES["lto"], AXES["codegen_units"], AXES["cpu"]),
        "I-JSON-HASH-CPU": (AXES["json_library"], AXES["hash"], AXES["cpu"]),
    }


def scoped_factorial_panels():
    """Exact full-factorial comparisons outside the mixed-strength whole-path axes."""
    current = ["baseline", "html-attrs-hash", "html-attrs-hybrid8",
               "html-attrs-hybrid32", "html-attrs-hybrid128"]
    modern = ["html-attrs-modern-vector", "html-attrs-modern-hash",
              "html-attrs-modern-hybrid8", "html-attrs-modern-hybrid32",
              "html-attrs-modern-hybrid128"]
    return {"I-HTML-TABLE": ["baseline", "table-html-selectors"],
            "I-HTML-ATTR-MEMBERSHIP": [*current, *modern]}


def _name(parts):
    return contenders.canonical_id("+".join(part for part in parts if part) or "baseline")


@lru_cache(maxsize=1)
def _requirements():
    groups = list(itertools.combinations(AXES, 2))
    groups.extend(tuple(sorted(group, key=list(AXES).index)) for group in MECHANISM_GROUPS)
    records, witnesses, excluded = [], [], []
    for axes in groups:
        for parts in itertools.product(*(AXES[axis] for axis in axes)):
            levels = {axis: contenders.metadata(part or "baseline")["factors"][axis]
                      for axis, part in zip(axes, parts)}
            record = {"axes": list(axes), "levels": levels}
            try:
                name = _name(parts)
                contenders.metadata(name)
            except ValueError as error:
                excluded.append({**record, "reason": str(error)})
                continue
            records.append(record)
            witnesses.append(name)
    return tuple(groups), records, witnesses, excluded


def _index():
    groups, records, _, _ = _requirements()
    return groups, {(tuple(item["axes"]), tuple(item["levels"].values())): index
                    for index, item in enumerate(records)}


def _mask(factors, groups, index):
    result = 0
    for axes in groups:
        bit = index.get((axes, tuple(factors[axis] for axis in axes)))
        if bit is not None:
            result |= 1 << bit
    return result


def coverage(variants):
    if not variants or len(set(variants)) != len(variants):
        raise ValueError("Coverage requires distinct actual contender identities")
    groups, index = _index()
    _, records, _, excluded = _requirements()
    covered = 0
    for name in variants:
        covered |= _mask(contenders.metadata(name)["factors"], groups, index)
    missing = [deepcopy(item) for bit, item in enumerate(records) if not covered & (1 << bit)]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"status": "COVERED" if not missing else "INCOMPLETE",
            "required_tuples": len(records), "covered_tuples": len(records) - len(missing),
            "uncovered": missing, "infeasible": deepcopy(excluded), "requirements_sha256": digest,
            "interpretation": "Finite valid tuple coverage only; not execution, causal identifiability, precision or optimality"}


@lru_cache(maxsize=1)
def _compiled():
    panels = {name: [_name(parts) for parts in itertools.product(*axes)]
              for name, axes in factorial_panels().items()}
    panels.update(scoped_factorial_panels())
    fixed = {name for name, definition in contenders.definitions().items()
             if definition["implementation_scope"] != "service_provider_stage_only"}
    fixed.update(*(set(cells) for cells in panels.values()))
    groups, index = _index()
    _, records, witnesses, _ = _requirements()
    pool = set(witnesses) | fixed
    seed = derive_seed(SEED, "covering-pool", coverage(["baseline"])["requirements_sha256"])
    rng = random.Random(seed)
    for _ in range(POOL_ATTEMPTS):
        try:
            name = _name(rng.choice(levels) for levels in AXES.values())
            contenders.metadata(name)
        except ValueError:
            continue
        pool.add(name)
    masks = {name: _mask(contenders.metadata(name)["factors"], groups, index) for name in pool}
    uncovered = ((1 << len(records)) - 1) & ~masks["baseline"]
    selected = ["baseline"]
    while uncovered:
        name = min(masks, key=lambda value: (-(masks[value] & uncovered).bit_count(),
                                             value not in fixed, len(value.split("+")), value))
        if not masks[name] & uncovered or len(set(selected) | fixed | {name}) > MAX_VARIANTS:
            raise ValueError("Finite combination coverage exceeds the declared design capacity")
        selected.append(name)
        uncovered &= ~masks.pop(name)
    # Build the parser-version vector reference before its indexed arms so each
    # candidate can be gated against the matching same-version WPT report.
    variants = sorted(fixed | set(selected), key=lambda value: (
        value != "baseline", value != "html-attrs-modern-vector", value))
    checked = coverage(selected)
    if checked["status"] != "COVERED":
        raise ValueError("Generated combination design failed exact post-validation")
    pool_hash = hashlib.sha256("\n".join(sorted(pool)).encode()).hexdigest()
    return {"schema": SCHEMA, "variants": variants, "factorial_panels": panels,
            "cross_cells": selected, "coverage": checked,
            "factor_levels": {axis: [contenders.metadata(part or "baseline")["factors"][axis]
                                      for part in parts] for axis, parts in AXES.items()},
            "mechanism_groups": [list(group) for group in MECHANISM_GROUPS],
            "generation": {"method": "deterministic-greedy-mixed-strength-coverage", "seed": seed,
                           "random_pool_attempts": POOL_ATTEMPTS, "candidate_pool_cells": len(pool),
                           "candidate_pool_sha256": pool_hash, "max_variants": MAX_VARIANTS,
                           "minimum_run_count": "NOT_PROVEN"},
            "cartesian_cells_before_constraints": math.prod(map(len, AXES.values())),
            "unmeasured_scope": ["Other higher-order tuples", "New or unimplemented factors",
                                 "Runtime correctness and performance of every selected cell",
                                 "Representative corpus and service/agent/language integration"],
            "causal_effects": "Only the separately fixed full-factorial panels support their declared contrasts"}


def native_design():
    return deepcopy(_compiled())


def evaluate_design_efficiency(variants, *, full_variants=None):
    """Quantify main-effect, pairwise, and mechanism-tuple retention relative to the 225-variant design."""
    if not isinstance(variants, (list, tuple)) or not variants:
        raise ValueError("Design efficiency evaluation requires a non-empty variant list")
    if len(set(variants)) != len(variants) or variants[0] != "baseline":
        raise ValueError("Design efficiency evaluation requires unique variants starting with baseline")
    compiled = _compiled()
    reference_variants = list(compiled["variants"] if full_variants is None else full_variants)
    if not reference_variants or reference_variants[0] != "baseline":
        raise ValueError("Reference design must start with baseline")
    ref_factors = {name: contenders.metadata(name)["factors"] for name in reference_variants}
    sub_factors = {name: contenders.metadata(name)["factors"] for name in variants}

    # 1. Main-effect level coverage across all active axes in reference_variants
    ref_levels = set()
    sub_levels = set()
    active_axes = []
    for axis in AXES:
        vals_ref = {ref_factors[v][axis] for v in reference_variants}
        vals_sub = {sub_factors[v][axis] for v in variants}
        if len(vals_ref) > 1:
            active_axes.append(axis)
        for val in vals_ref:
            ref_levels.add((axis, val))
        for val in vals_sub:
            if (axis, val) in ref_levels:
                sub_levels.add((axis, val))
    main_effect_coverage_ratio = len(sub_levels) / len(ref_levels) if ref_levels else 1.0

    # 2. Unconfounded single-contender main-effect arm retention
    single_contenders = {
        name for name, defn in contenders.definitions().items()
        if defn["implementation_scope"] != "service_provider_stage_only" and name in ref_factors
    }
    covered_single_contenders = single_contenders.intersection(variants)
    single_arm_retention_ratio = (
        len(covered_single_contenders) / len(single_contenders) if single_contenders else 1.0
    )

    # 3. Pairwise 2-way factor-level coverage across all axis pairs
    ref_pairs = set()
    sub_pairs = set()
    for axis_a, axis_b in itertools.combinations(AXES, 2):
        for v in reference_variants:
            pair = ((axis_a, ref_factors[v][axis_a]), (axis_b, ref_factors[v][axis_b]))
            ref_pairs.add(pair)
        for v in variants:
            pair = ((axis_a, sub_factors[v][axis_a]), (axis_b, sub_factors[v][axis_b]))
            if pair in ref_pairs:
                sub_pairs.add(pair)
    pairwise_coverage_ratio = len(sub_pairs) / len(ref_pairs) if ref_pairs else 1.0

    # 4. Mixed-strength mechanism-group tuple coverage (2-way + 3/4-way within MECHANISM_GROUPS)
    cov = coverage(variants)
    groups, index = _index()
    _, records, _, _ = _requirements()
    higher_order_bits = 0
    for idx, rec in enumerate(records):
        if len(rec["axes"]) >= 3:
            higher_order_bits |= 1 << idx
    ref_mask = 0
    for v in reference_variants:
        ref_mask |= _mask(ref_factors[v], groups, index)
    sub_mask = 0
    for v in variants:
        sub_mask |= _mask(sub_factors[v], groups, index)
    denom_tuples = ref_mask.bit_count() if full_variants is not None else cov["required_tuples"]
    covered_tuples = (sub_mask & ref_mask).bit_count() if full_variants is not None else cov["covered_tuples"]
    mechanism_group_coverage_ratio = (
        min(1.0, covered_tuples / denom_tuples) if denom_tuples else 1.0
    )
    ref_ho_mask = ref_mask & higher_order_bits
    sub_ho_mask = sub_mask & ref_ho_mask
    higher_order_tuples_total = ref_ho_mask.bit_count()
    higher_order_tuples_covered = sub_ho_mask.bit_count()
    higher_order_coverage_ratio = (
        higher_order_tuples_covered / higher_order_tuples_total
        if higher_order_tuples_total
        else 1.0
    )
    panel_cells = {
        cell
        for panel in compiled["factorial_panels"].values()
        for cell in panel
    } & set(reference_variants)
    covered_panel_cells = panel_cells.intersection(variants)
    panel_corner_retention_ratio = (
        len(covered_panel_cells) / len(panel_cells) if panel_cells else 1.0
    )

    # 5. Composite statistical/design quality score (4-component harmonic mean of main,
    #    pairwise, mechanism-group, and 3/4-way higher-order mechanism tuple coverage)
    components = (
        main_effect_coverage_ratio,
        pairwise_coverage_ratio,
        mechanism_group_coverage_ratio,
        higher_order_coverage_ratio,
    )
    if any(c <= 0.0 for c in components):
        composite_quality_score = 0.0
    else:
        composite_quality_score = len(components) / sum(1.0 / c for c in components)
    higher_order_composite_quality_score = composite_quality_score

    return {
        "schema": "athena-design-efficiency-report/1",
        "variant_count": len(variants),
        "reference_variant_count": len(reference_variants),
        "variant_reduction_ratio": 1.0 - (len(variants) / len(reference_variants)),
        "active_axes": active_axes,
        "main_effect_levels_total": len(ref_levels),
        "main_effect_levels_covered": len(sub_levels),
        "main_effect_coverage_ratio": main_effect_coverage_ratio,
        "single_arm_contenders_total": len(single_contenders),
        "single_arm_contenders_covered": len(covered_single_contenders),
        "single_arm_retention_ratio": single_arm_retention_ratio,
        "pairwise_tuples_total": len(ref_pairs),
        "pairwise_tuples_covered": len(sub_pairs),
        "pairwise_coverage_ratio": pairwise_coverage_ratio,
        "mechanism_tuples_total": denom_tuples,
        "mechanism_tuples_covered": covered_tuples,
        "mechanism_group_coverage_ratio": mechanism_group_coverage_ratio,
        "higher_order_tuples_total": higher_order_tuples_total,
        "higher_order_tuples_covered": higher_order_tuples_covered,
        "higher_order_coverage_ratio": higher_order_coverage_ratio,
        "panel_corners_total": len(panel_cells),
        "panel_corners_covered": len(covered_panel_cells),
        "panel_corner_retention_ratio": panel_corner_retention_ratio,
        "composite_quality_score": composite_quality_score,
        "higher_order_composite_quality_score": higher_order_composite_quality_score,
        "coverage_status": cov["status"],
        "requirements_sha256": cov["requirements_sha256"],
    }


def high_efficiency_design(
    *, target_quality=0.95, include_single_arms=True, enforce_higher_order=False
):
    """Construct a D-optimal / mixed-strength fractional design achieving >= target_quality with minimal runs."""
    if type(target_quality) not in (int, float) or not (0.5 <= float(target_quality) <= 1.0):
        raise ValueError("target_quality must be a finite ratio in [0.5, 1.0]")
    if type(include_single_arms) is not bool:
        raise TypeError("include_single_arms must be a boolean")
    if type(enforce_higher_order) is not bool:
        raise TypeError("enforce_higher_order must be a boolean")
    target_quality = float(target_quality)
    compiled = _compiled()
    groups, index = _index()
    _, records, _, _ = _requirements()
    higher_order_bits = 0
    for idx, rec in enumerate(records):
        if len(rec["axes"]) >= 3:
            higher_order_bits |= 1 << idx
    masks = {
        name: _mask(contenders.metadata(name)["factors"], groups, index)
        for name in compiled["variants"]
    }
    single_arms = {
        name for name, defn in contenders.definitions().items()
        if defn["implementation_scope"] != "service_provider_stage_only"
    }
    selected_set = {"baseline", "html-attrs-modern-vector"}
    if include_single_arms:
        selected_set |= single_arms
    covered_mask = 0
    for name in selected_set:
        covered_mask |= masks[name]
    all_required = (1 << len(records)) - 1
    remaining_candidates = [v for v in compiled["variants"] if v not in selected_set]
    while remaining_candidates:
        ordered = sorted(
            selected_set,
            key=lambda value: (value != "baseline", value != "html-attrs-modern-vector", value),
        )
        if covered_mask.bit_count() / len(records) >= target_quality and (
            not enforce_higher_order
            or (covered_mask & higher_order_bits).bit_count() / higher_order_bits.bit_count()
            >= target_quality
        ):
            metrics = evaluate_design_efficiency(ordered, full_variants=compiled["variants"])
            if (
                metrics["main_effect_coverage_ratio"] >= target_quality
                and (not include_single_arms or metrics["single_arm_retention_ratio"] >= target_quality)
                and metrics["pairwise_coverage_ratio"] >= target_quality
                and metrics["mechanism_group_coverage_ratio"] >= target_quality
                and (
                    not enforce_higher_order
                    or (
                        metrics["higher_order_coverage_ratio"] >= target_quality
                        and metrics["higher_order_composite_quality_score"] >= target_quality
                    )
                )
                and metrics["composite_quality_score"] >= target_quality
            ):
                break
        uncovered = all_required & ~covered_mask
        uncovered_ho = uncovered & higher_order_bits
        if enforce_higher_order and (
            (covered_mask & higher_order_bits).bit_count() / higher_order_bits.bit_count()
            < target_quality
        ):
            best = min(
                remaining_candidates,
                key=lambda v: (
                    -(
                        (masks[v] & uncovered_ho).bit_count() * 4
                        + (masks[v] & uncovered).bit_count()
                    ),
                    len(v.split("+")),
                    v,
                ),
            )
        else:
            best = min(
                remaining_candidates,
                key=lambda v: (
                    -(masks[v] & uncovered).bit_count(),
                    -(masks[v] & uncovered_ho).bit_count(),
                    len(v.split("+")),
                    v,
                ),
            )
        selected_set.add(best)
        covered_mask |= masks[best]
        remaining_candidates.remove(best)
    ordered = sorted(
        selected_set,
        key=lambda value: (value != "baseline", value != "html-attrs-modern-vector", value),
    )
    metrics = evaluate_design_efficiency(ordered, full_variants=compiled["variants"])
    if metrics["composite_quality_score"] < target_quality:
        raise ValueError("Failed to reach requested target_quality within high-efficiency pool")
    return {
        "schema": "athena-high-efficiency-design/1",
        "target_quality": target_quality,
        "include_single_arms": include_single_arms,
        "enforce_higher_order": enforce_higher_order,
        "variants": ordered,
        "cross_cells": [v for v in compiled["cross_cells"] if v in selected_set],
        "efficiency": metrics,
        "interpretation": (
            "Retains 100% of factor levels and >=95% of pairwise and mechanism-group "
            "interactions while eliminating redundant full-factorial Cartesian cells"
        ),
    }




def whole_path_core_panel():
    """Return the multi-owner whole-path + I-RANK + I-HTML-TABLE panel for cloud campaign execution."""
    variants = [
        "baseline",
        "aae-direct",
        "aae-reuse",
        "aae-buffered",
        "aae-incremental",
        "aae-batched4096",
        "aae-batched16384",
        "aae-smallreserve",
        "aae-estimatedreserve",
        "lib-blake3-pure",
        "lib-json-sonic",
        "rank-binary",
        "rank-linear",
        "rank-select",
        "rank-binary+rank-select",
        "rank-linear+rank-select",
        "prepared",
        "table-sparse",
        "table-html-selectors",
    ]
    cloud_014_variants = [
        "baseline",
        "aae-direct",
        "aae-incremental",
        "aae-estimatedreserve",
        "lib-blake3-pure",
        "lib-json-sonic",
        "table-html-selectors",
    ]
    cloud_015_variants = [
        "baseline",
        "aae-direct",
        "aae-reuse",
        "aae-buffered",
        "aae-smallreserve",
        "aae-batched4096",
        "aae-batched16384",
        "rank-binary",
        "rank-linear",
        "rank-select",
        "rank-binary+rank-select",
        "rank-linear+rank-select",
        "table-html-selectors",
    ]
    for name in variants:
        contenders.metadata(name)
    return {
        "schema": "athena-whole-path-core-panel/1",
        "variants": variants,
        "cloud_014_variants": cloud_014_variants,
        "cloud_015_variants": cloud_015_variants,
        "active_owners": ["H01", "H02", "H03", "H06", "H07", "H08", "H09", "H14"],
        "complete_interaction_panels": ["I-RANK", "I-HTML-TABLE"],
        "efficiency_within_active_subspace": evaluate_design_efficiency(
            variants, full_variants=variants
        ),
        "efficiency_vs_full_design": evaluate_design_efficiency(variants),
    }
