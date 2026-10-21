"""Reviewed dependency factors and consumer evidence for research snapshots."""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path


MANIFEST_SHA256 = "d5fe9f4c7d1b796f9449a13bed164e9fbde7894a1e8457b12a265e24e042ba38"
LOCK_SHA256 = "d1280bf0d2b7d388801293ee79a17707af687092a3b83649f871548d41ddf5fe"
SONIC_LOCK = "cloud_experiments/locks/sonic-0.5.10.lock"
SONIC_LOCK_SHA256 = "4cc00c7248aefcdace378e797b3c235fd53cbaabd5f7803ccbb46d8a40c66f64"
LEAN_LOCK = "cloud_experiments/locks/scraper-lean.lock"
LEAN_LOCK_SHA256 = "3c53fb75d9df4d7f9debeb2e43b9ae5b2fef97bdf51c849a8fa69cade306376d"
SONIC_LEAN_LOCK = "cloud_experiments/locks/sonic-0.5.10-scraper-lean.lock"
SONIC_LEAN_LOCK_SHA256 = "c1707aa51d0f13f4fbb5250e3d20671c1c83cabac33eff39c2ecadebb8d10ade"


def lock_profile(factors):
    if factors.get("json_library") == "sonic":
        return (SONIC_LEAN_LOCK, SONIC_LEAN_LOCK_SHA256) if factors.get("html_library") == "lean" else (SONIC_LOCK, SONIC_LOCK_SHA256)
    return (LEAN_LOCK, LEAN_LOCK_SHA256) if factors.get("html_library") == "lean" else ("Cargo.lock", LOCK_SHA256)


def transform(files, factors):
    output = dict(files)
    if not any(key in factors for key in ("digest_library", "html_library", "json_library")):
        return output
    for name, digest in (("Cargo.toml", MANIFEST_SHA256), ("Cargo.lock", LOCK_SHA256)):
        data = files.get(name)
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Unreviewed dependency input: " + name)
    manifest = files["Cargo.toml"].decode().replace("\r\n", "\n")

    def replace(old, new):
        nonlocal manifest
        if manifest.count(old) != 1:
            raise ValueError("Dependency declaration absent or ambiguous")
        manifest = manifest.replace(old, new, 1)

    digest = factors.get("digest_library")
    if digest is not None:
        feature = {"pure": "pure", "intrinsics": "prefer_intrinsics"}[digest]
        replace('blake3 = "1.8"', 'blake3 = { version = "=1.8.7", features = ["' + feature + '"] }')
    html = factors.get("html_library")
    if html == "lean":
        replace('scraper = "=0.27.0"', 'scraper = { version = "=0.27.0", default-features = false, features = ["errors"] }')
    elif html == "atomic":
        replace('scraper = "=0.27.0"', 'scraper = { version = "=0.27.0", features = ["atomic"] }')
    if factors.get("json_library") == "sonic":
        replace('serde_json = "1"', 'serde_json = "1"\nsonic-rs = { version = "=0.5.10", default-features = false }')
    path, expected = lock_profile(factors)
    lock = files.get(path)
    if not isinstance(lock, bytes) or hashlib.sha256(lock).hexdigest() != expected:
        raise ValueError("Missing reviewed dependency candidate lock: " + path)
    if factors.get("json_library") == "sonic":
        sonic = next((p for p in tomllib.loads(lock.decode())["package"] if p["name"] == "sonic-rs"), {})
        if (sonic.get("version") != "0.5.10"
                or sonic.get("checksum") != "756586caeeb21126a57c17d1ef2b9386e2613ba784aae0f27104f5e9982335ec"):
            raise ValueError("Candidate does not match verified upstream/registry Sonic identity")
    output["Cargo.lock"] = lock
    output["Cargo.toml"] = manifest.encode()
    return output


def lock_delta(before, after):
    def packages(data):
        return {(p["name"], p["version"], p.get("source", "workspace")): p
                for p in tomllib.loads(data.decode())["package"]}
    old, new = packages(before), packages(after)
    # The workspace root's dependency list necessarily changes for a new adapter.
    changed = [key for key in old.keys() & new.keys()
               if old[key] != new[key] and key[2] != "workspace"]
    return {"added": [new[k] for k in sorted(new.keys() - old.keys())],
            "removed": [old[k] for k in sorted(old.keys() - new.keys())],
            "changed_existing": [list(k) for k in sorted(changed)]}


