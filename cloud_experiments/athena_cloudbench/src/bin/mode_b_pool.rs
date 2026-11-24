//! Mode B: Persistent Homogeneous P-Core Streaming IPC Pool (`--qos worker`, `50ms` Soft-Budget + Watchdog Quarantine).
//! Consumes raw `[u32 total_len][u32 uri_len][uri][u32 hdr_len][headers][u32 body_len][raw_body_bytes]`
//! binary frames (`0` fallback URLs, `0` lossy UTF-8 distortion, `MainContentV1` policy parity).

use athena_cloudbench::bench_common::{
    CharsetMode, Contender, DEFAULT_MAX_INPUT_BYTES, DEFAULT_SOFT_BUDGET_MS,
    boilerplate_policy_cli_str, boilerplate_policy_str, build_parse_limits_with_policy,
    decode_html_bytes, effective_limits, parse_boilerplate_policy, validate_real_url,
};
use athena_cloudbench::framing::{
    DEFAULT_MAX_FRAME_BYTES, RawWarcFrame, encode_raw_warc_frame, read_raw_warc_frame,
};
use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile};
use athena_core_v2::projection::{
    BoilerplatePolicy, ParseLimits, Projection, parse_html_breakthrough, parse_html_with_limits,
};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, parse_and_project_str_pure_with_profile,
    parse_tree_str_with_limits_and_profile,
};
use serde::Serialize;
use serde_json::{Value, json};
use std::env;
use std::fs::{self, File};
use std::io::{self, BufReader, BufWriter, Cursor, Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const WORKER_READY_MAGIC: &[u8; 4] = b"RDY\n";

#[derive(Clone, Debug, PartialEq, Eq)]
struct PoolConfig {
    internal_slot: Option<usize>,
    qos: QosProfile,
    workers: usize,
    pin_cpu: Option<usize>,
    watchdog_ms: u64,
    soft_budget_ms: u64,
    contender: Option<Contender>,
    boilerplate_policy: BoilerplatePolicy,
    charset_mode: CharsetMode,
    max_input_bytes: usize,
    cli_url: Option<String>,
    frames_in: Option<String>,
    jsonl_out: Option<String>,
    summary_out: Option<String>,
    self_test: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct PoolAccountingSummary {
    total_records: usize,
    ok_records: usize,
    quarantined_records: usize,
    soft_budget_exceeded_records: usize,
    hard_watchdog_timeouts: usize,
    worker_respawns: usize,
    soft_budget_ms: u64,
    watchdog_ms: u64,
    stage: &'static str,
    boilerplate_policy: Option<&'static str>,
    charset_mode: &'static str,
    contender: &'static str,
    max_input_bytes: usize,
    threads: usize,
    pin_cpu: Option<usize>,
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

fn parse_contender_arg(raw: &str) -> Result<Option<Contender>, String> {
    if raw.trim().eq_ignore_ascii_case("all") {
        Ok(None)
    } else {
        Contender::parse(raw).map(Some)
    }
}

fn parse_cli_args(args: &[String]) -> Result<PoolConfig, String> {
    let mut internal_slot: Option<usize> = None;
    let mut qos = QosProfile::Worker;
    let mut workers: usize = 8;
    let mut pin_cpu: Option<usize> = Some(0);
    let mut watchdog_ms: u64 = DEFAULT_SOFT_BUDGET_MS;
    let mut soft_budget_ms: u64 = DEFAULT_SOFT_BUDGET_MS;
    let mut contender: Option<Contender> = None;
    let mut boilerplate_policy = BoilerplatePolicy::MainContentV1;
    let mut charset_mode = CharsetMode::Utf8Assumed;
    let mut max_input_bytes = DEFAULT_MAX_INPUT_BYTES;
    let mut cli_url: Option<String> = None;
    let mut frames_in: Option<String> = None;
    let mut jsonl_out: Option<String> = None;
    let mut summary_out: Option<String> = None;
    let mut self_test = false;

    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--internal-worker-slot" => {
                i += 1;
                internal_slot = Some(
                    args.get(i)
                        .ok_or("missing --internal-worker-slot")?
                        .parse::<usize>()
                        .map_err(|e| e.to_string())?,
                );
            }
            "--qos" => {
                i += 1;
                qos = QosProfile::parse(args.get(i).ok_or("missing --qos value")?)?;
            }
            "--workers" | "--threads" => {
                i += 1;
                workers = args
                    .get(i)
                    .ok_or("missing --workers/--threads value")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?;
                if workers == 0 {
                    return Err("--workers/--threads must be >= 1".to_owned());
                }
            }
            "--cpu" | "--pin-cpu" => {
                i += 1;
                pin_cpu = parse_pin_cpu(args.get(i).ok_or("missing --cpu/--pin-cpu value")?)?;
            }
            "--watchdog-ms" => {
                i += 1;
                watchdog_ms = args
                    .get(i)
                    .ok_or("missing --watchdog-ms")?
                    .parse::<u64>()
                    .map_err(|e| e.to_string())?;
                if watchdog_ms == 0 {
                    return Err("--watchdog-ms must be >= 1".to_owned());
                }
            }
            "--soft-budget-ms" => {
                i += 1;
                soft_budget_ms = args
                    .get(i)
                    .ok_or("missing --soft-budget-ms")?
                    .parse::<u64>()
                    .map_err(|e| e.to_string())?;
                if soft_budget_ms == 0 {
                    return Err("--soft-budget-ms must be >= 1".to_owned());
                }
            }
            "--contender" => {
                i += 1;
                contender = parse_contender_arg(args.get(i).ok_or("missing --contender value")?)?;
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
            "--url" => {
                i += 1;
                let raw = args.get(i).ok_or("missing --url value")?.clone();
                let _ = validate_real_url(&raw)?;
                cli_url = Some(raw);
            }
            "--frames-in" => {
                i += 1;
                frames_in = Some(args.get(i).ok_or("missing --frames-in")?.clone());
            }
            "--jsonl-out" => {
                i += 1;
                jsonl_out = Some(args.get(i).ok_or("missing --jsonl-out")?.clone());
            }
            "--summary-out" => {
                i += 1;
                summary_out = Some(args.get(i).ok_or("missing --summary-out")?.clone());
            }
            "--self-test" => self_test = true,
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    let _ = build_parse_limits_with_policy(max_input_bytes, boilerplate_policy)?;
    if !self_test && internal_slot.is_none() && cli_url.is_none() && frames_in.is_none() {
        return Err(
            "missing required --url <real_url> or --frames-in <path> (or pass --self-test)"
                .to_owned(),
        );
    }

    Ok(PoolConfig {
        internal_slot,
        qos,
        workers,
        pin_cpu,
        watchdog_ms,
        soft_budget_ms,
        contender,
        boilerplate_policy,
        charset_mode,
        max_input_bytes,
        cli_url,
        frames_in,
        jsonl_out,
        summary_out,
        self_test,
    })
}

fn parse_header_u64(headers: &[u8], target_header: &[u8]) -> Option<u64> {
    for line in headers.split(|&b| b == b'\n') {
        let trimmed = line.strip_suffix(b"\r").unwrap_or(line);
        if let Some(colon) = trimmed.iter().position(|&b| b == b':') {
            let name = &trimmed[..colon];
            if name.eq_ignore_ascii_case(target_header) {
                let val_str = std::str::from_utf8(&trimmed[colon + 1..]).ok()?.trim();
                return val_str.parse::<u64>().ok();
            }
        }
    }
    None
}

fn hash_projection(proj: &Projection) -> String {
    let mut hasher = blake3::Hasher::new();
    for c in &proj.chunks {
        hasher.update(c.role.as_bytes());
        hasher.update(&[0]);
        hasher.update(c.text.as_bytes());
        hasher.update(&[0xff]);
    }
    hasher.finalize().to_hex().to_string()
}

struct ArmRunOutcome {
    utf8_valid: bool,
    status: &'static str,
    units: usize,
    blake3: String,
    elapsed_us: u64,
}

#[allow(clippy::too_many_arguments)]
fn run_timed_rust_arm(
    use_breakthrough: bool,
    raw_body: &[u8],
    raw_headers: &[u8],
    url: &url::Url,
    base_limits: ParseLimits,
    max_input_bytes: usize,
    charset_mode: CharsetMode,
    stall_ms: u64,
) -> ArmRunOutcome {
    let t_arm = Instant::now();
    if stall_ms > 0 {
        thread::sleep(Duration::from_millis(stall_ms));
    }
    let decoded = match decode_html_bytes(raw_body, raw_headers, max_input_bytes, charset_mode) {
        Ok(s) => s,
        Err(_) => {
            return ArmRunOutcome {
                utf8_valid: false,
                status: "NON_UTF8_RAW_BODY_REJECTED",
                units: 0,
                blake3: String::new(),
                elapsed_us: t_arm.elapsed().as_micros() as u64,
            };
        }
    };
    let html_str = decoded.as_ref();
    let eff = effective_limits(base_limits, html_str.len());
    let res = if use_breakthrough {
        parse_html_breakthrough(html_str, url, eff).map(|p| p.projection)
    } else {
        parse_html_with_limits(html_str, url, eff)
    };
    let (status, units, blake3) = match res {
        Ok(proj) => ("OK", proj.chunks.len(), hash_projection(&proj)),
        Err(_) => ("TRACK_A_PARSE_ERR", 0usize, String::new()),
    };
    ArmRunOutcome {
        utf8_valid: true,
        status,
        units,
        blake3,
        elapsed_us: t_arm.elapsed().as_micros() as u64,
    }
}

#[allow(clippy::too_many_arguments)]
fn run_timed_lexbor_arm(
    contender: Contender,
    raw_body: &[u8],
    raw_headers: &[u8],
    url: &url::Url,
    base_limits: ParseLimits,
    max_input_bytes: usize,
    charset_mode: CharsetMode,
    stall_ms: u64,
) -> ArmRunOutcome {
    let t_arm = Instant::now();
    if stall_ms > 0 {
        thread::sleep(Duration::from_millis(stall_ms));
    }
    let decoded = match decode_html_bytes(raw_body, raw_headers, max_input_bytes, charset_mode) {
        Ok(s) => s,
        Err(_) => {
            return ArmRunOutcome {
                utf8_valid: false,
                status: "NON_UTF8_RAW_BODY_REJECTED",
                units: 0,
                blake3: String::new(),
                elapsed_us: t_arm.elapsed().as_micros() as u64,
            };
        }
    };
    let html_str = decoded.as_ref();
    let eff = effective_limits(base_limits, html_str.len());
    let (status, units, blake3) = match contender {
        Contender::LexborPure => {
            match parse_and_project_str_pure_with_profile(
                html_str,
                url,
                eff,
                LexborAttrProfile::Minimal,
            ) {
                Ok(proj) => ("OK", proj.chunks.len(), hash_projection(&proj)),
                Err(_) => ("LEXBOR_PARSE_ERR", 0usize, String::new()),
            }
        }
        Contender::LexborStageIr => {
            match parse_and_project_str_pure_with_profile(
                html_str,
                url,
                eff,
                LexborAttrProfile::StageIr,
            ) {
                Ok(proj) => ("OK", proj.chunks.len(), hash_projection(&proj)),
                Err(_) => ("LEXBOR_PARSE_ERR", 0usize, String::new()),
            }
        }
        Contender::LexborMinimal => {
            match parse_tree_str_with_limits_and_profile(html_str, eff, LexborAttrProfile::Minimal)
            {
                Ok(tree) => ("OK", tree.nodes.len(), String::new()),
                Err(_) => ("LEXBOR_PARSE_ERR", 0usize, String::new()),
            }
        }
        _ => ("INVALID_CONTENDER", 0usize, String::new()),
    };
    ArmRunOutcome {
        utf8_valid: true,
        status,
        units,
        blake3,
        elapsed_us: t_arm.elapsed().as_micros() as u64,
    }
}

fn pool_stage_str(contender: Option<Contender>) -> &'static str {
    match contender {
        Some(c) => c.stage(),
        None => "full_projection",
    }
}

fn pool_boilerplate_policy_str(
    contender: Option<Contender>,
    policy: BoilerplatePolicy,
) -> Option<&'static str> {
    match contender {
        Some(c) => c.effective_boilerplate_policy_str(policy),
        None => Some(boilerplate_policy_str(policy)),
    }
}

fn read_bounded_worker_payload<R: Read>(
    rdr: &mut R,
    max_frame_bytes: usize,
) -> Result<Option<Vec<u8>>, String> {
    let mut len_buf = [0u8; 4];
    match rdr.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.to_string()),
    }
    let n = u32::from_be_bytes(len_buf) as usize;
    if n > max_frame_bytes {
        return Err(format!(
            "worker response frame length {n} exceeds max {max_frame_bytes}"
        ));
    }
    let mut payload = Vec::new();
    payload
        .try_reserve_exact(n)
        .map_err(|e| format!("worker response frame allocation failed ({n} bytes): {e}"))?;
    payload.resize(n, 0u8);
    rdr.read_exact(&mut payload).map_err(|e| e.to_string())?;
    Ok(Some(payload))
}

