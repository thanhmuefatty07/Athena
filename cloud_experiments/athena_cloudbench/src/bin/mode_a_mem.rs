//! Mode A-Mem: Single-Thread Exact Memory Twin.
//! Combines Rust `#[global_allocator] MeteredAlloc`, C `lexbor_memory_setup` growth hooks
//! (`athena_lxb_set_growth_hook` + `athena_lxb_current_live_bytes`) for true simultaneous
//! $\sup_t (R(t) + C(t))$, and OS kernel `mach task_vm_info.phys_footprint` /
//! Linux `/proc/self/smaps_rollup` (`Pss`, `RssAnon`).

use athena_cloudbench::bench_common::{
    CharsetMode, Contender, DEFAULT_MAX_INPUT_BYTES, boilerplate_policy_cli_str,
    boilerplate_policy_str, build_parse_limits_with_policy, decode_html_bytes, effective_limits,
    parse_boilerplate_policy, validate_real_url,
};
use athena_cloudbench::host_qos::{
    QosProfile, QosStatus, apply_qos_profile, read_os_memory_snapshot, release_allocator_free_pages,
};
use athena_core_v2::projection::{
    BoilerplatePolicy, ParseLimits, parse_html_breakthrough, parse_html_with_limits,
};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, lexbor_current_live_bytes, lexbor_set_growth_hook,
    lexbor_shim_heap_counters, lexbor_shim_reset_and_enable_metering,
    parse_and_project_str_pure_with_profile, parse_tree_str_with_limits_and_profile,
};
use serde_json::{Value, json};
use std::alloc::{GlobalAlloc, Layout, System};
use std::env;
use std::fs;
use std::hint::black_box;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicIsize, AtomicUsize, Ordering};
use url::Url;

struct MeteredAlloc;

static RUST_LIVE_BYTES: AtomicUsize = AtomicUsize::new(0);

static TRACKING_PTHREAD: AtomicUsize = AtomicUsize::new(0);
static TRACKED_ALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static TRACKED_DEALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static TRACKED_REALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static TRACKED_RUST_LIVE_DELTA: AtomicIsize = AtomicIsize::new(0);
static TRACKED_RUST_PEAK_DELTA: AtomicUsize = AtomicUsize::new(0);
static TRACKING_BASE_C: AtomicUsize = AtomicUsize::new(0);
static COMBINED_PEAK_LIVE_BYTES: AtomicUsize = AtomicUsize::new(0);
static IN_GROWTH_SAMPLE: AtomicBool = AtomicBool::new(false);
static MEASURE_LOCK: Mutex<()> = Mutex::new(());

#[global_allocator]
static GLOBAL_METERED_ALLOC: MeteredAlloc = MeteredAlloc;

#[inline]
fn current_pthread_token() -> usize {
    #[cfg(unix)]
    {
        // SAFETY: `libc::pthread_self()` returns the calling thread handle without allocating.
        let raw = unsafe { libc::pthread_self() } as usize;
        if raw == 0 { 1 } else { raw }
    }
    #[cfg(not(unix))]
    {
        1
    }
}

#[inline]
fn update_atomic_max(target: &AtomicUsize, val: usize) {
    let mut prev = target.load(Ordering::Relaxed);
    while val > prev {
        match target.compare_exchange_weak(prev, val, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => break,
            Err(actual) => prev = actual,
        }
    }
}

#[inline]
fn sample_combined_peak_from_rust_delta(rust_delta: usize) {
    if IN_GROWTH_SAMPLE
        .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
        .is_ok()
    {
        let c_delta =
            lexbor_current_live_bytes().saturating_sub(TRACKING_BASE_C.load(Ordering::Relaxed));
        update_atomic_max(
            &COMBINED_PEAK_LIVE_BYTES,
            rust_delta.saturating_add(c_delta),
        );
        IN_GROWTH_SAMPLE.store(false, Ordering::Release);
    }
}

#[inline]
fn record_tracked_alloc(bytes: usize) {
    let tracked = TRACKING_PTHREAD.load(Ordering::Relaxed);
    if tracked != 0 && current_pthread_token() == tracked {
        TRACKED_ALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
        let step = bytes as isize;
        let cur = TRACKED_RUST_LIVE_DELTA.fetch_add(step, Ordering::Relaxed) + step;
        let rust_delta = cur.max(0) as usize;
        update_atomic_max(&TRACKED_RUST_PEAK_DELTA, rust_delta);
        sample_combined_peak_from_rust_delta(rust_delta);
    }
}

extern "C" fn on_lexbor_growth(c_live: usize) {
    let tracked = TRACKING_PTHREAD.load(Ordering::Relaxed);
    if tracked != 0 && current_pthread_token() == tracked {
        let rust_delta = TRACKED_RUST_LIVE_DELTA.load(Ordering::Relaxed).max(0) as usize;
        let c_delta = c_live.saturating_sub(TRACKING_BASE_C.load(Ordering::Relaxed));
        update_atomic_max(
            &COMBINED_PEAK_LIVE_BYTES,
            rust_delta.saturating_add(c_delta),
        );
    }
}

