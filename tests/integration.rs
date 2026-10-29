use athena_core_v2::{
    aae::export_jsonl,
    projection::parse_html,
    table::{Cell, Row, expand},
};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
use url::Url;

struct TempHtml(std::path::PathBuf);

impl Drop for TempHtml {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[test]
fn html5_repairs_entities_and_escaped_aae() {
    let u = Url::parse("https://example.com/").unwrap();
    let p = parse_html(
        "<main><h2>A &amp; B</h2><p>Rust <b>works</b> reliably.</p></main>",
        &u,
        1000,
        100,
    )
    .unwrap();
    let aae = export_jsonl(&p, &u, 17, Some("Rust reliably"), 3).unwrap();
    let frames: Vec<serde_json::Value> = aae
        .lines()
        .map(|x| serde_json::from_str(x).unwrap())
        .collect();
    assert_eq!(frames.first().unwrap()["type"], "meta");
    assert_eq!(frames.last().unwrap()["type"], "end");
    assert!(frames.iter().any(|f| f["text"] == "A & B"));
}
#[test]
fn span_grid_has_bounded_memory() {
    let rows = vec![Row {
        group: 0,
        cells: vec![Cell {
            text: "large".into(),
            rowspan: 10000,
            colspan: 10000,
            is_header: true,
        }],
    }];
    assert!(expand(&rows, 4, 16).is_err());
}

#[test]
fn cli_rejects_html_above_the_parser_byte_limit_before_parsing() {
    const MAX_HTML_BYTES: usize = 4 * 1024 * 1024;
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "athena-cli-input-{}-{unique}.html",
        std::process::id()
    ));
    let _cleanup = TempHtml(path.clone());
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    let block = [b'x'; 64 * 1024];
    for _ in 0..(MAX_HTML_BYTES / block.len()) {
        file.write_all(&block).unwrap();
    }
    file.write_all(b"x").unwrap();
    drop(file);

    let output = Command::new(env!("CARGO_BIN_EXE_athena-core-v2"))
        .arg(&path)
        .arg("https://example.org/")
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(2), "stderr: {stderr}");
    assert!(
        stderr.contains("Input exceeds 4194304-byte limit"),
        "input should be rejected by the byte-bounded reader before DOM parsing: {stderr}"
    );
    assert!(output.stdout.is_empty());
}