fn run_worker_loop(cfg: &PoolConfig, slot: usize) -> Result<(), String> {
    let cpu_pin = cfg.pin_cpu.map(|base| base.saturating_add(slot));
    let qos_status = apply_qos_profile(cfg.qos, cpu_pin);
    let stdin = io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut writer = BufWriter::new(stdout.lock());

    writer
        .write_all(WORKER_READY_MAGIC)
        .and_then(|()| writer.flush())
        .map_err(|e| e.to_string())?;

    let base_limits = build_parse_limits_with_policy(cfg.max_input_bytes, cfg.boilerplate_policy)?;

    while let Some(frame) =
        read_raw_warc_frame(&mut reader, DEFAULT_MAX_FRAME_BYTES).map_err(|e| e.to_string())?
    {
        let common_stall_ms =
            parse_header_u64(&frame.headers, b"x-athena-test-stall-ms").unwrap_or(0);
        let track_a_stall_ms = parse_header_u64(&frame.headers, b"x-athena-test-tracka-stall-ms")
            .unwrap_or(common_stall_ms);
        let lexbor_stall_ms = parse_header_u64(&frame.headers, b"x-athena-test-lexbor-stall-ms")
            .unwrap_or(common_stall_ms);

        let uri_utf8 = std::str::from_utf8(&frame.uri).ok();
        let parsed_url = uri_utf8.and_then(|u| validate_real_url(u).ok());
        let within_input_cap = frame.raw_body_bytes.len() <= cfg.max_input_bytes;

        let mut contender_timings_us = serde_json::Map::new();
        let mut track_a_elapsed_us: Option<u64> = None;
        let mut lexbor_pure_elapsed_us: Option<u64> = None;
        let mut contender_elapsed_us: Option<u64> = None;
        let mut worker_elapsed_us: Option<u64> = None;
        let mut track_a_utf8_valid = false;

        let (
            track_a_status,
            track_a_chunks,
            track_a_blake3,
            lexbor_status,
            lexbor_chunks,
            contender_status,
            contender_chunks,
            contender_blake3,
        ) = match (uri_utf8, &parsed_url) {
            (_, None) | (None, _) => {
                if common_stall_ms > 0 {
                    thread::sleep(Duration::from_millis(common_stall_ms));
                }
                (
                    "INVALID_RAW_URI",
                    0usize,
                    String::new(),
                    "INVALID_RAW_URI",
                    0usize,
                    "INVALID_RAW_URI",
                    0usize,
                    String::new(),
                )
            }
            (Some(_), Some(url)) => {
                if !within_input_cap {
                    if common_stall_ms > 0 {
                        thread::sleep(Duration::from_millis(common_stall_ms));
                    }
                    (
                        "INPUT_EXCEEDS_MAX_BYTES",
                        0,
                        String::new(),
                        "INPUT_EXCEEDS_MAX_BYTES",
                        0,
                        "INPUT_EXCEEDS_MAX_BYTES",
                        0,
                        String::new(),
                    )
                } else {
                    match cfg.contender {
                        None => {
                            let ta = run_timed_rust_arm(
                                false,
                                &frame.raw_body_bytes,
                                &frame.headers,
                                url,
                                base_limits,
                                cfg.max_input_bytes,
                                cfg.charset_mode,
                                track_a_stall_ms,
                            );
                            let lx = run_timed_lexbor_arm(
                                Contender::LexborPure,
                                &frame.raw_body_bytes,
                                &frame.headers,
                                url,
                                base_limits,
                                cfg.max_input_bytes,
                                cfg.charset_mode,
                                lexbor_stall_ms,
                            );
                            track_a_utf8_valid = ta.utf8_valid;
                            track_a_elapsed_us = Some(ta.elapsed_us);
                            lexbor_pure_elapsed_us = Some(lx.elapsed_us);
                            contender_timings_us.insert(
                                Contender::Legacy.as_str().to_owned(),
                                json!(ta.elapsed_us),
                            );
                            contender_timings_us.insert(
                                Contender::LexborPure.as_str().to_owned(),
                                json!(lx.elapsed_us),
                            );
                            contender_elapsed_us = None;
                            worker_elapsed_us = None;
                            (
                                ta.status,
                                ta.units,
                                ta.blake3.clone(),
                                lx.status,
                                lx.units,
                                ta.status,
                                ta.units,
                                ta.blake3,
                            )
                        }
                        Some(Contender::Legacy) => {
                            let ta = run_timed_rust_arm(
                                false,
                                &frame.raw_body_bytes,
                                &frame.headers,
                                url,
                                base_limits,
                                cfg.max_input_bytes,
                                cfg.charset_mode,
                                track_a_stall_ms,
                            );
                            track_a_utf8_valid = ta.utf8_valid;
                            track_a_elapsed_us = Some(ta.elapsed_us);
                            contender_elapsed_us = Some(ta.elapsed_us);
                            worker_elapsed_us = Some(ta.elapsed_us);
                            contender_timings_us.insert(
                                Contender::Legacy.as_str().to_owned(),
                                json!(ta.elapsed_us),
                            );
                            (
                                ta.status,
                                ta.units,
                                ta.blake3.clone(),
                                "SKIPPED",
                                0,
                                ta.status,
                                ta.units,
                                ta.blake3,
                            )
                        }
                        Some(Contender::Breakthrough) => {
                            let bt = run_timed_rust_arm(
                                true,
                                &frame.raw_body_bytes,
                                &frame.headers,
                                url,
                                base_limits,
                                cfg.max_input_bytes,
                                cfg.charset_mode,
                                track_a_stall_ms,
                            );
                            track_a_utf8_valid = bt.utf8_valid;
                            track_a_elapsed_us = Some(bt.elapsed_us);
                            contender_elapsed_us = Some(bt.elapsed_us);
                            worker_elapsed_us = Some(bt.elapsed_us);
                            contender_timings_us.insert(
                                Contender::Breakthrough.as_str().to_owned(),
                                json!(bt.elapsed_us),
                            );
                            (
                                bt.status,
                                bt.units,
                                bt.blake3.clone(),
                                "SKIPPED",
                                0,
                                bt.status,
                                bt.units,
                                bt.blake3,
                            )
                        }
                        Some(c) => {
                            let lx = run_timed_lexbor_arm(
                                c,
                                &frame.raw_body_bytes,
                                &frame.headers,
                                url,
                                base_limits,
                                cfg.max_input_bytes,
                                cfg.charset_mode,
                                lexbor_stall_ms,
                            );
                            track_a_utf8_valid = lx.utf8_valid;
                            if c == Contender::LexborPure {
                                lexbor_pure_elapsed_us = Some(lx.elapsed_us);
                            }
                            contender_elapsed_us = Some(lx.elapsed_us);
                            worker_elapsed_us = Some(lx.elapsed_us);
                            contender_timings_us
                                .insert(c.as_str().to_owned(), json!(lx.elapsed_us));
                            (
                                "SKIPPED",
                                0,
                                String::new(),
                                lx.status,
                                lx.units,
                                lx.status,
                                lx.units,
                                lx.blake3,
                            )
                        }
                    }
                }
            }
        };

        let resp = json!({
            "status": "OK",
            "worker_slot": slot,
            "qos": qos_status.profile,
            "stage": pool_stage_str(cfg.contender),
            "boilerplate_policy": pool_boilerplate_policy_str(cfg.contender, cfg.boilerplate_policy),
            "charset_mode": cfg.charset_mode.as_str(),
            "contender": cfg.contender.map(Contender::as_str).unwrap_or("all"),
            "max_input_bytes": cfg.max_input_bytes,
            "uri_lossy": String::from_utf8_lossy(&frame.uri),
            "raw_header_bytes": frame.headers.len(),
            "raw_body_bytes": frame.raw_body_bytes.len(),
            "track_a_utf8_valid": track_a_utf8_valid,
            "track_a_status": track_a_status,
            "track_a_chunks": track_a_chunks,
            "track_a_blake3": track_a_blake3,
            "track_a_elapsed_us": track_a_elapsed_us,
            "lexbor_pure_status": lexbor_status,
            "lexbor_pure_chunks": lexbor_chunks,
            "lexbor_pure_elapsed_us": lexbor_pure_elapsed_us,
            "contender_status": contender_status,
            "contender_chunks": contender_chunks,
            "contender_blake3": contender_blake3,
            "contender_elapsed_us": contender_elapsed_us,
            "contender_timings_us": contender_timings_us,
            "worker_elapsed_us": worker_elapsed_us,
        });
        let resp_bytes = serde_json::to_vec(&resp).map_err(|e| e.to_string())?;
        if resp_bytes.len() > DEFAULT_MAX_FRAME_BYTES {
            return Err(format!(
                "worker response length {} exceeds max {DEFAULT_MAX_FRAME_BYTES}",
                resp_bytes.len()
            ));
        }
        let resp_len = u32::try_from(resp_bytes.len()).map_err(|_| "response too large")?;
        writer
            .write_all(&resp_len.to_be_bytes())
            .and_then(|()| writer.write_all(&resp_bytes))
            .and_then(|()| writer.flush())
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn apply_soft_budget_to_response(val: &mut Value, wall_us: u64, soft_budget_ms: u64) {
    let soft_budget_us = soft_budget_ms.saturating_mul(1_000);
    let status_is_ok = val.get("status").and_then(Value::as_str) == Some("OK");

    let mut per_arm_exceeded = serde_json::Map::new();
    let exceeded = if let Some(timings) = val
        .get("contender_timings_us")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty())
    {
        let mut any_arm_exceeded = false;
        let mut sum_arm_us = 0u64;
        for (name, v) in timings {
            let us = v.as_u64().unwrap_or(0);
            let over = us > soft_budget_us;
            if over {
                any_arm_exceeded = true;
            }
            sum_arm_us = sum_arm_us.saturating_add(us);
            per_arm_exceeded.insert(name.clone(), json!(over));
        }
        if timings.len() >= 2 {
            let ipc_overhead_us = wall_us.saturating_sub(sum_arm_us);
            any_arm_exceeded || ipc_overhead_us > soft_budget_us
        } else {
            any_arm_exceeded || wall_us > soft_budget_us
        }
    } else {
        let single_worker_us = val
            .get("contender_elapsed_us")
            .and_then(Value::as_u64)
            .or_else(|| val.get("worker_elapsed_us").and_then(Value::as_u64))
            .unwrap_or(0);
        wall_us > soft_budget_us || single_worker_us > soft_budget_us
    };

    if let Some(obj) = val.as_object_mut() {
        obj.insert("soft_budget_ms".into(), json!(soft_budget_ms));
        obj.insert("soft_budget_exceeded".into(), json!(exceeded));
        if !per_arm_exceeded.is_empty() {
            obj.insert(
                "contender_soft_budget_exceeded".into(),
                Value::Object(per_arm_exceeded),
            );
        }
        obj.entry("worker_respawned".to_owned())
            .or_insert_with(|| json!(false));
        if status_is_ok && exceeded {
            obj.insert("status".into(), json!("PAGE_QUARANTINED"));
            obj.insert(
                "quarantine_reason".into(),
                json!(format!("SOFT_BUDGET_{soft_budget_ms}MS_EXCEEDED")),
            );
        }
    }
}

fn summarize_pool_records(records: &[Value], cfg: &PoolConfig) -> PoolAccountingSummary {
    let mut ok_records = 0usize;
    let mut quarantined_records = 0usize;
    let mut soft_budget_exceeded_records = 0usize;
    let mut hard_watchdog_timeouts = 0usize;
    let mut worker_respawns = 0usize;

    for rec in records {
        let status = rec.get("status").and_then(Value::as_str).unwrap_or("");
        if status == "OK" {
            ok_records += 1;
        } else if status == "PAGE_QUARANTINED" {
            quarantined_records += 1;
        }
        if rec
            .get("soft_budget_exceeded")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            soft_budget_exceeded_records += 1;
        }
        let reason = rec
            .get("quarantine_reason")
            .and_then(Value::as_str)
            .unwrap_or("");
        if reason.starts_with("WATCHDOG_TIMEOUT_") {
            hard_watchdog_timeouts += 1;
        }
        if rec
            .get("worker_respawned")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            worker_respawns += 1;
        }
    }

    PoolAccountingSummary {
        total_records: records.len(),
        ok_records,
        quarantined_records,
        soft_budget_exceeded_records,
        hard_watchdog_timeouts,
        worker_respawns,
        soft_budget_ms: cfg.soft_budget_ms,
        watchdog_ms: cfg.watchdog_ms,
        stage: pool_stage_str(cfg.contender),
        boilerplate_policy: pool_boilerplate_policy_str(cfg.contender, cfg.boilerplate_policy),
        charset_mode: cfg.charset_mode.as_str(),
        contender: cfg.contender.map(Contender::as_str).unwrap_or("all"),
        max_input_bytes: cfg.max_input_bytes,
        threads: cfg.workers,
        pin_cpu: cfg.pin_cpu,
    }
}