unsafe impl GlobalAlloc for MeteredAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // SAFETY: Forwards exact `layout` to `std::alloc::System`.
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            RUST_LIVE_BYTES.fetch_add(layout.size(), Ordering::Relaxed);
            record_tracked_alloc(layout.size());
        }
        ptr
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        // SAFETY: Forwards exact `layout` to `std::alloc::System`.
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            RUST_LIVE_BYTES.fetch_add(layout.size(), Ordering::Relaxed);
            record_tracked_alloc(layout.size());
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: Forwards exact `(ptr, layout)` to `std::alloc::System`.
        unsafe { System.dealloc(ptr, layout) };
        let _ = RUST_LIVE_BYTES.try_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
            Some(v.saturating_sub(layout.size()))
        });
        let tracked = TRACKING_PTHREAD.load(Ordering::Relaxed);
        if tracked != 0 && current_pthread_token() == tracked {
            TRACKED_DEALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
            TRACKED_RUST_LIVE_DELTA.fetch_sub(layout.size() as isize, Ordering::Relaxed);
        }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        // SAFETY: Forwards exact `(ptr, layout, new_size)` to `std::alloc::System`.
        let new_ptr = unsafe { System.realloc(ptr, layout, new_size) };
        if !new_ptr.is_null() {
            if new_size >= layout.size() {
                let delta = new_size - layout.size();
                RUST_LIVE_BYTES.fetch_add(delta, Ordering::Relaxed);
            } else {
                let delta = layout.size() - new_size;
                let _ = RUST_LIVE_BYTES.try_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
                    Some(v.saturating_sub(delta))
                });
            }
            let tracked = TRACKING_PTHREAD.load(Ordering::Relaxed);
            if tracked != 0 && current_pthread_token() == tracked {
                TRACKED_REALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
                let signed_diff = (new_size as isize) - (layout.size() as isize);
                let cur =
                    TRACKED_RUST_LIVE_DELTA.fetch_add(signed_diff, Ordering::Relaxed) + signed_diff;
                if signed_diff > 0 {
                    let rust_delta = cur.max(0) as usize;
                    update_atomic_max(&TRACKED_RUST_PEAK_DELTA, rust_delta);
                    sample_combined_peak_from_rust_delta(rust_delta);
                }
            }
        }
        new_ptr
    }
}

struct HookResetGuard;

impl Drop for HookResetGuard {
    fn drop(&mut self) {
        TRACKING_PTHREAD.store(0, Ordering::SeqCst);
        lexbor_set_growth_hook(None);
    }
}

#[derive(Clone, Copy, Debug)]
struct RustHeapSnapshot {
    alloc_calls: usize,
    dealloc_calls: usize,
    realloc_calls: usize,
    baseline_live_bytes: usize,
    peak_delta_bytes: usize,
    retained_while_alive_bytes: usize,
    live_after_drop_delta_bytes: usize,
    peak_combined_live_bytes: u64,
}

