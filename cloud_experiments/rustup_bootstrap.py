"""Install the pinned rustup-init binary for the Linux research VM."""

import hashlib
import hmac
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
from kaggle_experiments.runner import (
    MAX_RUSTUP_INSTALLER_BYTES, RUSTUP_DOWNLOAD_TIMEOUT_SECONDS,
    RUSTUP_DOWNLOAD_WORKER_SOURCE, RUSTUP_INSTALLER_CHUNK_BYTES,
    run_bounded_process,
)


RUSTUP_VERSION = "1.29.1"
RUST_TOOLCHAIN = "1.99.1"
MAX_INSTALLER_BYTES = MAX_RUSTUP_INSTALLER_BYTES
_CHUNK_BYTES = RUSTUP_INSTALLER_CHUNK_BYTES
INSTALL_TIMEOUT_SECONDS = 1800
INSTALL_OUTPUT_LIMIT_BYTES = 2 * 1024 ** 2
DOWNLOAD_TIMEOUT_SECONDS = RUSTUP_DOWNLOAD_TIMEOUT_SECONDS
PINNED_SHA256 = {
    "x86_64-unknown-linux-gnu": "dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71",
    "aarch64-unknown-linux-gnu": "15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433",
}
_ARCHITECTURE_TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}


def target_for_architecture(machine):
    try:
        return _ARCHITECTURE_TARGETS[machine.lower()]
    except (AttributeError, KeyError):
        raise ValueError(f"Unsupported Linux architecture for rustup-init: {machine!r}") from None


def install_rustup(target=None, scratch_root="/opt/athena", *, machine=None):
    if target is None:
        target = target_for_architecture(machine or platform.machine())
    try:
        expected_digest = PINNED_SHA256[target]
    except KeyError:
        raise ValueError(f"Unsupported rustup-init target: {target!r}") from None
    if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
        raise ValueError("Pinned rustup-init checksum is malformed")

    url = ("https://static.rust-lang.org/rustup/archive/"
           f"{RUSTUP_VERSION}/{target}/rustup-init")
    scratch_root = Path(scratch_root)
    with tempfile.TemporaryDirectory(prefix="rustup-init-", dir=scratch_root) as directory:
        os.chmod(directory, 0o755)
        installer = Path(directory) / "rustup-init"
        download_worker = Path(directory) / "download-rustup-installer.py"
        download_worker.write_text(RUSTUP_DOWNLOAD_WORKER_SOURCE, encoding="utf-8")
        download_command = [sys.executable, str(download_worker), url, str(installer),
                            str(MAX_INSTALLER_BYTES)]
        download = run_bounded_process(
            download_command,
            timeout=DOWNLOAD_TIMEOUT_SECONDS, output_limit=INSTALL_OUTPUT_LIMIT_BYTES)
        if download.status == "TIMEOUT":
            raise TimeoutError("Rustup installer download exceeded 10 minutes")
        if download.status == "OUTPUT_LIMIT":
            raise RuntimeError("Rustup installer download exceeded the combined output cap")
        if download.status != "EXITED":
            raise RuntimeError("Rustup download process control failed: " + download.status)
        if download.returncode != 0:
            raise subprocess.CalledProcessError(download.returncode, download_command,
                output=download.stdout, stderr=download.stderr)

        hasher = hashlib.sha256()
        total_bytes = 0
        with installer.open("rb") as source:
            while True:
                chunk = source.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_INSTALLER_BYTES:
                    raise ValueError("Rustup installer exceeds the configured byte limit")
                hasher.update(chunk)

        actual_digest = hasher.hexdigest()
        if total_bytes == 0 or not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError("Rustup installer checksum mismatch")

        os.chmod(installer, 0o755)
        command = [
            "runuser", "-u", "athena-research", "--", str(installer),
            "-y", "--profile", "minimal", "--default-toolchain", RUST_TOOLCHAIN,
            "--no-modify-path",
        ]
        print("Installing the pinned Rust toolchain (maximum 30 minutes).", flush=True)
        result = run_bounded_process(command, timeout=INSTALL_TIMEOUT_SECONDS,
                                     output_limit=INSTALL_OUTPUT_LIMIT_BYTES)
        if result.stdout:
            sys.stdout.write(result.stdout.decode("utf-8", errors="replace"))
            sys.stdout.flush()
        if result.stderr:
            sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
            sys.stderr.flush()
        if result.status == "TIMEOUT":
            raise TimeoutError("Pinned rustup installation exceeded 30 minutes")
        if result.status == "OUTPUT_LIMIT":
            raise RuntimeError("Pinned rustup installation exceeded the combined output cap")
        if result.status != "EXITED":
            raise RuntimeError("Pinned rustup process control failed: " + result.status)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command)


def main():
    try:
        install_rustup()
    except Exception as error:
        print(f"Pinned rustup installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
