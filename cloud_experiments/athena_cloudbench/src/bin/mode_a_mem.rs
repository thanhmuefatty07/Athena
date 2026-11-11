//! Mode A-Mem: Single-Thread Exact Memory Twin.
//! Combines Rust `#[global_allocator] MeteredAlloc`, C `lexbor_memory_setup` (16-byte aligned size header),
//! and OS kernel `mach task_vm_info.phys_footprint` / Linux `/proc/self/smaps_rollup` (`#forbid(unsafe_code)` untouched in `athena-core-v2`).

use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile, read_os_memory_snapshot};
use athena_core_v2::projection::{
    BoilerplatePolicy, ParseLimits, parse_html_breakthrough, parse_html_with_limits,
};
use athena_lexbor_adapter_research::{
    LexborAttrProfile, Limits as LexborLimits, lexbor_shim_heap_counters,
    lexbor_shim_reset_and_enable_metering, parse_and_project_pure, parse_tree_with_profile,
};
use serde_json::json;
use std::alloc::{GlobalAlloc, Layout, System};
use std::env;
use std::fs;
use std::hint::black_box;
use std::sync::atomic::{AtomicUsize, Ordering};
use url::Url;

struct MeteredAlloc;

static RUST_ALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static RUST_DEALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static RUST_REALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static RUST_LIVE_BYTES: AtomicUsize = AtomicUsize::new(0);
static RUST_PEAK_BYTES: AtomicUsize = AtomicUsize::new(0);

#[global_allocator]
static GLOBAL_METERED_ALLOC: MeteredAlloc = MeteredAlloc;

#[inline]
fn update_peak(cur: usize) {
    let mut prev = RUST_PEAK_BYTES.load(Ordering::Relaxed);
    while cur > prev {
        match RUST_PEAK_BYTES.compare_exchange_weak(prev, cur, Ordering::Relaxed, Ordering::Relaxed)
        {
            Ok(_) => break,
            Err(actual) => prev = actual,
        }
    }
}

unsafe impl GlobalAlloc for MeteredAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // SAFETY: Forwards exact `layout` to `std::alloc::System`.
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            RUST_ALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
            let cur = RUST_LIVE_BYTES.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
            update_peak(cur);
        }
        ptr
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        // SAFETY: Forwards exact `layout` to `std::alloc::System`.
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            RUST_ALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
            let cur = RUST_LIVE_BYTES.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
            update_peak(cur);
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: Forwards exact `(ptr, layout)` to `std::alloc::System`.
        unsafe { System.dealloc(ptr, layout) };
        RUST_DEALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
        let _ = RUST_LIVE_BYTES.try_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
            Some(v.saturating_sub(layout.size()))
        });
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        // SAFETY: Forwards exact `(ptr, layout, new_size)` to `std::alloc::System`.
        let new_ptr = unsafe { System.realloc(ptr, layout, new_size) };
        if !new_ptr.is_null() {
            RUST_REALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
            if new_size >= layout.size() {
                let delta = new_size - layout.size();
                let cur = RUST_LIVE_BYTES.fetch_add(delta, Ordering::Relaxed) + delta;
                update_peak(cur);
            } else {
                let delta = layout.size() - new_size;
                let _ = RUST_LIVE_BYTES.try_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
                    Some(v.saturating_sub(delta))
                });
            }
        }
        new_ptr
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
}

fn measure_arm<T, F: FnOnce() -> T>(f: F) -> Result<(serde_json::Value, RustHeapSnapshot), String> {
    let _ = lexbor_shim_reset_and_enable_metering();
    let base_live = RUST_LIVE_BYTES.load(Ordering::SeqCst);
    RUST_PEAK_BYTES.store(base_live, Ordering::SeqCst);
    let base_alloc = RUST_ALLOC_CALLS.load(Ordering::SeqCst);
    let base_dealloc = RUST_DEALLOC_CALLS.load(Ordering::SeqCst);
    let base_realloc = RUST_REALLOC_CALLS.load(Ordering::SeqCst);
    let os_before = read_os_memory_snapshot();

    let value = black_box(f());
    let live_while_held = RUST_LIVE_BYTES.load(Ordering::SeqCst);
    let peak_while_held = RUST_PEAK_BYTES.load(Ordering::SeqCst);
    let os_during = read_os_memory_snapshot();
    drop(value);

    let live_after_drop = RUST_LIVE_BYTES.load(Ordering::SeqCst);
    let c_counters = lexbor_shim_heap_counters();
    let os_after = read_os_memory_snapshot();

    let rust_snap = RustHeapSnapshot {
        alloc_calls: RUST_ALLOC_CALLS
            .load(Ordering::SeqCst)
            .saturating_sub(base_alloc),
        dealloc_calls: RUST_DEALLOC_CALLS
            .load(Ordering::SeqCst)
            .saturating_sub(base_dealloc),
        realloc_calls: RUST_REALLOC_CALLS
            .load(Ordering::SeqCst)
            .saturating_sub(base_realloc),
        baseline_live_bytes: base_live,
        peak_delta_bytes: peak_while_held.saturating_sub(base_live),
        retained_while_alive_bytes: live_while_held.saturating_sub(base_live),
        live_after_drop_delta_bytes: live_after_drop.saturating_sub(base_live),
    };
    let combined_peak =
        (rust_snap.peak_delta_bytes as u64).saturating_add(c_counters.peak_bytes as u64);

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
        "combined_rust_plus_c_peak_heap_bytes": combined_peak,
        "os_before": os_before,
        "os_during": os_during,
        "os_after": os_after,
    });
    Ok((val, rust_snap))
}

