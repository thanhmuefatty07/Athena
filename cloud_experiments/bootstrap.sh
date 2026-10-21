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
if ! id athena-research >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash athena-research
fi
install -d -m 700 -o athena-research -g athena-research /opt/athena/runs
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python3 "$SCRIPT_DIR/rustup_bootstrap.py"
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustup component add --toolchain 1.99.1 rustfmt clippy
python3 --version
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustc +1.99.1 -Vv
touch /opt/athena/bootstrap-complete
