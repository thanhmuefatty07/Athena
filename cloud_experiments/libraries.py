"""Reviewed dependency factors and consumer evidence for research snapshots."""
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from kaggle_experiments.protocol import strict_json_loads
from kaggle_experiments import runner as kaggle_runner


MANIFEST_SHA256 = "76afd75c7a61ae4ac130aad8b2fe1e9baf8c68614963ff8cabffa197978be92c"
LOCK_SHA256 = "bce353e5dc8bcbd8681f931da667a98751323a18f7b70baa2912fd02db9d761f"
SONIC_LOCK = "cloud_experiments/locks/sonic-0.5.10.lock"
SONIC_LOCK_SHA256 = "ab097f1fcf48b72e02ed8d107f325545d543a81617748cc80b59c13b6a460d4c"
LEAN_LOCK = "cloud_experiments/locks/scraper-lean.lock"
LEAN_LOCK_SHA256 = "11cd1ae73deaa5bd824a35a6718f2e6939d3589a294c71cfa68326c12f1ba1e8"
SONIC_LEAN_LOCK = "cloud_experiments/locks/sonic-0.5.10-scraper-lean.lock"
SONIC_LEAN_LOCK_SHA256 = "4f23de1fc7a3bb09b7f8a7e1b6f06fbf1cabd9d74b2afb45615ef12a447ec19e"
MODERN_LOCK = "cloud_experiments/locks/scraper-0.29.0-html5ever-0.40.1.lock"
MODERN_LOCK_SHA256 = "588edbfa71a6291c12457508ee37942ee0591b2d20f860d09e76cb6e6edb8e07"
SONIC_MODERN_LOCK = "cloud_experiments/locks/sonic-0.5.10-scraper-0.29.0-html5ever-0.40.1.lock"
SONIC_MODERN_LOCK_SHA256 = "086eeb2c2c164ebcf43f2c8c7ebc936579413932be4181749044b4c68fde52e5"


def offline_dependency_metadata(run, source, *, cargo_feature_args=("--features", "research-probes"),
                                target="x86_64-unknown-linux-gnu"):
    """Read a locked Cargo graph while explicitly forbidding Cargo network use."""
    class OfflineMetadataRun:
        def run(self, command, **kwargs):
            command = list(command)
            if command[:2] != ["cargo", "metadata"] or "--offline" in command:
                raise ValueError("Offline dependency metadata adapter received an unexpected command")
            command.insert(2, "--offline")
            return run.run(command, **kwargs)

    return kaggle_runner.dependency_metadata(
        OfflineMetadataRun(), source, cargo_feature_args=cargo_feature_args, target=target)
ATTR_CURRENT_LOCK = "cloud_experiments/locks/html5ever-0.39.0-attr-index.lock"
ATTR_CURRENT_LOCK_SHA256 = "9a900882f46a481a2b47aba5de3ffac470fd1c62dc657e80e1b5ff475c13d96c"
ATTR_MODERN_LOCK = "cloud_experiments/locks/html5ever-0.40.1-attr-index.lock"
ATTR_MODERN_LOCK_SHA256 = "3dd144bc370c9676e2016a5d84e138ba27ea28764512720aeb4311e0dd7246d7"


def lock_profile(factors):
    attr_lookup = factors.get("html_attr_lookup", "vector")
    if attr_lookup not in {"vector", "hash", "hybrid8", "hybrid32", "hybrid128"}:
        raise ValueError("Unknown HTML attribute lookup strategy")
    if attr_lookup != "vector":
        if factors.get("json_library", "serde") == "sonic":
            raise ValueError("HTML attribute study does not compose with the unadmitted Sonic profile")
        if factors.get("html_library", "default") not in ("default", "modern"):
            raise ValueError("HTML attribute study only admits the two pinned parser tuples")
        return ((ATTR_MODERN_LOCK, ATTR_MODERN_LOCK_SHA256)
                if factors.get("html_library") == "modern"
                else (ATTR_CURRENT_LOCK, ATTR_CURRENT_LOCK_SHA256))
    if factors.get("html_library") == "modern":
        return (SONIC_MODERN_LOCK, SONIC_MODERN_LOCK_SHA256) if factors.get("json_library") == "sonic" else (MODERN_LOCK, MODERN_LOCK_SHA256)
    if factors.get("json_library") == "sonic":
        return (SONIC_LEAN_LOCK, SONIC_LEAN_LOCK_SHA256) if factors.get("html_library") == "lean" else (SONIC_LOCK, SONIC_LOCK_SHA256)
    return (LEAN_LOCK, LEAN_LOCK_SHA256) if factors.get("html_library") == "lean" else ("Cargo.lock", LOCK_SHA256)