fn measure_arm<T, E, F>(f: F) -> Result<(Value, RustHeapSnapshot), String>
where
    E: std::fmt::Display,
    F: FnOnce() -> Result<T, E>,
{
    let _serial = MEASURE_LOCK
        .lock()
        .map_err(|_| "mode_a_mem measurement mutex poisoned".to_owned())?;
    let _ = lexbor_shim_reset_and_enable_metering();
    let base_c = lexbor_current_live_bytes();
    lexbor_set_growth_hook(Some(on_lexbor_growth));
    let _hook_guard = HookResetGuard;

    let os_before = read_os_memory_snapshot();

    let pthread_tok = current_pthread_token();
    let base_live = RUST_LIVE_BYTES.load(Ordering::SeqCst);
    TRACKED_ALLOC_CALLS.store(0, Ordering::SeqCst);
    TRACKED_DEALLOC_CALLS.store(0, Ordering::SeqCst);
    TRACKED_REALLOC_CALLS.store(0, Ordering::SeqCst);
    TRACKED_RUST_LIVE_DELTA.store(0, Ordering::SeqCst);
    TRACKED_RUST_PEAK_DELTA.store(0, Ordering::SeqCst);
    TRACKING_BASE_C.store(base_c, Ordering::SeqCst);
    COMBINED_PEAK_LIVE_BYTES.store(0, Ordering::SeqCst);
    TRACKING_PTHREAD.store(pthread_tok, Ordering::SeqCst);

    let value = black_box(f().map_err(|e| format!("contender arm failed: {e}"))?);
    let live_while_held_delta = TRACKED_RUST_LIVE_DELTA.load(Ordering::SeqCst).max(0) as usize;
    sample_combined_peak_from_rust_delta(live_while_held_delta);
    let peak_delta_bytes = TRACKED_RUST_PEAK_DELTA.load(Ordering::SeqCst);

    TRACKING_PTHREAD.store(0, Ordering::SeqCst);
    let os_during = read_os_memory_snapshot();
    black_box(&value);
    TRACKING_PTHREAD.store(pthread_tok, Ordering::SeqCst);
    drop(value);
    TRACKING_PTHREAD.store(0, Ordering::SeqCst);

    let live_after_drop_delta = TRACKED_RUST_LIVE_DELTA.load(Ordering::SeqCst).max(0) as usize;
    let alloc_calls = TRACKED_ALLOC_CALLS.load(Ordering::SeqCst);
    let dealloc_calls = TRACKED_DEALLOC_CALLS.load(Ordering::SeqCst);
    let realloc_calls = TRACKED_REALLOC_CALLS.load(Ordering::SeqCst);
    lexbor_set_growth_hook(None);

    let c_counters = lexbor_shim_heap_counters();
    let os_after_allocator_cached = read_os_memory_snapshot();
    release_allocator_free_pages();
    let os_after = read_os_memory_snapshot();

    let simultaneous_peak = COMBINED_PEAK_LIVE_BYTES
        .load(Ordering::SeqCst)
        .max(peak_delta_bytes)
        .max(c_counters.peak_bytes) as u64;

    let rust_snap = RustHeapSnapshot {
        alloc_calls,
        dealloc_calls,
        realloc_calls,
        baseline_live_bytes: base_live,
        peak_delta_bytes,
        retained_while_alive_bytes: live_while_held_delta,
        live_after_drop_delta_bytes: live_after_drop_delta,
        peak_combined_live_bytes: simultaneous_peak,
    };

    let val = json!({
        "rust_baseline_live_bytes": rust_snap.baseline_live_bytes,
        "rust_alloc_calls": rust_snap.alloc_calls,
        "rust_dealloc_calls": rust_snap.dealloc_calls,
        "rust_realloc_calls": rust_snap.realloc_calls,
        "rust_peak_heap_bytes": rust_snap.peak_delta_bytes,
        "rust_retained_while_alive_bytes": rust_snap.retained_while_alive_bytes,
        "rust_live_after_drop_bytes": rust_snap.live_after_drop_delta_bytes,
        "c_lexbor_alloc_calls": c_counters.alloc_calls,
        "c_lexbor_peak_heap_bytes": c_counters.peak_bytes,
        "c_lexbor_live_after_destroy_bytes": c_counters.live_bytes,
        "peak_combined_live_bytes": rust_snap.peak_combined_live_bytes,
        "combined_rust_plus_c_peak_heap_bytes": rust_snap.peak_combined_live_bytes,
        "os_before": os_before,
        "os_during": os_during,
        "os_after": os_after,
        "os_after_allocator_cached": os_after_allocator_cached,
    });
    Ok((val, rust_snap))
}

struct TempFileGuard(Option<PathBuf>);

