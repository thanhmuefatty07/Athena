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
    "html_library": (None, "lib-scraper-lean", "lib-scraper-atomic"),
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
    return {"I-HTML-TABLE": ["baseline", "table-html-selectors"]}


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
    fixed = set(contenders.definitions()).union(*(set(cells) for cells in panels.values()))
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
    variants = sorted(fixed | set(selected), key=lambda value: (value != "baseline", value))
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
