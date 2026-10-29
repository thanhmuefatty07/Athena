"""Bounded native-core study; source snapshots never modify the checkout."""
import argparse
import base64
import hashlib
import itertools
import json
import math
import os
from pathlib import Path, PureWindowsPath
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

from cloud_experiments import libraries
from kaggle_experiments import pack_snapshots, protocol, runner

PROTOCOL = "athena-cloud-native/1"
ROOT = Path(__file__).resolve().parent.parent
CLOUD_EXPERIMENT_BUNDLE_FILES = (
    "cloud_experiments/agent_lab.py",
    "cloud_experiments/agent_tasks.json",
    "cloud_experiments/bench.rs",
    "cloud_experiments/bootstrap.sh",
    "cloud_experiments/campaign.py",
    "cloud_experiments/contenders.py",
    "cloud_experiments/core_decisions.json",
    "cloud_experiments/core_probe.rs",
    "cloud_experiments/experimental_design.py",
    "cloud_experiments/finish.sh",
    "cloud_experiments/gcp_secrets.py",
    "cloud_experiments/http_transport.py",
    "cloud_experiments/lexbor_adapter/build.rs",
    "cloud_experiments/lexbor_adapter/Cargo.lock",
    "cloud_experiments/lexbor_adapter/Cargo.toml",
    "cloud_experiments/lexbor_adapter/src/lexbor_shim.c",
    "cloud_experiments/lexbor_adapter/src/lib.rs",
    "cloud_experiments/lexbor_adapter/src/tables.rs",
    "cloud_experiments/lexbor_adapter/tests/semantic_parity.rs",
    "cloud_experiments/lexbor/build_and_smoke.sh",
    "cloud_experiments/lexbor/canary.c",
    "cloud_experiments/libraries.py",
    "cloud_experiments/library_candidates.json",
    "cloud_experiments/library_decisions.json",
    "cloud_experiments/lifecycle.py",
    "cloud_experiments/locks/html5ever-0.39.0-attr-index.lock",
    "cloud_experiments/locks/html5ever-0.40.1-attr-index.lock",
    "cloud_experiments/locks/scraper-0.29.0-html5ever-0.40.1.lock",
    "cloud_experiments/locks/scraper-lean.lock",
    "cloud_experiments/locks/sonic-0.5.10-scraper-0.29.0-html5ever-0.40.1.lock",
    "cloud_experiments/locks/sonic-0.5.10-scraper-lean.lock",
    "cloud_experiments/locks/sonic-0.5.10.lock",
    "cloud_experiments/mcp_sdk/.gitignore",
    "cloud_experiments/mcp_sdk/README.md",
    "cloud_experiments/mcp_sdk/__init__.py",
    "cloud_experiments/mcp_sdk/core_fixtures.json",
    "cloud_experiments/mcp_sdk/driver.py",
    "cloud_experiments/mcp_sdk/protocol_contract.json",
    "cloud_experiments/mcp_sdk/task_inputs.json",
    "cloud_experiments/mcp_sdk/rust/Cargo.lock",
    "cloud_experiments/mcp_sdk/rust/Cargo.toml",
    "cloud_experiments/mcp_sdk/rust/src/main.rs",
    "cloud_experiments/mcp_sdk/test_driver.py",
    "cloud_experiments/mcp_sdk/typescript/package-lock.json",
    "cloud_experiments/mcp_sdk/typescript/package.json",
    "cloud_experiments/mcp_sdk/typescript/server.mjs",
    "cloud_experiments/nim.py",
    "cloud_experiments/parser_study.py",
    "cloud_experiments/process_isolation.py",
    "cloud_experiments/README.md",
    "cloud_experiments/research.py",
    "cloud_experiments/rustup_bootstrap.py",
    "cloud_experiments/service_lab.py",
    "cloud_experiments/service_probe.rs",
    "cloud_experiments/service_study.py",
    "cloud_experiments/test_campaign.py",
    "cloud_experiments/test_lifecycle.py",
    "cloud_experiments/test_wceb.py",
    "cloud_experiments/test_wceb_runner.py",
    "cloud_experiments/vendor/html5ever-0.39.0.crate",
    "cloud_experiments/vendor/html5ever-0.40.1.crate",
    "cloud_experiments/wceb.py",
    "cloud_experiments/wceb_runner.py",
    "cloud_experiments/wceb_v1_tree_manifest.json",
    "cloud_experiments/workloads.py",
)