def transform(files, factors):
    output = dict(files)
    if not any(key in factors for key in ("digest_library", "html_library", "json_library", "html_attr_lookup")):
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
    elif html == "modern":
        replace('scraper = "=0.27.0"', 'scraper = "=0.29.0"')
        replace('html5ever = "=0.39.0"', 'html5ever = "=0.40.1"')
    if factors.get("json_library") == "sonic":
        replace('serde_json = "1"', 'serde_json = "1"\nsonic-rs = { version = "=0.5.10", default-features = false }')
    attr_lookup = factors.get("html_attr_lookup", "vector")
    if attr_lookup != "vector":
        from cloud_experiments import parser_study

        version = "0.40.1" if html == "modern" else "0.39.0"
        directory = parser_study.vendor_directory(version, attr_lookup)
        if "[patch.crates-io]" in manifest:
            raise ValueError("Ambiguous source patch profile in dependency manifest")
        manifest += f'\n[patch.crates-io]\nhtml5ever = {{ path = "{directory}" }}\n'
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
    for alias in ("blake3", "html5ever", "scraper", "serde_json", "sonic_rs", "reqwest"):
        direct = graph["direct"].get(alias)
        if direct is not None:
            node = next(n for n in graph["nodes"] if n["id"] == direct["id"])
            enabled[alias] = {"version": direct["version"], "features": sorted(node["features"])}
    html = factors.get("html_library", "default")
    expected_html5ever = "0.40.1" if html == "modern" else "0.39.0"
    expected_scraper = "0.29.0" if html == "modern" else "0.27.0"
    for alias, expected in (("blake3", "1.8.7"), ("html5ever", expected_html5ever),
                            ("scraper", expected_scraper), ("serde_json", "1.0.154"),
                            ("reqwest", "0.13.5")):
        if enabled.get(alias, {}).get("version") != expected:
            raise ValueError("Candidate resolved an unreviewed library version: " + alias)
    attr_lookup = factors.get("html_attr_lookup", "vector")
    if attr_lookup != "vector":
        package = graph["packages"].get(graph["direct"]["html5ever"]["id"], {})
        if package.get("source") is not None:
            raise ValueError("HTML attribute candidate did not use the pinned local tokenizer source")
    digest = factors.get("digest_library", "default")
    digest_features = set(enabled["blake3"]["features"])
    if (("pure" in digest_features) != (digest == "pure")
            or ("prefer_intrinsics" in digest_features) != (digest == "intrinsics")):
        raise ValueError("BLAKE3 backend factor was not actually selected")
    html_features = set(enabled["scraper"]["features"])
    if ("errors" not in html_features or ("main" in html_features) != (html != "lean")
            or ("atomic" in html_features) != (html == "atomic")):
        raise ValueError("HTML feature factor changed the wrong consumer contract")
    sonic = factors.get("json_library") == "sonic"
    if ("sonic_rs" in enabled) != sonic or sonic and enabled["sonic_rs"]["version"] != "0.5.10":
        raise ValueError("Sonic serializer dependency factor was not actually selected")
    provider = factors.get("tls_provider", "aws-lc")
    rustls_nodes = [node for node in graph["nodes"]
                    if graph["packages"].get(node["id"], {}).get("name") == "rustls"
                    and graph["packages"].get(node["id"], {}).get("version") == "0.23.46"]
    reqwest_node = next((node for node in graph["nodes"]
                         if node["id"] == graph["direct"].get("reqwest", {}).get("id")), None)
    if len(rustls_nodes) != 1 or reqwest_node is None:
        raise ValueError("Actual target graph lacks the pinned Reqwest/Rustls nodes")
    rustls_features = set(rustls_nodes[0]["features"])
    reqwest_features = set(reqwest_node["features"])
    expected = {"aws-lc": "aws-lc-rs", "ring": "ring"}.get(provider)
    inactive = {"aws-lc": "ring", "ring": "aws-lc-rs"}.get(provider)
    reqwest_provider = "rustls" if provider == "aws-lc" else "rustls-no-provider"
    inactive_reqwest_provider = "rustls-no-provider" if provider == "aws-lc" else "rustls"
    if (expected not in rustls_features or inactive in rustls_features
            or reqwest_provider not in reqwest_features or inactive_reqwest_provider in reqwest_features):
        raise ValueError("Actual target graph does not select exactly the reviewed TLS selected CryptoProvider")
    tls_provider = {"selected": provider, "rustls_version": "0.23.46",
                    "rustls_features": sorted(rustls_features),
                    "reqwest_features": sorted(reqwest_features),
                    "status": "TARGET_FEATURE_GRAPH_VERIFIED_NOT_RUNTIME_PROOF"}
    return {"libraries": enabled, "lock_delta": delta,
            "tls_provider": tls_provider,
            "status": "GRAPH_VERIFIED_NOT_RUNTIME_PROOF",
            "remaining": ["Candidate-specific behavioral and protected performance gates", "Transitive advisories/license/MSRV review"]}


