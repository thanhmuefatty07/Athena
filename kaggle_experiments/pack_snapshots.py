"""Create deterministic source-only snapshots. No build, network call, or Kaggle run."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "8de5709e6c47fd57713ca821131579b2e8c25a36"
GIT_COMMAND_TIMEOUT_SECONDS = 30.0
GIT_ARCHIVE_OUTPUT_BYTES = 16 * 1024 ** 2
GIT_SOURCE_OUTPUT_BYTES = 8 * 1024 ** 2
GIT_METADATA_OUTPUT_BYTES = 4096


def digest(value):
    return hashlib.sha256(value).hexdigest()


def bounded_command_output(command, *, cwd, timeout_s=GIT_COMMAND_TIMEOUT_SECONDS,
                           max_bytes=GIT_ARCHIVE_OUTPUT_BYTES):
    """Run a local preparation command with bounded stdout/stderr and lifetime."""
    if type(timeout_s) not in (int, float) or not 0 < timeout_s <= GIT_COMMAND_TIMEOUT_SECONDS:
        raise ValueError("Preparation command timeout is outside its bound")
    if type(max_bytes) is not int or not 0 < max_bytes <= GIT_ARCHIVE_OUTPUT_BYTES:
        raise ValueError("Preparation command output bound is outside its limit")
    process_kwargs = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "posix":
        process_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **process_kwargs)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, buffer):
        while True:
            chunk = stream.read(min(64 * 1024, max_bytes + 1))
            if not chunk:
                return
            if len(buffer) + len(chunk) > max_bytes:
                overflow.set()
                return
            buffer.extend(chunk)

    readers = [threading.Thread(target=drain, args=(stream, buffer), daemon=True)
               for stream, buffer in zip((process.stdout, process.stderr), buffers)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_s
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=1)
    for stream in (process.stdout, process.stderr):
        stream.close()
    if overflow.is_set():
        raise ValueError("Preparation command output exceeds its byte bound")
    if timed_out:
        raise TimeoutError("Preparation command exceeded its time bound")
    stdout, stderr = (bytes(buffer) for buffer in buffers)
    if returncode:
        raise subprocess.CalledProcessError(returncode, command, stdout, stderr)
    return stdout


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
    original = bounded_command_output(
        ["git", "archive", "--format=tar", BASELINE, "src", "Cargo.toml",
         "Cargo.lock", "rust-toolchain.toml", "tests", "fixtures"], cwd=ROOT,
        max_bytes=GIT_ARCHIVE_OUTPUT_BYTES)
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
                "candidate_commit": bounded_command_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT,
                    max_bytes=GIT_METADATA_OUTPUT_BYTES).decode("ascii").strip(),
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
