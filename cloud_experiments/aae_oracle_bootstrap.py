"""Install the pinned Python BLAKE3 oracle outside the measured run."""

import hashlib
import hmac
import os
import platform
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
from kaggle_experiments.runner import run_bounded_process


PYTHON_ABI = "cp313-cp313"
ORACLE_VERSION = "1.0.8"
MAX_WHEEL_BYTES = 8 * 1024 ** 2
DOWNLOAD_TIMEOUT_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 300
INSTALL_OUTPUT_LIMIT_BYTES = 2 * 1024 ** 2
_CHUNK_BYTES = 64 * 1024

PINNED_WHEELS = {
    "x86_64": {
        "filename": "blake3-1.0.8-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/21/ae/6e55c19c8460fada86cd1306a390a09b0c5a2e2e424f9317d2edacea439f/blake3-1.0.8-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "sha256": "e4072196547484c95a5a09adbb952e9bb501949f03f9e2a85e7249ef85faaba8",
    },
    "aarch64": {
        "filename": "blake3-1.0.8-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
        "url": "https://files.pythonhosted.org/packages/77/57/e8a85fa261894bf7ce7af928ff3408aab60287ab8d58b55d13a3f700b619/blake3-1.0.8-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
        "sha256": "19fc6f2b7edab8acff6895fc6e38c19bd79f4c089e21153020c75dfc7397d52d",
    },
}


def wheel_for_machine(machine=None):
    key = (machine or platform.machine()).lower()
    try:
        return PINNED_WHEELS[key]
    except KeyError:
        raise ValueError(f"Unsupported Linux architecture for Python BLAKE3 oracle: {key!r}") from None


def _verify_status(result, label):
    if result.status == "TIMEOUT":
        raise TimeoutError(f"Pinned BLAKE3 {label} exceeded its time limit")
    if result.status == "OUTPUT_LIMIT":
        raise RuntimeError(f"Pinned BLAKE3 {label} exceeded its output limit")
    if result.status != "EXITED":
        raise RuntimeError(f"Pinned BLAKE3 {label} process control failed: {result.status}")
    if result.returncode != 0:
        raise RuntimeError(f"Pinned BLAKE3 {label} failed with exit code {result.returncode}")


def install_oracle(python, target_dir="/opt/athena/python-site", scratch_root="/opt/athena", *, machine=None):
    if not isinstance(python, (str, os.PathLike)) or not str(python):
        raise ValueError("An explicit Python interpreter is required")
    wheel = wheel_for_machine(machine)
    target_dir = Path(target_dir)
    scratch_root = Path(scratch_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blake3-wheel-", dir=scratch_root) as directory:
        wheel_path = Path(directory) / wheel["filename"]
        hasher = hashlib.sha256()
        total = 0
        with urlopen(wheel["url"], timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, wheel_path.open("xb") as output:
            final_url = getattr(response, "geturl", lambda: wheel["url"])()
            parsed = urlsplit(final_url)
            if parsed.scheme.lower() != "https" or parsed.hostname != "files.pythonhosted.org":
                raise ValueError("Python BLAKE3 wheel redirected away from its pinned HTTPS host")
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ValueError("Python BLAKE3 wheel response was not binary")
                total += len(chunk)
                if total > MAX_WHEEL_BYTES:
                    raise ValueError("Python BLAKE3 wheel exceeds the configured byte limit")
                hasher.update(chunk)
                output.write(chunk)
        if total == 0 or not hmac.compare_digest(hasher.hexdigest(), wheel["sha256"]):
            raise ValueError("Python BLAKE3 wheel checksum mismatch")

        install = run_bounded_process([
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--no-deps", "--no-index", "--target", str(target_dir), str(wheel_path),
        ], timeout=INSTALL_TIMEOUT_SECONDS, output_limit=INSTALL_OUTPUT_LIMIT_BYTES)
        _verify_status(install, "wheel installation")

        oracle_environment = {
            "PYTHONPATH": str(target_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        check = run_bounded_process([
            str(python), "-c", "from blake3 import blake3; assert blake3(b'').hexdigest() == "
            "'af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262'",
        ], timeout=30, output_limit=64 * 1024, env=oracle_environment,
        )
        _verify_status(check, "oracle verification")
    return {"version": ORACLE_VERSION, "python_abi": PYTHON_ABI,
            "wheel": wheel["filename"], "sha256": wheel["sha256"],
            "target_dir": str(target_dir)}


if __name__ == "__main__":
    raise SystemExit("Use install_oracle from the bounded bootstrap; no implicit installation is allowed")
