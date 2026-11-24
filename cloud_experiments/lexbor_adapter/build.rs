use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const PINNED_COMMIT: &str = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe";
const PINNED_ARCHIVE_SHA256: &str =
    "2ac0d4234a305a6e992a6a5d2bb0ebb468be93a22e5631e334f7da8c127ba282";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SanitizerMode {
    None,
    AddressUndefined,
    Thread,
}

impl SanitizerMode {
    fn from_env() -> Self {
        match env::var("ATHENA_LEXBOR_SANITIZER") {
            Err(_) => Self::None,
            Ok(raw) => match raw.trim() {
                "" | "none" => Self::None,
                "address_undefined" => Self::AddressUndefined,
                "thread" => Self::Thread,
                other => panic!(
                    "unsupported ATHENA_LEXBOR_SANITIZER={other:?}; expected none, address_undefined, or thread"
                ),
            },
        }
    }

    fn slug(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::AddressUndefined => "address_undefined",
            Self::Thread => "thread",
        }
    }

    fn c_flags(self) -> &'static [&'static str] {
        match self {
            Self::None => &[],
            Self::AddressUndefined => &[
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
                "-g",
            ],
            Self::Thread => &["-fsanitize=thread", "-fno-omit-frame-pointer", "-g"],
        }
    }
}

fn non_empty_env(key: &str) -> Option<String> {
    env::var(key)
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
}

fn resolve_cc_bin(sanitizer: SanitizerMode) -> String {
    if let Some(explicit) = non_empty_env("CC") {
        return explicit;
    }
    if sanitizer != SanitizerMode::None
        && Command::new("clang")
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    {
        return "clang".to_owned();
    }
    "cc".to_owned()
}

fn emit_sanitizer_link_args(sanitizer: SanitizerMode, cc_bin: &str) {
    if sanitizer == SanitizerMode::None {
        return;
    }
    let target_upper = env::var("TARGET")
        .unwrap_or_default()
        .to_ascii_uppercase()
        .replace('-', "_");
    let target_rf = if target_upper.is_empty() {
        String::new()
    } else {
        let var_name = format!("CARGO_TARGET_{target_upper}_RUSTFLAGS");
        println!("cargo:rerun-if-env-changed={var_name}");
        env::var(var_name).unwrap_or_default()
    };
    let rustflags = format!(
        "{} {} {}",
        env::var("RUSTFLAGS").unwrap_or_default(),
        env::var("CARGO_ENCODED_RUSTFLAGS").unwrap_or_default(),
        target_rf
    );
    let rust_has_asan = rustflags.contains("sanitizer=address");
    let rust_has_tsan = rustflags.contains("sanitizer=thread");
    match sanitizer {
        SanitizerMode::None => {}
        SanitizerMode::AddressUndefined => {
            if !rust_has_asan {
                println!("cargo:rustc-link-arg=-fsanitize=address,undefined");
            }
        }
        SanitizerMode::Thread => {
            if !rust_has_tsan {
                println!("cargo:rustc-link-arg=-fsanitize=thread");
            }
        }
    }
    let rust_owns_sanitizer_rt = match sanitizer {
        SanitizerMode::None => false,
        SanitizerMode::AddressUndefined => rust_has_asan,
        SanitizerMode::Thread => rust_has_tsan,
    };
    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "macos"
        && !rust_owns_sanitizer_rt
        && let Ok(output) = Command::new(cc_bin).arg("--print-runtime-dir").output()
        && output.status.success()
    {
        let rt_dir = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        let rt_path = Path::new(&rt_dir);
        if !rt_dir.is_empty() && rt_path.is_dir() {
            println!("cargo:rustc-link-search=native={rt_dir}");
            println!("cargo:rustc-link-arg=-Wl,-rpath,{rt_dir}");
            match sanitizer {
                SanitizerMode::None => {}
                SanitizerMode::AddressUndefined => {
                    if rt_path.join("libclang_rt.asan_osx_dynamic.dylib").is_file() {
                        println!("cargo:rustc-link-lib=dylib=clang_rt.asan_osx_dynamic");
                    } else if rt_path
                        .join("libclang_rt.ubsan_osx_dynamic.dylib")
                        .is_file()
                    {
                        println!("cargo:rustc-link-lib=dylib=clang_rt.ubsan_osx_dynamic");
                    }
                }
                SanitizerMode::Thread => {
                    if rt_path.join("libclang_rt.tsan_osx_dynamic.dylib").is_file() {
                        println!("cargo:rustc-link-lib=dylib=clang_rt.tsan_osx_dynamic");
                    }
                }
            }
        }
    }
}