def verify_build_inputs(files, transformed, factors):
    """Reject unreviewed Cargo inputs before any command can run build scripts."""
    for name, expected in (("Cargo.toml", MANIFEST_SHA256), ("Cargo.lock", LOCK_SHA256)):
        data = files.get(name)
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Unreviewed baseline Cargo input: " + name)

    defaults = {"digest_library": "default", "html_library": "default", "json_library": "serde",
                "html_attr_lookup": "vector",
                "tls_provider": "aws-lc"}
    selected = {name: value for name, value in factors.items()
                if name in defaults and value != defaults[name]}
    expected = transform(files, selected)
    for name in ("Cargo.toml", "Cargo.lock"):
        if transformed.get(name) != expected.get(name):
            raise ValueError("Candidate Cargo input differs from reviewed profile: " + name)
    evidence = {"baseline_manifest_sha256": MANIFEST_SHA256, "baseline_lock_sha256": LOCK_SHA256,
            "candidate_manifest_sha256": hashlib.sha256(transformed["Cargo.toml"]).hexdigest(),
            "candidate_lock_sha256": hashlib.sha256(transformed["Cargo.lock"]).hexdigest(),
            "profile": lock_profile(selected)[0] if selected else "Cargo.toml+Cargo.lock",
            "status": "STATIC_INPUTS_VERIFIED_BEFORE_BUILD",
            "remaining": ["Network and metadata isolation for build/test execution is NOT VERIFIED"]}
    attr_lookup = selected.get("html_attr_lookup", "vector")
    if attr_lookup != "vector":
        from cloud_experiments import parser_study

        version = "0.40.1" if selected.get("html_library") == "modern" else "0.39.0"
        tokenizer = parser_study.patched_tokenizer(files, version, attr_lookup)
        evidence["candidate_parser"] = {"crate": "html5ever", "version": version,
            "strategy": attr_lookup, "archive_sha256": parser_study.CRATES[version][1],
            "tokenizer_source_sha256": hashlib.sha256(tokenizer).hexdigest(),
            "status": "ARCHIVE_AND_TRANSFORM_HASH_VERIFIED"}
    return evidence


def dossier(root, graph=None):
    """Bind curated consumer obligations to exact locked direct edges, not name matches."""
    root = Path(root).resolve(strict=True)
    manifest_bytes, lock_bytes = (root / "Cargo.toml").read_bytes(), (root / "Cargo.lock").read_bytes()
    manifest, lock = tomllib.loads(manifest_bytes.decode()), tomllib.loads(lock_bytes.decode())
    policy = strict_json_loads((root / "cloud_experiments/library_decisions.json").read_text(encoding="utf-8"))
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
