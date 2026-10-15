//! Research harness: warm CPU stages, original sample order, setup outside timing.
use athena_core_v2::{
    aae::export_jsonl,
    projection::{Chunk, parse_html},
    rank::bm25,
    table::{Cell, Row, expand},
};
use serde_json::json;
use std::{env, fs, hint::black_box, time::Instant};
use url::Url;

fn main() {
    let args: Vec<_> = env::args().collect();
    let stage = args.get(1).cloned().unwrap_or_else(|| "rank".to_owned());
    let iterations: usize = args
        .get(3)
        .map(|value| value.parse().unwrap())
        .unwrap_or(100);
    assert!((1..=100_000).contains(&iterations));
    let external = stage == "fixture";
    let size = if external {
        0
    } else {
        args.get(2)
            .map(|value| value.parse::<usize>().unwrap())
            .unwrap_or(1000)
    };
    assert!(external || (1..=20_000).contains(&size));
    assert!(matches!(
        stage.as_str(),
        "rank" | "table" | "parse" | "export" | "pipeline" | "pipeline_no_query" | "fixture"
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
    let html = if external {
        fs::read_to_string(&args[2]).unwrap()
    } else {
        chunks
            .iter()
            .enumerate()
            .map(|(i, c)| format!("<p>{}<a href='/source/{i}'>source</a></p>", c.text))
            .collect::<String>()
    };
    let page = (stage == "export").then(|| parse_html(&html, &base, 250_000, 1200).unwrap());
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
    let query = if matches!(stage.as_str(), "pipeline_no_query" | "fixture") {
        None
    } else {
        Some("tokio runtime")
    };
    let run = || match stage.as_str() {
        "rank" => black_box(bm25(black_box(&chunks), "tokio runtime", 1.2, 0.75)).len(),
        "table" => black_box(expand(black_box(&rows), size, 64).unwrap())
            .cells
            .len(),
        "parse" => black_box(parse_html(black_box(&html), &base, 250_000, 1200).unwrap())
            .chunks
            .len(),
        "export" => {
            black_box(export_jsonl(black_box(page.as_ref().unwrap()), &base, 0, query, 5).unwrap())
                .len()
        }
        _ => {
            let parsed = parse_html(black_box(&html), &base, 250_000, 1200).unwrap();
            black_box(export_jsonl(&parsed, &base, 0, query, 5).unwrap()).len()
        }
    };
    let oracle_payload = match stage.as_str() {
        "rank" => serde_json::to_vec(&bm25(&chunks, "tokio runtime", 1.2, 0.75)).unwrap(),
        "table" => {
            let grid = expand(&rows, size, 64).unwrap();
            serde_json::to_vec(&json!({"slots": grid.slots, "width": grid.width,
                "texts": grid.cells.iter().map(|c| &c.text).collect::<Vec<_>>()}))
            .unwrap()
        }
        "parse" => {
            let parsed = parse_html(&html, &base, 250_000, 1200).unwrap();
            serde_json::to_vec(&json!({"chunks": parsed.chunks.iter().map(|c|
                json!({"role": c.role, "text": c.text, "links": c.links})).collect::<Vec<_>>(),
                "links": parsed.links}))
            .unwrap()
        }
        _ => {
            let parsed = parse_html(&html, &base, 250_000, 1200).unwrap();
            export_jsonl(&parsed, &base, 0, query, 5)
                .unwrap()
                .into_bytes()
        }
    };
    let fingerprint = blake3::hash(&oracle_payload).to_hex().to_string();
    for _ in 0..10 {
        black_box(run());
    }
    let mut raw_ns = Vec::with_capacity(iterations);
    let mut offsets_ns = Vec::with_capacity(iterations);
    let mut output_size = 0;
    let origin = Instant::now();
    for _ in 0..iterations {
        let start = Instant::now();
        output_size = black_box(run());
        let elapsed = start.elapsed().as_nanos();
        let offset = start.duration_since(origin).as_nanos();
        assert!(elapsed > 0);
        raw_ns.push(elapsed);
        offsets_ns.push(offset);
    }
    let mut sorted = raw_ns.clone();
    sorted.sort_unstable();
    let percentile = |q: f64| sorted[((iterations - 1) as f64 * q).ceil() as usize] as f64 / 1e6;
    println!(
        "{}",
        json!({
            "protocol": "athena-research/2", "stage": stage, "size": size,
            "iterations": iterations, "fixture_bytes": html.len(), "output_size": output_size,
            "fingerprint": fingerprint, "raw_ns": raw_ns, "sample_offsets_ns": offsets_ns,
            "p50_ms": percentile(0.50), "p95_ms": percentile(0.95), "p99_ms": percentile(0.99),
            "summary_quantile_method": "ceil_index; v2 analysis uses raw samples and declares its own estimator",
            "warmup": 10, "pid": std::process::id()
        })
    );
}
