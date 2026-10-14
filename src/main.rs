//! Offline deterministic demonstrator: run with `cargo run -- file.html https://example.org/ [query]`
use athena_core_v2::{aae::export_jsonl, projection::parse_html};
use std::{env, fs, process};
use url::Url;
fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: athena-core-v2 <file.html> <https-base-url> [query]");
        process::exit(2);
    }
    let html = fs::read_to_string(&args[1]).expect("Input must be a UTF-8 HTML document");
    let base = Url::parse(&args[2]).expect("Invalid base URL");
    let page = parse_html(&html, &base, 250_000, 1200).expect("DOM parser limit exceeded");
    let out = export_jsonl(&page, &base, 0, args.get(3).map(String::as_str), 5)
        .expect("Could not serialize AAE");
    print!("{out}");
}
