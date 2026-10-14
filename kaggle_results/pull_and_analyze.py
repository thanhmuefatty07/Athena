"""Pull outputs from Kaggle benchmark kernels and compile synthesis report.
"""
import os
import sys
import json
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "kaggle_results"

KERNELS = {
    "c1": "vnthanhphm/athena-c1-core-stages-20261015",
    "c2": "vnthanhphm/athena-c2-dom-rss-20261015",
    "c3": "vnthanhphm/athena-c3-deps-concurrency-20261015"
}

def check_status(kernel_id: str) -> dict:
    import kaggle
    res = kaggle.api.kernels_status(kernel_id)
    return res

def pull_kernel_output(kernel_id: str, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["kaggle", "kernels", "output", kernel_id, "-p", str(dest_dir)]
    print(f"Pulling output for {kernel_id} to {dest_dir}...")
    p = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Pull result: {p.stdout.strip()}")
    if p.stderr:
        print(f"Stderr: {p.stderr.strip()}")

def main():
    print("Checking Kaggle kernel statuses...")
    statuses = {}
    for k_key, k_id in KERNELS.items():
        st = check_status(k_id)
        statuses[k_key] = st
        print(f"[{k_key}] {k_id}: {st}")
        
    for k_key, k_id in KERNELS.items():
        st_obj = statuses[k_key]
        st_val = getattr(st_obj, "status", None) or (st_obj.get("status") if isinstance(st_obj, dict) else str(st_obj))
        if st_val == "COMPLETE":
            target = OUT_DIR / k_key
            pull_kernel_output(k_id, target)
        elif st_val == "ERROR":
            print(f"WARNING: {k_id} failed with ERROR! Pulling logs for diagnosis...")
            target = OUT_DIR / k_key
            pull_kernel_output(k_id, target)

if __name__ == "__main__":
    main()