impl Drop for TempFileGuard {
    fn drop(&mut self) {
        if let Some(path) = self.0.take() {
            let _ = fs::remove_file(path);
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct MemCliConfig {
    contender: Option<Contender>,
    isolate_arms: bool,
    url: Option<String>,
    boilerplate_policy: BoilerplatePolicy,
    charset_mode: CharsetMode,
    max_input_bytes: usize,
    threads: usize,
    pin_cpu: Option<usize>,
    qos: QosProfile,
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

fn parse_arm_selection(raw: &str) -> Result<Option<Contender>, String> {
    if raw.trim().eq_ignore_ascii_case("all") {
        Ok(None)
    } else {
        Contender::parse(raw).map(Some)
    }
}

fn parse_cli_args(args: &[String]) -> Result<MemCliConfig, String> {
    let mut saw_arm: Option<Option<Contender>> = None;
    let mut saw_contender: Option<Option<Contender>> = None;
    let mut isolate_arms = true;
    let mut url: Option<String> = None;
    let mut boilerplate_policy = BoilerplatePolicy::MainContentV1;
    let mut charset_mode = CharsetMode::Utf8Assumed;
    let mut max_input_bytes = DEFAULT_MAX_INPUT_BYTES;
    let mut threads = 1usize;
    let mut pin_cpu: Option<usize> = Some(0);
    let mut qos = QosProfile::Interactive;
    let mut html_path: Option<String> = None;
    let mut self_test = false;

    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--self-test" => self_test = true,
            "--isolate-arms" => isolate_arms = true,
            "--no-isolate-arms" => isolate_arms = false,
            "--arm" => {
                i += 1;
                saw_arm = Some(parse_arm_selection(
                    args.get(i).ok_or("missing --arm value")?,
                )?);
            }
            "--contender" => {
                i += 1;
                saw_contender = Some(parse_arm_selection(
                    args.get(i).ok_or("missing --contender value")?,
                )?);
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
            "--html-file" => {
                i += 1;
                html_path = Some(args.get(i).ok_or("missing --html-file")?.clone());
            }
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    if let (Some(arm_sel), Some(cont_sel)) = (saw_arm, saw_contender)
        && arm_sel != cont_sel
    {
        return Err("conflicting --arm and --contender arguments".to_owned());
    }
    let contender = saw_arm.or(saw_contender).flatten();

    if threads != 1 {
        return Err(format!(
            "mode_a_mem enforces single-thread execution (--threads 1), got {threads}"
        ));
    }
    let _ = build_parse_limits_with_policy(max_input_bytes, boilerplate_policy)?;

    if !self_test {
        let raw_url = url
            .as_deref()
            .ok_or("missing required --url <real_url> (forbidden placeholder defaults)")?;
        let _ = validate_real_url(raw_url)?;
    } else if let Some(raw_url) = url.as_deref() {
        let _ = validate_real_url(raw_url)?;
    }

    Ok(MemCliConfig {
        contender,
        isolate_arms,
        url,
        boilerplate_policy,
        charset_mode,
        max_input_bytes,
        threads,
        pin_cpu,
        qos,
        html_path,
        self_test,
    })
}

fn default_1000_node_fixture() -> Vec<u8> {
    let mut html = String::from(
        "<!doctype html><html><head><title>Mode A-Mem 1000-Node Fixture</title></head><body><main>",
    );
    for i in 0..340 {
        html.push_str(&format!(
            "<div class=\"card-{i}\" data-bloat=\"long-attribute-payload-{i}-0123456789abcdef\" style=\"color: #222; font-size: 14px;\"><p>Paragraph {i} with <a href=\"https://en.wikipedia.org/wiki/Item_{i}\">anchor {i}</a>.</p></div>"
        ));
    }
    html.push_str("</main></body></html>");
    html.into_bytes()
}

fn measure_contender(
    contender: Contender,
    html_str: &str,
    url: &Url,
    core_limits: ParseLimits,
    policy: BoilerplatePolicy,
) -> Result<(Value, RustHeapSnapshot), String> {
    let (mut val, snap) = match contender {
        Contender::Legacy => measure_arm(|| parse_html_with_limits(html_str, url, core_limits))?,
        Contender::Breakthrough => {
            measure_arm(|| parse_html_breakthrough(html_str, url, core_limits))?
        }
        Contender::LexborPure => measure_arm(|| {
            parse_and_project_str_pure_with_profile(
                html_str,
                url,
                core_limits,
                LexborAttrProfile::Minimal,
            )
        })?,
        Contender::LexborStageIr => measure_arm(|| {
            parse_and_project_str_pure_with_profile(
                html_str,
                url,
                core_limits,
                LexborAttrProfile::StageIr,
            )
        })?,
        Contender::LexborMinimal => measure_arm(|| {
            parse_tree_str_with_limits_and_profile(
                html_str,
                core_limits,
                LexborAttrProfile::Minimal,
            )
        })?,
    };
    if let Some(obj) = val.as_object_mut() {
        obj.insert("contender".into(), json!(contender.as_str()));
        obj.insert("stage".into(), json!(contender.stage()));
        obj.insert(
            "boilerplate_policy".into(),
            json!(contender.effective_boilerplate_policy_str(policy)),
        );
        obj.insert(
            "utf8_decode_placement".into(),
            json!("pre_decoded_outside_metered_region"),
        );
    }
    Ok((val, snap))
}

fn contender_report_key(c: Contender) -> &'static str {
    match c {
        Contender::Legacy => "legacy_html5ever",
        Contender::Breakthrough => "breakthrough_html5ever",
        Contender::LexborPure => "lexbor_pure_minimal",
        Contender::LexborMinimal => "lexbor_tree_minimal",
        Contender::LexborStageIr => "lexbor_pure_stage_ir",
    }
}

fn validate_self_test_arms(
    arms_map: &serde_json::Map<String, Value>,
    policy: BoilerplatePolicy,
) -> Result<(), String> {
    for rust_key in ["legacy_html5ever", "breakthrough_html5ever"] {
        if let Some(arm_json) = arms_map.get(rust_key) {
            let r_peak = arm_json["rust_peak_heap_bytes"].as_u64().unwrap_or(0);
            if r_peak == 0 {
                return Err(format!(
                    "MeteredAlloc failed to record positive Rust peak_bytes for {rust_key}"
                ));
            }
            let during_phys = arm_json["os_during"]["phys_footprint_bytes"]
                .as_u64()
                .unwrap_or(0);
            let after_phys = arm_json["os_after"]["phys_footprint_bytes"]
                .as_u64()
                .unwrap_or(0);
            if during_phys == 0 || after_phys == 0 {
                return Err(format!(
                    "{rust_key} OS memory snapshot reported 0 phys_footprint_bytes"
                ));
            }
        }
    }

    for label in ["lexbor_pure_minimal", "lexbor_pure_stage_ir"] {
        if let Some(lxb_json) = arms_map.get(label) {
            if lxb_json["stage"] != "full_projection"
                || lxb_json["boilerplate_policy"] != boilerplate_policy_str(policy)
            {
                return Err(format!(
                    "{label} missing full_projection metadata: {lxb_json}"
                ));
            }
            let c_peak = lxb_json["c_lexbor_peak_heap_bytes"].as_u64().unwrap_or(0);
            let c_live = lxb_json["c_lexbor_live_after_destroy_bytes"]
                .as_u64()
                .unwrap_or(u64::MAX);
            let r_peak = lxb_json["rust_peak_heap_bytes"].as_u64().unwrap_or(0);
            let comb = lxb_json["peak_combined_live_bytes"].as_u64().unwrap_or(0);
            if c_peak == 0 || comb == 0 || comb > r_peak.saturating_add(c_peak) {
                return Err(format!(
                    "{label} simultaneous peak invariant failed: c_peak={c_peak}, r_peak={r_peak}, combined={comb}"
                ));
            }
            if c_live != 0 {
                return Err(format!(
                    "{label} Lexbor C heap leaked {c_live} live_bytes after lxb_html_document_destroy"
                ));
            }
            let during_phys = lxb_json["os_during"]["phys_footprint_bytes"]
                .as_u64()
                .unwrap_or(0);
            let after_phys = lxb_json["os_after"]["phys_footprint_bytes"]
                .as_u64()
                .unwrap_or(0);
            if during_phys == 0 || after_phys == 0 {
                return Err("OS memory snapshot reported 0 phys_footprint_bytes".into());
            }
        }
    }

    if let Some(min_json) = arms_map.get("lexbor_tree_minimal") {
        if min_json["stage"] != "dom_parse_only" || !min_json["boilerplate_policy"].is_null() {
            return Err(format!(
                "raw LexborTree arm must report stage=dom_parse_only and boilerplate_policy=null, got {min_json}"
            ));
        }
        let during_phys = min_json["os_during"]["phys_footprint_bytes"]
            .as_u64()
            .unwrap_or(0);
        if during_phys == 0 {
            return Err(
                "lexbor_tree_minimal OS memory snapshot reported 0 phys_footprint_bytes".into(),
            );
        }
    }

    if let (Some(pure_json), Some(ir_json)) = (
        arms_map.get("lexbor_pure_minimal"),
        arms_map.get("lexbor_pure_stage_ir"),
    ) {
        let pure_peak = pure_json["rust_peak_heap_bytes"].as_u64().unwrap_or(0);
        let ir_peak = ir_json["rust_peak_heap_bytes"].as_u64().unwrap_or(0);
        if pure_peak >= ir_peak {
            return Err(format!(
                "LexborPure Minimal projection peak ({pure_peak}) should allocate strictly fewer Rust peak heap bytes than LexborStageIr projection ({ir_peak})"
            ));
        }
    }

    Ok(())
}

fn emit_mode_a_mem_report(
    cfg: &MemCliConfig,
    qos: &QosStatus,
    url_str: &str,
    html_bytes_len: usize,
    decoded_utf8_bytes_len: usize,
    effective_isolated: bool,
    arms_map: serde_json::Map<String, Value>,
) -> Result<(), String> {
    let top_stage = match cfg.contender {
        Some(c) => c.stage(),
        None => "per_arm",
    };
    let top_boilerplate_policy = match cfg.contender {
        Some(c) => c.effective_boilerplate_policy_str(cfg.boilerplate_policy),
        None => Some(boilerplate_policy_str(cfg.boilerplate_policy)),
    };

    let report = json!({
        "schema": "AthenaCloudBenchModeAMem/1",
        "qos": qos,
        "stage": top_stage,
        "boilerplate_policy": top_boilerplate_policy,
        "utf8_decode_placement": "pre_decoded_outside_metered_region",
        "contender": cfg.contender.map(Contender::as_str),
        "arm": cfg.contender.map(contender_report_key).unwrap_or("all"),
        "isolate_arms": effective_isolated,
        "arm_isolation": if effective_isolated { "process_per_arm" } else { "in_process" },
        "url": url_str,
        "charset_mode": cfg.charset_mode.as_str(),
        "max_input_bytes": cfg.max_input_bytes,
        "threads": cfg.threads,
        "pin_cpu": cfg.pin_cpu,
        "html_bytes": html_bytes_len,
        "decoded_utf8_bytes": decoded_utf8_bytes_len,
        "arms": arms_map,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
    );
    Ok(())
}

fn run_isolated_arms_parent(
    cfg: &MemCliConfig,
    qos: &QosStatus,
    url_str: &str,
) -> Result<(), String> {
    let mut temp_guard = TempFileGuard(None);
    let child_html_arg: Option<String> = if let Some(path) = &cfg.html_path {
        let meta = fs::metadata(path).map_err(|e| format!("failed to read {path}: {e}"))?;
        if meta.is_file() {
            Some(path.clone())
        } else {
            let bytes = fs::read(path).map_err(|e| format!("failed to read {path}: {e}"))?;
            let tmp_path = env::temp_dir().join(format!(
                "athena-mode-a-mem-input-{}.html",
                std::process::id()
            ));
            fs::write(&tmp_path, &bytes)
                .map_err(|e| format!("failed to stage input to {}: {e}", tmp_path.display()))?;
            let staged = tmp_path.to_string_lossy().into_owned();
            temp_guard.0 = Some(tmp_path);
            Some(staged)
        }
    } else if cfg.self_test {
        None
    } else {
        return Err("missing required --html-file <path> outside --self-test".to_owned());
    };

    let exe = env::current_exe().map_err(|e| format!("resolve current_exe: {e}"))?;
    let pin_str = cfg
        .pin_cpu
        .map(|p| p.to_string())
        .unwrap_or_else(|| "none".to_owned());

    let mut arms_map = serde_json::Map::new();
    let mut html_bytes_len = 0usize;
    let mut decoded_utf8_bytes_len = 0usize;

    for arm in Contender::ALL {
        let key = contender_report_key(arm);
        let mut cmd = Command::new(&exe);
        cmd.arg("--arm")
            .arg(key)
            .arg("--no-isolate-arms")
            .arg("--url")
            .arg(url_str)
            .arg("--boilerplate-policy")
            .arg(boilerplate_policy_cli_str(cfg.boilerplate_policy))
            .arg("--charset-mode")
            .arg(cfg.charset_mode.as_str())
            .arg("--max-input-bytes")
            .arg(cfg.max_input_bytes.to_string())
            .arg("--threads")
            .arg("1")
            .arg("--pin-cpu")
            .arg(&pin_str)
            .arg("--qos")
            .arg(cfg.qos.as_str());
        if let Some(hpath) = &child_html_arg {
            cmd.arg("--html-file").arg(hpath);
        }
        if cfg.self_test {
            cmd.arg("--self-test");
        }

        let out = cmd
            .output()
            .map_err(|e| format!("failed to spawn isolated child for {key}: {e}"))?;
        if !out.status.success() {
            return Err(format!(
                "isolated child arm {key} failed (status {:?}): {}",
                out.status.code(),
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }

        let child_doc: Value = serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("invalid JSON from isolated child {key}: {e}"))?;
        html_bytes_len = child_doc["html_bytes"].as_u64().unwrap_or(0) as usize;
        decoded_utf8_bytes_len = child_doc["decoded_utf8_bytes"].as_u64().unwrap_or(0) as usize;
        let arm_val = child_doc
            .get("arms")
            .and_then(|a| a.get(key))
            .cloned()
            .ok_or_else(|| format!("isolated child {key} omitted arm entry"))?;
        arms_map.insert(key.to_owned(), arm_val);
    }

    drop(temp_guard);

    if cfg.self_test {
        validate_self_test_arms(&arms_map, cfg.boilerplate_policy)?;
    }

    emit_mode_a_mem_report(
        cfg,
        qos,
        url_str,
        html_bytes_len,
        decoded_utf8_bytes_len,
        true,
        arms_map,
    )
}

fn main() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    let cfg = parse_cli_args(&args)?;

    let qos = apply_qos_profile(cfg.qos, cfg.pin_cpu);
    let url_str = cfg
        .url
        .as_deref()
        .unwrap_or("https://en.wikipedia.org/wiki/Mode_a_mem");
    let url = validate_real_url(url_str)?;

    if cfg.contender.is_none() && cfg.isolate_arms {
        return run_isolated_arms_parent(&cfg, &qos, url_str);
    }

    let html_bytes = if let Some(path) = &cfg.html_path {
        fs::read(path).map_err(|e| format!("failed to read {path}: {e}"))?
    } else if cfg.self_test {
        default_1000_node_fixture()
    } else {
        return Err("missing required --html-file <path> outside --self-test".to_owned());
    };

    let base_limits = build_parse_limits_with_policy(cfg.max_input_bytes, cfg.boilerplate_policy)?;
    let html_decoded = decode_html_bytes(&html_bytes, &[], cfg.max_input_bytes, cfg.charset_mode)?;
    let html_str = html_decoded.as_ref();
    let core_limits = effective_limits(base_limits, html_str.len());

    let selected: &[Contender] = match &cfg.contender {
        Some(c) => std::slice::from_ref(c),
        None => &Contender::ALL,
    };

    let mut arms_map = serde_json::Map::new();
    for &arm in selected {
        let (val, _) = measure_contender(arm, html_str, &url, core_limits, cfg.boilerplate_policy)?;
        arms_map.insert(contender_report_key(arm).to_owned(), val);
    }

    if cfg.self_test {
        validate_self_test_arms(&arms_map, cfg.boilerplate_policy)?;
    }

    emit_mode_a_mem_report(
        &cfg,
        &qos,
        url_str,
        html_bytes.len(),
        html_str.len(),
        false,
        arms_map,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use athena_lexbor_adapter_research::{LexborCharsetMode, parse_and_project_pure_with_charset};

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|v| (*v).to_owned()).collect()
    }

    #[test]
    fn simultaneous_combined_peak_is_less_than_sum_of_staggered_rust_and_c_peaks() {
        let html = default_1000_node_fixture();
        let limits = build_parse_limits_with_policy(
            DEFAULT_MAX_INPUT_BYTES,
            BoilerplatePolicy::MainContentV1,
        )
        .expect("limits");
        let (val, rust_snap) = measure_arm(|| -> Result<usize, String> {
            let proj = parse_and_project_pure_with_charset(
                &html,
                &[],
                "https://en.wikipedia.org/wiki/Staggered_mem",
                limits,
                LexborCharsetMode::StrictUtf8,
            )
            .map_err(|e| format!("{e:?}"))?;
            let chunk_count = proj.chunks.len();
            drop(proj);

            let mut rust_spike = vec![7u8; 4 * 1024 * 1024];
            black_box(&mut rust_spike);
            let len = rust_spike.len();
            drop(rust_spike);
            Ok(chunk_count + len)
        })
        .expect("staggered measurement succeeds");

        let c_peak = val["c_lexbor_peak_heap_bytes"].as_u64().expect("c_peak");
        let c_live_end = val["c_lexbor_live_after_destroy_bytes"]
            .as_u64()
            .expect("c_live");
        let r_peak = rust_snap.peak_delta_bytes as u64;
        let combined = rust_snap.peak_combined_live_bytes;

        assert!(c_peak > 0, "expected positive C peak: {val}");
        assert_eq!(c_live_end, 0, "expected 0 leaked C bytes");
        assert!(
            r_peak >= 4 * 1024 * 1024,
            "expected >= 4 MiB Rust peak, got {r_peak}"
        );
        assert!(
            combined <= r_peak + c_peak,
            "combined {combined} must not exceed r_peak {r_peak} + c_peak {c_peak}"
        );
        assert!(
            combined < r_peak + c_peak,
            "staggered peaks must yield simultaneous sup(R+C)={combined} strictly < R_max + C_max ({})",
            r_peak + c_peak
        );
    }

    #[test]
    fn stage_labels_distinguish_full_projection_from_dom_parse_only() {
        let html_bytes = default_1000_node_fixture();
        let html_str = std::str::from_utf8(&html_bytes).unwrap();
        let url = validate_real_url("https://en.wikipedia.org/wiki/Mem_parity").unwrap();
        let limits = build_parse_limits_with_policy(
            DEFAULT_MAX_INPUT_BYTES,
            BoilerplatePolicy::MainContentV1,
        )
        .unwrap();

        let (ir_val, _) = measure_contender(
            Contender::LexborStageIr,
            html_str,
            &url,
            limits,
            BoilerplatePolicy::MainContentV1,
        )
        .unwrap();
        assert_eq!(ir_val["stage"], "full_projection");
        assert_eq!(ir_val["boilerplate_policy"], "MainContentV1");

        let (min_val, _) = measure_contender(
            Contender::LexborMinimal,
            html_str,
            &url,
            limits,
            BoilerplatePolicy::MainContentV1,
        )
        .unwrap();
        assert_eq!(min_val["stage"], "dom_parse_only");
        assert!(min_val["boilerplate_policy"].is_null());
    }

    #[test]
    fn mem_cli_rejects_missing_or_placeholder_url_and_non_unit_threads() {
        assert!(parse_cli_args(&s(&["mode_a_mem"])).is_err());
        assert!(parse_cli_args(&s(&["mode_a_mem", "--url", "https://bench.invalid/mem"])).is_err());
        assert!(parse_cli_args(&s(&["mode_a_mem", "--url", "https://example.org/mem"])).is_err());
        assert!(
            parse_cli_args(&s(&[
                "mode_a_mem",
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--threads",
                "4"
            ]))
            .is_err()
        );
        let cfg = parse_cli_args(&s(&[
            "mode_a_mem",
            "--contender",
            "lexbor_pure",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--charset-mode",
            "sniffing_enabled",
            "--max-input-bytes",
            "33554432",
            "--pin-cpu",
            "2",
        ]))
        .expect("valid args");
        assert_eq!(cfg.contender, Some(Contender::LexborPure));
        assert_eq!(cfg.boilerplate_policy, BoilerplatePolicy::MainContentV1);
        assert_eq!(cfg.charset_mode, CharsetMode::SniffingEnabled);
        assert_eq!(cfg.max_input_bytes, 33_554_432);
        assert_eq!(cfg.pin_cpu, Some(2));

        let cfg2 = parse_cli_args(&s(&[
            "mode_a_mem",
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--boilerplate-policy",
            "full_dom",
            "--charset-mode",
            "strict_utf8",
        ]))
        .expect("full_dom + strict_utf8");
        assert_eq!(cfg2.boilerplate_policy, BoilerplatePolicy::FullDom);
        assert_eq!(cfg2.charset_mode, CharsetMode::Utf8Assumed);
        assert!(cfg2.isolate_arms);
    }

    #[test]
    fn arm_cli_supports_all_canonical_names_and_isolate_arms_flags() {
        for (raw, expected) in [
            ("legacy_html5ever", Some(Contender::Legacy)),
            ("breakthrough_html5ever", Some(Contender::Breakthrough)),
            ("lexbor_pure_minimal", Some(Contender::LexborPure)),
            ("lexbor_pure_stage_ir", Some(Contender::LexborStageIr)),
            ("lexbor_tree_minimal", Some(Contender::LexborMinimal)),
            ("all", None),
        ] {
            let cfg = parse_cli_args(&s(&[
                "mode_a_mem",
                "--arm",
                raw,
                "--isolate-arms",
                "--url",
                "https://en.wikipedia.org/wiki/Memory",
            ]))
            .expect("valid --arm");
            assert_eq!(cfg.contender, expected);
            assert!(cfg.isolate_arms);
        }

        let no_iso = parse_cli_args(&s(&[
            "mode_a_mem",
            "--arm",
            "all",
            "--no-isolate-arms",
            "--url",
            "https://en.wikipedia.org/wiki/Memory",
        ]))
        .expect("valid --no-isolate-arms");
        assert_eq!(no_iso.contender, None);
        assert!(!no_iso.isolate_arms);

        assert!(
            parse_cli_args(&s(&[
                "mode_a_mem",
                "--arm",
                "legacy_html5ever",
                "--contender",
                "lexbor_tree_minimal",
                "--url",
                "https://en.wikipedia.org/wiki/Memory",
            ]))
            .is_err()
        );
    }

    #[test]
    fn os_during_captures_live_footprint_while_parsed_output_is_held_before_drop() {
        struct LiveMappedPayload {
            map_ptr: *mut libc::c_void,
            map_len: usize,
            rust_payload: Vec<u8>,
        }

        impl Drop for LiveMappedPayload {
            fn drop(&mut self) {
                black_box(self.rust_payload.as_slice());
                if !self.map_ptr.is_null() && self.map_ptr != libc::MAP_FAILED {
                    // SAFETY: `map_ptr` was returned by `libc::mmap` with length `map_len`.
                    unsafe {
                        let _ = libc::munmap(self.map_ptr, self.map_len);
                    }
                }
            }
        }

        const MAP_BYTES: usize = 16 * 1024 * 1024;
        const VEC_BYTES: usize = 4 * 1024 * 1024;

        let (val, snap) = measure_arm(|| -> Result<LiveMappedPayload, String> {
            // SAFETY: Anonymous private read-write mapping with fd=-1 and offset=0.
            let ptr = unsafe {
                libc::mmap(
                    std::ptr::null_mut(),
                    MAP_BYTES,
                    libc::PROT_READ | libc::PROT_WRITE,
                    libc::MAP_PRIVATE | libc::MAP_ANON,
                    -1,
                    0,
                )
            };
            if ptr == libc::MAP_FAILED {
                return Err("mmap failed".to_owned());
            }
            // SAFETY: `ptr` points to `MAP_BYTES` writable bytes for the lifetime of `LiveMappedPayload`.
            unsafe {
                std::ptr::write_bytes(ptr.cast::<u8>(), 0x5A, MAP_BYTES);
            }
            let mut rust_payload = vec![0xA5u8; VEC_BYTES];
            for page in rust_payload.chunks_mut(4096) {
                page[0] = page[0].wrapping_add(1);
            }
            Ok(LiveMappedPayload {
                map_ptr: ptr,
                map_len: MAP_BYTES,
                rust_payload,
            })
        })
        .expect("live payload measurement");

        assert!(snap.retained_while_alive_bytes >= VEC_BYTES);
        assert_eq!(snap.live_after_drop_delta_bytes, 0);
        assert_eq!(val["rust_live_after_drop_bytes"].as_u64(), Some(0));

        let before_phys = val["os_before"]["phys_footprint_bytes"].as_u64().unwrap();
        let during_phys = val["os_during"]["phys_footprint_bytes"].as_u64().unwrap();
        let after_phys = val["os_after"]["phys_footprint_bytes"].as_u64().unwrap();

        assert!(
            during_phys >= before_phys + 12 * 1024 * 1024,
            "os_during ({during_phys}) must capture live mapped + heap pages above os_before ({before_phys})"
        );
        assert!(
            during_phys >= after_phys + 12 * 1024 * 1024,
            "os_during ({during_phys}) must be sampled while value is STILL LIVE before drop vs os_after ({after_phys})"
        );

        #[cfg(all(target_os = "linux", target_env = "gnu"))]
        if env::var_os("ASAN_OPTIONS").is_none() {
            let (vec_val, _) = measure_arm(|| -> Result<Vec<u8>, String> {
                let mut v = vec![0x3Cu8; MAP_BYTES];
                for page in v.chunks_mut(4096) {
                    page[0] = page[0].wrapping_add(1);
                }
                Ok(v)
            })
            .expect("linux heap trim measurement");
            let v_during = vec_val["os_during"]["pss_bytes"].as_u64().unwrap();
            let v_after = vec_val["os_after"]["pss_bytes"].as_u64().unwrap();
            assert!(
                v_during > v_after,
                "Linux pss_bytes during live Vec ({v_during}) must exceed post-drop trimmed os_after ({v_after})"
            );
        }
    }
}
