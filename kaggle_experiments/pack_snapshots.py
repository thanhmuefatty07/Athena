"""Create deterministic source-only snapshots. No build, network call, or Kaggle run."""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import stat
import tarfile

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "8de5709e6c47fd57713ca821131579b2e8c25a36"
COMPARISON = "source-only; identical dependency graph, toolchain, release profile and benchmark harness"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def checked_repo_path(root, relative):
    """Return a repo-relative path only when resolution stays inside and follows no links."""
    root = Path(root).resolve(strict=True)
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Repository path is not a contained relative path")
    path = root / relative
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("Repository path cannot be resolved safely") from error
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Repository path escapes the checkout")

    current = path
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while current != root:
        try:
            info = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError("Repository path cannot be inspected safely") from error
        else:
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                raise ValueError("Repository path contains a link or reparse point")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError("Repository path contains a hard-linked file")
        current = current.parent
    return path


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
        candidate[name] = checked_repo_path(ROOT, name).read_bytes()
    probe_source = "cloud_experiments/core_probe.rs"
    candidate[probe_source] = checked_repo_path(ROOT, probe_source).read_bytes()
    for directory in ("src", "tests", "fixtures"):
        source_root = checked_repo_path(ROOT, directory)
        for path in sorted(source_root.rglob("*")):
            relative = path.relative_to(ROOT)
            if "__pycache__" in relative.parts:
                continue
            safe_path = checked_repo_path(ROOT, relative)
            if safe_path.is_file():
                candidate[relative.as_posix()] = safe_path.read_bytes()
    controls = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "src/bin/bench_core.rs",
                "cloud_experiments/core_probe.rs")
    transformations = {}
    for name in controls:
        transformations[name] = {"original_sha256": digest(baseline[name]) if name in baseline else None,
                                 "controlled_sha256": digest(candidate[name])}
        baseline[name] = candidate[name]
    return baseline, candidate, transformations


def current_commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def build_snapshot_artifacts(baseline, candidate, transformations, candidate_commit):
    metadata = {"protocol": "athena-research/2", "baseline_commit": BASELINE,
                "candidate_commit": candidate_commit, "comparison": COMPARISON,
                "baseline_controls": transformations}
    archives = {}
    for label, files in (("baseline", baseline), ("candidate", candidate)):
        raw = archive_files(files)
        metadata[label + "_tar_bytes"] = len(raw)
        metadata[label + "_tar_sha256"] = digest(raw)
        metadata[label + "_files"] = {path: digest(content) for path, content in sorted(files.items())}
        archives[label] = raw
    return metadata, archives


def verify_current_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Snapshot metadata is not an object; regenerate source snapshots")
    baseline, candidate, transformations = collect_snapshots()
    expected, _archives = build_snapshot_artifacts(
        baseline, candidate, transformations, current_commit())
    if metadata != expected:
        changed = sorted(key for key in set(metadata) | set(expected)
                         if metadata.get(key) != expected.get(key))
        raise ValueError("Snapshot source inventory is stale (changed fields: "
                         + ", ".join(changed) + "); regenerate snapshots before notebook generation")


def main():
    baseline, candidate, transformations = collect_snapshots()
    metadata, archives = build_snapshot_artifacts(baseline, candidate, transformations, current_commit())
    output_paths = {label: checked_repo_path(ROOT, "kaggle_experiments/" + label + ".b64")
                    for label in archives}
    metadata_path = checked_repo_path(ROOT, "kaggle_experiments/snapshots_meta.json")
    for label, raw in archives.items():
        output_paths[label].write_text(base64.b64encode(raw).decode(), encoding="ascii")
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print("Prepared deterministic snapshots only; no benchmark or Kaggle operation.")


if __name__ == "__main__":
    main()
