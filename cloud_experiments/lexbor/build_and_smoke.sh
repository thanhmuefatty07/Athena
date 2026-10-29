#!/usr/bin/env bash
set -euo pipefail

commit=2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe
archive_sha256=2ac0d4234a305a6e992a6a5d2bb0ebb468be93a22e5631e334f7da8c127ba282
archive_url="https://codeload.github.com/lexbor/lexbor/zip/${commit}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
if [[ $# == 0 && -n "${ATHENA_LEXBOR_WORK_ROOT:-}" ]]; then
    raw_scratch="$ATHENA_LEXBOR_WORK_ROOT"
elif [[ $# == 1 ]]; then
    raw_scratch="$1"
else
    fail 'usage: build_and_smoke.sh ABSOLUTE_FRESH_SCRATCH_ROOT'
fi
[[ "$raw_scratch" == /* ]] || fail 'scratch root must be an absolute path'
command -v realpath >/dev/null || fail 'realpath is required'
scratch="$(realpath -m -- "$raw_scratch")"
case "${scratch}/" in
    "${repo_root}/"*) fail 'scratch root must be outside the repository' ;;
esac
[[ ! -e "$scratch" && ! -L "$scratch" ]] || fail 'scratch root must not already exist'
for tool in cargo cmake cc sha256sum timeout unzip; do
    command -v "$tool" >/dev/null || fail "required tool not found: $tool"
done
rustc +1.99.1 --version >/dev/null 2>&1 || fail 'Rust 1.99.1 must be installed before this run'
cargo +1.99.1 --version >/dev/null 2>&1 || fail 'Cargo 1.99.1 must be installed before this run'

scratch_parent="$(dirname -- "$scratch")"
[[ -d "$scratch_parent" ]] || fail 'scratch parent must already exist'
free_kib="$(df -Pk -- "$scratch_parent" | awk 'NR==2 {print $4}')"
[[ "$free_kib" =~ ^[0-9]+$ ]] || fail 'cannot measure scratch capacity'
(( free_kib >= 8388608 )) || fail 'need at least 8 GiB free for the pinned native and Rust adapter build'

mkdir -- "$scratch"
archive="$scratch/lexbor-${commit}.zip"
source="$scratch/lexbor-${commit}"
build="$scratch/build"
logs="$scratch/logs"
mkdir -- "$logs"

archive_cache="${ATHENA_LEXBOR_ARCHIVE_CACHE:-}"
if [[ -n "$archive_cache" && -f "$archive_cache" && ! -L "$archive_cache" ]]; then
    cached_sha256="$(sha256sum "$archive_cache" | awk '{print $1}')"
    if [[ "$cached_sha256" == "$archive_sha256" ]]; then
        cp -- "$archive_cache" "$archive"
    fi
fi
if [[ ! -f "$archive" ]]; then
    [[ "${ATHENA_OFFLINE:-0}" != "1" ]] || fail 'offline mode requires a pre-verified ATHENA_LEXBOR_ARCHIVE_CACHE'
    command -v curl >/dev/null || fail 'required tool not found: curl'
    curl --fail --location --silent --show-error --max-time 120 --max-filesize 16777216 \
        "$archive_url" --output "$archive"
fi
actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$actual_sha256" == "$archive_sha256" ]] || fail 'pinned Lexbor archive SHA-256 mismatch'
if [[ -n "$archive_cache" && ! -e "$archive_cache" && ! -L "$archive_cache" ]]; then
    cp -- "$archive" "${archive_cache}.tmp.$$"
    mv -f -- "${archive_cache}.tmp.$$" "$archive_cache"
fi
unzip -q "$archive" -d "$scratch"
[[ -f "$source/CMakeLists.txt" ]] || fail 'pinned ZIP did not extract the expected source tree'

run_bounded() {
    local label="$1"; shift
    local log="$logs/${label}.log"
    if timeout --signal=TERM --kill-after=30s 900s "$@" >"$log" 2>&1; then
        return 0
    else
        local status=$?
        printf 'FAILED: %s (exit %s); log: %s\n' "$label" "$status" "$log" >&2
        tail -n 80 "$log" >&2 || true
        exit "$status"
    fi
}

run_bounded configure cmake -S "$source" -B "$build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DLEXBOR_BUILD_SHARED=OFF -DLEXBOR_BUILD_STATIC=ON \
    -DLEXBOR_BUILD_TESTS=OFF -DLEXBOR_BUILD_EXAMPLES=OFF \
    -DLEXBOR_BUILD_UTILS=OFF -DLEXBOR_BUILD_BENCHMARKS=OFF \
    -DLEXBOR_BUILD_WITH_ASAN=OFF -DLEXBOR_BUILD_SEPARATELY=OFF
run_bounded build cmake --build "$build" --config Release --target lexbor_static --parallel 2

mapfile -t libraries < <(find "$build" -type f -name 'liblexbor_static.a' -print)
[[ ${#libraries[@]} == 1 ]] || fail "expected one liblexbor_static.a, found ${#libraries[@]}"
run_bounded canary-build cc -std=c11 -O2 -Wall -Wextra -Werror -DLEXBOR_STATIC \
    -I "$source/source" -I "$build" "$script_dir/canary.c" "${libraries[0]}" \
    -lm -ldl -pthread -o "$scratch/lexbor-canary"
run_bounded canary-run "$scratch/lexbor-canary"

library_dir="$(dirname -- "${libraries[0]}")"
cargo_offline_args=()
if [[ "${ATHENA_OFFLINE:-0}" == "1" ]] || cargo +1.99.1 metadata --locked --offline --format-version 1 \
    --manifest-path "$repo_root/cloud_experiments/lexbor_adapter/Cargo.toml" >/dev/null 2>&1; then
    cargo_offline_args=(--offline)
fi
run_bounded adapter-tests env \
    ATHENA_LEXBOR_SOURCE_DIR="$source" \
    ATHENA_LEXBOR_BUILD_DIR="$build" \
    ATHENA_LEXBOR_LIB_DIR="$library_dir" \
    CARGO_TARGET_DIR="${ATHENA_LEXBOR_ADAPTER_TARGET_DIR:-${CARGO_TARGET_DIR:-$scratch/cargo-target}}" \
    cargo +1.99.1 test "${cargo_offline_args[@]}" --locked \
        --manifest-path "$repo_root/cloud_experiments/lexbor_adapter/Cargo.toml" \
        --test semantic_parity -- --nocapture

printf '{"status":"LOCAL_NATIVE_AND_ADAPTER_CHARACTERIZATION_PASS","adapter_result":"PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY","l01_status":"NOT_ADMITTED","commit":"%s","archive_sha256":"%s","library_bytes":%s,"scope":"fresh local Lexbor build, one C API canary, six adapter tests; L01 admission still requires native/2 integration, Linux/GCE validation, ownership/failure and resource/cost evidence; no hard memory-limit or comparative-performance claim"}\n' \
    "$commit" "$actual_sha256" "$(stat -c %s "${libraries[0]}")"
