"""Prepare self-contained Kaggle scripts only; never uploads or starts a kernel."""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path

if __package__:
    from .protocol import ANALYSIS_POLICY, C1_STAGES, STUDY_REVISION, strict_json_loads
    from .runner import MAX_CAPTURED_COMMAND_OUTPUT_BYTES
    from . import pack_snapshots
else:
    from protocol import ANALYSIS_POLICY, C1_STAGES, STUDY_REVISION, strict_json_loads
    from runner import MAX_CAPTURED_COMMAND_OUTPUT_BYTES
    import pack_snapshots

ROOT = Path(__file__).resolve().parent
CLUSTERS = {
    "c1": "athena-c1-core-stages-20261015",
    "c2": "athena-c2-dom-rss-20261015",
    "c3": "athena-c3-deps-concurrency-20261015",
}


def case(identity, stage, size, rounds, iterations, unit="ms", quantiles=(0.5, 0.95, 0.99), **extra):
    return {"id": identity, "stage": stage, "size": size, "rounds": rounds,
            "iterations": iterations, "unit": unit, "quantiles": list(quantiles), **extra}


def specifications(screen=False):
    rounds, iterations = (2, 5) if screen else (20, 250)
    c1 = [case(stage, stage, 5000 if stage == "table" else 1000, rounds, iterations)
          for stage in C1_STAGES]
    c2 = []
    for anchors in (100, 500, 1000, 2500, 5000, 10000, 20000):
        c2.extend([
            case(f"links-{anchors}", "fixture", anchors, rounds, iterations),
            case(f"rss-{anchors}", "rss", anchors, rounds, 1, "KiB", (0.5,)),
        ])
    shapes = ((1, 1), (2, 5), (5, 2), (0.5, 0.5), (1000, 1000), (1, 100), (100, 1), (1000000, 1000000))
    c3 = [case(f"rng-{a}-{b}", "rng", 1000 if screen else 100000, rounds, 1,
               "ns/draw", (0.5,), alpha=a, beta=b) for a, b in shapes]
    family = sum(len(item["quantiles"]) for cases in (c1, c2, c3) for item in cases)
    return {name: {"run_kind": "screen" if screen else "confirm", "simultaneous_family_size": family,
                   "study_revision": STUDY_REVISION, "analysis_policy": ANALYSIS_POLICY,
                   "limits": {"command_output_bytes": MAX_CAPTURED_COMMAND_OUTPUT_BYTES},
                   "cases": cases, "drift_threshold": 0.5, "family_alpha": 0.05,
                   "wall_budget_seconds": 5400}
            for name, cases in (("c1", c1), ("c2", c2), ("c3", c3))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="vnthanhphm")
    parser.add_argument("--screen", action="store_true", help="Small smoke experiment; never acceptance evidence")
    args = parser.parse_args(argv)
    if not args.owner or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in args.owner):
        parser.error("Invalid Kaggle owner")
    input_paths = {name: pack_snapshots.checked_repo_path(ROOT, name)
                   for name in ("snapshots_meta.json", "baseline.b64", "candidate.b64",
                                "probe.rs", "protocol.py", "runner.py")}
    output_paths = {}
    for cluster, directory in CLUSTERS.items():
        output_paths[cluster] = (
            pack_snapshots.checked_repo_path(ROOT, directory),
            pack_snapshots.checked_repo_path(ROOT, Path(directory) / "main.py"),
            pack_snapshots.checked_repo_path(ROOT, Path(directory) / "kernel-metadata.json"),
        )
    bundle_path = pack_snapshots.checked_repo_path(ROOT, "bundle.json")

    metadata = strict_json_loads(input_paths["snapshots_meta.json"].read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        parser.error("Snapshot metadata must be a JSON object")
    if metadata.get("protocol") != "athena-research/2":
        parser.error("Prepare v2 snapshots first: python -B -m kaggle_experiments.pack_snapshots")
    try:
        pack_snapshots.verify_current_metadata(metadata)
    except ValueError as exc:
        parser.error(str(exc))
    payload = {"metadata": metadata,
               "probe": base64.b64encode(input_paths["probe.rs"].read_bytes()).decode()}
    for arm in ("baseline", "candidate"):
        encoded = input_paths[arm + ".b64"].read_text(encoding="ascii").strip()
        raw = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(raw).hexdigest() != metadata[arm + "_tar_sha256"]:
            parser.error("Snapshot hash mismatch: " + arm)
        payload[arm] = encoded
    protocol = input_paths["protocol.py"].read_text(encoding="utf-8")
    runner = input_paths["runner.py"].read_text(encoding="utf-8")
    specs = specifications(args.screen)
    identity = {"metadata": metadata, "specifications": specs,
                "protocol_sha256": hashlib.sha256(protocol.encode()).hexdigest(),
                "runner_sha256": hashlib.sha256(runner.encode()).hexdigest(),
                "probe_sha256": hashlib.sha256(base64.b64decode(payload["probe"])).hexdigest()}
    bundle_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for cluster, directory in CLUSTERS.items():
        destination, main_path, metadata_path = output_paths[cluster]
        destination.mkdir(exist_ok=True)
        code = ("# Generated by kaggle_experiments.generate_clusters; edit protocol.py/runner.py/probe.rs instead.\n"
                + protocol + "\n\n" + runner + "\n\nPAYLOAD = " + repr(payload)
                + "\nSPEC = " + repr(specs[cluster])
                + "\nBUNDLE_ID = " + repr(bundle_id)
                + "\nif __name__ == '__main__':\n"
                + f"    execute_cluster({cluster!r}, PAYLOAD, SPEC, BUNDLE_ID)\n")
        ast.parse(code)
        main_path.write_text(code, encoding="utf-8")
        suffix = "-v21-screen" if args.screen else "-v21"
        kernel_metadata = {"id": f"{args.owner}/{directory}{suffix}",
                           "title": (directory + suffix).replace("-", " "),
                           "code_file": "main.py", "language": "python", "kernel_type": "script",
                           "is_private": True, "enable_gpu": False, "enable_internet": True,
                           "dataset_sources": [], "competition_sources": [], "kernel_sources": []}
        metadata_path.write_text(json.dumps(kernel_metadata, indent=2) + "\n", encoding="utf-8")
    bundle_path.write_text(json.dumps({"bundle_id": bundle_id, **identity}, indent=2) + "\n",
                            encoding="utf-8")
    print("Prepared", bundle_id, "; no Kaggle upload/run was performed.")


if __name__ == "__main__":
    main()