struct ManagedWorker {
    slot: usize,
    cfg: PoolConfig,
    exe_path: std::path::PathBuf,
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Result<Vec<u8>, String>>,
    reader_thread: Option<JoinHandle<()>>,
}

impl ManagedWorker {
    fn spawn(slot: usize, cfg: &PoolConfig, exe_path: std::path::PathBuf) -> Result<Self, String> {
        let mut cmd = Command::new(&exe_path);
        cmd.arg("--internal-worker-slot")
            .arg(slot.to_string())
            .arg("--qos")
            .arg(cfg.qos.as_str())
            .arg("--workers")
            .arg(cfg.workers.to_string())
            .arg("--pin-cpu")
            .arg(
                cfg.pin_cpu
                    .map(|p| p.to_string())
                    .unwrap_or_else(|| "none".to_owned()),
            )
            .arg("--watchdog-ms")
            .arg(cfg.watchdog_ms.to_string())
            .arg("--soft-budget-ms")
            .arg(cfg.soft_budget_ms.to_string())
            .arg("--contender")
            .arg(cfg.contender.map(Contender::as_str).unwrap_or("all"))
            .arg("--boilerplate-policy")
            .arg(boilerplate_policy_cli_str(cfg.boilerplate_policy))
            .arg("--charset-mode")
            .arg(cfg.charset_mode.as_str())
            .arg("--max-input-bytes")
            .arg(cfg.max_input_bytes.to_string());

        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("spawn worker {slot}: {e}"))?;

