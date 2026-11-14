#!/usr/bin/env bash
set -euo pipefail

commit="2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe"
expected_sha256="2ac0d4234a305a6e992a6a5d2bb0ebb468be93a22e5631e334f7da8c127ba282"
archive_url="https://codeload.github.com/lexbor/lexbor/zip/${commit}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
canary_src="${script_dir}/canary.c"
adapter_manifest="${repo_root}/cloud_experiments/lexbor_adapter/Cargo.toml"

sanitizer="${ATHENA_LEXBOR_SANITIZER:-none}"
case "$sanitizer" in
    ""|none)
        sanitizer="none"
        ;;
    address_undefined|thread)
        ;;
    *)
        echo "unsupported ATHENA_LEXBOR_SANITIZER=${sanitizer} (expected none, address_undefined, or thread)" >&2
        exit 2
        ;;
esac

auto_scratch_parent=""
cleanup_auto_scratch() {
    if [[ -n "$auto_scratch_parent" && -d "$auto_scratch_parent" ]]; then
        rm -rf "$auto_scratch_parent"
    fi
}
trap cleanup_auto_scratch EXIT

if [[ $# -gt 1 ]]; then
    echo "usage: build_and_smoke.sh [ABSOLUTE_FRESH_SCRATCH_ROOT]" >&2
    exit 2
fi
if [[ $# -eq 1 ]]; then
    scratch="$1"
elif [[ -n "${ATHENA_LEXBOR_WORK_ROOT:-}" ]]; then
    scratch="$ATHENA_LEXBOR_WORK_ROOT"
else
    auto_scratch_parent="$(mktemp -d "${TMPDIR:-/tmp}/athena-lexbor-smoke.XXXXXX")"
    scratch="${auto_scratch_parent}/run"
fi

if [[ -z "$scratch" || "$scratch" != /* || -e "$scratch" ]]; then
    echo "scratch root must be a non-existing absolute path: $scratch" >&2
    exit 2
fi

case "$scratch" in
    "$repo_root"|"$repo_root"/*)
        echo "scratch root must be outside repository root: $scratch" >&2
        exit 2
        ;;
esac

v5_pinned_dir="${HOME:-}/athena_data/v5_pinned"
case "$scratch" in
    "$v5_pinned_dir"|"$v5_pinned_dir"/*)
        echo "scratch root must never touch read-only v5_pinned: $scratch" >&2
        exit 2
        ;;
esac

mkdir -p "$scratch"

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

now_ms() {
    python3 -c 'import time; print(int(time.time() * 1000))'
}

step_log="${scratch}/steps.tsv"
: > "$step_log"

run_step() {
    local name="$1"
    shift
    local log="${scratch}/${name}.log"
    local started ended rc
    started="$(now_ms)"
    set +e
    "$@" >"$log" 2>&1
    rc=$?
    set -e
    ended="$(now_ms)"
    printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$((ended - started))" "$log" >> "$step_log"
    if [[ "$rc" -ne 0 ]]; then
        cat "$log" >&2
        return "$rc"
    fi
}

archive="${scratch}/lexbor-${commit}.zip"
pinned_local_cache="${v5_pinned_dir}/lexbor-${commit}.zip"
explicit_archive_cache="${ATHENA_LEXBOR_ARCHIVE_CACHE:-}"

if [[ -n "$explicit_archive_cache" ]]; then
    case "$explicit_archive_cache" in
        "$v5_pinned_dir"|"$v5_pinned_dir"/*)
            # Treat anything under ~/athena_data/v5_pinned as strictly read-only.
            if [[ ! -f "$explicit_archive_cache" ]]; then
                echo "refusing to write missing archive inside read-only v5_pinned: $explicit_archive_cache" >&2
                exit 2
            fi
            run_step fetch cp "$explicit_archive_cache" "$archive"
            ;;
        *)
            if [[ -f "$explicit_archive_cache" ]]; then
                run_step fetch cp "$explicit_archive_cache" "$archive"
            elif [[ -f "$pinned_local_cache" ]]; then
                run_step fetch cp "$pinned_local_cache" "$archive"
                mkdir -p "$(dirname "$explicit_archive_cache")"
                cp "$archive" "${explicit_archive_cache}.tmp.$$"
                mv "${explicit_archive_cache}.tmp.$$" "$explicit_archive_cache"
            elif [[ "${ATHENA_OFFLINE:-0}" == "1" ]]; then
                echo "ATHENA_OFFLINE=1 and Lexbor archive cache is missing: $explicit_archive_cache" >&2
                exit 2
            else
                run_step fetch curl -fsSL --retry 3 --output "$archive" "$archive_url"
                actual_prefetch_sha256="$(sha256_file "$archive")"
                if [[ "$actual_prefetch_sha256" != "$expected_sha256" ]]; then
                    echo "Lexbor archive digest mismatch: expected ${expected_sha256}, got ${actual_prefetch_sha256}" >&2
                    exit 1
                fi
                mkdir -p "$(dirname "$explicit_archive_cache")"
                cp "$archive" "${explicit_archive_cache}.tmp.$$"
                mv "${explicit_archive_cache}.tmp.$$" "$explicit_archive_cache"
            fi
            ;;
    esac
elif [[ -f "$pinned_local_cache" ]]; then
    # Read-only copy from ~/athena_data/v5_pinned; zero writes to v5_pinned.
    run_step fetch cp "$pinned_local_cache" "$archive"
elif [[ "${ATHENA_OFFLINE:-0}" == "1" ]]; then
    echo "ATHENA_OFFLINE=1 and pinned Lexbor archive is missing: $pinned_local_cache" >&2
    exit 2
else
    run_step fetch curl -fsSL --retry 3 --output "$archive" "$archive_url"
fi

actual_sha256="$(sha256_file "$archive")"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    echo "Lexbor archive digest mismatch: expected ${expected_sha256}, got ${actual_sha256}" >&2
    exit 1
fi

run_step unpack unzip -q "$archive" -d "$scratch"
src_dir="${scratch}/lexbor-${commit}"
build_dir="${scratch}/build-${sanitizer}"
install_dir="${scratch}/install-${sanitizer}"

san_cflags=()
san_ldflags=()
if [[ "$sanitizer" == "address_undefined" ]]; then
    san_cflags=(-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1)
    san_ldflags=(-fsanitize=address,undefined)
elif [[ "$sanitizer" == "thread" ]]; then
    san_cflags=(-fsanitize=thread -fno-omit-frame-pointer -g -O1)
    san_ldflags=(-fsanitize=thread)
fi

if [[ "$sanitizer" != "none" && "$(uname -s)" == "Darwin" ]]; then
    clang_rt_dir="$(cc --print-runtime-dir 2>/dev/null || true)"
    if [[ -n "$clang_rt_dir" && -d "$clang_rt_dir" ]]; then
        san_ldflags+=("-Wl,-rpath,${clang_rt_dir}")
    fi
fi

san_cflags_joined="${san_cflags[*]:-}"

build_backend="cmake"
if command -v cmake >/dev/null 2>&1; then
    cmake_extra=()
    if [[ -n "$san_cflags_joined" ]]; then
        cmake_extra+=("-DCMAKE_C_FLAGS=${san_cflags_joined}")
    fi
    run_step configure cmake -S "$src_dir" -B "$build_dir" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$install_dir" \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DLEXBOR_BUILD_SHARED=OFF \
        -DLEXBOR_BUILD_STATIC=ON \
        -DLEXBOR_BUILD_TESTS=OFF \
        -DLEXBOR_BUILD_EXAMPLES=OFF \
        -DLEXBOR_WITHOUT_THREADS=ON \
        ${cmake_extra[@]+"${cmake_extra[@]}"}
    run_step build cmake --build "$build_dir" --parallel "${CARGO_BUILD_JOBS:-3}"
    run_step install cmake --install "$build_dir"
else
    build_backend="posix_cc_script"
    run_step build python3 - "$src_dir" "$build_dir" "$install_dir" "$san_cflags_joined" <<'PY'
import concurrent.futures
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

src_dir = pathlib.Path(sys.argv[1])
build_dir = pathlib.Path(sys.argv[2])
install_dir = pathlib.Path(sys.argv[3])
extra_cflags = shlex.split(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else []
source_root = src_dir / "source"
obj_root = build_dir / "objects"
lib_dir = install_dir / "lib"
include_dir = install_dir / "include"
obj_root.mkdir(parents=True, exist_ok=True)
lib_dir.mkdir(parents=True, exist_ok=True)

sources = [
    path for path in sorted(source_root.rglob("*.c"))
    if "ports/windows_nt" not in path.as_posix()
]
if not sources:
    raise SystemExit("no Lexbor C sources discovered")

cc = os.environ.get("CC", "cc")
ar = os.environ.get("AR", "ar")
ranlib = os.environ.get("RANLIB", "ranlib")
opt_flag = "-O1" if extra_cflags else "-O2"
flags = [
    opt_flag, "-fPIC", "-std=c99",
    "-DLEXBOR_STATIC", "-DLEXBOR_WITHOUT_THREADS",
    f"-I{source_root}",
    *extra_cflags,
]

def compile_one(source: pathlib.Path) -> pathlib.Path:
    rel = source.relative_to(source_root)
    obj = (obj_root / rel).with_suffix(".o")
    obj.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([cc, *flags, "-c", str(source), "-o", str(obj)], check=True)
    return obj

workers = min(int(os.environ.get("CARGO_BUILD_JOBS", "3") or "3"), os.cpu_count() or 1, 8)
with concurrent.futures.ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
    objects = list(pool.map(compile_one, sources))

archive = lib_dir / "liblexbor_static.a"
subprocess.run([ar, "rcs", str(archive), *(str(obj) for obj in objects)], check=True)
subprocess.run([ranlib, str(archive)], check=True)
shutil.copytree(source_root / "lexbor", include_dir / "lexbor", dirs_exist_ok=True)
PY
fi

lib_path=""
for candidate in \
    "${install_dir}/lib/liblexbor_static.a" \
    "${install_dir}/lib64/liblexbor_static.a" \
    "${build_dir}/liblexbor_static.a"
do
    if [[ -f "$candidate" ]]; then
        lib_path="$candidate"
        break
    fi
done
if [[ -z "$lib_path" ]]; then
    echo "liblexbor_static.a was not produced" >&2
    exit 1
fi
lib_dir="$(dirname "$lib_path")"
include_dir="${install_dir}/include"
canary_bin="${scratch}/lexbor_canary"

canary_opt="-O2"
if [[ "$sanitizer" != "none" ]]; then
    canary_opt="-O1"
fi

run_step canary-build cc "$canary_opt" -std=c99 -Wall -Wextra -Werror \
    -DLEXBOR_STATIC \
    ${san_cflags[@]+"${san_cflags[@]}"} \
    -I"$include_dir" \
    "$canary_src" \
    "$lib_path" \
    ${san_ldflags[@]+"${san_ldflags[@]}"} \
    -o "$canary_bin"

asan_opts="halt_on_error=1"
if [[ "$(uname -s)" == "Darwin" ]]; then
    asan_opts="detect_leaks=0:halt_on_error=1"
fi
ubsan_opts="print_stacktrace=1:halt_on_error=1"
tsan_opts="halt_on_error=1"

run_step canary-run env \
    ASAN_OPTIONS="$asan_opts" \
    UBSAN_OPTIONS="$ubsan_opts" \
    TSAN_OPTIONS="$tsan_opts" \
    "$canary_bin"
canary_json="$(cat "${scratch}/canary-run.log")"

cargo_offline_args=()
if [[ "${ATHENA_OFFLINE:-0}" == "1" ]]; then
    cargo_offline_args=(--offline)
fi
adapter_target_dir="${ATHENA_LEXBOR_ADAPTER_TARGET_DIR:-${scratch}/adapter-target-${sanitizer}}"
if [[ -n "${ATHENA_LEXBOR_ADAPTER_TARGET_DIR:-}" && "$sanitizer" != "none" ]]; then
    adapter_target_dir="${ATHENA_LEXBOR_ADAPTER_TARGET_DIR}-${sanitizer}"
fi

run_step adapter-tests env \
    ATHENA_LEXBOR_LIB_DIR="$lib_dir" \
    ATHENA_LEXBOR_INCLUDE_DIR="$include_dir" \
    ATHENA_LEXBOR_SANITIZER="$sanitizer" \
    CARGO_TARGET_DIR="$adapter_target_dir" \
    CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-3}" \
    ASAN_OPTIONS="$asan_opts" \
    UBSAN_OPTIONS="$ubsan_opts" \
    TSAN_OPTIONS="$tsan_opts" \
    cargo test --manifest-path "$adapter_manifest" --locked ${cargo_offline_args[@]+"${cargo_offline_args[@]}"}

python3 - "$commit" "$expected_sha256" "$actual_sha256" "$build_backend" "$lib_path" "$step_log" "$canary_json" "$sanitizer" <<'PY'
import json
import os
import pathlib
import sys

commit, expected_sha, actual_sha, backend, lib_path, step_log, canary_json, sanitizer = sys.argv[1:]
steps = []
for line in pathlib.Path(step_log).read_text(encoding="utf-8").splitlines():
    name, rc, elapsed_ms, log = line.split("\t")
    steps.append({
        "step": name,
        "returncode": int(rc),
        "elapsed_ms": int(elapsed_ms),
        "log_bytes": os.path.getsize(log),
    })
print(json.dumps({
    "schema": "AthenaLexborFeasibilitySmoke/1",
    "lexbor_commit": commit,
    "expected_archive_sha256": expected_sha,
    "actual_archive_sha256": actual_sha,
    "build_backend": backend,
    "sanitizer": sanitizer,
    "static_lib_bytes": os.path.getsize(lib_path),
    "canary": json.loads(canary_json),
    "steps": steps,
}, sort_keys=True))
PY
