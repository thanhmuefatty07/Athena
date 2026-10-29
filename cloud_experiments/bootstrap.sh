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
run_bounded_apt env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential ca-certificates cloud-guest-utils cmake curl e2fsprogs git nodejs npm openssl pkg-config python3 python3-numpy time unzip util-linux
root_source="$(findmnt -n -o SOURCE / || true)"
if [[ -n "$root_source" && -e "$root_source" ]]; then
    root_source="$(realpath -e -- "$root_source" 2>/dev/null || printf '%s' "$root_source")"
fi
if [[ "$root_source" =~ ^(/dev/[a-z0-9]+n[0-9]+)p([0-9]+)$ ]]; then
    growpart "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" >/dev/null 2>&1 || true
    resize2fs "$root_source" >/dev/null 2>&1 || true
elif [[ "$root_source" =~ ^(/dev/[a-z]+)([0-9]+)$ ]]; then
    growpart "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" >/dev/null 2>&1 || true
    resize2fs "$root_source" >/dev/null 2>&1 || true
fi
if ! id athena-research >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash athena-research
fi
install -d -m 700 -o athena-research -g athena-research /opt/athena/runs
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python3 "$SCRIPT_DIR/rustup_bootstrap.py"
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustup component add --toolchain 1.99.1 rustfmt clippy
python3 --version
python3 -c 'import numpy,sys; assert sys.version_info[:2] == (3, 13), sys.version; print("NumPy", numpy.__version__, "selected by", sys.executable)'
node --version
node -e 'const major = Number(process.versions.node.split(".")[0]); if (!Number.isInteger(major) || major < 20) { throw new Error("Node >= 20 required, got " + process.versions.node); }'
run_bounded_command runuser -u athena-research -- /home/athena-research/.cargo/bin/rustc +1.99.1 -Vv
touch /opt/athena/bootstrap-complete
