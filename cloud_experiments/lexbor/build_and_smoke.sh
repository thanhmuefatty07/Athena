#!/usr/bin/env bash
set -euo pipefail

commit=2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe
archive_sha256=2ac0d4234a305a6e992a6a5d2bb0ebb468be93a22e5631e334f7da8c127ba282
archive_url="https://codeload.github.com/lexbor/lexbor/zip/${commit}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "${script_dir}/../.." && pwd -P)"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

if [[ $# == 0 && -n "${ATHENA_LEXBOR_WORK_ROOT:-}" ]]; then
    raw_scratch="$ATHENA_LEXBOR_WORK_ROOT"
elif [[ $# == 1 ]]; then
    raw_scratch="$1"
else
    fail 'usage: build_and_smoke.sh ABSOLUTE_FRESH_SCRATCH_ROOT'
fi
[[ "$raw_scratch" == /* ]] || fail 'scratch root must be an absolute path'

for tool in cargo rustc cc ar unzip python3; do
    command -v "$tool" >/dev/null 2>&1 || fail "required tool not found: $tool"
done

sha256_file() {
    local target="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$target" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$target" | awk '{print $1}'
    else
        python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$target"
    fi
}

scratch="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$raw_scratch")"
case "${scratch}/" in
    "${repo_root}/"*) fail 'scratch root must be outside the repository' ;;
esac
[[ ! -e "$scratch" && ! -L "$scratch" ]] || fail 'scratch root must not already exist'

if rustc --version 2>/dev/null | grep -q '^rustc 1\.99\.1 '; then
    rustc_cmd=(rustc)
    cargo_cmd=(cargo)
elif rustc +1.99.1 --version >/dev/null 2>&1 && cargo +1.99.1 --version >/dev/null 2>&1; then
    rustc_cmd=(rustc +1.99.1)
    cargo_cmd=(cargo +1.99.1)
else
    fail 'Rust 1.99.1 must be installed before this run'
fi
"${rustc_cmd[@]}" --version >/dev/null 2>&1 || fail 'Rust 1.99.1 check failed'
"${cargo_cmd[@]}" --version >/dev/null 2>&1 || fail 'Cargo 1.99.1 check failed'

scratch_parent="$(dirname "$scratch")"
[[ -d "$scratch_parent" ]] || fail 'scratch parent must already exist'
free_kib="$(df -Pk "$scratch_parent" | awk 'NR==2 {print $4}')"
[[ "$free_kib" =~ ^[0-9]+$ ]] || fail 'cannot measure scratch capacity'
(( free_kib >= 8388608 )) || fail 'need at least 8 GiB free for the pinned native and Rust adapter build'

mkdir "$scratch"
archive="$scratch/lexbor-${commit}.zip"
source="$scratch/lexbor-${commit}"
build="$scratch/build"
logs="$scratch/logs"
mkdir "$logs"

pinned_local_cache="${HOME:-}/athena_data/v5_pinned/lexbor-${commit}.zip"
archive_cache="${ATHENA_LEXBOR_ARCHIVE_CACHE:-}"
if [[ -z "$archive_cache" && -n "${HOME:-}" && -d "${HOME}/athena_data/v5_pinned" ]]; then
    archive_cache="$pinned_local_cache"
fi

for candidate_cache in "$archive_cache" "$pinned_local_cache"; do
    if [[ -n "$candidate_cache" && -f "$candidate_cache" && ! -L "$candidate_cache" ]]; then
        cached_sha256="$(sha256_file "$candidate_cache")"
        if [[ "$cached_sha256" == "$archive_sha256" ]]; then
            cp "$candidate_cache" "$archive"
            break
        fi
    fi
done

if [[ ! -f "$archive" ]]; then
    [[ "${ATHENA_OFFLINE:-0}" != "1" ]] || fail 'offline mode requires a pre-verified ATHENA_LEXBOR_ARCHIVE_CACHE'
    command -v curl >/dev/null 2>&1 || fail 'required tool not found: curl'
    curl --fail --location --silent --show-error --max-time 120 --max-filesize 16777216 \
        "$archive_url" --output "$archive"
fi
actual_sha256="$(sha256_file "$archive")"
[[ "$actual_sha256" == "$archive_sha256" ]] || fail 'pinned Lexbor archive SHA-256 mismatch'
if [[ -n "$archive_cache" && ! -e "$archive_cache" && ! -L "$archive_cache" ]]; then
    cache_parent="$(dirname "$archive_cache")"
    if [[ -d "$cache_parent" ]]; then
        cp "$archive" "${archive_cache}.tmp.$$"
        mv -f "${archive_cache}.tmp.$$" "$archive_cache"
    fi
fi
unzip -q "$archive" -d "$scratch"
[[ -f "$source/CMakeLists.txt" ]] || fail 'pinned ZIP did not extract the expected source tree'

run_bounded() {
    local label="$1"; shift
    local log="$logs/${label}.log"
    local status=0
    if command -v timeout >/dev/null 2>&1; then
        timeout --signal=TERM --kill-after=30s 900s "$@" >"$log" 2>&1 || status=$?
    else
        python3 - "$log" "$@" <<'PY' || status=$?
import os, signal, subprocess, sys
log_path = sys.argv[1]
cmd = sys.argv[2:]
with open(log_path, "wb") as out:
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        rc = proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        sys.exit(124)
    sys.exit(rc)
PY
    fi
    if [[ "$status" -eq 0 ]]; then
        return 0
    fi
    printf 'FAILED: %s (exit %s); log: %s\n' "$label" "$status" "$log" >&2
    tail -n 80 "$log" >&2 || true
    exit "$status"
}

if command -v cmake >/dev/null 2>&1 && [[ "${ATHENA_LEXBOR_FORCE_CC_FALLBACK:-0}" != "1" ]]; then
    run_bounded configure cmake -S "$source" -B "$build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DLEXBOR_BUILD_SHARED=OFF -DLEXBOR_BUILD_STATIC=ON \
        -DLEXBOR_BUILD_TESTS=OFF -DLEXBOR_BUILD_EXAMPLES=OFF \
        -DLEXBOR_BUILD_UTILS=OFF -DLEXBOR_BUILD_BENCHMARKS=OFF \
        -DLEXBOR_BUILD_WITH_ASAN=OFF -DLEXBOR_BUILD_SEPARATELY=OFF
    run_bounded build cmake --build "$build" --config Release --target lexbor_static --parallel 2
else
    mkdir -p "$build/objs"
    posix_cc_script='
import concurrent.futures, os, shutil, subprocess, sys
from pathlib import Path
src_root = Path(sys.argv[1])
build_dir = Path(sys.argv[2])
obj_dir = build_dir / "objs"
cc = os.environ.get("CC", "cc")
ar = os.environ.get("AR", "ar")
c_files = [
    path for path in sorted((src_root / "lexbor").rglob("*.c"))
    if not ("ports" in path.relative_to(src_root).parts and "windows_nt" in path.relative_to(src_root).parts)
]
if not c_files:
    raise SystemExit("No Lexbor POSIX C sources found")
def compile_one(c_path: Path) -> str:
    rel = "__".join(c_path.relative_to(src_root).parts) + ".o"
    obj_path = obj_dir / rel
    subprocess.run([cc, "-std=c99", "-O2", "-DLEXBOR_STATIC", "-I", str(src_root), "-c", str(c_path), "-o", str(obj_path)], check=True)
    return str(obj_path)
jobs = max(1, min(int(os.environ.get("CARGO_BUILD_JOBS", "3")), 4))
with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
    objs = list(pool.map(compile_one, c_files))
lib_path = build_dir / "liblexbor_static.a"
subprocess.run([ar, "rcs", str(lib_path), *objs], check=True)
if shutil.which("ranlib"):
    subprocess.run(["ranlib", str(lib_path)], check=True)
print(f"Built {lib_path} from {len(objs)} translation units")
'
    run_bounded build-posix-cc python3 -c "$posix_cc_script" "$source/source" "$build"
fi

libraries=()
while IFS= read -r found_lib; do
    if [[ -n "$found_lib" ]]; then
        libraries+=("$found_lib")
    fi
done < <(find "$build" -type f -name 'liblexbor_static.a' -print)
[[ ${#libraries[@]} == 1 ]] || fail "expected one liblexbor_static.a, found ${#libraries[@]}"

canary_link_flags=(-lm -pthread)
if [[ "$(uname -s)" != "Darwin" ]]; then
    canary_link_flags=(-lm -ldl -pthread)
fi
run_bounded canary-build cc -std=c11 -O2 -Wall -Wextra -Werror -DLEXBOR_STATIC \
    -I "$source/source" -I "$build" "$script_dir/canary.c" "${libraries[0]}" \
    "${canary_link_flags[@]}" -o "$scratch/lexbor-canary"
run_bounded canary-run "$scratch/lexbor-canary"

library_dir="$(dirname "${libraries[0]}")"
cargo_offline_args=()
if [[ "${ATHENA_OFFLINE:-0}" == "1" ]] || "${cargo_cmd[@]}" metadata --locked --offline --format-version 1 \
    --manifest-path "$repo_root/cloud_experiments/lexbor_adapter/Cargo.toml" >/dev/null 2>&1; then
    cargo_offline_args=(--offline)
fi
run_bounded adapter-tests env \
    CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-3}" \
    ATHENA_LEXBOR_SOURCE_DIR="$source" \
    ATHENA_LEXBOR_BUILD_DIR="$build" \
    ATHENA_LEXBOR_LIB_DIR="$library_dir" \
    CARGO_TARGET_DIR="${ATHENA_LEXBOR_ADAPTER_TARGET_DIR:-${CARGO_TARGET_DIR:-$scratch/cargo-target}}" \
    "${cargo_cmd[@]}" test "${cargo_offline_args[@]}" --locked \
        --manifest-path "$repo_root/cloud_experiments/lexbor_adapter/Cargo.toml" -- --nocapture

library_bytes="$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "${libraries[0]}")"
printf '{"status":"LOCAL_NATIVE_AND_ADAPTER_CHARACTERIZATION_PASS","adapter_result":"PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY","l01_status":"NOT_ADMITTED","commit":"%s","archive_sha256":"%s","library_bytes":%s,"scope":"fresh local Lexbor build, one C API canary, six adapter tests; L01 admission still requires native/2 integration, Linux/GCE validation, ownership/failure and resource/cost evidence; no hard memory-limit or comparative-performance claim"}\n' \
    "$commit" "$actual_sha256" "$library_bytes"