        let stdin = child.stdin.take().ok_or("missing worker stdin")?;
        let mut stdout = child.stdout.take().ok_or("missing worker stdout")?;

        let mut magic = [0u8; 4];
        stdout
            .read_exact(&mut magic)
            .map_err(|e| format!("worker {slot} failed startup handshake: {e}"))?;
        if &magic != WORKER_READY_MAGIC {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("worker {slot} bad handshake magic: {magic:?}"));
        }

        let (tx, rx) = mpsc::channel::<Result<Vec<u8>, String>>();
        let reader_thread = thread::spawn(move || {
            let mut rdr = BufReader::new(stdout);
            loop {
                match read_bounded_worker_payload(&mut rdr, DEFAULT_MAX_FRAME_BYTES) {
                    Ok(Some(payload)) => {
                        if tx.send(Ok(payload)).is_err() {
                            break;
                        }
                    }
                    Ok(None) => break,
                    Err(err) => {
                        let _ = tx.send(Err(err));
                        break;
                    }
                }
            }
        });

        Ok(Self {
            slot,
            cfg: cfg.clone(),
            exe_path,
            child,
            stdin,
            rx,
            reader_thread: Some(reader_thread),
        })
    }

    fn kill_and_respawn(&mut self) -> Result<(), String> {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(handle) = self.reader_thread.take() {
            let _ = handle.join();
        }
        let replacement = Self::spawn(self.slot, &self.cfg, self.exe_path.clone())?;
        *self = replacement;
        Ok(())
    }

    fn evaluate_frame(
        &mut self,
        record_index: usize,
        frame: &RawWarcFrame,
    ) -> Result<Value, String> {
        let t0 = Instant::now();
        let watchdog = Duration::from_millis(self.cfg.watchdog_ms.max(1));
        let soft_budget_us = self.cfg.soft_budget_ms.saturating_mul(1_000);
        let wire = encode_raw_warc_frame(&frame.uri, &frame.headers, &frame.raw_body_bytes)
            .map_err(|e| e.to_string())?;
        if self
            .stdin
            .write_all(&wire)
            .and_then(|()| self.stdin.flush())
            .is_err()
        {
            self.kill_and_respawn()?;
            let wall_us = t0.elapsed().as_micros() as u64;
            return Ok(json!({
                "record_index": record_index,
                "status": "PAGE_QUARANTINED",
                "quarantine_reason": "WORKER_PIPE_BROKEN",
                "worker_slot": self.slot,
                "worker_respawned": true,
                "soft_budget_ms": self.cfg.soft_budget_ms,
                "soft_budget_exceeded": wall_us > soft_budget_us,
                "uri_lossy": String::from_utf8_lossy(&frame.uri),
                "raw_body_bytes": frame.raw_body_bytes.len(),
                "wall_us": wall_us,
            }));
        }

        match self.rx.recv_timeout(watchdog) {
            Ok(Ok(bytes)) => {
                let wall_us = t0.elapsed().as_micros() as u64;
                let mut val: Value = match serde_json::from_slice::<Value>(&bytes) {
                    Ok(v) if v.is_object() => v,
                    Ok(_) | Err(_) => {
                        self.kill_and_respawn()?;
                        return Ok(json!({
                            "record_index": record_index,
                            "status": "PAGE_QUARANTINED",
                            "quarantine_reason": "WORKER_PROTOCOL_CORRUPT",
                            "worker_slot": self.slot,
                            "worker_respawned": true,
                            "soft_budget_ms": self.cfg.soft_budget_ms,
                            "soft_budget_exceeded": wall_us > soft_budget_us,
                            "uri_lossy": String::from_utf8_lossy(&frame.uri),
                            "raw_body_bytes": frame.raw_body_bytes.len(),
                            "wall_us": wall_us,
                        }));
                    }
                };
                if let Some(obj) = val.as_object_mut() {
                    obj.insert("record_index".into(), json!(record_index));
                    obj.insert("wall_us".into(), json!(wall_us));
                }
                apply_soft_budget_to_response(&mut val, wall_us, self.cfg.soft_budget_ms);
                Ok(val)
            }
            Ok(Err(pipe_err)) => {
                let wall_us = t0.elapsed().as_micros() as u64;
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": format!("WORKER_CRASHED:{pipe_err}"),
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "soft_budget_ms": self.cfg.soft_budget_ms,
                    "soft_budget_exceeded": wall_us > soft_budget_us,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "wall_us": wall_us,
                }))
            }
            Err(RecvTimeoutError::Timeout) => {
                let elapsed_us = t0.elapsed().as_micros() as u64;
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": format!("WATCHDOG_TIMEOUT_{}MS", watchdog.as_millis()),
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "soft_budget_ms": self.cfg.soft_budget_ms,
                    "soft_budget_exceeded": elapsed_us > soft_budget_us,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "watchdog_elapsed_us": elapsed_us,
                    "wall_us": t0.elapsed().as_micros() as u64,
                }))
            }
            Err(RecvTimeoutError::Disconnected) => {
                let wall_us = t0.elapsed().as_micros() as u64;
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": "WORKER_DISCONNECTED",
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "soft_budget_ms": self.cfg.soft_budget_ms,
                    "soft_budget_exceeded": wall_us > soft_budget_us,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "wall_us": wall_us,
                }))
            }
        }
    }

    fn shutdown(mut self) {
        drop(self.stdin);
        let _ = self.child.wait();
        if let Some(handle) = self.reader_thread.take() {
            let _ = handle.join();
        }
    }
}

