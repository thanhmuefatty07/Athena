"""Download user-started Kaggle runs into a fresh directory; never starts a run."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
CLUSTERS = {
    "c1": "athena-c1-core-stages-20261015",
    "c2": "athena-c2-dom-rss-20261015",
    "c3": "athena-c3-deps-concurrency-20261015",
}


def normalized_status(response):
    value = response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
    value = getattr(value, "name", value)
    return str(value).rsplit(".", 1)[-1].upper()


def pull_kernel_output(kernel_id, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(["kaggle", "kernels", "output", kernel_id, "-p", str(destination)],
                   check=True, timeout=600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True, help="Must be a new directory")
    parser.add_argument("--diagnostics", action="store_true", help="Also collect ERROR/CANCELLED runs, without approving them")
    args = parser.parse_args(argv)
    if args.destination.exists():
        parser.error("Destination exists; never mix old and new raw evidence")
    bundle = json.loads((ROOT / "kaggle_experiments/bundle.json").read_text(encoding="utf-8"))
    kernels = {cluster: json.loads((ROOT / "kaggle_experiments" / directory / "kernel-metadata.json")
                                   .read_text(encoding="utf-8"))["id"]
               for cluster, directory in CLUSTERS.items()}
    import kaggle
    statuses = {cluster: normalized_status(kaggle.api.kernels_status(kernel))
                for cluster, kernel in kernels.items()}
    allowed = {"COMPLETE", "ERROR", "CANCELLED"} if args.diagnostics else {"COMPLETE"}
    if any(status not in allowed for status in statuses.values()):
        parser.error("Runs are not all ready: " + json.dumps(statuses))
    args.destination.mkdir(parents=True, exist_ok=False)
    receipt = {"expected_bundle_id": bundle["bundle_id"], "kernels": kernels, "statuses": statuses, "manifests": {}}
    try:
        for cluster, kernel in kernels.items():
            folder = args.destination / cluster
            pull_kernel_output(kernel, folder)
            manifests = list(folder.rglob("manifest.json"))
            if len(manifests) != 1:
                raise ValueError("Expected one v2 manifest in " + str(folder))
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            if (manifest.get("bundle_id") != bundle["bundle_id"]
                    or manifest.get("protocol") != "athena-research/2" or manifest.get("cluster") != cluster):
                raise ValueError("Downloaded source/spec bundle is stale or different: " + cluster)
            if statuses[cluster] == "COMPLETE" and manifest.get("status") != "COMPLETE":
                raise ValueError("Kaggle status does not match evidence status: " + cluster)
            receipt["manifests"][cluster] = str(manifests[0])
            print(cluster, statuses[cluster], manifests[0])
    finally:
        (args.destination / "pull_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if any(status != "COMPLETE" for status in statuses.values()):
        raise SystemExit("Diagnostic evidence saved; failed runs cannot be accepted")


if __name__ == "__main__":
    main()
