"""Create deterministic source-only snapshots. No build, network call, or Kaggle run."""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "8de5709e6c47fd57713ca821131579b2e8c25a36"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def archive_files(files):
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path, content in sorted(files.items()):
                info = tarfile.TarInfo(path)
                info.size, info.mode, info.mtime = len(content), 0o644, 0
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def collect_snapshots():
    original = subprocess.check_output(
        ["git", "archive", "--format=tar", BASELINE, "src", "Cargo.toml",
         "Cargo.lock", "rust-toolchain.toml", "tests", "fixtures"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
        baseline = {member.name: archive.extractfile(member).read()
                    for member in archive if member.isfile()}
    candidate = {}
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml"):
        candidate[name] = (ROOT / name).read_bytes()
    for directory in ("src", "tests", "fixtures"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                candidate[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    controls = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "src/bin/bench_core.rs")
    transformations = {}
    for name in controls:
        transformations[name] = {"original_sha256": digest(baseline[name]) if name in baseline else None,
                                 "controlled_sha256": digest(candidate[name])}
        baseline[name] = candidate[name]
    return baseline, candidate, transformations


def main():
    baseline, candidate, transformations = collect_snapshots()
    destination = Path(__file__).resolve().parent
    metadata = {"protocol": "athena-research/2", "baseline_commit": BASELINE,
                "candidate_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "comparison": "source-only; identical dependency graph, toolchain, release profile and benchmark harness",
                "baseline_controls": transformations}
    for label, files in (("baseline", baseline), ("candidate", candidate)):
        raw = archive_files(files)
        metadata[label + "_tar_bytes"] = len(raw)
        metadata[label + "_tar_sha256"] = digest(raw)
        metadata[label + "_files"] = {path: digest(content) for path, content in sorted(files.items())}
        (destination / (label + ".b64")).write_text(base64.b64encode(raw).decode(), encoding="ascii")
    (destination / "snapshots_meta.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print("Prepared deterministic snapshots only; no benchmark or Kaggle operation.")


if __name__ == "__main__":
    main()