fn cc_supports_no_sanitize_function(cc_bin: &str, out_dir: &Path) -> bool {
    let probe = out_dir.join(format!("probe_nofn_{}.o", std::process::id()));
    let ok = Command::new(cc_bin)
        .args([
            "-fsanitize=undefined",
            "-fno-sanitize=function",
            "-x",
            "c",
            "-c",
            "/dev/null",
            "-o",
        ])
        .arg(&probe)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    let _ = fs::remove_file(&probe);
    ok
}

fn explicit_dir_conflicts_with_sanitizer(dir: &Path, sanitizer: SanitizerMode) -> bool {
    let s = dir.to_string_lossy();
    for mode in [
        SanitizerMode::None,
        SanitizerMode::AddressUndefined,
        SanitizerMode::Thread,
    ] {
        for prefix in ["install-", "build-"] {
            let marker = format!("{prefix}{}", mode.slug());
            if s.contains(&marker) && mode != sanitizer {
                return true;
            }
        }
    }
    false
}

fn resolve_explicit_lexbor_dirs(sanitizer: SanitizerMode) -> Option<(PathBuf, PathBuf)> {
    let lib_raw = non_empty_env("ATHENA_LEXBOR_LIB_DIR")?;
    let lib_base = PathBuf::from(&lib_raw);
    if explicit_dir_conflicts_with_sanitizer(&lib_base, sanitizer) {
        return None;
    }
    let mut lib_candidates = vec![lib_base.clone()];
    if let Some(parent) = lib_base.parent() {
        lib_candidates.push(parent.join("lib"));
        lib_candidates.push(parent.join("lib64"));
    }
    lib_candidates.push(lib_base.join("lib"));
    lib_candidates.push(lib_base.join("lib64"));

    let resolved_lib_dir = lib_candidates
        .into_iter()
        .find(|dir| dir.join("liblexbor_static.a").is_file())
        .unwrap_or_else(|| {
            panic!(
                "ATHENA_LEXBOR_LIB_DIR={lib_raw:?} does not contain liblexbor_static.a (checked lib and lib64)"
            )
        });

    if let Some(inc_raw) = non_empty_env("ATHENA_LEXBOR_INCLUDE_DIR") {
        let inc_dir = PathBuf::from(&inc_raw);
        assert!(
            inc_dir.join("lexbor").is_dir(),
            "ATHENA_LEXBOR_INCLUDE_DIR={inc_raw:?} does not contain lexbor/ headers"
        );
        return Some((resolved_lib_dir, inc_dir));
    }

    if let Some(parent) = resolved_lib_dir.parent() {
        let sibling_inc = parent.join("include");
        if sibling_inc.join("lexbor").is_dir() {
            return Some((resolved_lib_dir, sibling_inc));
        }
    }
    if let Some(src_raw) = non_empty_env("ATHENA_LEXBOR_SOURCE_DIR") {
        let src_inc = PathBuf::from(src_raw).join("source");
        if src_inc.join("lexbor").is_dir() {
            return Some((resolved_lib_dir, src_inc));
        }
    }
    None
}

