"""Fail-closed Linux namespaces for repository-controlled research children."""

import os
from pathlib import Path
import shutil
import sys


POLICY = "linux-unshare-user-mount-pid-net-loopback-kill-child/1"
CARGO_FETCH_POLICY = "linux-unshare-user-mount-pid-net-offline-cargo-fetch/1"
CARGO_FETCH_NETWORK_MODE = "offline-cargo-fetch"
CARGO_FETCH_ARGS = ("fetch", "--offline", "--locked", "--target", "x86_64-unknown-linux-gnu")


def command_argv(command, *, required=True, network_mode="deny"):
    """Wrap a command with private PID containment and an explicit network policy."""
    if type(required) is not bool:
        raise TypeError("Process-isolation selector must be a boolean")
    if network_mode not in ("deny", CARGO_FETCH_NETWORK_MODE):
        raise ValueError("Unknown child network policy")
    argv = [str(value) for value in command]
    if not argv or not argv[0]:
        raise ValueError("Process-isolation command must not be empty")
    if network_mode == CARGO_FETCH_NETWORK_MODE and (
            Path(argv[0]).name.lower() not in ("cargo", "cargo.exe")
            or tuple(argv[1:]) != CARGO_FETCH_ARGS):
        raise ValueError("Offline Cargo-cache validation permits only the exact locked x86_64 Linux fetch")
    if not required:
        if network_mode != "deny":
            raise RuntimeError("Cargo-cache validation requires process containment")
        return argv
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Required process isolation is available only on Linux")
    executable = shutil.which("unshare", path=os.defpath)
    if not executable:
        raise RuntimeError("Required util-linux unshare executable is unavailable")
    executable = str(Path(executable).resolve(strict=True))
    if not os.path.isabs(executable) or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        raise RuntimeError("Required util-linux unshare executable is invalid")
    namespace_args = [executable, "--user", "--map-root-user", "--net",
                      "--pid", "--fork", "--mount-proc", "--kill-child", "--"]
    initializer = str(Path(__file__).resolve(strict=True))
    namespace_args.extend([sys.executable, "-B", initializer, "--athena-isolated-init", "--"])
    return [*namespace_args, *argv]


def _enable_loopback():
    """Bring up only lo inside the new network namespace; never add a route."""
    import fcntl
    import socket
    import struct

    request = struct.pack("16sH22x", b"lo", 0)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags = struct.unpack_from("H", fcntl.ioctl(control, 0x8913, request), 16)[0]
        if not flags & 0x1:
            request = struct.pack("16sH22x", b"lo", flags | 0x1)
            fcntl.ioctl(control, 0x8914, request)
        verified = struct.unpack_from(
            "H", fcntl.ioctl(control, 0x8913, struct.pack("16sH22x", b"lo", 0)), 16
        )[0]
    if not verified & 0x1:
        raise RuntimeError("Could not enable loopback inside the private network namespace")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) < 3 or argv[0] != "--athena-isolated-init" or argv[1] != "--":
        return 2
    _enable_loopback()
    command = argv[2:]
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
