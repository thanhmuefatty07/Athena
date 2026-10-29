use std::{env, path::PathBuf};

fn required_path(name: &str) -> PathBuf {
    let value =
        env::var_os(name).unwrap_or_else(|| panic!("{name} must point to the pinned Lexbor build"));
    PathBuf::from(value)
}

fn main() {
    for name in [
        "ATHENA_LEXBOR_SOURCE_DIR",
        "ATHENA_LEXBOR_BUILD_DIR",
        "ATHENA_LEXBOR_LIB_DIR",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    println!("cargo:rerun-if-changed=src/lexbor_shim.c");

    let source = required_path("ATHENA_LEXBOR_SOURCE_DIR");
    let build = required_path("ATHENA_LEXBOR_BUILD_DIR");
    let libraries = required_path("ATHENA_LEXBOR_LIB_DIR");
    let source_include = source.join("source");
    if !source_include
        .join("lexbor/html/interfaces/document.h")
        .is_file()
        || !build.is_dir()
        || !libraries.is_dir()
    {
        panic!("Lexbor source/build/library paths do not identify the pinned native build");
    }

    cc::Build::new()
        .file("src/lexbor_shim.c")
        .include(source_include)
        .include(build)
        .define("LEXBOR_STATIC", None)
        .warnings(true)
        .warnings_into_errors(true)
        .compile("athena_lexbor_shim");

    println!("cargo:rustc-link-search=native={}", libraries.display());
    println!("cargo:rustc-link-lib=static=lexbor_static");
    let target = env::var("TARGET").expect("Cargo sets TARGET");
    if target.contains("linux") || target.contains("freebsd") {
        for library in ["m", "dl", "pthread"] {
            println!("cargo:rustc-link-lib={library}");
        }
    }
}