fn main() {
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_LIB_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_INCLUDE_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_ARCHIVE");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_ARCHIVE_CACHE");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_SOURCE_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_SANITIZER");
    println!("cargo:rerun-if-env-changed=ATHENA_OFFLINE");
    println!("cargo:rerun-if-env-changed=CC");
    println!("cargo:rerun-if-env-changed=RUSTFLAGS");
    println!("cargo:rerun-if-env-changed=CARGO_ENCODED_RUSTFLAGS");
    println!("cargo:rerun-if-changed=src/lexbor_shim.c");

    let manifest_dir =
        PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"));
    let shim_path = manifest_dir.join("src").join("lexbor_shim.c");
    let sanitizer = SanitizerMode::from_env();
    let cc_bin = resolve_cc_bin(sanitizer);

    let (lib_dir, include_dir) = resolve_explicit_lexbor_dirs(sanitizer)
        .unwrap_or_else(|| build_auto_lexbor(&manifest_dir, sanitizer, &cc_bin));

    let mut shim_build = cc::Build::new();
    if sanitizer != SanitizerMode::None {
        shim_build.compiler(&cc_bin);
    }
    shim_build
        .file(shim_path)
        .include(&include_dir)
        .define("LEXBOR_STATIC", None)
        .flag_if_supported("-std=c11");
    if sanitizer != SanitizerMode::None {
        shim_build.opt_level(1);
        for flag in sanitizer.c_flags() {
            shim_build.flag(flag);
        }
        if sanitizer == SanitizerMode::AddressUndefined {
            shim_build.flag_if_supported("-fno-sanitize=function");
        }
    }
    shim_build.compile("athena_lexbor_shim");

    println!("cargo:rustc-link-search=native={}", lib_dir.display());
    println!("cargo:rustc-link-lib=static=lexbor_static");
    emit_sanitizer_link_args(sanitizer, &cc_bin);
}

