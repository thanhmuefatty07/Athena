//! Mode A: 1-P-Core Deterministic CPU Twin (`QOS_CLASS_USER_INTERACTIVE` / `sched_setaffinity`).
//! Strictly ZERO `#[global_allocator]` wrapper and zero harness heap allocations inside the timed loop.

use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile};
use athena_core_v2::projection::{
    BoilerplatePolicy, ParseLimits, parse_html_breakthrough, parse_html_with_limits,
};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, Limits as LexborLimits, parse_and_project_pure, parse_tree_with_profile,
};
use serde_json::json;
use std::env;
use std::fs;
use std::hint::black_box;
use std::time::Instant;
use url::Url;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Contender {
    Legacy,
    Breakthrough,
    LexborPure,
    LexborMinimal,
    LexborStageIr,
}

impl Contender {
    fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "legacy" => Ok(Self::Legacy),
            "breakthrough" => Ok(Self::Breakthrough),
            "lexbor_pure" => Ok(Self::LexborPure),
            "lexbor_minimal" => Ok(Self::LexborMinimal),
            "lexbor_stage_ir" => Ok(Self::LexborStageIr),
            other => Err(format!("unknown contender '{other}'")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Legacy => "legacy",
            Self::Breakthrough => "breakthrough",
            Self::LexborPure => "lexbor_pure",
            Self::LexborMinimal => "lexbor_minimal",
            Self::LexborStageIr => "lexbor_stage_ir",
        }
    }
}

#[inline(never)]
fn run_contender_once(
    contender: Contender,
    html_str: &str,
    html_bytes: &[u8],
    base_url: &Url,
    base_url_str: &str,
    core_limits: ParseLimits,
    lxb_limits: LexborLimits,
) -> usize {
    match contender {
        Contender::Legacy => parse_html_with_limits(black_box(html_str), base_url, core_limits)
            .map(|p| black_box(p.chunks.len()))
            .unwrap_or(0),
        Contender::Breakthrough => {
            parse_html_breakthrough(black_box(html_str), base_url, core_limits)
                .map(|p| black_box(p.projection.chunks.len()))
                .unwrap_or(0)
        }
        Contender::LexborPure => {
            parse_and_project_pure(black_box(html_bytes), base_url_str, lxb_limits)
                .map(|p| black_box(p.chunks.len()))
                .unwrap_or(0)
        }
        Contender::LexborMinimal => {
            parse_tree_with_profile(black_box(html_bytes), LexborAttrProfile::Minimal)
                .map(|t| black_box(t.nodes.len()))
                .unwrap_or(0)
        }
        Contender::LexborStageIr => {
            parse_tree_with_profile(black_box(html_bytes), LexborAttrProfile::StageIr)
                .map(|t| black_box(t.nodes.len()))
                .unwrap_or(0)
        }
    }
}

