//! Mode A: 1-P-Core Deterministic CPU Twin (`QOS_CLASS_USER_INTERACTIVE` / `sched_setaffinity`).
//! Strictly ZERO `#[global_allocator]` wrapper and zero harness heap allocations inside the timed loop.

use athena_cloudbench::bench_common::{
    CharsetMode, Contender, DEFAULT_MAX_INPUT_BYTES, build_parse_limits_with_policy,
    decode_html_bytes, effective_limits, parse_boilerplate_policy, validate_real_url,
};
use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile};
use athena_core_v2::projection::{
    BoilerplatePolicy, ParseLimits, parse_html_breakthrough, parse_html_with_limits,
};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, parse_and_project_str_pure_with_profile,
    parse_tree_str_with_limits_and_profile,
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
    boilerplate_policy: BoilerplatePolicy,
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MacroSlot {
    ArmA,
    ArmB,
}

#[inline]
const fn macroblock_slots(mb: usize) -> [MacroSlot; 4] {
    if mb.is_multiple_of(2) {
        [
            MacroSlot::ArmA,
            MacroSlot::ArmB,
            MacroSlot::ArmB,
            MacroSlot::ArmA,
        ]
    } else {
        [
            MacroSlot::ArmB,
            MacroSlot::ArmA,
            MacroSlot::ArmA,
            MacroSlot::ArmB,
        ]
    }
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
    let mut boilerplate_policy = BoilerplatePolicy::MainContentV1;
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
            "--boilerplate-policy" => {
                i += 1;
                boilerplate_policy = parse_boilerplate_policy(
                    args.get(i).ok_or("missing --boilerplate-policy value")?,
                )?;
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
                    .map_err(|e| format!("invalid --macroblocks: {e}"))?;
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
    if macroblocks < 64 || !macroblocks.is_multiple_of(2) {
        return Err(format!(
            "mode_a_cpu requires an even --macroblocks >= 64 for 1:1 ABBA/BAAB counter-balancing, got {macroblocks}"
        ));
    }
    if !warmup_blocks.is_multiple_of(2) {
        return Err(format!(
            "mode_a_cpu requires an even --warmup-blocks count for balanced ABBA/BAAB warmup, got {warmup_blocks}"
        ));
    }
    let _ = build_parse_limits_with_policy(max_input_bytes, boilerplate_policy)?;

    if let Some(c) = contender {
        match (saw_arm_a, saw_arm_b) {
            (false, false) => {
                arm_a = c;
                arm_b = c;
            }
            (true, false) => {
                arm_b = c;
            }
            (false, true) => {
                arm_a = c;
            }
            (true, true) => {
                if arm_a != c || arm_b != c {
                    return Err(format!(
                        "conflicting --contender '{}' with explicit --arm-a '{}' and --arm-b '{}'",
                        c.as_str(),
                        arm_a.as_str(),
                        arm_b.as_str()
                    ));
                }
            }
        }
    }

    if arm_a.stage() != arm_b.stage() {
        return Err(format!(
            "stage asymmetry forbidden in paired Mode A CPU comparison: arm_a '{}' is '{}', arm_b '{}' is '{}'",
            arm_a.as_str(),
            arm_a.stage(),
            arm_b.as_str(),
            arm_b.stage(),
        ));
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
        boilerplate_policy,
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
fn run_contender_once(
    contender: Contender,
    html_str: &str,
    base_url: &Url,
    core_limits: ParseLimits,
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
        Contender::LexborPure => parse_and_project_str_pure_with_profile(
            black_box(html_str),
            base_url,
            core_limits,
            LexborAttrProfile::Minimal,
        )
        .map(|p| black_box(p.chunks.len()))
        .unwrap_or(0),
        Contender::LexborStageIr => parse_and_project_str_pure_with_profile(
            black_box(html_str),
            base_url,
            core_limits,
            LexborAttrProfile::StageIr,
        )
        .map(|p| black_box(p.chunks.len()))
        .unwrap_or(0),
        Contender::LexborMinimal => parse_tree_str_with_limits_and_profile(
            black_box(html_str),
            core_limits,
            LexborAttrProfile::Minimal,
        )
        .map(|t| black_box(t.nodes.len()))
        .unwrap_or(0),
    }
}

#[inline(never)]
fn execute_counter_balanced_macroblocks<F>(
    macroblocks: usize,
    inner_iters: usize,
    ns_a: &mut [u64],
    ns_b: &mut [u64],
    mut run_slot_iter: F,
) -> usize
where
    F: FnMut(MacroSlot) -> usize,
{
    let mut sink = 0usize;
    for mb in 0..macroblocks {
        let mut acc_a = 0u64;
        let mut acc_b = 0u64;
        for slot in macroblock_slots(mb) {
            let t_slot = Instant::now();
            for _ in 0..inner_iters {
                sink ^= run_slot_iter(slot);
            }
            let elapsed_ns = t_slot.elapsed().as_nanos() as u64;
            match slot {
                MacroSlot::ArmA => acc_a = acc_a.saturating_add(elapsed_ns),
                MacroSlot::ArmB => acc_b = acc_b.saturating_add(elapsed_ns),
            }
        }
        ns_a[mb] = acc_a;
        ns_b[mb] = acc_b;
    }
    sink
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
            "<section><h2>Section {i}</h2><p>Firestorm P-core counter-balanced ABBA/BAAB macroblock evaluation paragraph {i} with <a href=\"https://en.wikipedia.org/wiki/Doc_{i}\">reference {i}</a>.</p></section>"
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

    let base_limits = build_parse_limits_with_policy(cfg.max_input_bytes, cfg.boilerplate_policy)?;
    let html_decoded = decode_html_bytes(&html_bytes, &[], cfg.max_input_bytes, cfg.charset_mode)?;
    let html_str = html_decoded.as_ref();
    let core_limits = effective_limits(base_limits, html_str.len());

    let mut ns_a = vec![0u64; cfg.macroblocks];
    let mut ns_b = vec![0u64; cfg.macroblocks];
    let mut sink = 0usize;

    for wb in 0..cfg.warmup_blocks {
        for slot in macroblock_slots(wb) {
            let contender = match slot {
                MacroSlot::ArmA => cfg.arm_a,
                MacroSlot::ArmB => cfg.arm_b,
            };
            for _ in 0..cfg.inner_iters {
                sink ^= run_contender_once(contender, html_str, &base_url, core_limits);
            }
        }
    }

    sink ^= execute_counter_balanced_macroblocks(
        cfg.macroblocks,
        cfg.inner_iters,
        &mut ns_a,
        &mut ns_b,
        |slot| {
            let contender = match slot {
                MacroSlot::ArmA => cfg.arm_a,
                MacroSlot::ArmB => cfg.arm_b,
            };
            run_contender_once(contender, html_str, &base_url, core_limits)
        },
    );
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
            return Err("Mode A requires >= 64 counter-balanced ABBA/BAAB macroblocks".into());
        }
        if us_kib_a[0] <= 0.0 || us_kib_b[0] <= 0.0 {
            return Err("Mode A measured non-positive latency".into());
        }
        let stage_ir_chunks =
            run_contender_once(Contender::LexborStageIr, html_str, &base_url, core_limits);
        let pure_chunks =
            run_contender_once(Contender::LexborPure, html_str, &base_url, core_limits);
        if stage_ir_chunks == 0 || stage_ir_chunks != pure_chunks {
            return Err(format!(
                "LexborStageIr full projection chunk mismatch vs LexborPure: stage_ir={stage_ir_chunks}, pure={pure_chunks}"
            ));
        }
    }

    let effective_policy = cfg
        .arm_a
        .effective_boilerplate_policy_str(cfg.boilerplate_policy);
    let report = json!({
        "schema": "AthenaCloudBenchModeACpu/1",
        "global_alloc_wrapped": false,
        "qos": qos,
        "stage": cfg.arm_a.stage(),
        "boilerplate_policy": effective_policy,
        "utf8_decode_placement": "pre_decoded_outside_timed_region",
        "macroblock_schedule": "ABBA_BAAB_alternating",
        "contender": cfg
            .contender
            .filter(|&c| cfg.arm_a == c && cfg.arm_b == c)
            .map(Contender::as_str),
        "url": url_str,
        "charset_mode": cfg.charset_mode.as_str(),
        "max_input_bytes": cfg.max_input_bytes,
        "threads": cfg.threads,
        "pin_cpu": cfg.pin_cpu,
        "html_bytes": html_bytes.len(),
        "decoded_utf8_bytes": html_str.len(),
        "macroblocks": cfg.macroblocks,
        "warmup_blocks": cfg.warmup_blocks,
        "inner_iters": cfg.inner_iters,
        "arm_a": {
            "name": cfg.arm_a.as_str(),
            "stage": cfg.arm_a.stage(),
            "boilerplate_policy": cfg.arm_a.effective_boilerplate_policy_str(cfg.boilerplate_policy),
            "us_per_kib_p50": quantile_sorted(&us_kib_a, 0.50),
            "us_per_kib_p95": quantile_sorted(&us_kib_a, 0.95),
            "us_per_kib_p99": quantile_sorted(&us_kib_a, 0.99),
        },
        "arm_b": {
            "name": cfg.arm_b.as_str(),
            "stage": cfg.arm_b.stage(),
            "boilerplate_policy": cfg.arm_b.effective_boilerplate_policy_str(cfg.boilerplate_policy),
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
    fn cli_rejects_cross_stage_arm_asymmetry_and_labels_dom_only_without_main_content() {
        assert!(
            parse_cli_args(&s(&[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--arm-a",
                "legacy",
                "--arm-b",
                "lexbor_minimal",
            ]))
            .is_err()
        );
        for bad in [
            &[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--contender",
                "lexbor_minimal",
                "--arm-b",
                "legacy",
            ][..],
            &[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--contender",
                "lexbor_stage_ir",
                "--arm-a",
                "legacy",
                "--arm-b",
                "lexbor_pure",
            ][..],
            &[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--macroblocks",
                "65",
            ][..],
            &[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--macroblocks",
                "32",
            ][..],
            &[
                "mode_a_cpu",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--warmup-blocks",
                "3",
            ][..],
        ] {
            assert!(
                parse_cli_args(&s(bad)).is_err(),
                "expected error for {bad:?}"
            );
        }
        let dom_only_cfg = parse_cli_args(&s(&[
            "mode_a_cpu",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--contender",
            "lexbor_minimal",
        ]))
        .expect("single dom_parse_only contender allowed");
        assert_eq!(dom_only_cfg.arm_a.stage(), "dom_parse_only");
        assert_eq!(
            dom_only_cfg
                .arm_a
                .effective_boilerplate_policy_str(dom_only_cfg.boilerplate_policy),
            None
        );
    }

    #[test]
    fn macroblocks_alternate_abba_on_even_and_baab_on_odd_blocks() {
        assert_eq!(
            macroblock_slots(0),
            [
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmB,
                MacroSlot::ArmA
            ]
        );
        assert_eq!(
            macroblock_slots(1),
            [
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmA,
                MacroSlot::ArmB
            ]
        );
        assert_eq!(
            macroblock_slots(2),
            [
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmB,
                MacroSlot::ArmA
            ]
        );
        assert_eq!(
            macroblock_slots(3),
            [
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmA,
                MacroSlot::ArmB
            ]
        );

        let mut observed = Vec::new();
        let mut ns_a = [0u64; 4];
        let mut ns_b = [0u64; 4];
        let _ = execute_counter_balanced_macroblocks(4, 1, &mut ns_a, &mut ns_b, |slot| {
            observed.push(slot);
            1
        });
        assert_eq!(
            observed,
            vec![
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmB,
                MacroSlot::ArmA,
                MacroSlot::ArmA,
                MacroSlot::ArmB,
            ]
        );
    }

    #[test]
    fn lexbor_stage_ir_executes_full_projection_matching_lexbor_pure_and_legacy() {
        let html = r#"<!doctype html><html><body><nav><a href="/n">Nav</a></nav><main><article><h1>Title</h1><p>Substantive article paragraph with plenty of words for MainContentV1 extraction.</p></article></main><footer>Footer boilerplate text</footer></body></html>"#;
        let url = validate_real_url("https://en.wikipedia.org/wiki/Stage_parity").unwrap();
        let limits = build_parse_limits_with_policy(
            DEFAULT_MAX_INPUT_BYTES,
            BoilerplatePolicy::MainContentV1,
        )
        .unwrap();
        let legacy_chunks = run_contender_once(Contender::Legacy, html, &url, limits);
        let pure_chunks = run_contender_once(Contender::LexborPure, html, &url, limits);
        let stage_ir_chunks = run_contender_once(Contender::LexborStageIr, html, &url, limits);
        let dom_nodes = run_contender_once(Contender::LexborMinimal, html, &url, limits);

        assert!(legacy_chunks > 0);
        assert_eq!(pure_chunks, legacy_chunks);
        assert_eq!(stage_ir_chunks, legacy_chunks);
        assert!(dom_nodes > stage_ir_chunks);
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
        assert_eq!(cfg.arm_a.stage(), "full_projection");
        assert_eq!(cfg.boilerplate_policy, BoilerplatePolicy::MainContentV1);
        assert_eq!(cfg.charset_mode, CharsetMode::SniffingEnabled);
        assert_eq!(cfg.max_input_bytes, 33_554_432);
        assert_eq!(cfg.pin_cpu, Some(3));

        let cfg2 = parse_cli_args(&s(&[
            "mode_a_cpu",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--boilerplate-policy",
            "full_dom",
            "--charset-mode",
            "transcode_whatwg",
        ]))
        .expect("full_dom + transcode_whatwg");
        assert_eq!(cfg2.boilerplate_policy, BoilerplatePolicy::FullDom);
        assert_eq!(cfg2.charset_mode, CharsetMode::SniffingEnabled);
    }
}
