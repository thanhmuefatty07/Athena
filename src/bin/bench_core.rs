//! Offline stage benchmarks; setup is excluded and outputs are consumed.
use athena_core_v2::{
    aae::export_jsonl,
    projection::{Chunk, parse_html},
    rank::bm25,
    table::{Cell, Row, expand},
};
use std::{env, hint::black_box, time::Instant};
use url::Url;

fn main() {
    let args: Vec<_> = env::args().collect();
    let stage = args.get(1).map(String::as_str).unwrap_or("rank");
    let size: usize = args.get(2).map(|v| v.parse().unwrap()).unwrap_or(1000);
    let iterations: usize = args.get(3).map(|v| v.parse().unwrap()).unwrap_or(100);
    assert!((1..=20_000).contains(&size) && (1..=100_000).contains(&iterations));
    assert!(matches!(
        stage,
        "rank" | "table" | "parse" | "export" | "pipeline"
    ));
    let base = Url::parse("https://example.org/").unwrap();
    let chunks: Vec<_> = (0..size)
        .map(|i| Chunk {
            node: i as u32,
            role: "paragraph",
            text: format!(
                "Document {i}: CPU memory network latency throughput. {} {}",
                "ordinary multilingual tài liệu 内容 details and reproducible evidence ".repeat(8),
                if i % 17 == 0 {
                    "tokio runtime"
                } else {
                    "unrelated topic"
                }
            ),
            links: vec![],
        })
        .collect();
    let html: String = chunks
        .iter()
        .enumerate()
        .map(|(i, c)| format!("<p>{}<a href='/source/{i}'>source</a></p>", c.text))
        .collect();
    let page = parse_html(&html, &base, 250_000, 1200).unwrap();
    let rows: Vec<_> = (0..size)
        .map(|i| Row {
            group: 0,
            cells: vec![Cell {
                text: i.to_string(),
                rowspan: 1,
                colspan: 1,
                is_header: false,
            }],
        })
        .collect();
    let run = || match stage {
        "rank" => black_box(bm25(black_box(&chunks), "tokio runtime", 1.2, 0.75)).len(),
        "table" => black_box(expand(black_box(&rows), size, 64).unwrap())
            .cells
            .len(),
        "parse" => black_box(parse_html(black_box(&html), &base, 250_000, 1200).unwrap())
            .chunks
            .len(),
        "export" => {
            black_box(export_jsonl(black_box(&page), &base, 0, Some("tokio runtime"), 5).unwrap())
                .len()
        }
        "pipeline" => {
            let parsed = parse_html(black_box(&html), &base, 250_000, 1200).unwrap();
            black_box(export_jsonl(&parsed, &base, 0, Some("tokio runtime"), 5).unwrap()).len()
        }
        _ => unreachable!(),
    };
    for _ in 0..10 {
        black_box(run());
    }
    let mut samples = Vec::with_capacity(iterations);
    let mut output_size = 0;
    for _ in 0..iterations {
        let start = Instant::now();
        output_size = black_box(run());
        samples.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    samples.sort_by(f64::total_cmp);
    let percentile = |p: f64| samples[((iterations - 1) as f64 * p).ceil() as usize];
    println!(
        "{}",
        serde_json::json!({
            "stage": stage, "size": size, "iterations": iterations,
            "fixture_bytes": html.len(), "output_size": output_size,
            "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99)
        })
    );
}
