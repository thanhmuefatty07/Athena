//! Mode A: 1-P-Core Deterministic CPU Twin (`QOS_CLASS_USER_INTERACTIVE` / `sched_setaffinity`).
//! Strictly ZERO `#[global_allocator]` wrapper and zero harness heap allocations inside the timed loop.

use athena_cloudbench::bench_common::{
    CharsetMode, Contender, DEFAULT_MAX_INPUT_BYTES, build_parse_limits, decode_html_bytes,
    effective_limits, validate_real_url,
};
use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile};
use athena_core_v2::projection::{ParseLimits, parse_html_breakthrough, parse_html_with_limits};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, LexborCharsetMode, Limits as LexborLimits,
    parse_and_project_pure_with_charset, parse_tree_with_limits_profile_and_charset,
};
use serde_json::json;
use std::env;
use std::fs;
use std::hint::black_box;
use std::time::Instant;
use url::Url;

#[derive(Clone, Debug, PartialEq, Eq)]
struct CpuCliConfig {
    arm_a: Contender,
    arm_b: Contender,
    contender: Option<Contender>,
    url: Option<String>,
    charset_mode: CharsetMode,
    max_input_bytes: usize,
    threads: usize,
    pin_cpu: Option<usize>,
    qos: QosProfile,
    macroblocks: usize,
    warmup_blocks: usize,
    inner_iters: usize,
    html_path: Option<String>,
    self_test: bool,
}

fn parse_pin_cpu(raw: &str) -> Result<Option<usize>, String> {
    let trimmed = raw.trim();
    if trimmed.eq_ignore_ascii_case("none") || trimmed.eq_ignore_ascii_case("off") {
        Ok(None)
    } else {
        trimmed
            .parse::<usize>()
            .map(Some)
            .map_err(|e| format!("invalid --pin-cpu '{trimmed}': {e}"))
    }
}