/// Automatically extracts and builds static `liblexbor_static.a` strictly inside `OUT_DIR`
/// (zero writes to `~/athena_data/v5_pinned`) when `ATHENA_LEXBOR_LIB_DIR` is unset.
fn build_auto_lexbor(
    manifest_dir: &Path,
    sanitizer: SanitizerMode,
    cc_bin: &str,
) -> (PathBuf, PathBuf) {
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR is set by Cargo"));
    let auto_root = out_dir.join(format!("lexbor-auto-{}", sanitizer.slug()));
    let install_dir = auto_root.join("install");
    let cached_lib = install_dir.join("lib").join("liblexbor_static.a");
    let cached_lib64 = install_dir.join("lib64").join("liblexbor_static.a");
    let cached_inc = install_dir.join("include");

    if cached_inc.join("lexbor").exists() {
        if cached_lib.exists() {
            return (install_dir.join("lib"), cached_inc);
        }
        if cached_lib64.exists() {
            return (install_dir.join("lib64"), cached_inc);
        }
    }

    fs::create_dir_all(&auto_root).expect("create OUT_DIR/lexbor-auto");

    let src_dir = if let Some(explicit_src) = non_empty_env("ATHENA_LEXBOR_SOURCE_DIR") {
        let pb = PathBuf::from(&explicit_src);
        assert!(
            pb.join("source").join("lexbor").is_dir(),
            "ATHENA_LEXBOR_SOURCE_DIR={explicit_src:?} does not contain source/lexbor"
        );
        pb
    } else {
        let archive = resolve_or_fetch_pinned_archive(manifest_dir, &auto_root);
        let unpack_dir = auto_root.join("src");
        let extracted = unpack_dir.join(format!("lexbor-{PINNED_COMMIT}"));
        if !extracted.exists() {
            fs::create_dir_all(&unpack_dir).expect("create unpack_dir");
            let status = Command::new("unzip")
                .args([
                    "-q",
                    archive.to_str().expect("utf8 archive path"),
                    "-d",
                    unpack_dir.to_str().expect("utf8 unpack path"),
                ])
                .status()
                .expect("failed to invoke unzip for pinned Lexbor archive");
            assert!(status.success(), "unzip failed on {}", archive.display());
        }
        extracted
    };

    let build_dir = auto_root.join("build");
    let has_cmake = Command::new("cmake")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);

    if has_cmake {
        let mut cfg_cmd = Command::new("cmake");
        if sanitizer != SanitizerMode::None {
            cfg_cmd.env("CC", cc_bin);
        }
        cfg_cmd.args([
            "-S",
            src_dir.to_str().expect("utf8 src_dir"),
            "-B",
            build_dir.to_str().expect("utf8 build_dir"),
            if sanitizer == SanitizerMode::None {
                "-DCMAKE_BUILD_TYPE=Release"
            } else {
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
            },
            &format!("-DCMAKE_INSTALL_PREFIX={}", install_dir.display()),
            "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
            "-DLEXBOR_BUILD_SHARED=OFF",
            "-DLEXBOR_BUILD_STATIC=ON",
            "-DLEXBOR_BUILD_TESTS=OFF",
            "-DLEXBOR_BUILD_EXAMPLES=OFF",
            "-DLEXBOR_WITHOUT_THREADS=ON",
        ]);
        if sanitizer != SanitizerMode::None {
            let mut flags: Vec<&str> = sanitizer.c_flags().to_vec();
            if sanitizer == SanitizerMode::AddressUndefined
                && cc_supports_no_sanitize_function(cc_bin, &auto_root)
            {
                flags.push("-fno-sanitize=function");
            }
            cfg_cmd.arg(format!("-DCMAKE_C_FLAGS={}", flags.join(" ")));
        }
        let cfg = cfg_cmd
            .status()
            .expect("failed to configure Lexbor via cmake");
        assert!(cfg.success(), "cmake configure for Lexbor failed");

        let jobs = env::var("CARGO_BUILD_JOBS").unwrap_or_else(|_| "3".to_owned());
        let bld = Command::new("cmake")
            .args([
                "--build",
                build_dir.to_str().expect("utf8 build_dir"),
                "--parallel",
                &jobs,
            ])
            .status()
            .expect("failed to build Lexbor via cmake");
        assert!(bld.success(), "cmake build for Lexbor failed");

        let ins = Command::new("cmake")
            .args(["--install", build_dir.to_str().expect("utf8 build_dir")])
            .status()
            .expect("failed to install Lexbor via cmake");
        assert!(ins.success(), "cmake install for Lexbor failed");
    } else {
        let source_root = src_dir.join("source");
        let mut c_files = Vec::new();
        collect_c_sources(&source_root, &mut c_files);
        let mut build = cc::Build::new();
        if sanitizer != SanitizerMode::None {
            build.compiler(cc_bin);
        }
        build
            .cargo_metadata(false)
            .include(&source_root)
            .define("LEXBOR_STATIC", None)
            .define("LEXBOR_WITHOUT_THREADS", None)
            .warnings(false)
            .flag_if_supported("-std=c99")
            .opt_level(if sanitizer == SanitizerMode::None {
                2
            } else {
                1
            });
        for flag in sanitizer.c_flags() {
            build.flag(flag);
        }
        if sanitizer == SanitizerMode::AddressUndefined {
            build.flag_if_supported("-fno-sanitize=function");
        }
        for f in &c_files {
            build.file(f);
        }
        build.compile("lexbor_static");
        let inc_dst = install_dir.join("include").join("lexbor");
        copy_headers_recursive(&source_root.join("lexbor"), &inc_dst);
        let lib_dst = install_dir.join("lib");
        fs::create_dir_all(&lib_dst).expect("create lib_dst");
        fs::copy(
            out_dir.join("liblexbor_static.a"),
            lib_dst.join("liblexbor_static.a"),
        )
        .expect("copy liblexbor_static.a");
    }

    if cached_lib.exists() {
        (install_dir.join("lib"), cached_inc)
    } else if cached_lib64.exists() {
        (install_dir.join("lib64"), cached_inc)
    } else {
        panic!(
            "liblexbor_static.a missing after auto-build in {}",
            install_dir.display()
        );
    }
}

fn find_pinned_archive(manifest_dir: &Path, auto_root: &Path) -> Option<PathBuf> {
    if let Some(p) = non_empty_env("ATHENA_LEXBOR_ARCHIVE") {
        let pb = PathBuf::from(&p);
        assert!(
            pb.is_file(),
            "ATHENA_LEXBOR_ARCHIVE={p:?} does not exist or is not a regular file"
        );
        return Some(pb);
    }
    if let Some(p) = non_empty_env("ATHENA_LEXBOR_ARCHIVE_CACHE") {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            return Some(pb);
        }
    }
    let archive_name = format!("lexbor-{PINNED_COMMIT}.zip");
    if let Some(home) = non_empty_env("HOME") {
        let pb = PathBuf::from(home)
            .join("athena_data")
            .join("v5_pinned")
            .join(&archive_name);
        if pb.is_file() {
            return Some(pb);
        }
    }
    if let Some(repo_root) = manifest_dir.parent().and_then(Path::parent) {
        let repo_candidate = repo_root
            .join("athena_data")
            .join("v5_pinned")
            .join(&archive_name);
        if repo_candidate.is_file() {
            return Some(repo_candidate);
        }
    }
    let out_cached = auto_root.join(&archive_name);
    if out_cached.is_file() {
        return Some(out_cached);
    }
    None
}

