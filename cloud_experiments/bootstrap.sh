#!/bin/bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
install -d -m 755 /opt/athena
APT_TIMEOUT_SECONDS=1800
run_bounded_apt() {
    timeout --kill-after=30s "$APT_TIMEOUT_SECONDS" "$@"
}
COMMAND_TIMEOUT_SECONDS=1800
run_bounded_command() {
    timeout --kill-after=30s "$COMMAND_TIMEOUT_SECONDS" "$@"
}
run_bounded_apt apt-get update
run_bounded_apt env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential ca-certificates curl git openssl pkg-config python3 python3-numpy time util-linux

# The native runner deliberately binds scientific evidence to Python 3.13.
# Select the exact interpreter before installing Rust so a Debian/Ubuntu image
# with a different `python3` default fails immediately instead of producing a
# partially prepared VM that can never execute the frozen study.
PYTHON_BIN=""
for candidate in python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo 'Athena requires Python 3.13; select the Debian 13 image or install python3.13 before bootstrap.' >&2
    exit 42
fi
"$PYTHON_BIN" -c 'import numpy'

if ! id athena-research >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash athena-research
fi
install -d -m 700 -o athena-research -g athena-research /opt/athena/runs
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
"$PYTHON_BIN" "$SCRIPT_DIR/rustup_bootstrap.py"
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustup component add --toolchain 1.99.1 rustfmt clippy
"$PYTHON_BIN" --version
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustc +1.99.1 -Vv
touch /opt/athena/bootstrap-complete