def audit_decision_map(root, spec):
    """Check core/library inventory mapping without attesting execution or closure."""
    root = Path(root).resolve(strict=True)
    issues, unresolved = [], []

    def issue(code, identity):
        issues.append({"code": code, "id": identity})

    def strings(value, minimum=0):
        return (isinstance(value, list) and len(value) >= minimum
                and all(isinstance(item, str) and item.strip() for item in value)
                and len(set(value)) == len(value))

    def validate_harness(item, identity):
        if "harness" not in item:
            return
        references = item.get("harness")
        if not strings(references, 1):
            issue("invalid_harness", identity)
            return
        for name in references:
            relative = Path(name)
            if ("\\" in name or relative.is_absolute() or PureWindowsPath(name).drive
                    or PureWindowsPath(name).root
                    or ".." in relative.parts):
                issue("unsafe_harness_path", identity + ":" + name)
                continue
            path = root / relative
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                issue("missing_harness_path", identity + ":" + name)
                continue
            current, linked = root, False
            for component in relative.parts:
                if component in ("", "."):
                    continue
                current = current / component
                if current.is_symlink():
                    linked = True
                    break
            if (linked or not resolved.is_relative_to(root)
                    or not (resolved.is_file() or resolved.is_dir())):
                issue("unsafe_harness_path", identity + ":" + name)

    sources = set()
    for path in (root / "src").rglob("*.rs"):
        name = path.relative_to(root).as_posix()
        if name.startswith("src/bin/"):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            issue("invalid_source", name)
        elif path.is_file():
            sources.add(name)
    if not sources:
        issue("missing_sources", "src")
    manifest_path = root / "Cargo.toml"
    if not manifest_path.resolve().is_relative_to(root):
        raise ValueError("Manifest escapes source root")
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    dependencies = set()
    tables = [manifest, *manifest.get("target", {}).values()]
    for table in tables:
        for kind in ("dependencies", "build-dependencies", "dev-dependencies"):
            dependencies.update(table.get(kind, {}))
    allowed_owners = sources | {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}
    covered_sources, covered_dependencies, identities = set(), set(), set()
    decision_dependencies = {}
    if not isinstance(spec, dict) or spec.get("schema") != "athena-core-decisions/1":
        issue("invalid_schema", "contract")
        spec = {}
    decisions = spec.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        issue("invalid_decisions", "contract")
        decisions = []
    for decision in decisions:
        if not isinstance(decision, dict):
            issue("invalid_decision", "unknown")
            continue
        identity = decision.get("id")
        if not isinstance(identity, str) or not identity.strip():
            issue("invalid_decision", "unknown")
            continue
        if identity in identities:
            issue("duplicate_decision", identity)
        identities.add(identity)
        validate_harness(decision, identity)
        if (any(not isinstance(decision.get(key), str) or not decision[key].strip()
                for key in ("question", "oracle", "closure"))
                or not strings(decision.get("alternatives"), 2)
                or not strings(decision.get("gaps"))):
            issue("invalid_decision", identity)
        owners = decision.get("owners")
        if not strings(owners, 1):
            issue("invalid_owner", identity)
            owners = []
        for name in owners:
            path = root / name
            if (name not in allowed_owners or "\\" in name or ".." in Path(name).parts
                    or not path.is_file() or path.is_symlink()
                    or not path.resolve().is_relative_to(root)):
                issue("invalid_owner", identity + ":" + name)
            elif name in sources:
                covered_sources.add(name)
        declared_dependencies = decision.get("dependencies")
        if not strings(declared_dependencies):
            issue("unknown_dependency", identity)
            declared_dependencies = []
        decision_dependencies[identity] = declared_dependencies
        for dependency in declared_dependencies:
            if dependency not in dependencies:
                issue("unknown_dependency", identity + ":" + dependency)
            else:
                covered_dependencies.add(dependency)
        if decision.get("gaps"):
            unresolved.append({"id": identity, "gaps": decision["gaps"]})
    interactions = spec.get("interactions")
    if not isinstance(interactions, list):
        issue("invalid_interaction", "contract")
        interactions = []
    interaction_ids = set()
    for interaction in interactions:
        if not isinstance(interaction, dict):
            issue("invalid_interaction", "unknown")
            continue
        identity = interaction.get("id")
        members = interaction.get("decisions")
        if (not isinstance(identity, str) or not identity.strip()
                or identity in interaction_ids or not strings(members, 2)
                or not set(members).issubset(identities)
                or any(not isinstance(interaction.get(key), str) or not interaction[key].strip()
                       for key in ("mechanism", "design"))):
            issue("invalid_interaction", str(identity))
        if isinstance(identity, str):
            interaction_ids.add(identity)
    expected_language_panels = {f"L{i:02}" for i in range(1, 7)}
    athena_core_decisions = {f"H{i:02}" for i in range(1, 15)}
    requires_language_panels = bool(identities & athena_core_decisions) or "language_panels" in spec
    inventory_decision_dependencies = set(decision_dependencies.get("H09", []))
    if requires_language_panels and "H09" not in identities:
        issue("missing_inventory_decision", "H09")
    uncovered_inventory_dependencies = (sorted(dependencies - inventory_decision_dependencies)
                                        if requires_language_panels else [])
    stale_inventory_dependencies = (sorted(inventory_decision_dependencies - dependencies)
                                    if requires_language_panels else [])
    for dependency in uncovered_inventory_dependencies:
        issue("missing_inventory_dependency", "H09:" + dependency)
    for dependency in stale_inventory_dependencies:
        issue("stale_inventory_dependency", "H09:" + dependency)
    language_panels = spec.get("language_panels", [])
    if requires_language_panels and not isinstance(language_panels, list):
        issue("invalid_language_panels", "contract")
        language_panels = []
    elif not isinstance(language_panels, list):
        language_panels = []
    language_panel_ids = set()
    for panel in language_panels:
        if not isinstance(panel, dict):
            issue("invalid_language_panel", "unknown")
            continue
        identity = panel.get("id")
        admission, status = panel.get("admission"), panel.get("status")
        if not isinstance(identity, str) or not identity.strip():
            issue("invalid_language_panel", "unknown")
            continue
        if identity in language_panel_ids:
            issue("duplicate_language_panel", identity)
        language_panel_ids.add(identity)
        validate_harness(panel, identity)
        allowed_statuses = ({"NOT_IMPLEMENTED", "IMPLEMENTED_UNVERIFIED"}
                            if admission == "REQUIRED"
                            else {"NOT_ADMITTED", "ADMITTED_NOT_IMPLEMENTED", "IMPLEMENTED_UNVERIFIED"}
                            if admission == "CONDITIONAL" else set())
        if (not isinstance(status, str) or status not in allowed_statuses
                or any(not isinstance(panel.get(key), str) or not panel[key].strip()
                       for key in ("gate", "evidence_basis"))):
            issue("invalid_language_panel", identity)
        panel_decisions = panel.get("owner_decisions")
        if not strings(panel_decisions, 1) or not set(panel_decisions).issubset(identities):
            issue("invalid_language_panel_owner", identity)
        panel_interactions = panel.get("interactions")
        if (not strings(panel_interactions)
                or not set(panel_interactions).issubset(interaction_ids)):
            issue("invalid_language_panel_interaction", identity)
    if requires_language_panels:
        for identity in sorted(expected_language_panels - language_panel_ids):
            issue("missing_language_panel", identity)
        for identity in sorted(language_panel_ids - expected_language_panels):
            issue("unexpected_language_panel", identity)
    uncovered_sources = sorted(sources - covered_sources)
    uncovered_dependencies = sorted(dependencies - covered_dependencies)
    library_relative = "cloud_experiments/library_decisions.json"
    library_path = root / library_relative
    library_entries = {}
    try:
        resolved_library_path = library_path.resolve(strict=True)
    except FileNotFoundError:
        issue("missing_library_registry", library_relative)
    except (OSError, RuntimeError):
        issue("invalid_library_registry", library_relative)
    else:
        if (library_path.is_symlink() or not resolved_library_path.is_relative_to(root)
                or not resolved_library_path.is_file()):
            issue("invalid_library_registry", library_relative)
        else:
            try:
                library_registry = protocol.strict_json_loads(
                    resolved_library_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                issue("invalid_library_registry", library_relative)
            else:
                if (not isinstance(library_registry, dict)
                        or library_registry.get("schema") != "athena-library-research/1"):
                    issue("invalid_library_schema", library_relative)
                elif not isinstance(library_registry.get("libraries"), dict):
                    issue("invalid_library_registry", library_relative + ":libraries")
                else:
                    library_entries = library_registry["libraries"]

    uncovered_library_dependencies = sorted(dependencies - set(library_entries))
    stale_library_decisions = sorted(set(library_entries) - dependencies)
    for dependency in uncovered_library_dependencies:
        issue("missing_library_decision", dependency)
    for dependency in stale_library_decisions:
        issue("stale_library_decision", dependency)
    for name, decision in library_entries.items():
        if (not isinstance(decision, dict)
                or not strings(decision.get("consumer"), 1)
                or any(not isinstance(decision.get(key), str) or not decision[key].strip()
                       for key in ("purpose", "gate", "source"))
                or not strings(decision.get("alternatives"), 2)):
            issue("invalid_library_decision", name)
    return {"schema": "athena-core-decision-audit/1",
            "mapping_status": "INCOMPLETE" if issues or uncovered_sources or uncovered_dependencies else "MAPPED",
            "inventory": {"core_sources": sorted(sources), "direct_dependencies": sorted(dependencies),
                          "library_decisions": sorted(library_entries)},
            "uncovered_sources": uncovered_sources, "uncovered_dependencies": uncovered_dependencies,
            "inventory_decision_dependencies": sorted(inventory_decision_dependencies),
            "uncovered_inventory_dependencies": uncovered_inventory_dependencies,
            "stale_inventory_dependencies": stale_inventory_dependencies,
            "uncovered_library_dependencies": uncovered_library_dependencies,
            "stale_library_decisions": stale_library_decisions,
            "language_panels": [{key: panel[key] for key in
                                  ("id", "admission", "status", "gate", "evidence_basis")}
                                 for panel in language_panels if isinstance(panel, dict)],
            "issues": issues, "unresolved": unresolved,
            "decision_sufficiency": "NOT_EVALUATED", "experiment_execution": "NOT_EVALUATED",
            "limit": "Checks declared source/core/library inventory mapping only; does not verify semantic coverage, runnable comparators, evidence quality, dependency approval or optimality."}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reviewed_cloud_experiment_sources(root):
    root = Path(root).resolve(strict=True)
    directory = root / "cloud_experiments"
    if (directory.is_symlink() or not directory.resolve(strict=True).is_relative_to(root)
            or not directory.is_dir()):
        raise ValueError("Reviewed cloud experiment source directory is missing or escaped")
    allowed = set(CLOUD_EXPERIMENT_BUNDLE_FILES)
    discovered = set()

    def walk_error(error):
        raise error

    for current, directories, filenames in os.walk(directory, topdown=True, followlinks=False, onerror=walk_error):
        parent = Path(current)
        for child in tuple(directories):
            path = parent / child
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Source link/escape rejected")
            if child.lower() in ("target", "__pycache__", "node_modules"):
                directories.remove(child)
        for filename in filenames:
            path = parent / filename
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Source link/escape rejected")
            if not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            if name not in allowed:
                raise ValueError("Unreviewed cloud experiment file: " + name)
            discovered.add(name)

    missing = sorted(allowed - discovered)
    if missing:
        raise ValueError("Reviewed cloud experiment source is missing: " + missing[0])
    return sorted(allowed)


def paired_rows(rows, case, phase, blocks, iterations, *, order_offset=0):
    if type(order_offset) is not int or order_offset not in (0, 1):
        raise ValueError("Invalid predeclared arm-order offset")
    pairs = [{"baseline": {}, "candidate": {}} for _ in range(blocks)]
    seen, fingerprints = set(), set()
    for row in rows:
        if row.get("case") != case:
            raise ValueError("Wrong case")
        arm, block, i = row.get("arm"), row.get("block"), row.get("iteration")
        if (arm not in ("baseline", "candidate") or type(block) is not int
                or not 0 <= block < blocks or type(i) is not int or not 0 <= i < iterations
                or row.get("phase") != phase
                or row.get("order") != ((0 if arm == "baseline" else 1) ^ (block % 2) ^ order_offset)
                or type(row.get("ns")) is not int or row["ns"] <= 0
                or not protocol.is_digest(row.get("fingerprint"))):
            raise ValueError("Invalid timing identity, output, phase, order or value")
        key = (arm, block, i)
        if key in seen:
            raise ValueError("Duplicate sample")
        seen.add(key)
        fingerprints.add(row["fingerprint"])
        pairs[block][arm][i] = row["ns"]
    if len(fingerprints) != 1 or len(seen) != 2 * blocks * iterations:
        raise ValueError("Missing samples or divergent outputs")
    return [{arm: [pair[arm][i] for i in range(iterations)]
             for arm in ("baseline", "candidate")} for pair in pairs]


def select_finalists(records, required_cases, count):
    feasible = []
    for name, record in records.items():
        ratios = record.get("ratios", {})
        if (not record.get("correct") or set(ratios) != set(required_cases)
                or any(not math.isfinite(v) or v <= 0 for v in ratios.values())):
            continue
        score = math.exp(math.fsum(math.log(ratios[key]) for key in required_cases) / len(required_cases))
        feasible.append((score, name))
    return [name for _, name in sorted(feasible)[:count]]


def source_files(root, names):
    root = Path(root).resolve(strict=True)
    files, source_bytes = {}, 0
    file_limit, source_limit = 8 * 1024 * 1024, 16 * 1024 * 1024
    for name in names:
        origin = root / name
        if origin.is_symlink() or not origin.resolve().is_relative_to(root):
            raise ValueError("Source link/escape rejected")
        if origin.is_file():
            paths = [origin]
        elif origin.is_dir():
            paths = []

            def walk_error(error):
                raise error

            for directory, directories, filenames in os.walk(
                    origin, topdown=True, followlinks=False, onerror=walk_error):
                parent = Path(directory)
                for child in tuple(directories):
                    path = parent / child
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        raise ValueError("Source link/escape rejected")
                    if child.lower() in ("target", "__pycache__", "node_modules"):
                        directories.remove(child)
                paths.extend(parent / filename for filename in filenames)
        else:
            paths = []
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Source link/escape rejected")
            if path.is_file():
                remaining = source_limit - source_bytes
                if remaining <= 0:
                    raise ValueError("Source bundle exceeds budget")
                limit = min(file_limit, remaining)
                chunks, size = [], 0
                with path.open("rb") as stream:
                    while size <= limit:
                        chunk = stream.read(min(65536, limit - size + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                if size > file_limit:
                    raise ValueError("Oversized source input")
                if size > remaining:
                    raise ValueError("Source bundle exceeds budget")
                data = (chunks[0] if chunks else b"") if len(chunks) <= 1 else b"".join(chunks)
                source_bytes += len(data)
                relative_name = path.relative_to(root).as_posix()
                files[relative_name] = canonical_source_bytes(data, relative_name)
    if sum(map(len, files.values())) > source_limit:
        raise ValueError("Source bundle exceeds budget")
    return files


def canonical_source_bytes(data, relative_path=None):
    """Normalize source text, but preserve data fixtures byte-for-byte."""
    if relative_path is not None:
        normalized_path = str(relative_path).replace("\\", "/")
        if normalized_path.startswith(("fixtures/", "tests/fixtures/", "cloud_experiments/corpus/")):
            return data
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Frozen source does not match reviewed transformation")
    return text.replace(old, new, 1)


def aae_variant(text, writer, reserve, hashing):
    if not reserve:
        start = text.index("    let estimated = 512")
        end = text.index("    let mut writer =", start)
        text = text[:start] + "    let estimated = 0;\n" + text[end:]
    if not hashing:
        start = text.index("    let all_text = page")
        end = text.index("\n    writer.write_frame", start)
        text = text[:start] + '''    let mut hash = blake3::Hasher::new();
    for (i, chunk) in page.chunks.iter().enumerate() {
        if i != 0 { hash.update(b"\\n"); }
        hash.update(chunk.text.as_bytes());
    }
    let digest = hash.finalize().to_hex().to_string();
''' + text[end:]
    if not writer:
        start = text.index("struct BoundedWriter {")
        end = text.index("\npub fn export_jsonl(", start)
        text = text[:start] + '''struct BoundedWriter { out: Vec<u8>, limit: usize }
impl io::Write for BoundedWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.out.len()) {
            return Err(io::Error::other("AAE output byte budget exceeded"));
        }
        self.out.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}
impl BoundedWriter {
    fn new(capacity: usize, limit: usize) -> Self {
        Self { out: Vec::with_capacity(capacity.min(limit)), limit }
    }
    fn write_frame(&mut self, frame: &Frame<'_>) -> Result<(), serde_json::Error> {
        serde_json::to_writer(&mut *self, frame)?;
        io::Write::write_all(self, b"\\n").map_err(serde_json::Error::io)
    }
    fn finish(self) -> String {
        String::from_utf8(self.out).expect("serde_json emits UTF-8")
    }
}
''' + text[end:]
    return text


def linear_rank(text):
    start = text.index("    let term_ids: HashMap<_, _> = terms")
    end = text.index("    let mut postings", start)
    text = text[:start] + text[end:]
    text = replace_once(text, "use std::collections::{HashMap, HashSet};", "use std::collections::HashSet;")
    return replace_once(text,
        "if let Some(&term) = term_ids.get(lowercase(word).as_ref()) {",
        "if let Ok(term) = terms.binary_search_by(|term| term.as_str().cmp(lowercase(word).as_ref())) {")


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def prepare_directory(destination, build):
    """Build a complete output tree privately, then publish it as one directory."""
    destination = Path(destination)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.prepare-", dir=parent) as temporary:
        staging = Path(temporary)
        result = build(staging)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        os.rename(staging, destination)
        sync_directory(parent)
    return result


def write_json(path, value, *, max_bytes=None):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1 or len(data) > max_bytes):
        raise ValueError("JSON publication exceeds its byte bound")
    if path.is_symlink():
        raise ValueError("JSON output cannot be a symlink")
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(destination):
    destination = Path(destination)

    def build(staging):
        files = source_files(ROOT, ["src", "tests", "fixtures", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
                                    *reviewed_cloud_experiment_sources(ROOT), "kaggle_experiments/protocol.py",
                                    "kaggle_experiments/runner.py", "kaggle_experiments/probe.rs",
                                    "kaggle_experiments/test_protocol.py", "kaggle_experiments/pack_snapshots.py",
                                    "kaggle_experiments/generate_clusters.py", "kaggle_results/verify_statistics.py",
                                    "kaggle_results/pull_and_analyze.py", "model"])
        files = {key: value for key, value in files.items() if "__pycache__" not in key}
        previous = subprocess.check_output(["git", "show", "HEAD:src/aae.rs"], cwd=ROOT)
        files["cloud_experiments/previous_aae.rs"] = previous
        raw = pack_snapshots.archive_files(files)
        (staging / "source.tgz").write_bytes(raw)
        spec = study_spec()
        spec.update(source_sha256=digest(raw), files={key: digest(value) for key, value in sorted(files.items())},
                    git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    working_tree="Frozen allowlisted bytes; includes existing user changes")
        spec["study_id"] = digest(json.dumps(spec, sort_keys=True).encode())
        write_json(staging / "spec.json", spec)
        return {"study_id": spec["study_id"], "bundle_bytes": len(raw), "files": len(files)}

    return prepare_directory(destination, build)


def study_spec():
    return {"protocol": PROTOCOL, "kind": "screen-then-independent-confirm",
            "wall_seconds": 18000, "command_timeout_seconds": 1800,
            "runtime": {"rust": "1.99.1", "python_min": [3, 11], "confirmation_python": [3, 13]},
            "screen": {"blocks": 6, "iterations": 30, "seed": 38111},
            "confirm": {"blocks": 20, "iterations": 250, "seed": 98273,
                        "max_finalists": 2, "family_size": 48, "alpha": .05,
                        "bootstrap_draws": 192000, "minimum_tail_draws": 100,
                        "metrics": ["p50_ns", "p95_ns", "p99_ns", "rss_p50_KiB"],
                        "drift_threshold": .5},
            "factors": {"writer": ["bounded_vec", "frame_string"], "reserve": ["zero", "estimate"],
                        "content_hash": ["incremental", "joined"], "rank_lookup": ["hash", "binary_search"]},
            "selection": "Two fastest complete contract-passing arms by geometric mean of screen p50 ratios; exploratory only",
            "stop": "Finish fixed panel, or fail contract/toolchain/dependency, or 5h job bound; no automatic production adoption",
            "result_authenticity": "NOT VERIFIED: runner and evidence share OS authority",
            "limits": {"source_MiB": 16, "expected_evidence_MiB": 250, "expected_build_GiB": 12,
                       "minimum_free_GiB": 20, "file_limit_MiB": 512,
                       "memory_GiB": 12, "processes": 128},
            "coverage": coverage()}


def coverage():
    return {
        "R1_arena": "Existing layout/bounds/reset tests; new dependency identity; no new arena rival without hotspot evidence",
        "R2_projection": "Real parser workload size/shape and exact content oracles; HTML5 TreeSink rival deferred until profile justifies",
        "R3_access": "Existing HTTPS policy, invalid validator, DNS cancellation gates; no live production path or client-pooling comparator",
        "R4_rank": "Hash versus binary query lookup; real BM25 numerical/order oracles, Unicode, duplicate queries, novelty; no semantic ranker claim",
        "R5_table": "Existing independent span/grid/error oracles and scaling; HTML-to-header-grid integration NOT IMPLEMENTED",
        "R6_selection": "Existing bandit/RNG consumer gates; no value from repeating prior million-draw study without a new hypothesis",
        "R7_AAE": "2x2x2 factorial, byte identity, output cap, framing, query/no-query, resource stress, pipeline confirmation",
        "R8_concurrency": "Existing real async admission, sharing, cancellation/drain gates; no cloud service throughput/SLO claim",
        "R9_dependencies": "Frozen lock, cargo metadata graph, checksum verification by Cargo; no unreviewed mass update",
        "workflow_harness": "F01-F08/W0-W3 contracts remain required; planner/SDK/model/sandbox comparators NOT IMPLEMENTED; no synthetic success labels",
        "index": "Explicitly deferred by user",
        "deployment": "One GCE machine family; cross-VM, other CPU architecture and deployed service confirmation NOT VERIFIED"}


def fixtures(folder):
    folder.mkdir(exist_ok=False)
    pages = {}
    for split, seed in (("screen", 38111), ("confirm", 98273)):
        rng = random.Random(seed)
        for shape, count in (("tiny", 8), ("article", 128), ("large", 1800),
                             ("unicode", 128), ("escaped", 128), ("links", 1000),
                             ("deep", 300), ("duplicates", 1000)):
            parts = []
            for i in range(count):
                term = rng.choice(["rust tokio runtime", "network latency", "ordinary details"])
                text = f"Document {i} {term} " + "context evidence useful source " * rng.randrange(2, 8)
                if shape == "unicode":
                    text += "TÀI liệu 内容 日本語 Tiếng Việt " * 4
                if shape == "escaped":
                    text += '&quot;\\ &lt; &amp; {"type":"end"} ' * 8
                if shape in ("links", "duplicates"):
                    target = i if shape == "links" else i % 5
                    text += f"<a href='/source/{target}'>source</a>"
                parts.append(f"<p>{text}</p>")
            html = "".join(parts)
            if shape == "deep":
                html = "<div>" * count + "<p>rust tokio runtime</p>" + "</div>" * count
            for with_query in (False, True):
                key = f"{split}-{shape}-{'q' if with_query else 'plain'}"
                data = {"html": html, "query": "rust tokio runtime" if with_query else None}
                path = folder / (key + ".json")
                write_json(path, data)
                pages[key] = path
    for name, n in (("budget-small", 1024), ("budget-large", 16 * 1024 * 1024)):
        path = folder / (name + ".json")
        write_json(path, {"html": "<p>x</p>", "oversized_text_bytes": n})
        pages[name] = path
    return pages


def case_panel(pages, phase):
    if phase == "screen":
        cases = [(stage, shape, q) for shape in ("tiny", "article", "large", "unicode", "escaped", "links", "deep", "duplicates")
                 for q in ("q", "plain") for stage in ("export", "pipeline")]
        cases += [("rank", shape, "q") for shape in ("tiny", "article", "large", "unicode")]
    else:
        cases = [(stage, shape, q) for stage, shape in (("export", "article"), ("pipeline", "large"), ("pipeline", "escaped"))
                 for q in ("q", "plain")]
    return [{"id": f"{stage}-{shape}-{q}", "stage": stage,
             "fixture": str(pages[f"{phase}-{shape}-{q}"])} for stage, shape, q in cases]


def benchmark(run, binary, case, iterations, cpu):
    packet = protocol.strict_json_loads(run.run(["taskset", "-c", cpu, binary / "cloud_bench", case["stage"], case["fixture"], iterations],
                                                timeout=180, log_output=False))
    if (packet.get("protocol") != PROTOCOL or packet.get("stage") != case["stage"]
            or packet.get("iterations") != iterations or len(packet.get("raw_ns", [])) != iterations
            or len(packet.get("offsets_ns", [])) != iterations
            or not protocol.is_digest(packet.get("fingerprint"))
            or any(type(ns) is not int or ns <= 0 for ns in packet["raw_ns"])
            or any(type(ns) is not int or ns < 0 for ns in packet["offsets_ns"])
            or packet["offsets_ns"] != sorted(set(packet["offsets_ns"]))):
        raise ValueError("Invalid native timing packet")
    return packet


def paired_measure(run, binaries, name, cases, phase, pages, cpu):
    settings = run.manifest["spec"][phase]
    rows, peak = [], {}
    order_rng = random.Random(settings["seed"])
    for block in range(settings["blocks"]):
        order_cases = list(cases)
        order_rng.shuffle(order_cases)
        for case in order_cases:
            for arm in (("baseline", "candidate") if block % 2 == 0 else ("candidate", "baseline")):
                binary = binaries["baseline" if arm == "baseline" else name]
                packet = benchmark(run, binary, case, settings["iterations"], cpu)
                for i, ns in enumerate(packet["raw_ns"]):
                    row = {"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"], "variant": name,
                           "case": case["id"], "phase": phase, "arm": arm, "block": block, "iteration": i,
                           "ns": ns, "offset_ns": packet["offsets_ns"][i], "fingerprint": packet["fingerprint"],
                           "order": (0 if arm == "baseline" else 1) ^ (block % 2)}
                    run.sample_stream.write(json.dumps(row, allow_nan=False) + "\n")
                    rows.append(row)
                run.sample_stream.flush()
                if phase == "confirm":
                    rss_file = run.directory / "rss-current.txt"
                    run.run(["/usr/bin/time", "-f", "%M", "-o", rss_file, "taskset", "-c", cpu,
                             binary / "cloud_bench", case["stage"], case["fixture"], "1"], log_output=False)
                    value = protocol.parse_peak_rss(rss_file.read_text())
                    peak.setdefault(case["id"], {}).setdefault(block, {})[arm] = value
                    run.sample_stream.write(json.dumps({"protocol": PROTOCOL,
                        "study_id": run.manifest["bundle_id"], "variant": name, "kind": "rss",
                        "case": case["id"], "phase": phase, "arm": arm, "block": block,
                        "order": (0 if arm == "baseline" else 1) ^ (block % 2),
                        "value": value, "unit": "KiB"}) + "\n")
                    run.sample_stream.flush()
            run.save()
    groups = {case["id"]: paired_rows([row for row in rows if row["case"] == case["id"]],
               case["id"], phase, settings["blocks"], settings["iterations"]) for case in cases}
    medians = {key: protocol.quantile([v for block in blocks for v in block["candidate"]], .5)
              / protocol.quantile([v for block in blocks for v in block["baseline"]], .5)
               for key, blocks in groups.items()}
    return groups, peak, medians


def oracle_gate(run, binary, baseline, pages, phase):
    cases = case_panel(pages, phase)
    for case in cases:
        command = [case["stage"], case["fixture"], "0"]
        actual = run.run([binary / "cloud_bench", *command], log_output=False)
        expected = run.run([baseline / "cloud_bench", *command], log_output=False)
        if actual != expected:
            raise ValueError("Full output byte/rank parity failed for " + case["id"])
        if case["stage"] in ("export", "pipeline"):
            protocol.validate_aae(
                actual,
                expected_source_url=case["fixture"].get("url") or "https://example.org/",
            )
    runner.probe_oracles(run, binary)
    fixture_dir = run.directory / ("oracle-" + binary.name + "-" + phase)
    fixture_dir.mkdir(exist_ok=False)
    runner.content_oracles(run, binary, fixture_dir, include_pre=True)
    for shape in ("budget-small", "budget-large"):
        result = protocol.strict_json_loads(run.run([binary / "cloud_bench", "budget", pages[shape], "0"], log_output=False))
        if result != {"status": "EXPECTED_ERROR", "error": "output_budget"}:
            raise ValueError("Output limit gate failed")
    return {"parity_cases": len(cases), "independent_content_cases": 9, "independent_rank_grid": True,
            "resource_error_cases": 2, "limit": "Finite real code checks; not exhaustive"}


def build_variant(run, source, frozen, aae, rank, name, bins, full=False):
    (source / "src/aae.rs").write_text(aae, encoding="utf-8")
    (source / "src/rank.rs").write_text(rank, encoding="utf-8")
    run.log("BUILD_VARIANT " + name)
    run.run(["rustfmt", "--edition", "2024", source / "src/aae.rs", source / "src/rank.rs"], cwd=source)
    run.run(["cargo", "fmt", "--all", "--", "--check"], cwd=source)
    scopes = [["--all-targets"]] if full else [["--lib", "aae::tests"], ["--lib", "rank::tests"], ["--test", "integration"]]
    for scope in scopes:
        run.run(["cargo", "test", "--locked", "-j", "2", *scope], cwd=source, timeout=1200)
    run.run(["cargo", "clippy", "--locked", "--all-targets", "-j", "2", "--", "-D", "warnings"], cwd=source, timeout=900)
    run.run(["cargo", "build", "--locked", "--release", "--bins", "-j", "2"], cwd=source, timeout=1800)
    if protocol.sha256_file(source / "Cargo.lock") != frozen["Cargo.lock"]:
        raise ValueError("Dependency graph changed during locked build")
    destination = bins / name
    destination.mkdir(exist_ok=False)
    for executable in ("cloud_bench", "research_probe", "athena-core-v2", "bench_core"):
        shutil.copy2(Path(os.environ["CARGO_TARGET_DIR"]) / "release" / executable, destination / executable)
    run.manifest["snapshots"][name] = {
        "aae_sha256": protocol.sha256_file(source / "src/aae.rs"),
        "rank_sha256": protocol.sha256_file(source / "src/rank.rs"),
        "lock_sha256": frozen["Cargo.lock"],
        "binaries": {p.name: protocol.sha256_file(p) for p in destination.iterdir()}}
    run.save()
    return destination


def resource_probe(run, binary, pages, cpu):
    measurements = {}
    for case in ("budget-small", "budget-large"):
        measurements[case] = []
        for repeat in range(3):
            path = run.directory / "rss-current.txt"
            actual = protocol.strict_json_loads(run.run(["/usr/bin/time", "-f", "%M", "-o", path,
                "taskset", "-c", cpu, binary / "cloud_bench", "budget", pages[case], "0"], log_output=False))
            if actual != {"status": "EXPECTED_ERROR", "error": "output_budget"}:
                raise ValueError("Resource negative case returned success")
            value = protocol.parse_peak_rss(path.read_text())
            measurements[case].append(value)
            run.sample_stream.write(json.dumps({"protocol": PROTOCOL, "study_id": run.manifest["bundle_id"],
                "variant": binary.name, "case": case, "phase": "resource", "kind": "rss", "repeat": repeat,
                "status": "EXPECTED_ERROR", "value": value, "unit": "KiB"}) + "\n")
            run.sample_stream.flush()
    return measurements


def dependency_advisories(run, graph):
    from cloud_experiments.http_transport import bounded_request
    lock_sha256 = graph.get("lock_sha256")
    if (not isinstance(lock_sha256, str) or len(lock_sha256) != 64
            or any(character not in "0123456789abcdef" for character in lock_sha256)):
        raise ValueError("Dependency advisory query requires a lowercase Cargo.lock SHA-256")
    destination = run.directory / ("dependency-advisories-" + lock_sha256 + ".json")
    if destination.is_symlink():
        raise ValueError("Dependency advisory report cannot be a symlink")
    prior = run.manifest.get("dependency_advisories_by_lock", {}).get(lock_sha256)
    if destination.is_file():
        result = protocol.strict_json_loads(destination.read_text(encoding="utf-8"))
        if result.get("lock_sha256") != lock_sha256:
            raise ValueError("Existing advisory report belongs to another Cargo.lock")
        report_sha256 = protocol.sha256_file(destination)
        if prior is not None and prior.get("sha256") != report_sha256:
            raise ValueError("Existing advisory report changed after it was recorded")
    else:
        if prior is not None:
            raise ValueError("Recorded advisory report is missing")
        packages = sorted({(p["name"], p["version"]) for p in graph["packages"].values()
                           if p["source"] and p["source"].startswith("registry+")})
        results = []
        try:
            for start in range(0, len(packages), 100):
                batch = packages[start:start + 100]
                body = json.dumps({"queries": [{"package": {"name": name, "ecosystem": "crates.io"},
                            "version": version} for name, version in batch]}).encode()
                timeout = 45.0
                deadline = getattr(run, "deadline", None)
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError("Campaign deadline exhausted before advisory request")
                status, _headers, raw = bounded_request(
                    "POST", "https://api.osv.dev/v1/querybatch",
                    {"Content-Type": "application/json"}, body, timeout, 4 * 1024 * 1024)
                if status != 200:
                    raise ValueError("Advisory endpoint returned HTTP status " + str(status))
                reply = protocol.strict_json_loads(raw)
                if len(reply.get("results", [])) != len(batch):
                    raise ValueError("Incomplete advisory response")
                for (name, version), result_item in zip(batch, reply["results"]):
                    if result_item.get("next_page_token") or result_item.get("error"):
                        raise ValueError("Partial/paginated advisory response; no clean-dependency claim")
                    results.append({"name": name, "version": version, "vulnerabilities": result_item.get("vulns", [])})
            result = {"status": "QUERIED", "source": "https://api.osv.dev/v1/querybatch",
                      "queried_at": runner.utc_now(), "lock_sha256": lock_sha256, "packages": results,
                      "limit": "Public known-advisory lookup; no reachability or zero-vulnerability guarantee"}
        except Exception as error:
            result = {"status": "NOT VERIFIED", "error": str(error), "lock_sha256": lock_sha256,
                      "partial_results": results}
        write_json(destination, result)
        report_sha256 = protocol.sha256_file(destination)

    reported = result.get("packages", result.get("partial_results", []))
    run.manifest.setdefault("dependency_advisories_by_lock", {})[lock_sha256] = {
        "status": result["status"], "report": destination.name, "sha256": report_sha256,
        "package_count": len(reported),
        "vulnerable_package_count": sum(bool(item.get("vulnerabilities")) for item in reported)}
    run.save()
    return result["status"]


def confirm_analysis(run, name, groups, peak):
    cases = {}
    for case_id, rounds in groups.items():
        common = {"family_size": 48, "analysis_policy": protocol.ANALYSIS_POLICY, "screen": False}
        cases[case_id] = {**common, "rounds": rounds, "quantiles": [.5, .95, .99], "spec": {"unit": "ns"}}
        rss_rounds = [{arm: [value] for arm, value in peak[case_id][block].items()} for block in range(20)]
        cases[case_id + "/rss"] = {**common, "rounds": rss_rounds, "quantiles": [.5], "spec": {"unit": "KiB"}}
    report = protocol.analyze_measurements(cases, draws=192000, seed=98273, alpha=.05, backend="numpy")
    report.update(protocol=PROTOCOL, variant=name, study_id=run.manifest["bundle_id"],
                  result_authenticity=run.manifest["spec"]["result_authenticity"],
                  scope="Conditional on one GCE host and held-out synthetic seeds; no production approval")
    write_json(run.directory / ("confirmation-" + name + ".json"), report)
    return {key: value["decision"] for key, value in report["cases"].items()}


def execute(source, spec_path, output):
    source, spec_path, output = Path(source).resolve(), Path(spec_path).resolve(), Path(output).resolve()
    spec = protocol.strict_json_loads(spec_path.read_text(encoding="utf-8"))
    identity = spec.pop("study_id")
    if digest(json.dumps(spec, sort_keys=True).encode()) != identity or spec["protocol"] != PROTOCOL:
        raise ValueError("Spec identity/protocol mismatch")
    spec["study_id"] = identity
    if platform.system() != "Linux" or sys.version_info[:2] != (3, 13):
        raise RuntimeError("Confirmation requires Linux/Python 3.13; local preparation supports Python 3.11+")
    if shutil.disk_usage(output.parent).free < 20 * 1024 ** 3:
        raise RuntimeError("Less than 20 GiB free before build")
    run = runner.EvidenceRun(output, "cloud-native", identity, spec)
    run.deadline = time.monotonic() + spec["wall_seconds"]
    run.manifest.update(protocol=PROTOCOL, result_authenticity=spec["result_authenticity"],
                        decision="HOLD", coverage=spec["coverage"], current_phase="preflight")
    run.save()
    try:
        for name, expected in spec["files"].items():
            path = source / name
            if path.is_symlink() or not path.resolve().is_relative_to(source) or protocol.sha256_file(path) != expected:
                raise ValueError("Frozen source mismatch: " + name)
        runtime = run.run(["rustc", "-Vv"]).decode()
        if not runtime.startswith("rustc 1.99.1 "):
            raise RuntimeError("Pinned Rust 1.99.1 required")
        if os.environ.get("RUSTFLAGS") or any(key.startswith("CARGO_PROFILE_RELEASE_") for key in os.environ):
            raise RuntimeError("Uncontrolled compiler/profile flags")
        affinity = sorted(os.sched_getaffinity(0))
        cpu = str(affinity[-1])
        run.manifest["environment"] = {"python": sys.version, "platform": platform.platform(), "rustc": runtime,
            "lscpu": run.run(["lscpu", "--json"], log_output=False).decode(), "affinity": affinity,
            "timing_cpu": cpu, "free_bytes_start": shutil.disk_usage(output.parent).free,
            "timing": "10 warmups; immutable fixture setup, initial output fingerprint and JSON report outside timer; allocation/free inside operation; no live network",
            "rss": "GNU time fresh process peak, including input, parse/setup, initial output and warmup; not component-only allocations"}
        run.run([sys.executable, "-B", "-m", "unittest", "cloud_experiments.test_campaign",
                 "kaggle_experiments.test_protocol", "model.test_models", "-v"], cwd=source)
        workspace = output / "work"
        workspace.mkdir()
        for name in spec["files"]:
            if name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml") or name.startswith(("src/", "tests/", "fixtures/")):
                destination = workspace / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((source / name).read_bytes())
        for original, injected in ((source / "cloud_experiments/bench.rs", "cloud_bench.rs"),
                                   (source / "kaggle_experiments/probe.rs", "research_probe.rs")):
            destination = workspace / "src/bin" / injected
            destination.write_bytes(original.read_bytes())
            run.run(["rustfmt", "--edition", "2024", destination], cwd=workspace)
        os.environ["CARGO_TARGET_DIR"] = str(output.parent / "cargo-target")
        bins = output / "bins"
        bins.mkdir()
        pages = fixtures(output / "fixtures")
        write_json(output / "fixture-inventory.json", {key: protocol.sha256_file(path) for key, path in pages.items()})
        aae0 = (source / "src/aae.rs").read_text(encoding="utf-8")
        rank0 = (source / "src/rank.rs").read_text(encoding="utf-8")
        definitions = {"baseline": (aae0, rank0)}
        for writer, reserve, hashing in itertools.product((0, 1), repeat=3):
            if (writer, reserve, hashing) != (1, 1, 1):
                definitions[f"w{writer}c{reserve}h{hashing}"] = (aae_variant(aae0, writer, reserve, hashing), rank0)
        definitions["rank_binary"] = (aae0, linear_rank(rank0))
        definitions["previous_aae"] = ((source / "cloud_experiments/previous_aae.rs").read_text(encoding="utf-8"), rank0)
        write_json(output / "variant-definitions.json", {name: {"aae_sha256": digest(a.encode()), "rank_sha256": digest(r.encode())}
                                                      for name, (a, r) in definitions.items()})
        binaries, screening, resources = {}, {}, {}
        for name, (aae, rank) in definitions.items():
            run.manifest.update(current_phase="build", current_variant=name)
            run.save()
            binaries[name] = build_variant(run, workspace, spec["files"], aae, rank, name, bins, full=name == "baseline")
            if name == "baseline":
                graph = libraries.offline_dependency_metadata(run, workspace)
                write_json(output / "dependency-graph.json", graph)
                run.manifest["dependency_advisories"] = dependency_advisories(run, graph)
            run.manifest["checks"]["oracle_" + name] = {"status": "PASS", "evidence":
                oracle_gate(run, binaries[name], binaries["baseline"], pages, "screen")}
            resources[name] = resource_probe(run, binaries[name], pages, cpu)
            write_json(output / "resource-probes.json", resources)
            run.save()
        run.manifest["current_phase"] = "pilot"
        run.save()
        pilot_start = time.monotonic()
        pilot = [benchmark(run, binaries["baseline"], case, 5, cpu) for case in case_panel(pages, "screen")]
        mean_ns = {case["id"]: math.fsum(packet["raw_ns"]) / len(packet["raw_ns"])
                   for case, packet in zip(case_panel(pages, "screen"), pilot)}
        run.manifest["pilot"] = {"wall_seconds": time.monotonic() - pilot_start, "mean_ns": mean_ns,
            "screen_operation_seconds_estimate": sum(mean_ns.values()) / 1e9 * 2 * 6 * 30 * (len(binaries) - 1),
            "limit": "Excludes process/setup/warmup overhead, analysis, future build and cloud scheduling noise"}
        run.save()
        case_ids = [case["id"] for case in case_panel(pages, "screen")]
        order = sorted(name for name in binaries if name != "baseline")
        random.Random(38111).shuffle(order)
        for name in order:
            run.manifest.update(current_phase="screen", current_variant=name)
            run.save()
            groups, _, ratios = paired_measure(run, binaries, name, case_panel(pages, "screen"), "screen", pages, cpu)
            screening[name] = {"correct": True, "ratios": ratios, "status": "EXPLORATORY"}
            write_json(output / "screening.json", screening)
        shortlist = select_finalists(screening, case_ids, 1)
        if shortlist and shortlist[0].startswith("w"):
            selected = shortlist[0]
            aae, _ = definitions[selected]
            definitions["combined"] = (aae, linear_rank(rank0))
            run.manifest.update(current_phase="build", current_variant="combined")
            binaries["combined"] = build_variant(run, workspace, spec["files"], aae, definitions["combined"][1], "combined", bins)
            oracle_gate(run, binaries["combined"], binaries["baseline"], pages, "screen")
            resources["combined"] = resource_probe(run, binaries["combined"], pages, cpu)
            write_json(output / "resource-probes.json", resources)
            groups, _, ratios = paired_measure(run, binaries, "combined", case_panel(pages, "screen"), "screen", pages, cpu)
            screening["combined"] = {"correct": True, "ratios": ratios, "status": "EXPLORATORY"}
            write_json(output / "screening.json", screening)
        shortlist = select_finalists(screening, case_ids, 2)
        run.manifest.update(finalists=shortlist, current_phase="confirm", decisions={})
        run.save()
        for name in shortlist:
            aae, rank = definitions[name]
            (workspace / "src/aae.rs").write_text(aae, encoding="utf-8")
            (workspace / "src/rank.rs").write_text(rank, encoding="utf-8")
            run.run(["rustfmt", "--edition", "2024", workspace / "src/aae.rs", workspace / "src/rank.rs"], cwd=workspace)
            run.run(["cargo", "test", "--locked", "--all-targets", "-j", "2"], cwd=workspace, timeout=1200)
            oracle_gate(run, binaries[name], binaries["baseline"], pages, "confirm")
            run.manifest.update(current_variant=name)
            run.save()
            groups, peak, _ = paired_measure(run, binaries, name, case_panel(pages, "confirm"), "confirm", pages, cpu)
            run.manifest["current_phase"] = "analysis"
            run.save()
            # The expensive analysis is supervised as a child, so the common deadline also covers it.
            inputs = output / ("analysis-input-" + name + ".json")
            write_json(inputs, {"groups": groups, "peak": peak})
            run.run([sys.executable, "-B", "-m", "cloud_experiments.campaign", "analyze", inputs,
                     output / ("confirmation-" + name + ".json"), name, identity], cwd=source, timeout=3600)
            report = protocol.strict_json_loads((output / ("confirmation-" + name + ".json")).read_text())
            report["oversized_api_rss"] = {"baseline": resources["baseline"], "candidate": resources[name],
                "status": "HOLD" if any(max(resources[name][case]) > max(resources["baseline"][case])
                                          for case in resources[name]) else "NO_ADVERSE_MAXIMUM_OBSERVED",
                "limit": "Three fresh processes/size; no population tail or zero-risk claim"}
            write_json(output / ("confirmation-" + name + ".json"), report)
            run.manifest["decisions"][name] = {key: case["decision"] for key, case in report["cases"].items()}
            inputs.unlink()
            run.save()
        run.manifest.update(current_phase="finished", decision="HOLD_FOR_REVIEW",
                            cleanup="Source work, compatible cache and evidence retained on private cloud disk; no user source removed")
    except BaseException as error:
        run.manifest["current_phase"] = "failed"
        run.finish(error)
        raise
    run.finish()


def analyze_file(input_path, output_path, name, identity):
    from types import SimpleNamespace
    data = protocol.strict_json_loads(Path(input_path).read_text())
    data["peak"] = {case: {int(block): values for block, values in blocks.items()} for case, blocks in data["peak"].items()}
    fake = SimpleNamespace(directory=Path(output_path).parent,
        manifest={"bundle_id": identity, "spec": {"result_authenticity": "NOT VERIFIED: shared runner OS authority"}})
    confirm_analysis(fake, name, data["groups"], data["peak"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("destination")
    audit_parser = commands.add_parser("audit-decisions")
    audit_parser.add_argument("contract", nargs="?", default=ROOT / "cloud_experiments/core_decisions.json")
    audit_parser.add_argument("--root", default=ROOT)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("source")
    run_parser.add_argument("spec")
    run_parser.add_argument("output")
    analysis_parser = commands.add_parser("analyze")
    for name in ("input", "output", "variant", "identity"):
        analysis_parser.add_argument(name)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        print(json.dumps(prepare(arguments.destination)))
    elif arguments.command == "run":
        execute(arguments.source, arguments.spec, arguments.output)
    elif arguments.command == "analyze":
        analyze_file(arguments.input, arguments.output, arguments.variant, arguments.identity)
    else:
        result = audit_decision_map(arguments.root, protocol.strict_json_loads(
            Path(arguments.contract).read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        raise SystemExit(0 if result["mapping_status"] == "MAPPED" else 2)
