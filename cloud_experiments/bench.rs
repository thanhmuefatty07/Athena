//! Whole-page CPU boundary; immutable fixture setup precedes the timer.
use athena_core_v2::{
    aae::export_jsonl_with_limit,
    projection::{Projection, parse_html},
    rank::{bm25, highlight_indices},
};
use serde_json::{Value, json};
use std::{env, fs, hint::black_box, time::Instant};
use url::Url;

fn payload(stage: &str, page: &Projection, base: &Url, input: &Value) -> Vec<u8> {
    let query = input["query"].as_str();
    match stage {
        "parse" => serde_json::to_vec(&json!({
            "chunks": page.chunks.iter().map(|c| json!({"role":c.role,"text":c.text,"links":c.links})).collect::<Vec<_>>(),
            "links": page.links, "warnings": page.parse_warnings
        })).unwrap(),
        "rank" => serde_json::to_vec(&bm25(&page.chunks, query.unwrap_or(""), 1.2, 0.75)).unwrap(),
        "highlight" => serde_json::to_vec(&highlight_indices(&page.chunks, query.unwrap_or(""), 5)).unwrap(),
        _ => export_jsonl_with_limit(page, base, 17, query, 5,
            input["limit"].as_u64().unwrap_or(16 * 1024 * 1024) as usize).unwrap().into_bytes(),
    }
}

fn main() {
    let args: Vec<_> = env::args().collect();
    assert_eq!(args.len(), 4);
    let stage = args[1].as_str();
    assert!(matches!(stage, "parse" | "export" | "pipeline" | "rank" | "highlight" | "budget"));
    let input: Value = serde_json::from_slice(&fs::read(&args[2]).unwrap()).unwrap();
    let iterations: usize = args[3].parse().unwrap();
    assert!(iterations <= 250);
    let base = Url::parse("https://example.org/").unwrap();
    let html = input["html"].as_str().unwrap();
    assert!(html.len() <= 4 * 1024 * 1024);
    let mut page = parse_html(html, &base, 250_000, 1200).unwrap();
    if stage == "budget" {
        let bytes = input["oversized_text_bytes"].as_u64().unwrap() as usize;
        assert!(bytes <= 32 * 1024 * 1024 && !page.chunks.is_empty());
        page.chunks[0].text = "\"".repeat(bytes);
        let result = export_jsonl_with_limit(&page, &base, 17, None, 0, 1024);
        assert!(result.is_err(), "Oversized output must be rejected");
        println!("{}", json!({"status":"EXPECTED_ERROR","error":"output_budget"}));
        return;
    }
    let reference = payload(stage, &page, &base, &input);
    if iterations == 0 {
        print!("{}", String::from_utf8(reference).unwrap());
        return;
    }
    let run = || {
        if matches!(stage, "parse" | "pipeline") {
            let parsed = parse_html(black_box(html), &base, 250_000, 1200).unwrap();
            if stage == "parse" {
                black_box(parsed).chunks.len()
            } else {
                black_box(export_jsonl_with_limit(&parsed, &base, 17, input["query"].as_str(), 5,
                    16 * 1024 * 1024).unwrap()).len()
            }
        } else if stage == "export" {
            black_box(export_jsonl_with_limit(black_box(&page), &base, 17, input["query"].as_str(), 5,
                16 * 1024 * 1024).unwrap()).len()
        } else if stage == "rank" {
            black_box(bm25(black_box(&page.chunks), input["query"].as_str().unwrap_or(""), 1.2, 0.75)).len()
        } else {
            black_box(highlight_indices(black_box(&page.chunks), input["query"].as_str().unwrap_or(""), 5)).len()
        }
    };
    for _ in 0..10 { black_box(run()); }
    let mut raw_ns = Vec::with_capacity(iterations);
    let mut offsets_ns = Vec::with_capacity(iterations);
    let start = Instant::now();
    let mut size = 0;
    for _ in 0..iterations {
        let before = Instant::now();
        size = black_box(run());
        let ns = before.elapsed().as_nanos();
        assert!(ns > 0);
        raw_ns.push(ns);
        offsets_ns.push(before.duration_since(start).as_nanos());
    }
    println!("{}", json!({"protocol":"athena-cloud-native/1","stage":stage,
        "iterations":iterations,"raw_ns":raw_ns,"offsets_ns":offsets_ns,"output_size":size,
        "fingerprint":blake3::hash(&reference).to_hex().to_string(),"warmup":10}));
}