fn default_1000_node_fixture() -> Vec<u8> {
    let mut html = String::from(
        "<!doctype html><html><head><title>Mode A-Mem 1000-Node Fixture</title></head><body><main>",
    );
    for i in 0..340 {
        html.push_str(&format!(
            "<div class=\"card-{i}\" data-bloat=\"long-attribute-payload-{i}-0123456789abcdef\" style=\"color: #222; font-size: 14px;\"><p>Paragraph {i} with <a href=\"https://bench.invalid/item/{i}\">anchor {i}</a>.</p></div>"
        ));
    }
    html.push_str("</main></body></html>");
    html.into_bytes()
}

fn main() -> Result<(), String> {
    let mut html_path: Option<String> = None;
    let mut self_test = false;
    let args: Vec<String> = env::args().collect();
    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--self-test" => self_test = true,
            "--html-file" => {
                i += 1;
                html_path = Some(args.get(i).ok_or("missing --html-file")?.clone());
            }
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    let qos = apply_qos_profile(QosProfile::Interactive, Some(0));
    let html_bytes = if let Some(path) = &html_path {
        fs::read(path).map_err(|e| format!("failed to read {path}: {e}"))?
    } else {
        default_1000_node_fixture()
    };
    let html_str = String::from_utf8_lossy(&html_bytes).into_owned();
    let url_str = "https://bench.invalid/mem";
    let url = Url::parse(url_str).map_err(|e| e.to_string())?;
    let core_limits = ParseLimits {
        boilerplate: BoilerplatePolicy::MainContentV1,
        ..ParseLimits::default()
    };
    let lxb_limits = LexborLimits::default();

    let (leg_json, leg_rust) =
        measure_arm(|| parse_html_with_limits(&html_str, &url, core_limits).unwrap())?;
    let (bt_json, bt_rust) =
        measure_arm(|| parse_html_breakthrough(&html_str, &url, core_limits).unwrap())?;
    let (lxb_pure_json, _) =
        measure_arm(|| parse_and_project_pure(&html_bytes, url_str, lxb_limits).unwrap())?;
    let (lxb_min_json, lxb_min_rust) =
        measure_arm(|| parse_tree_with_profile(&html_bytes, LexborAttrProfile::Minimal).unwrap())?;
    let (lxb_ir_json, lxb_ir_rust) =
        measure_arm(|| parse_tree_with_profile(&html_bytes, LexborAttrProfile::StageIr).unwrap())?;

    if self_test {
        if leg_rust.peak_delta_bytes == 0 || bt_rust.peak_delta_bytes == 0 {
            return Err("MeteredAlloc failed to record positive Rust peak_bytes".into());
        }
        let c_peak = lxb_pure_json["c_lexbor_peak_heap_bytes"]
            .as_u64()
            .unwrap_or(0);
        let c_live = lxb_pure_json["c_lexbor_live_after_destroy_bytes"]
            .as_u64()
            .unwrap_or(u64::MAX);
        if c_peak == 0 {
            return Err("lexbor_memory_setup failed to record positive C peak_bytes".into());
        }
        if c_live != 0 {
            return Err(format!(
                "Lexbor C heap leaked {c_live} live_bytes after lxb_html_document_destroy"
            ));
        }
        if lxb_min_rust.retained_while_alive_bytes >= lxb_ir_rust.retained_while_alive_bytes {
            return Err(format!(
                "LexborAttrProfile::Minimal ({}) should retain strictly fewer Rust attribute bytes than StageIr ({})",
                lxb_min_rust.retained_while_alive_bytes, lxb_ir_rust.retained_while_alive_bytes
            ));
        }
        let phys = lxb_pure_json["os_after"]["phys_footprint_bytes"]
            .as_u64()
            .unwrap_or(0);
        if phys == 0 {
            return Err("OS memory snapshot reported 0 phys_footprint_bytes".into());
        }
    }

    let report = json!({
        "schema": "AthenaCloudBenchModeAMem/1",
        "qos": qos,
        "html_bytes": html_bytes.len(),
        "arms": {
            "legacy_html5ever": leg_json,
            "breakthrough_html5ever": bt_json,
            "lexbor_pure_minimal": lxb_pure_json,
            "lexbor_tree_minimal": lxb_min_json,
            "lexbor_tree_stage_ir": lxb_ir_json,
        }
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
    );
    Ok(())
}
