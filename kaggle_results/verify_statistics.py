"""Analyze saved evidence only. Never starts a benchmark or rewrites historical data."""
import argparse
import json
from pathlib import Path

from kaggle_experiments.protocol import (
    PROTOCOL_VERSION, LEGACY_ANALYSIS_POLICY, analyze_measurements, declared_analysis_policy,
    is_digest, load_measurements, quantile, sha256_file,
)

ROOT = Path(__file__).resolve().parent


def combine_reports(paths):
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if (len(reports) != 3 or {report.get("cluster") for report in reports} != {"c1", "c2", "c3"}
            or any(report.get("protocol") != PROTOCOL_VERSION or report.get("legacy_input")
                   or not is_digest(report.get("bundle_id")) or not report.get("cases")
                   for report in reports)
            or len({report.get("bundle_id") for report in reports}) != 1
            or len({report.get("analysis_policy", LEGACY_ANALYSIS_POLICY) for report in reports}) != 1):
        raise ValueError("Combine requires c1/c2/c3 v2 reports from one exact bundle")
    for report in reports:
        inputs = report.get("inputs", {})
        if not inputs or any(not is_digest(digest) or not Path(path).is_file()
                             or sha256_file(path) != digest for path, digest in inputs.items()):
            raise ValueError("Analysis inputs are missing or changed")
        manifest_path, samples_path = report.get("source_manifest"), report.get("source_samples")
        if manifest_path not in inputs or samples_path not in inputs:
            raise ValueError("Missing raw/manifest analysis input identity")
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        cases = load_measurements(samples_path, manifest)
        if (manifest["bundle_id"] != report["bundle_id"] or manifest["cluster"] != report["cluster"]
                or declared_analysis_policy(manifest["spec"]) != report.get("analysis_policy", LEGACY_ANALYSIS_POLICY)
                or manifest["spec"]["run_kind"] != "confirm"
                or report.get("family_size") != manifest["spec"]["simultaneous_family_size"]
                or set(cases) != set(report["cases"])):
            raise ValueError("Analysis does not cover its complete confirmatory experiment")
        for case_id, case in cases.items():
            result = report["cases"][case_id]
            expected_count = case["spec"]["rounds"] * case["spec"]["iterations"]
            if (set(result.get("delta_intervals", {})) != {str(q) for q in case["quantiles"]}
                    or any(result.get(arm, {}).get("samples") != expected_count
                           for arm in ("baseline", "candidate"))):
                raise ValueError("Analysis omitted measurements or quantiles")
    by_cluster = {report["cluster"]: report for report in reports}
    def scoped_decision(clusters):
        decisions = [case["decision"] for cluster in clusters
                     for case in by_cluster[cluster]["cases"].values()]
        if "REJECT" in decisions:
            return "REJECT"
        return "READY_FOR_SCOPED_REVIEW" if all(value == "PASS_PERFORMANCE" for value in decisions) else "HOLD"

    matrix = {
        "cpu_core": {"decision": scoped_decision(("c1", "c2")),
                     "scope": "rank/table/projection/AAE source changes on measured cohorts only"},
        "rng_upgrade": {"decision": scoped_decision(("c3",)),
                        "scope": "exact RNG tuple and Bandit consumer; fresh dependency advisory review still required"},
        "resource_controls": {"decision": "UNIT_SUPPORTED_INTEGRATION_HOLD",
                              "scope": "SingleFlight and Engine unit regressions; no live DNS/TLS/egress proof"},
        "alternative_parser": {"decision": "NOT_RUN", "next": "Implement a bounded scratch comparator before choosing TreeSink/lol_html"},
        "transport_reuse": {"decision": "NOT_RUN", "next": "Supply a DNS/policy-aware pooling candidate and actual socket experiment"},
        "table_html_integration": {"decision": "NOT_RUN", "next": "Grid oracles do not validate headers/extraction integration"},
        "neural_rerank": {"decision": "NOT_RUN", "next": "Labelled corpus and explicit model required"},
    }
    return {"protocol": "athena-research/2", "bundle_id": reports[0]["bundle_id"],
            "analysis_policy": reports[0].get("analysis_policy", LEGACY_ANALYSIS_POLICY),
            "decision_matrix": matrix, "overall_decision": "HOLD",
            "meaning": "Readiness for a bounded core implementation review; never automatic deployment or global-optimum approval",
            "inputs": {str(path): sha256_file(path) for path in paths}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--input", type=Path, help="Raw samples.jsonl")
    operation.add_argument("--combine", nargs=3, type=Path, help="Three analyzed c1/c2/c3 reports")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New output file; must not exist")
    parser.add_argument("--legacy", action="store_true", help="Inspect old data; cannot approve it")
    parser.add_argument("--bootstrap", type=int, default=30000)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output exists; choose a new path to preserve earlier evidence")
    if not args.combine and not args.manifest and not args.legacy:
        parser.error("A v2 manifest is required, or explicitly select --legacy")
    if args.combine:
        report = combine_reports(args.combine)
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
        cases = load_measurements(args.input, manifest)
        report = analyze_measurements(cases, draws=args.bootstrap, legacy=args.legacy)
        report["source_samples"] = str(args.input.resolve())
        report["inputs"] = {str(args.input.resolve()): sha256_file(args.input)}
        if args.manifest:
            report["source_manifest"] = str(args.manifest.resolve())
            report["inputs"][str(args.manifest.resolve())] = sha256_file(args.manifest)
            report["bundle_id"] = manifest["bundle_id"]
            report["cluster"] = manifest["cluster"]
            report["checks"] = manifest["checks"]
            report["coverage"] = manifest.get("coverage", {})
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    for case_id, case in report.get("cases", {}).items():
        print(f"{case_id}: {case['decision']} {case['delta_intervals']} ({case['unit']})")
    print(f"Saved {args.output}; overall deployment decision remains HOLD")


if __name__ == "__main__":
    main()
