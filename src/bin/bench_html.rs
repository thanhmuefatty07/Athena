//! Local, deterministic microbenchmark. Never conflate with network p95.
//! cargo run --release --bin bench_html -- fixtures/example.html https://example.org/ 1000 query
use athena_core_v2::{aae::export_jsonl, projection::parse_html};
use std::{env,fs,hint::black_box,time::Instant};
use url::Url;
fn quantile_ms(samples: &[f64], p: f64) -> f64 {
    if samples.is_empty() { return f64::NAN; }
    let index = ((samples.len()-1) as f64*p).ceil() as usize;
    samples[index]
}
fn main() {
    let args:Vec<String> = env::args().collect();
    assert!(args.len() >= 4, "bench_html <input.html> <base-url> <iterations> [query]");
    let input = fs::read_to_string(&args[1]).expect("UTF8 fixture");
    let base = Url::parse(&args[2]).expect("base URL");
    let iters:usize = args[3].parse().expect("iterations");
    assert!((1..=100_000).contains(&iters));
    let query = args.get(4).map(String::as_str);
    for _ in 0..10 {
        let parsed = parse_html(black_box(&input),&base,250_000,1200).unwrap();
        black_box(export_jsonl(&parsed,&base,0,query,5).unwrap());
    }
    let mut samples = Vec::with_capacity(iters);
    let start_all = Instant::now();
    let mut bytes = 0usize;
    for _ in 0..iters {
        let start=Instant::now();
        let parsed=parse_html(black_box(&input),&base,250_000,1200).unwrap();
        let output=export_jsonl(&parsed,&base,0,query,5).unwrap();
        bytes=output.len();
        black_box(output);
        samples.push(start.elapsed().as_secs_f64()*1000.0);
    }
    let total = start_all.elapsed().as_secs_f64();
    samples.sort_by(|a,b|a.total_cmp(b));
    println!("{{\"fixture_bytes\":{},\"output_bytes\":{},\"iterations\":{},\"total_seconds\":{:.6},\"pages_per_second\":{:.3},\"p50_ms\":{:.6},\"p95_ms\":{:.6},\"p99_ms\":{:.6}}}",
        input.len(),bytes,iters,total,iters as f64/total,
        quantile_ms(&samples,0.50),quantile_ms(&samples,0.95),quantile_ms(&samples,0.99));
}