fn quantile_sorted(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn lag1_autocorrelation(xs: &[f64]) -> f64 {
    if xs.len() < 3 {
        return 0.0;
    }
    let n = xs.len() as f64;
    let mean = xs.iter().sum::<f64>() / n;
    let denom: f64 = xs.iter().map(|v| (v - mean).powi(2)).sum();
    if denom <= 1e-18 {
        return 0.0;
    }
    let num: f64 = xs.windows(2).map(|w| (w[0] - mean) * (w[1] - mean)).sum();
    (num / denom).clamp(-1.0, 1.0)
}

fn default_fixture_html() -> String {
    let mut s = String::from(
        "<!doctype html><html><head><title>Mode A Deterministic CPU Twin</title></head><body><main><article><h1>Deterministic Microbenchmark</h1>",
    );
    for i in 0..40 {
        s.push_str(&format!(
            "<section><h2>Section {i}</h2><p>Firestorm P-core paired A-B-B-A macroblock evaluation paragraph {i} with <a href=\"https://bench.invalid/s/{i}\">reference {i}</a>.</p></section>"
        ));
    }
    s.push_str("<table><thead><tr><th>Arm</th><th>p50</th><th>p99</th></tr></thead><tbody><tr><td>A</td><td>12.4</td><td>16.8</td></tr><tr><td>B</td><td>9.1</td><td>11.9</td></tr></tbody></table></article></main></body></html>");
    s
}

fn main() -> Result<(), String> {
    let mut arm_a = Contender::Legacy;
    let mut arm_b = Contender::LexborPure;
    let mut macroblocks: usize = 64;
    let mut warmup_blocks: usize = 8;
    let mut inner_iters: usize = 2;
    let mut cpu_slot: usize = 0;
    let mut html_path: Option<String> = None;
    let mut self_test = false;

    let args: Vec<String> = env::args().collect();
    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--self-test" => self_test = true,
            "--arm-a" => {
                i += 1;
                arm_a = Contender::parse(args.get(i).ok_or("missing --arm-a value")?)?;
            }
            "--arm-b" => {
                i += 1;
                arm_b = Contender::parse(args.get(i).ok_or("missing --arm-b value")?)?;
            }
            "--macroblocks" => {
                i += 1;
                macroblocks = args
                    .get(i)
                    .ok_or("missing --macroblocks")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?
                    .max(64);
            }
            "--warmup-blocks" => {
                i += 1;
                warmup_blocks = args
                    .get(i)
                    .ok_or("missing --warmup-blocks")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?;
            }
            "--inner-iters" => {
                i += 1;
                inner_iters = args
                    .get(i)
                    .ok_or("missing --inner-iters")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?
                    .max(1);
            }
            "--cpu" => {
                i += 1;
                cpu_slot = args
                    .get(i)
                    .ok_or("missing --cpu")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?;
            }
            "--html-file" => {
                i += 1;
                html_path = Some(args.get(i).ok_or("missing --html-file")?.clone());
            }
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    let qos = apply_qos_profile(QosProfile::Interactive, Some(cpu_slot));
    let html_bytes = if let Some(path) = &html_path {
        fs::read(path).map_err(|e| format!("failed to read {path}: {e}"))?
    } else {
        default_fixture_html().into_bytes()
    };
    let html_str = String::from_utf8_lossy(&html_bytes).into_owned();
    let base_url_str = "https://bench.invalid/doc";
    let base_url = Url::parse(base_url_str).map_err(|e| e.to_string())?;
    let core_limits = ParseLimits {
        boilerplate: BoilerplatePolicy::MainContentV1,
        ..ParseLimits::default()
    };
    let lxb_limits = LexborLimits::default();

    // Pre-allocate fixed buffers outside the timed macroblock loop (zero harness heap allocations inside loop).
    let mut ns_a = vec![0u64; macroblocks];
    let mut ns_b = vec![0u64; macroblocks];
    let mut sink = 0usize;

    for wb in 0..warmup_blocks {
        if wb % 2 == 0 {
            for _ in 0..inner_iters {
                sink ^= run_contender_once(
                    arm_a,
                    &html_str,
                    &html_bytes,
                    &base_url,
                    base_url_str,
                    core_limits,
                    lxb_limits,
                );
                sink ^= run_contender_once(
                    arm_b,
                    &html_str,
                    &html_bytes,
                    &base_url,
                    base_url_str,
                    core_limits,
                    lxb_limits,
                );
            }
        } else {
            for _ in 0..inner_iters {
                sink ^= run_contender_once(
                    arm_b,
                    &html_str,
                    &html_bytes,
                    &base_url,
                    base_url_str,
                    core_limits,
                    lxb_limits,
                );
                sink ^= run_contender_once(
                    arm_a,
                    &html_str,
                    &html_bytes,
                    &base_url,
                    base_url_str,
                    core_limits,
                    lxb_limits,
                );
            }
        }
    }

    // Paired A-B-B-A macroblocks (each macroblock executes A-B-B-A to cancel linear drift).
    for mb in 0..macroblocks {
        let t_a1 = Instant::now();
        for _ in 0..inner_iters {
            sink ^= run_contender_once(
                arm_a,
                &html_str,
                &html_bytes,
                &base_url,
                base_url_str,
                core_limits,
                lxb_limits,
            );
        }
        let d_a1 = t_a1.elapsed().as_nanos() as u64;

        let t_b1 = Instant::now();
        for _ in 0..inner_iters {
            sink ^= run_contender_once(
                arm_b,
                &html_str,
                &html_bytes,
                &base_url,
                base_url_str,
                core_limits,
                lxb_limits,
            );
        }
        let d_b1 = t_b1.elapsed().as_nanos() as u64;

        let t_b2 = Instant::now();
        for _ in 0..inner_iters {
            sink ^= run_contender_once(
                arm_b,
                &html_str,
                &html_bytes,
                &base_url,
                base_url_str,
                core_limits,
                lxb_limits,
            );
        }
        let d_b2 = t_b2.elapsed().as_nanos() as u64;

        let t_a2 = Instant::now();
        for _ in 0..inner_iters {
            sink ^= run_contender_once(
                arm_a,
                &html_str,
                &html_bytes,
                &base_url,
                base_url_str,
                core_limits,
                lxb_limits,
            );
        }
        let d_a2 = t_a2.elapsed().as_nanos() as u64;

        ns_a[mb] = d_a1.saturating_add(d_a2);
        ns_b[mb] = d_b1.saturating_add(d_b2);
    }
    black_box(sink);

    let kib = (html_bytes.len().max(1) as f64) / 1024.0;
    let calls_per_block = (2 * inner_iters) as f64;
    let mut us_kib_a: Vec<f64> = ns_a
        .iter()
        .map(|&ns| (ns as f64 / 1000.0) / calls_per_block / kib)
        .collect();
    let mut us_kib_b: Vec<f64> = ns_b
        .iter()
        .map(|&ns| (ns as f64 / 1000.0) / calls_per_block / kib)
        .collect();
    let paired_deltas: Vec<f64> = us_kib_a
        .iter()
        .zip(us_kib_b.iter())
        .map(|(a, b)| b - a)
        .collect();
    let r_lag1 = lag1_autocorrelation(&paired_deltas);

    us_kib_a.sort_by(|x, y| x.total_cmp(y));
    us_kib_b.sort_by(|x, y| x.total_cmp(y));

    if self_test {
        if macroblocks < 64 {
            return Err("Mode A requires >= 64 A-B-B-A macroblocks".into());
        }
        if us_kib_a[0] <= 0.0 || us_kib_b[0] <= 0.0 {
            return Err("Mode A measured non-positive latency".into());
        }
    }

    let report = json!({
        "schema": "AthenaCloudBenchModeACpu/1",
        "global_alloc_wrapped": false,
        "qos": qos,
        "html_bytes": html_bytes.len(),
        "macroblocks": macroblocks,
        "warmup_blocks": warmup_blocks,
        "inner_iters": inner_iters,
        "arm_a": {
            "name": arm_a.as_str(),
            "us_per_kib_p50": quantile_sorted(&us_kib_a, 0.50),
            "us_per_kib_p95": quantile_sorted(&us_kib_a, 0.95),
            "us_per_kib_p99": quantile_sorted(&us_kib_a, 0.99),
        },
        "arm_b": {
            "name": arm_b.as_str(),
            "us_per_kib_p50": quantile_sorted(&us_kib_b, 0.50),
            "us_per_kib_p95": quantile_sorted(&us_kib_b, 0.95),
            "us_per_kib_p99": quantile_sorted(&us_kib_b, 0.99),
        },
        "paired_delta_b_minus_a_mean_us_per_kib": paired_deltas.iter().sum::<f64>() / (paired_deltas.len() as f64),
        "r_lag1": r_lag1,
        "stationarity_gate_abs_r_lag1_lt_0_5": r_lag1.abs() < 0.5,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
    );
    Ok(())
}