fn execute_pool_stream<R: Read + Send>(reader: R, cfg: &PoolConfig) -> Result<Vec<Value>, String> {
    let exe_path = env::current_exe().map_err(|e| e.to_string())?;
    let worker_count = cfg.workers.max(1);

    let mut pool = Vec::with_capacity(worker_count);
    for slot in 0..worker_count {
        pool.push(ManagedWorker::spawn(slot, cfg, exe_path.clone())?);
    }

    let source = Mutex::new((0usize, BufReader::new(reader)));
    let outcomes: Mutex<Vec<Value>> = Mutex::new(Vec::new());

    let run_res: Result<(), String> = thread::scope(|scope| {
        let mut handles = Vec::with_capacity(pool.len());
        for mut worker in pool {
            let src_ref = &source;
            let out_ref = &outcomes;
            handles.push(scope.spawn(move || -> Result<(), String> {
                loop {
                    let next_item = {
                        let mut guard = src_ref.lock().map_err(|_| "poisoned source mutex")?;
                        let idx = guard.0;
                        match read_raw_warc_frame(&mut guard.1, DEFAULT_MAX_FRAME_BYTES)
                            .map_err(|e| e.to_string())?
                        {
                            Some(frame) => {
                                guard.0 += 1;
                                Some((idx, frame))
                            }
                            None => None,
                        }
                    };
                    let Some((record_idx, frame)) = next_item else {
                        break;
                    };
                    let outcome = worker.evaluate_frame(record_idx, &frame)?;
                    out_ref
                        .lock()
                        .map_err(|_| "poisoned outcomes mutex")?
                        .push(outcome);
                }
                worker.shutdown();
                Ok(())
            }));
        }

        for h in handles {
            match h.join() {
                Ok(res) => res?,
                Err(_) => return Err("pool coordinator thread panicked".to_owned()),
            }
        }
        Ok(())
    });
    run_res?;

    let mut results = outcomes
        .into_inner()
        .map_err(|_| "outcomes mutex poisoned")?;
    results.sort_by_key(|v| v.get("record_index").and_then(Value::as_u64).unwrap_or(0));
    Ok(results)
}

