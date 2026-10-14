"""Pack baseline (8de5709) and candidate (working tree) snapshots into base64 tarballs.
Computes SHA-256 for all constituent source files for audit provenance.
"""
import os
import io
import json
import base64
import tarfile
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def make_baseline_tar() -> tuple[bytes, dict]:
    # Extract git commit 8de5709
    raw_tar = subprocess.check_output(
        ["git", "archive", "--format=tar", "8de5709", "src", "Cargo.toml", "Cargo.lock", "tests", "fixtures"],
        cwd=ROOT
    )
    # Recompress with gzip
    gz_out = io.BytesIO()
    with tarfile.open(fileobj=gz_out, mode="w:gz") as gz:
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as src_tar:
            for member in src_tar.getmembers():
                f = src_tar.extractfile(member)
                gz.addfile(member, f)
    
    # Calculate file hashes from commit tree
    file_hashes = {}
    ls_tree = subprocess.check_output(["git", "ls-tree", "-r", "8de5709"], cwd=ROOT, text=True)
    for line in ls_tree.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "blob":
            file_hashes[parts[3]] = parts[2]
            
    return gz_out.getvalue(), file_hashes

def make_candidate_tar() -> tuple[bytes, dict]:
    gz_out = io.BytesIO()
    file_hashes = {}
    included_dirs = ["src", "tests", "fixtures"]
    included_files = ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml"]
    
    with tarfile.open(fileobj=gz_out, mode="w:gz") as gz:
        for f_name in included_files:
            p = ROOT / f_name
            if p.is_file():
                gz.add(p, arcname=f_name)
                file_hashes[f_name] = hash_file(p)
        for d_name in included_dirs:
            p = ROOT / d_name
            if p.is_dir():
                for item in p.rglob("*"):
                    if item.is_file() and not item.name.endswith(".pyc") and "__pycache__" not in item.parts:
                        rel = item.relative_to(ROOT).as_posix()
                        gz.add(item, arcname=rel)
                        file_hashes[rel] = hash_file(item)
                        
    return gz_out.getvalue(), file_hashes

def main():
    base_bytes, base_hashes = make_baseline_tar()
    cand_bytes, cand_hashes = make_candidate_tar()
    
    base_b64 = base64.b64encode(base_bytes).decode("ascii")
    cand_b64 = base64.b64encode(cand_bytes).decode("ascii")
    
    out_dir = Path(__file__).resolve().parent
    meta = {
        "baseline_commit": "8de5709e6c47fd57713ca821131579b2e8c25a36",
        "baseline_tar_bytes": len(base_bytes),
        "baseline_tar_sha256": hashlib.sha256(base_bytes).hexdigest(),
        "baseline_files": base_hashes,
        "candidate_tar_bytes": len(cand_bytes),
        "candidate_tar_sha256": hashlib.sha256(cand_bytes).hexdigest(),
        "candidate_files": cand_hashes,
    }
    
    (out_dir / "snapshots_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out_dir / "baseline.b64").write_text(base_b64, encoding="ascii")
    (out_dir / "candidate.b64").write_text(cand_b64, encoding="ascii")
    
    print(f"Baseline: {len(base_bytes)} bytes gz, SHA256={meta['baseline_tar_sha256'][:16]}...")
    print(f"Candidate: {len(cand_bytes)} bytes gz, SHA256={meta['candidate_tar_sha256'][:16]}...")
    print("Snapshots packaged successfully!")

if __name__ == "__main__":
    main()
