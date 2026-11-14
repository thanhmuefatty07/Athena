use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const PINNED_COMMIT: &str = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe";

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

fn emit_sanitizer_link_args(sanitizer: SanitizerMode) {
    if sanitizer == SanitizerMode::None {
        return;
    }
    let rustflags = format!(
        "{} {}",
        env::var("RUSTFLAGS").unwrap_or_default(),
        env::var("CARGO_ENCODED_RUSTFLAGS").unwrap_or_default()
    );
    match sanitizer {
        SanitizerMode::None => {}
        SanitizerMode::AddressUndefined => {
            if rustflags.contains("sanitizer=address") {
                println!("cargo:rustc-link-arg=-fsanitize=undefined");
            } else {
                println!("cargo:rustc-link-arg=-fsanitize=address,undefined");
            }
        }
        SanitizerMode::Thread => {
            if !rustflags.contains("sanitizer=thread") {
                println!("cargo:rustc-link-arg=-fsanitize=thread");
            }
        }
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "macos" {
        let cc_bin = env::var("CC").unwrap_or_else(|_| "cc".to_owned());
        if let Ok(output) = Command::new(&cc_bin).arg("--print-runtime-dir").output()
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
                        if !rustflags.contains("sanitizer=address") {
                            if rt_path.join("libclang_rt.asan_osx_dynamic.dylib").is_file() {
                                println!("cargo:rustc-link-lib=dylib=clang_rt.asan_osx_dynamic");
                            } else if rt_path
                                .join("libclang_rt.ubsan_osx_dynamic.dylib")
                                .is_file()
                            {
                                println!("cargo:rustc-link-lib=dylib=clang_rt.ubsan_osx_dynamic");
                            }
                        }
                    }
                    SanitizerMode::Thread => {
                        if !rustflags.contains("sanitizer=thread")
                            && rt_path.join("libclang_rt.tsan_osx_dynamic.dylib").is_file()
                        {
                            println!("cargo:rustc-link-lib=dylib=clang_rt.tsan_osx_dynamic");
                        }
                    }
                }
            }
        }
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_LIB_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_INCLUDE_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_ARCHIVE");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_SOURCE_DIR");
    println!("cargo:rerun-if-env-changed=ATHENA_LEXBOR_SANITIZER");
    println!("cargo:rerun-if-env-changed=RUSTFLAGS");
    println!("cargo:rerun-if-changed=src/lexbor_shim.c");

    let manifest_dir =
        PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"));
    let shim_path = manifest_dir.join("src").join("lexbor_shim.c");
    let sanitizer = SanitizerMode::from_env();

    let (lib_dir, include_dir) = match (
        env::var("ATHENA_LEXBOR_LIB_DIR").ok(),
        env::var("ATHENA_LEXBOR_INCLUDE_DIR").ok(),
    ) {
        (Some(l), Some(i)) => (PathBuf::from(l), PathBuf::from(i)),
        _ => build_auto_lexbor(&manifest_dir, sanitizer),
    };

    let mut shim_build = cc::Build::new();
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
    }
    shim_build.compile("athena_lexbor_shim");

    println!("cargo:rustc-link-search=native={}", lib_dir.display());
    println!("cargo:rustc-link-lib=static=lexbor_static");
    emit_sanitizer_link_args(sanitizer);
}

/// Automatically extracts and builds static `liblexbor_static.a` strictly inside `OUT_DIR`
/// (zero writes to `~/athena_data/v5_pinned`) when `ATHENA_LEXBOR_LIB_DIR` is unset.
fn build_auto_lexbor(manifest_dir: &Path, sanitizer: SanitizerMode) -> (PathBuf, PathBuf) {
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

    let src_dir = if let Ok(explicit_src) = env::var("ATHENA_LEXBOR_SOURCE_DIR") {
        PathBuf::from(explicit_src)
    } else {
        let archive = find_pinned_archive(manifest_dir).unwrap_or_else(|| {
            panic!(
                "ATHENA_LEXBOR_LIB_DIR not set and pinned Lexbor archive lexbor-{PINNED_COMMIT}.zip not found in ~/athena_data/v5_pinned/"
            )
        });
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
            cfg_cmd.arg(format!("-DCMAKE_C_FLAGS={}", sanitizer.c_flags().join(" ")));
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
        build
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

fn find_pinned_archive(manifest_dir: &Path) -> Option<PathBuf> {
    if let Ok(p) = env::var("ATHENA_LEXBOR_ARCHIVE") {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            return Some(pb);
        }
    }
    if let Ok(home) = env::var("HOME") {
        let pb = PathBuf::from(home)
            .join("athena_data")
            .join("v5_pinned")
            .join(format!("lexbor-{PINNED_COMMIT}.zip"));
        if pb.is_file() {
            return Some(pb);
        }
    }
    let repo_candidate = manifest_dir
        .parent()?
        .parent()?
        .join("athena_data")
        .join("v5_pinned")
        .join(format!("lexbor-{PINNED_COMMIT}.zip"));
    if repo_candidate.is_file() {
        return Some(repo_candidate);
    }
    None
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