fn resolve_or_fetch_pinned_archive(manifest_dir: &Path, auto_root: &Path) -> PathBuf {
    if let Some(existing) = find_pinned_archive(manifest_dir, auto_root) {
        verify_archive_sha256(&existing);
        return existing;
    }

    let archive_name = format!("lexbor-{PINNED_COMMIT}.zip");
    let offline = non_empty_env("ATHENA_OFFLINE").as_deref() == Some("1")
        || non_empty_env("CARGO_NET_OFFLINE").as_deref() == Some("true");
    if offline {
        panic!(
            "offline mode enabled and neither ATHENA_LEXBOR_LIB_DIR, ATHENA_LEXBOR_ARCHIVE, nor ~/athena_data/v5_pinned/{archive_name} was found"
        );
    }

    let url = format!("https://codeload.github.com/lexbor/lexbor/zip/{PINNED_COMMIT}");
    let dest = auto_root.join(&archive_name);
    let tmp = auto_root.join(format!("{archive_name}.tmp.{}", std::process::id()));
    let status = Command::new("curl")
        .args([
            "-fsSL",
            "--retry",
            "3",
            "--output",
            tmp.to_str().expect("utf8 tmp archive path"),
            &url,
        ])
        .status()
        .unwrap_or_else(|e| {
            panic!(
                "ATHENA_LEXBOR_LIB_DIR/ATHENA_LEXBOR_ARCHIVE unset, local {archive_name} absent, and curl invocation failed: {e}"
            )
        });
    if !status.success() {
        let _ = fs::remove_file(&tmp);
        panic!("failed to fetch pinned Lexbor archive from {url} (status: {status})");
    }
    verify_archive_sha256(&tmp);
    fs::rename(&tmp, &dest).expect("rename verified Lexbor archive into OUT_DIR");
    dest
}

fn verify_archive_sha256(path: &Path) {
    let bytes = fs::read(path)
        .unwrap_or_else(|e| panic!("failed to read Lexbor archive {}: {e}", path.display()));
    let actual = sha256_hex(&bytes);
    if actual != PINNED_ARCHIVE_SHA256 {
        panic!(
            "Lexbor archive SHA-256 mismatch for {}: expected {PINNED_ARCHIVE_SHA256}, got {actual}",
            path.display()
        );
    }
}

fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let rem = (data.len() + 9) % 64;
    let zero_pad = if rem == 0 { 0 } else { 64 - rem };
    let mut padded = Vec::with_capacity(data.len() + 9 + zero_pad);
    padded.extend_from_slice(data);
    padded.push(0x80);
    padded.resize(data.len() + 1 + zero_pad, 0);
    padded.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in padded.as_chunks::<64>().0 {
        let mut w = [0u32; 64];
        for (i, &word) in chunk.as_chunks::<4>().0.iter().enumerate() {
            w[i] = u32::from_be_bytes(word);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    let mut out = String::with_capacity(64);
    for word in h {
        use std::fmt::Write as _;
        let _ = write!(&mut out, "{word:08x}");
    }
    out
}

fn collect_c_sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let entries = fs::read_dir(dir).expect("read Lexbor source dir");
    for entry in entries {
        let path = entry.expect("dir entry").path();
        if path.to_string_lossy().contains("windows_nt") {
            continue;
        }
        if path.is_dir() {
            collect_c_sources(&path, out);
        } else if path.extension().and_then(|s| s.to_str()) == Some("c") {
            out.push(path);
        }
    }
}

fn copy_headers_recursive(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).expect("create header dir");
    for entry in fs::read_dir(src).expect("read header dir") {
        let path = entry.expect("header entry").path();
        let target = dst.join(path.file_name().expect("file_name"));
        if path.is_dir() {
            copy_headers_recursive(&path, &target);
        } else if path.extension().and_then(|s| s.to_str()) == Some("h") {
            fs::copy(&path, &target).expect("copy header");
        }
    }
}