def verify_graph(baseline_lock, candidate_lock, graph, factors):
    if (hashlib.sha256(baseline_lock).hexdigest() != LOCK_SHA256
            or hashlib.sha256(candidate_lock).hexdigest() != lock_profile(factors)[1]):
        raise ValueError("Dependency graph does not use the exact reviewed lock profile")
    delta = lock_delta(baseline_lock, candidate_lock)
    enabled = {}
    for alias in ("blake3", "scraper", "serde_json", "sonic_rs"):
        direct = graph["direct"].get(alias)
        if direct is not None:
            node = next(n for n in graph["nodes"] if n["id"] == direct["id"])
            enabled[alias] = {"version": direct["version"], "features": sorted(node["features"])}
    for alias, expected in (("blake3", "1.8.7"), ("scraper", "0.27.0"), ("serde_json", "1.0.154")):
        if enabled.get(alias, {}).get("version") != expected:
            raise ValueError("Candidate resolved an unreviewed library version: " + alias)
    digest = factors.get("digest_library", "default")
    digest_features = set(enabled["blake3"]["features"])
    if (("pure" in digest_features) != (digest == "pure")
            or ("prefer_intrinsics" in digest_features) != (digest == "intrinsics")):
        raise ValueError("BLAKE3 backend factor was not actually selected")
    html = factors.get("html_library", "default")
    html_features = set(enabled["scraper"]["features"])
    if ("errors" not in html_features or ("main" in html_features) != (html != "lean")
            or ("atomic" in html_features) != (html == "atomic")):
        raise ValueError("HTML feature factor changed the wrong consumer contract")
    sonic = factors.get("json_library") == "sonic"
    if ("sonic_rs" in enabled) != sonic or sonic and enabled["sonic_rs"]["version"] != "0.5.10":
        raise ValueError("Sonic serializer dependency factor was not actually selected")
    return {"libraries": enabled, "lock_delta": delta,
            "status": "GRAPH_VERIFIED_NOT_RUNTIME_PROOF",
            "remaining": ["Candidate-specific behavioral and protected performance gates", "Transitive advisories/license/MSRV review"]}


def verify_build_inputs(files, transformed, factors):
    """Reject unreviewed Cargo inputs before any command can run build scripts."""
    for name, expected in (("Cargo.toml", MANIFEST_SHA256), ("Cargo.lock", LOCK_SHA256)):
        data = files.get(name)
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Unreviewed baseline Cargo input: " + name)

    defaults = {"digest_library": "default", "html_library": "default", "json_library": "serde"}
    selected = {name: value for name, value in factors.items()
                if name in defaults and value != defaults[name]}
    expected = transform(files, selected)
    for name in ("Cargo.toml", "Cargo.lock"):
        if transformed.get(name) != expected.get(name):
            raise ValueError("Candidate Cargo input differs from reviewed profile: " + name)
    return {"baseline_manifest_sha256": MANIFEST_SHA256, "baseline_lock_sha256": LOCK_SHA256,
            "candidate_manifest_sha256": hashlib.sha256(transformed["Cargo.toml"]).hexdigest(),
            "candidate_lock_sha256": hashlib.sha256(transformed["Cargo.lock"]).hexdigest(),
            "profile": lock_profile(selected)[0] if selected else "Cargo.toml+Cargo.lock",
            "status": "STATIC_INPUTS_VERIFIED_BEFORE_BUILD",
            "remaining": ["Network and metadata isolation for build/test execution is NOT VERIFIED"]}


def dossier(root, graph=None):
    """Bind curated consumer obligations to exact locked direct edges, not name matches."""
    root = Path(root).resolve(strict=True)
    manifest_bytes, lock_bytes = (root / "Cargo.toml").read_bytes(), (root / "Cargo.lock").read_bytes()
    manifest, lock = tomllib.loads(manifest_bytes.decode()), tomllib.loads(lock_bytes.decode())
    policy = json.loads((root / "cloud_experiments/library_decisions.json").read_text(encoding="utf-8"))
    declarations, packages = manifest["dependencies"], lock["package"]
    if set(policy["libraries"]) != set(declarations):
        raise ValueError("Dependency policy does not cover exactly the direct declarations")
    roots = [p for p in packages if p["name"] == manifest["package"]["name"]
             and p["version"] == manifest["package"]["version"] and "source" not in p]
    if len(roots) != 1:
        raise ValueError("Ambiguous workspace package")
    records = {}
    for alias, declaration in declarations.items():
        requirement = {"version": declaration} if isinstance(declaration, str) else declaration
        name = requirement.get("package", alias)
        edges = [edge.split() for edge in roots[0]["dependencies"] if edge.split()[0] == name]
        if len(edges) != 1:
            raise ValueError("Ambiguous direct dependency edge: " + alias)
        edge = edges[0]
        matches = [p for p in packages if p["name"] == name and (len(edge) == 1 or p["version"] == edge[1])]
        if len(matches) != 1:
            raise ValueError("Unresolved direct package identity: " + alias)
        package = matches[0]
        record = {**policy["libraries"][alias], "declaration": requirement, "resolved": package,
                  "enabled_features": None, "status": "HOLD", "adoption": "NOT_AUTHORIZED"}
        if graph is not None:
            direct = graph["direct"].get(alias.replace("-", "_"))
            if not direct or (direct["name"], direct["version"]) != (name, package["version"]):
                raise ValueError("Actual Cargo direct graph differs: " + alias)
            node = next(n for n in graph["nodes"] if n["id"] == direct["id"])
            record["enabled_features"] = sorted(node["features"])
        records[alias] = record
    return {"schema": "athena-library-dossier/1", "libraries": records,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "scope": "Exact direct edges and declared consumer obligations; metadata is not runtime or security proof",
            "remaining": ["Target integration and protected performance for each candidate", "Transitive advisory/MSRV/license review", "Unimplemented alternate consumers"]}