fn run_self_test() -> Result<(), String> {
    let url_ok = "https://en.wikipedia.org/wiki/Article_1";
    let url_non_utf8 = "https://ja.wikipedia.org/wiki/Shift_JIS_fixture";
    let url_stall = "https://archive.mozilla.org/warc-00432-offset-544317574";
    let _ = validate_real_url(url_ok)?;
    let _ = validate_real_url(url_non_utf8)?;
    let _ = validate_real_url(url_stall)?;

    let mut stream1 = Vec::new();
    stream1.extend_from_slice(
        &encode_raw_warc_frame(
            url_ok.as_bytes(),
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><article><h1>Article 1</h1><p>Clean UTF-8 payload for worker pool mode b verification with sufficient body content.</p></article></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    stream1.extend_from_slice(
        &encode_raw_warc_frame(
            url_non_utf8.as_bytes(),
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n",
            b"<html><body><main><h1>NonUTF8</h1><p>\xff\xfe\xfd raw non-utf8 sequence</p></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    stream1.extend_from_slice(
        &encode_raw_warc_frame(
            url_stall.as_bytes(),
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-Stall-Ms: 500\r\nContent-Type: text/html\r\n\r\n",
            b"<html><body><main><p>Poison pill stalled page</p></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    for idx in 3..5 {
        let post_url = format!("https://en.wikipedia.org/wiki/Post_quarantine_{idx}");
        let _ = validate_real_url(&post_url)?;
        stream1.extend_from_slice(
            &encode_raw_warc_frame(
                post_url.as_bytes(),
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n",
                format!("<html><body><main><h1>Post Quarantine {idx}</h1><p>Recovered worker processes record {idx} cleanly under MainContentV1 policy.</p></main></body></html>").as_bytes(),
            )
            .map_err(|e| e.to_string())?,
        );
    }

    let cfg1 = PoolConfig {
        internal_slot: None,
        qos: QosProfile::Worker,
        workers: 2,
        pin_cpu: None,
        watchdog_ms: 50,
        soft_budget_ms: DEFAULT_SOFT_BUDGET_MS,
        contender: None,
        boilerplate_policy: BoilerplatePolicy::MainContentV1,
        charset_mode: CharsetMode::Utf8Assumed,
        max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
        cli_url: Some(url_ok.to_owned()),
        frames_in: None,
        jsonl_out: None,
        summary_out: None,
        self_test: true,
    };

    let results1 = execute_pool_stream(Cursor::new(stream1), &cfg1)?;
    if results1.len() != 5 {
        return Err(format!(
            "stream1 expected 5 records, got {}",
            results1.len()
        ));
    }
    if results1[0]["status"] != "OK"
        || results1[0]["track_a_status"] != "OK"
        || results1[0]["lexbor_pure_status"] != "OK"
        || results1[0]["boilerplate_policy"] != "MainContentV1"
        || !results1[0]["worker_elapsed_us"].is_null()
        || results1[0]["track_a_elapsed_us"].as_u64().is_none()
        || results1[0]["lexbor_pure_elapsed_us"].as_u64().is_none()
    {
        return Err(format!("stream1 record 0 failed: {}", results1[0]));
    }
    if results1[1]["track_a_utf8_valid"] != false
        || results1[1]["track_a_status"] != "NON_UTF8_RAW_BODY_REJECTED"
        || results1[1]["lexbor_pure_status"] != "NON_UTF8_RAW_BODY_REJECTED"
    {
        return Err(format!(
            "stream1 record 1 strict utf8_assumed parity failed: {}",
            results1[1]
        ));
    }
    if results1[2]["status"] != "PAGE_QUARANTINED" || results1[2]["worker_respawned"] != true {
        return Err(format!(
            "stream1 record 2 watchdog quarantine failed: {}",
            results1[2]
        ));
    }
    if results1[3]["status"] != "OK" || results1[4]["status"] != "OK" {
        return Err(format!(
            "stream1 post-quarantine records failed: {}, {}",
            results1[3], results1[4]
        ));
    }

    let accounting1 = summarize_pool_records(&results1, &cfg1);
    if accounting1.quarantined_records != 1
        || accounting1.hard_watchdog_timeouts != 1
        || accounting1.worker_respawns != 1
    {
        return Err(format!("stream1 accounting mismatch: {:?}", accounting1));
    }

    let url_sjis = "https://ja.wikipedia.org/wiki/Shift_JIS_sniffing_test";
    let url_soft = "https://archive.mozilla.org/soft-budget-50ms-stall";
    let url_isolated = "https://en.wikipedia.org/wiki/Isolated_arm_timing";
    let _ = validate_real_url(url_sjis)?;
    let _ = validate_real_url(url_soft)?;
    let _ = validate_real_url(url_isolated)?;

    let mut stream2 = Vec::new();
    stream2.extend_from_slice(
        &encode_raw_warc_frame(
            url_sjis.as_bytes(),
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=shift_jis\r\n\r\n",
            b"<html><body><main><article><h1>\x93\xfa\x96\x7b\x8c\xea</h1><p>Shift JIS \x83\x65\x83\x58\x83\x67 content body decoded cleanly under sniffing_enabled mode.</p></article></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    stream2.extend_from_slice(
        &encode_raw_warc_frame(
            url_soft.as_bytes(),
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-Stall-Ms: 75\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><p>Completes after 75ms inside 500ms watchdog, quarantined by 50ms soft budget.</p></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    stream2.extend_from_slice(
        &encode_raw_warc_frame(
            url_isolated.as_bytes(),
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-TrackA-Stall-Ms: 30\r\nX-Athena-Test-Lexbor-Stall-Ms: 30\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><article><h1>Isolated</h1><p>Each contender takes ~30ms (< 50ms); combined ~60ms must not sum into worker_elapsed_us or falsely quarantine.</p></article></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );

    let cfg2 = PoolConfig {
        internal_slot: None,
        qos: QosProfile::Worker,
        workers: 1,
        pin_cpu: None,
        watchdog_ms: 500,
        soft_budget_ms: DEFAULT_SOFT_BUDGET_MS,
        contender: None,
        boilerplate_policy: BoilerplatePolicy::MainContentV1,
        charset_mode: CharsetMode::SniffingEnabled,
        max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
        cli_url: Some(url_sjis.to_owned()),
        frames_in: None,
        jsonl_out: None,
        summary_out: None,
        self_test: true,
    };

    let results2 = execute_pool_stream(Cursor::new(stream2), &cfg2)?;
    if results2.len() != 3 {
        return Err(format!(
            "stream2 expected 3 records, got {}",
            results2.len()
        ));
    }
    if results2[0]["status"] != "OK"
        || results2[0]["track_a_status"] != "OK"
        || results2[0]["lexbor_pure_status"] != "OK"
        || results2[0]["track_a_chunks"].as_u64().unwrap_or(0) == 0
        || results2[0]["lexbor_pure_chunks"].as_u64().unwrap_or(0) == 0
    {
        return Err(format!(
            "stream2 Shift_JIS record 0 failed: {}",
            results2[0]
        ));
    }
    if results2[1]["status"] != "PAGE_QUARANTINED"
        || results2[1]["quarantine_reason"] != "SOFT_BUDGET_50MS_EXCEEDED"
        || results2[1]["soft_budget_exceeded"] != true
        || results2[1]["worker_respawned"] != false
    {
        return Err(format!(
            "stream2 50ms soft-budget quarantine failed: {}",
            results2[1]
        ));
    }
    if results2[2]["status"] != "OK"
        || results2[2]["soft_budget_exceeded"] != false
        || !results2[2]["worker_elapsed_us"].is_null()
        || results2[2]["contender_soft_budget_exceeded"]["legacy"] != false
        || results2[2]["contender_soft_budget_exceeded"]["lexbor_pure"] != false
    {
        return Err(format!(
            "stream2 isolated 30ms+30ms arm timing failed: {}",
            results2[2]
        ));
    }

    let accounting2 = summarize_pool_records(&results2, &cfg2);
    if accounting2.soft_budget_exceeded_records != 1
        || accounting2.ok_records != 2
        || accounting2.hard_watchdog_timeouts != 0
    {
        return Err(format!(
            "stream2 soft-budget accounting mismatch: {:?}",
            accounting2
        ));
    }

    let all_records: Vec<Value> = results1.iter().chain(results2.iter()).cloned().collect();
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "schema": "AthenaCloudBenchModeBPoolSelfTest/2",
            "status": "PASS",
            "records": all_records,
            "stream1_accounting": accounting1,
            "stream1_records": results1,
            "stream2_accounting": accounting2,
            "stream2_records": results2,
        }))
        .map_err(|e| e.to_string())?
    );
    Ok(())
}

fn main() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    let cfg = parse_cli_args(&args)?;

    if let Some(slot) = cfg.internal_slot {
        return run_worker_loop(&cfg, slot);
    }
    if cfg.self_test {
        return run_self_test();
    }

    let results = match (&cfg.frames_in, &cfg.cli_url) {
        (Some(in_path), _) if in_path == "-" => execute_pool_stream(io::stdin(), &cfg)?,
        (Some(in_path), _) => {
            let f = File::open(in_path).map_err(|e| format!("open {in_path}: {e}"))?;
            execute_pool_stream(f, &cfg)?
        }
        (None, Some(url_str)) => {
            let mut raw_body = Vec::new();
            io::stdin()
                .read_to_end(&mut raw_body)
                .map_err(|e| format!("read stdin body: {e}"))?;
            let wire = encode_raw_warc_frame(
                url_str.as_bytes(),
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n",
                &raw_body,
            )
            .map_err(|e| e.to_string())?;
            execute_pool_stream(Cursor::new(wire), &cfg)?
        }
        (None, None) => {
            return Err(
                "missing required --url <real_url> or --frames-in <path> (or pass --self-test)"
                    .to_owned(),
            );
        }
    };

    let summary = summarize_pool_records(&results, &cfg);

    let out_path = cfg.jsonl_out.as_deref().unwrap_or("-");
    let mut writer: Box<dyn Write> = if out_path == "-" {
        Box::new(BufWriter::new(io::stdout().lock()))
    } else {
        Box::new(BufWriter::new(
            File::create(out_path).map_err(|e| format!("create {out_path}: {e}"))?,
        ))
    };
    for row in &results {
        let line = serde_json::to_string(row).map_err(|e| e.to_string())?;
        writer
            .write_all(line.as_bytes())
            .and_then(|()| writer.write_all(b"\n"))
            .map_err(|e| e.to_string())?;
    }
    writer.flush().map_err(|e| e.to_string())?;

    if let Some(summary_path) = &cfg.summary_out {
        let sjson = serde_json::to_string_pretty(&summary).map_err(|e| e.to_string())?;
        fs::write(summary_path, format!("{sjson}\n"))
            .map_err(|e| format!("write {summary_path}: {e}"))?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|v| (*v).to_owned()).collect()
    }

    #[test]
    fn bounded_ipc_reader_rejects_child_panic_text_and_max_u32_before_allocation() {
        let panic_stdout = b"thread 'main' panicked at worker.rs:42:9:\nexplicit panic\n";
        let mut cur1 = Cursor::new(&panic_stdout[..]);
        let err1 = read_bounded_worker_payload(&mut cur1, DEFAULT_MAX_FRAME_BYTES)
            .expect_err("ASCII 'thre' (1.95 GiB) header must fail max_frame_bytes cap immediately");
        assert!(err1.contains("exceeds max"), "unexpected error: {err1}");

        let max_u32 = [0xff, 0xff, 0xff, 0xff];
        let mut cur2 = Cursor::new(&max_u32[..]);
        let err2 = read_bounded_worker_payload(&mut cur2, DEFAULT_MAX_FRAME_BYTES)
            .expect_err("u32::MAX (4 GiB) header must fail max_frame_bytes cap immediately");
        assert!(err2.contains("exceeds max"), "unexpected error: {err2}");

        let mut empty = Cursor::new(&b""[..]);
        assert_eq!(
            read_bounded_worker_payload(&mut empty, DEFAULT_MAX_FRAME_BYTES).unwrap(),
            None
        );
    }

    #[test]
    fn dual_arm_soft_budget_evaluates_isolated_contender_timings_without_summing() {
        let mut dual_fast = json!({
            "record_index": 0,
            "status": "OK",
            "worker_elapsed_us": Value::Null,
            "track_a_elapsed_us": 36_000_u64,
            "lexbor_pure_elapsed_us": 29_000_u64,
            "contender_timings_us": {
                "legacy": 36_000_u64,
                "lexbor_pure": 29_000_u64,
            },
        });
        apply_soft_budget_to_response(&mut dual_fast, 66_000, 50);
        assert_eq!(dual_fast["status"], "OK");
        assert_eq!(dual_fast["soft_budget_exceeded"], false);
        assert_eq!(dual_fast["contender_soft_budget_exceeded"]["legacy"], false);
        assert_eq!(
            dual_fast["contender_soft_budget_exceeded"]["lexbor_pure"],
            false
        );

        let mut one_slow = json!({
            "record_index": 1,
            "status": "OK",
            "worker_elapsed_us": Value::Null,
            "track_a_elapsed_us": 58_000_u64,
            "lexbor_pure_elapsed_us": 8_000_u64,
            "contender_timings_us": {
                "legacy": 58_000_u64,
                "lexbor_pure": 8_000_u64,
            },
        });
        apply_soft_budget_to_response(&mut one_slow, 67_000, 50);
        assert_eq!(one_slow["status"], "PAGE_QUARANTINED");
        assert_eq!(one_slow["soft_budget_exceeded"], true);
        assert_eq!(one_slow["contender_soft_budget_exceeded"]["legacy"], true);
        assert_eq!(
            one_slow["contender_soft_budget_exceeded"]["lexbor_pure"],
            false
        );
    }

    #[test]
    fn soft_budget_quarantines_slow_ok_record_and_updates_summary() {
        let cfg = parse_cli_args(&s(&[
            "mode_b_pool",
            "--url",
            "https://en.wikipedia.org/wiki/Latency_budget",
            "--workers",
            "4",
            "--pin-cpu",
            "2",
            "--contender",
            "lexbor_pure",
            "--charset-mode",
            "sniffing_enabled",
            "--max-input-bytes",
            "33554432",
            "--soft-budget-ms",
            "50",
            "--watchdog-ms",
            "250",
        ]))
        .expect("parse pool cli");

        let mut slow_ok = json!({
            "record_index": 0,
            "status": "OK",
            "worker_elapsed_us": 62_500_u64,
        });
        apply_soft_budget_to_response(&mut slow_ok, 63_000, cfg.soft_budget_ms);
        assert_eq!(slow_ok["status"], "PAGE_QUARANTINED");
        assert_eq!(slow_ok["quarantine_reason"], "SOFT_BUDGET_50MS_EXCEEDED");
        assert_eq!(slow_ok["soft_budget_exceeded"], true);
        assert_eq!(slow_ok["worker_respawned"], false);

        let mut fast_ok = json!({
            "record_index": 1,
            "status": "OK",
            "worker_elapsed_us": 1_200_u64,
        });
        apply_soft_budget_to_response(&mut fast_ok, 1_500, cfg.soft_budget_ms);
        assert_eq!(fast_ok["status"], "OK");
        assert_eq!(fast_ok["soft_budget_exceeded"], false);

        let summary = summarize_pool_records(&[slow_ok, fast_ok], &cfg);
        assert_eq!(summary.total_records, 2);
        assert_eq!(summary.ok_records, 1);
        assert_eq!(summary.quarantined_records, 1);
        assert_eq!(summary.soft_budget_exceeded_records, 1);
        assert_eq!(summary.hard_watchdog_timeouts, 0);
        assert_eq!(summary.stage, "full_projection");
        assert_eq!(summary.boilerplate_policy, Some("MainContentV1"));
        assert_eq!(summary.max_input_bytes, 33_554_432);
        assert_eq!(summary.threads, 4);
        assert_eq!(summary.pin_cpu, Some(2));
        assert_eq!(summary.contender, "lexbor_pure");
        assert_eq!(summary.charset_mode, "sniffing_enabled");

        let dom_only_cfg = parse_cli_args(&s(&[
            "mode_b_pool",
            "--url",
            "https://en.wikipedia.org/wiki/Latency_budget",
            "--contender",
            "lexbor_minimal",
        ]))
        .expect("dom only pool cli");
        let dom_summary = summarize_pool_records(&[], &dom_only_cfg);
        assert_eq!(dom_summary.stage, "dom_parse_only");
        assert_eq!(dom_summary.boilerplate_policy, None);
    }

    #[test]
    fn cli_rejects_missing_and_placeholder_urls_and_invalid_max_bytes() {
        assert!(parse_cli_args(&s(&["mode_b_pool"])).is_err());
        assert!(parse_cli_args(&s(&["mode_b_pool", "--url", "https://pool.invalid/x"])).is_err());
        assert!(parse_cli_args(&s(&["mode_b_pool", "--url", "https://example.org/x"])).is_err());
        assert!(
            parse_cli_args(&s(&[
                "mode_b_pool",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--max-input-bytes",
                "0"
            ]))
            .is_err()
        );
        assert!(parse_cli_args(&s(&["mode_b_pool", "--self-test"])).is_ok());
        let cfg = parse_cli_args(&s(&[
            "mode_b_pool",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--boilerplate-policy",
            "full_dom",
            "--charset-mode",
            "transcode_whatwg",
            "--contender",
            "breakthrough",
        ]))
        .expect("pool full_dom + transcode_whatwg");
        assert_eq!(cfg.boilerplate_policy, BoilerplatePolicy::FullDom);
        assert_eq!(cfg.charset_mode, CharsetMode::SniffingEnabled);
        assert_eq!(cfg.contender, Some(Contender::Breakthrough));
    }
}
