use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const LEXBOR_COMMIT: &str = "2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe";
const LEXBOR_ZIP_SHA256: &str = "2ac0d4234a305a6e992a6a5d2bb0ebb468be93a22e5631e334f7da8c127ba282";

fn collect_c_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let entries = fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("failed to read Lexbor directory {}: {e}", dir.display()));
    let mut paths: Vec<PathBuf> = entries
        .map(|entry| entry.expect("valid dir entry").path())
        .collect();
    paths.sort();
    for path in paths {
        if path.is_dir() {
            if path.file_name().and_then(|n| n.to_str()) == Some("windows_nt") {
                continue;
            }
            collect_c_files(&path, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some("c") {
            out.push(path);
        }
    }
}

fn verify_zip_sha256(zip_path: &Path) -> bool {
    for (prog, args) in [
        ("sha256sum", vec![zip_path.to_str().unwrap_or("")]),
        ("shasum", vec!["-a", "256", zip_path.to_str().unwrap_or("")]),
    ] {
        if let Ok(output) = Command::new(prog).args(&args).output()
            && output.status.success()
            && let Ok(text) = String::from_utf8(output.stdout)
            && let Some(digest) = text.split_whitespace().next()
        {
            return digest.eq_ignore_ascii_case(LEXBOR_ZIP_SHA256);
        }
    }
    false
}

fn locate_or_fetch_verified_archive(out_dir: &Path) -> PathBuf {
    let zip_name = format!("lexbor-{LEXBOR_COMMIT}.zip");
    let mut candidates = Vec::new();
    if let Some(explicit) = env::var_os("ATHENA_LEXBOR_ARCHIVE_CACHE")
        && !explicit.is_empty()
    {
        candidates.push(PathBuf::from(explicit));
    }
    if let Some(home) = env::var_os("HOME") {
        candidates.push(
            PathBuf::from(home)
                .join("athena_data")
                .join("v5_pinned")
                .join(&zip_name),
        );
    }
    candidates.push(PathBuf::from("/tmp/athena-lexbor-cache").join(&zip_name));
    candidates.push(out_dir.join(&zip_name));

    for candidate in &candidates {
        if candidate.is_file() && verify_zip_sha256(candidate) {
            return candidate.clone();
        }
    }

    if env::var("ATHENA_OFFLINE").ok().as_deref() == Some("1") {
        panic!("ATHENA_OFFLINE=1 and no SHA-256-verified Lexbor archive found in cache");
    }

    let dest = out_dir.join(&zip_name);
    let url = format!("https://codeload.github.com/lexbor/lexbor/zip/{LEXBOR_COMMIT}");
    let status = Command::new("curl")
        .args([
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            "120",
            "--max-filesize",
            "16777216",
            &url,
            "--output",
        ])
        .arg(&dest)
        .status()
        .expect("curl is required when no cached Lexbor archive is present");
    assert!(status.success(), "failed to download pinned Lexbor archive");
    assert!(
        verify_zip_sha256(&dest),
        "downloaded Lexbor archive failed SHA-256 verification"
    );
    dest
}

fn build_auto_lexbor(out_dir: &Path) -> PathBuf {
    let extracted = out_dir.join(format!("lexbor-{LEXBOR_COMMIT}"));
    let source_include = extracted.join("source");
    if !source_include
        .join("lexbor/html/interfaces/document.h")
        .is_file()
    {
        let archive = locate_or_fetch_verified_archive(out_dir);
        let status = Command::new("unzip")
            .arg("-q")
            .arg("-o")
            .arg(&archive)
            .arg("-d")
            .arg(out_dir)
            .status()
            .expect("unzip is required to unpack pinned Lexbor archive");
        assert!(status.success(), "unzip failed for pinned Lexbor archive");
    }

    let mut c_files = Vec::new();
    collect_c_files(&source_include.join("lexbor"), &mut c_files);
    assert!(!c_files.is_empty(), "no Lexbor C sources discovered");

    let mut build = cc::Build::new();
    build
        .include(&source_include)
        .define("LEXBOR_STATIC", None)
        .std("c99")
        .opt_level(2)
        .warnings(false);
    for file in &c_files {
        build.file(file);
    }
    build.compile("lexbor_static");
    source_include
}

fn main() {
    for name in [
        "ATHENA_LEXBOR_SOURCE_DIR",
        "ATHENA_LEXBOR_BUILD_DIR",
        "ATHENA_LEXBOR_LIB_DIR",
        "ATHENA_LEXBOR_ARCHIVE_CACHE",
        "ATHENA_OFFLINE",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    println!("cargo:rerun-if-changed=src/lexbor_shim.c");

    let src_env = env::var_os("ATHENA_LEXBOR_SOURCE_DIR");
    let bld_env = env::var_os("ATHENA_LEXBOR_BUILD_DIR");
    let lib_env = env::var_os("ATHENA_LEXBOR_LIB_DIR");

    let (source_include, extra_include) = match (src_env, bld_env, lib_env) {
        (Some(src), Some(bld), Some(lib)) => {
            let source = PathBuf::from(src);
            let build = PathBuf::from(bld);
            let libraries = PathBuf::from(lib);
            let include = source.join("source");
            if !include.join("lexbor/html/interfaces/document.h").is_file()
                || !build.is_dir()
                || !libraries.is_dir()
            {
                panic!("Lexbor source/build/library paths do not identify the pinned native build");
            }
            println!("cargo:rustc-link-search=native={}", libraries.display());
            println!("cargo:rustc-link-lib=static=lexbor_static");
            (include, Some(build))
        }
        (None, None, None) => {
            let out_dir = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo sets OUT_DIR"));
            let include = build_auto_lexbor(&out_dir);
            (include, None)
        }
        _ => panic!(
            "ATHENA_LEXBOR_SOURCE_DIR, ATHENA_LEXBOR_BUILD_DIR, and ATHENA_LEXBOR_LIB_DIR must either all be set or all be unset"
        ),
    };

    let mut shim = cc::Build::new();
    shim.file("src/lexbor_shim.c")
        .include(&source_include)
        .define("LEXBOR_STATIC", None)
        .std("c11")
        .warnings(true)
        .warnings_into_errors(true);
    if let Some(build_dir) = extra_include {
        shim.include(build_dir);
    }
    shim.compile("athena_lexbor_shim");

    let target = env::var("TARGET").expect("Cargo sets TARGET");
    if target.contains("linux") || target.contains("freebsd") {
        for library in ["m", "dl", "pthread"] {
            println!("cargo:rustc-link-lib={library}");
        }
    } else if target.contains("apple") || target.contains("darwin") {
        for library in ["m", "pthread"] {
            println!("cargo:rustc-link-lib={library}");
        }
    }
}