fn parse_cli_args(args: &[String]) -> Result<CpuCliConfig, String> {
    let mut arm_a = Contender::Legacy;
    let mut arm_b = Contender::LexborPure;
    let mut saw_arm_a = false;
    let mut saw_arm_b = false;
    let mut contender: Option<Contender> = None;
    let mut url: Option<String> = None;
    let mut charset_mode = CharsetMode::Utf8Assumed;
    let mut max_input_bytes = DEFAULT_MAX_INPUT_BYTES;
    let mut threads = 1usize;
    let mut pin_cpu: Option<usize> = Some(0);
    let mut qos = QosProfile::Interactive;
    let mut macroblocks: usize = 64;
    let mut warmup_blocks: usize = 8;
    let mut inner_iters: usize = 2;
    let mut html_path: Option<String> = None;
    let mut self_test = false;

    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--self-test" => self_test = true,
            "--contender" => {
                i += 1;
                contender = Some(Contender::parse(
                    args.get(i).ok_or("missing --contender value")?,
                )?);
            }
            "--arm-a" => {
                i += 1;
                arm_a = Contender::parse(args.get(i).ok_or("missing --arm-a value")?)?;
                saw_arm_a = true;
            }
            "--arm-b" => {
                i += 1;
                arm_b = Contender::parse(args.get(i).ok_or("missing --arm-b value")?)?;
                saw_arm_b = true;
            }
            "--url" => {
                i += 1;
                url = Some(args.get(i).ok_or("missing --url value")?.clone());
            }
            "--charset-mode" => {
                i += 1;
                charset_mode =
                    CharsetMode::parse(args.get(i).ok_or("missing --charset-mode value")?)?;
            }
            "--max-input-bytes" => {
                i += 1;
                max_input_bytes = args
                    .get(i)
                    .ok_or("missing --max-input-bytes value")?
                    .parse::<usize>()
                    .map_err(|e| format!("invalid --max-input-bytes: {e}"))?;
            }
            "--threads" => {
                i += 1;
                threads = args
                    .get(i)
                    .ok_or("missing --threads value")?
                    .parse::<usize>()
                    .map_err(|e| format!("invalid --threads: {e}"))?;
            }
            "--cpu" | "--pin-cpu" => {
                i += 1;
                pin_cpu = parse_pin_cpu(args.get(i).ok_or("missing --cpu/--pin-cpu value")?)?;
            }
            "--qos" => {
                i += 1;
                qos = QosProfile::parse(args.get(i).ok_or("missing --qos value")?)?;
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
            "--html-file" => {
                i += 1;
                html_path = Some(args.get(i).ok_or("missing --html-file")?.clone());
            }
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    if threads != 1 {
        return Err(format!(
            "mode_a_cpu enforces single-thread execution (--threads 1), got {threads}"
        ));
    }
    let _ = build_parse_limits(max_input_bytes)?;

    if let Some(c) = contender {
        if !saw_arm_a && !saw_arm_b {
            arm_a = c;
            arm_b = c;
        } else if !saw_arm_b {
            arm_b = c;
        }
    }

    if !self_test {
        let raw_url = url
            .as_deref()
            .ok_or("missing required --url <real_url> (forbidden placeholder defaults)")?;
        let _ = validate_real_url(raw_url)?;
    } else if let Some(raw_url) = url.as_deref() {
        let _ = validate_real_url(raw_url)?;
    }

    Ok(CpuCliConfig {
        arm_a,
        arm_b,
        contender,
        url,
        charset_mode,
        max_input_bytes,
        threads,
        pin_cpu,
        qos,
        macroblocks,
        warmup_blocks,
        inner_iters,
        html_path,
        self_test,
    })
}

#[inline(never)]
#[allow(clippy::too_many_arguments)]
fn run_contender_once(
    contender: Contender,
    html_str: &str,
    html_bytes: &[u8],
    base_url: &Url,
    base_url_str: &str,
    core_limits: ParseLimits,
    lxb_limits: LexborLimits,
    lxb_charset: LexborCharsetMode,
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
        Contender::LexborPure => parse_and_project_pure_with_charset(
            black_box(html_bytes),
            &[],
            base_url_str,
            lxb_limits,
            lxb_charset,
        )
        .map(|p| black_box(p.chunks.len()))
        .unwrap_or(0),
        Contender::LexborMinimal => parse_tree_with_limits_profile_and_charset(
            black_box(html_bytes),
            &[],
            lxb_limits,
            LexborAttrProfile::Minimal,
            lxb_charset,
        )
        .map(|t| black_box(t.nodes.len()))
        .unwrap_or(0),
        Contender::LexborStageIr => parse_tree_with_limits_profile_and_charset(
            black_box(html_bytes),
            &[],
            lxb_limits,
            LexborAttrProfile::StageIr,
            lxb_charset,
        )
        .map(|t| black_box(t.nodes.len()))
        .unwrap_or(0),
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

fn self_test_fixture_html() -> String {
    let mut s = String::from(
        "<!doctype html><html><head><title>Mode A Deterministic CPU Twin</title></head><body><main><article><h1>Deterministic Microbenchmark</h1>",
    );
    for i in 0..40 {
        s.push_str(&format!(
            "<section><h2>Section {i}</h2><p>Firestorm P-core paired A-B-B-A macroblock evaluation paragraph {i} with <a href=\"https://en.wikipedia.org/wiki/Doc_{i}\">reference {i}</a>.</p></section>"
        ));
    }
    s.push_str("<table><thead><tr><th>Arm</th><th>p50</th><th>p99</th></tr></thead><tbody><tr><td>A</td><td>12.4</td><td>16.8</td></tr><tr><td>B</td><td>9.1</td><td>11.9</td></tr></tbody></table></article></main></body></html>");
    s
}

fn main() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    let cfg = parse_cli_args(&args)?;

    let qos = apply_qos_profile(cfg.qos, cfg.pin_cpu);
    let html_bytes = if let Some(path) = &cfg.html_path {
        fs::read(path).map_err(|e| format!("failed to read {path}: {e}"))?
    } else if cfg.self_test {
        self_test_fixture_html().into_bytes()
    } else {
        return Err("missing required --html-file <path> outside --self-test".to_owned());
    };

    let url_str = cfg
        .url
        .as_deref()
        .unwrap_or("https://en.wikipedia.org/wiki/Mode_a_cpu");
    let base_url = validate_real_url(url_str)?;

    let base_limits = build_parse_limits(cfg.max_input_bytes)?;
    let html_decoded = decode_html_bytes(&html_bytes, &[], cfg.max_input_bytes, cfg.charset_mode)?;
    let html_str = html_decoded.as_ref();
    let core_limits = effective_limits(base_limits, html_str.len());
    let lxb_limits = base_limits;
    let lxb_charset = cfg.charset_mode.to_lexbor_charset_mode();

    // Pre-allocate fixed buffers outside the timed macroblock loop (zero harness heap allocations inside loop).
    let mut ns_a = vec![0u64; cfg.macroblocks];
    let mut ns_b = vec![0u64; cfg.macroblocks];
    let mut sink = 0usize;

    for wb in 0..cfg.warmup_blocks {
        if wb % 2 == 0 {
            for _ in 0..cfg.inner_iters {
                sink ^= run_contender_once(
                    cfg.arm_a,
                    html_str,
                    &html_bytes,
                    &base_url,
                    url_str,
                    core_limits,
                    lxb_limits,
                    lxb_charset,
                );
                sink ^= run_contender_once(
                    cfg.arm_b,
                    html_str,
                    &html_bytes,
                    &base_url,
                    url_str,
                    core_limits,
                    lxb_limits,
                    lxb_charset,
                );
            }
        } else {
            for _ in 0..cfg.inner_iters {
                sink ^= run_contender_once(
                    cfg.arm_b,
                    html_str,
                    &html_bytes,
                    &base_url,
                    url_str,
                    core_limits,
                    lxb_limits,
                    lxb_charset,
                );
                sink ^= run_contender_once(
                    cfg.arm_a,
                    html_str,
                    &html_bytes,
                    &base_url,
                    url_str,
                    core_limits,
                    lxb_limits,
                    lxb_charset,
                );
            }
        }
    }

    // Paired A-B-B-A macroblocks (each macroblock executes A-B-B-A to cancel linear drift).
    for mb in 0..cfg.macroblocks {
        let t_a1 = Instant::now();
        for _ in 0..cfg.inner_iters {
            sink ^= run_contender_once(
                cfg.arm_a,
                html_str,
                &html_bytes,
                &base_url,
                url_str,
                core_limits,
                lxb_limits,
                lxb_charset,
            );
        }
        let d_a1 = t_a1.elapsed().as_nanos() as u64;

        let t_b1 = Instant::now();
        for _ in 0..cfg.inner_iters {
            sink ^= run_contender_once(
                cfg.arm_b,
                html_str,
                &html_bytes,
                &base_url,
                url_str,
                core_limits,
                lxb_limits,
                lxb_charset,
            );
        }
        let d_b1 = t_b1.elapsed().as_nanos() as u64;

        let t_b2 = Instant::now();
        for _ in 0..cfg.inner_iters {
            sink ^= run_contender_once(
                cfg.arm_b,
                html_str,
                &html_bytes,
                &base_url,
                url_str,
                core_limits,
                lxb_limits,
                lxb_charset,
            );
        }
        let d_b2 = t_b2.elapsed().as_nanos() as u64;

        let t_a2 = Instant::now();
        for _ in 0..cfg.inner_iters {
            sink ^= run_contender_once(
                cfg.arm_a,
                html_str,
                &html_bytes,
                &base_url,
                url_str,
                core_limits,
                lxb_limits,
                lxb_charset,
            );
        }
        let d_a2 = t_a2.elapsed().as_nanos() as u64;

        ns_a[mb] = d_a1.saturating_add(d_a2);
        ns_b[mb] = d_b1.saturating_add(d_b2);
    }
    black_box(sink);

    let kib = (html_bytes.len().max(1) as f64) / 1024.0;
    let calls_per_block = (2 * cfg.inner_iters) as f64;
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

    if cfg.self_test {
        if cfg.macroblocks < 64 {
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
        "boilerplate_policy": "MainContentV1",
        "contender": cfg.contender.map(Contender::as_str),
        "url": url_str,
        "charset_mode": cfg.charset_mode.as_str(),
        "max_input_bytes": cfg.max_input_bytes,
        "threads": cfg.threads,
        "pin_cpu": cfg.pin_cpu,
        "html_bytes": html_bytes.len(),
        "macroblocks": cfg.macroblocks,
        "warmup_blocks": cfg.warmup_blocks,
        "inner_iters": cfg.inner_iters,
        "arm_a": {
            "name": cfg.arm_a.as_str(),
            "us_per_kib_p50": quantile_sorted(&us_kib_a, 0.50),
            "us_per_kib_p95": quantile_sorted(&us_kib_a, 0.95),
            "us_per_kib_p99": quantile_sorted(&us_kib_a, 0.99),
        },
        "arm_b": {
            "name": cfg.arm_b.as_str(),
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

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|v| (*v).to_owned()).collect()
    }

    #[test]
    fn cli_rejects_missing_or_placeholder_url_and_multi_thread() {
        assert!(parse_cli_args(&s(&["mode_a_cpu"])).is_err());
        assert!(parse_cli_args(&s(&["mode_a_cpu", "--url", "https://bench.invalid/x"])).is_err());
        assert!(parse_cli_args(&s(&["mode_a_cpu", "--url", "https://example.org/x"])).is_err());
        assert!(
            parse_cli_args(&s(&[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--threads",
                "2"
            ]))
            .is_err()
        );
    }

    #[test]
    fn cli_parses_contender_charset_max_input_bytes_and_pin_cpu() {
        let cfg = parse_cli_args(&s(&[
            "mode_a_cpu",
            "--contender",
            "lexbor_stage_ir",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--charset-mode",
            "sniffing_enabled",
            "--max-input-bytes",
            "33554432",
            "--threads",
            "1",
            "--pin-cpu",
            "3",
        ]))
        .expect("valid args");
        assert_eq!(cfg.contender, Some(Contender::LexborStageIr));
        assert_eq!(cfg.arm_a, Contender::LexborStageIr);
        assert_eq!(cfg.arm_b, Contender::LexborStageIr);
        assert_eq!(cfg.charset_mode, CharsetMode::SniffingEnabled);
        assert_eq!(cfg.max_input_bytes, 33_554_432);
        assert_eq!(cfg.pin_cpu, Some(3));
    }
}
