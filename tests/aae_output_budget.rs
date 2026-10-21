use athena_core_v2::aae::export_jsonl_with_limit;
use athena_core_v2::projection::parse_html;
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use url::Url;

struct MeteredSystem;

static LIVE_BYTES: AtomicUsize = AtomicUsize::new(0);
static PEAK_BYTES: AtomicUsize = AtomicUsize::new(0);

fn record_allocation(bytes: usize) {
    let live = LIVE_BYTES.fetch_add(bytes, Ordering::Relaxed) + bytes;
    PEAK_BYTES.fetch_max(live, Ordering::Relaxed);
}

unsafe impl GlobalAlloc for MeteredSystem {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            record_allocation(layout.size());
        }
        ptr
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            record_allocation(layout.size());
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        LIVE_BYTES.fetch_sub(layout.size(), Ordering::Relaxed);
        unsafe { System.dealloc(ptr, layout) };
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let resized = unsafe { System.realloc(ptr, layout, new_size) };
        if !resized.is_null() {
            if new_size >= layout.size() {
                record_allocation(new_size - layout.size());
            } else {
                LIVE_BYTES.fetch_sub(layout.size() - new_size, Ordering::Relaxed);
            }
        }
        resized
    }
}

#[global_allocator]
static ALLOCATOR: MeteredSystem = MeteredSystem;

#[test]
fn over_budget_export_does_not_materialize_escaped_frame_or_joined_page_copy() {
    let base = Url::parse("https://example.org/").unwrap();
    let html = format!("<p>{}</p>", "\u{1}".repeat(2_900_000));
    let page = parse_html(&html, &base, 100, 100).unwrap();
    assert_eq!(page.chunks.len(), 1);
    assert!(page.chunks[0].text.len() >= 2_900_000);
    drop(html);

    let small_page = parse_html("<p>small</p>", &base, 100, 100).unwrap();
    let small_output = export_jsonl_with_limit(&small_page, &base, 1, None, 0, 4096).unwrap();
    assert!(
        small_output.len() < 4096,
        "Metadata and framing fit within the test cap"
    );
    assert!(
        page.chunks[0].text.len().saturating_mul(6) > 4096,
        "Required JSON escaping makes the block payload independently exceed the cap"
    );
    drop(small_output);
    drop(small_page);

    let before = LIVE_BYTES.load(Ordering::Relaxed);
    PEAK_BYTES.store(before, Ordering::Relaxed);
    let result = export_jsonl_with_limit(&page, &base, 1, None, 0, 4096);
    let peak = PEAK_BYTES.load(Ordering::Relaxed);

    let error = result.expect_err("An oversized escaped block must be rejected");
    assert!(error.is_io());
    assert!(
        error
            .to_string()
            .contains("AAE output byte budget exceeded"),
        "Unrelated early I/O failures are not evidence of budget enforcement: {error}"
    );
    let temporary_peak = peak.saturating_sub(before);
    assert!(
        temporary_peak < 1024 * 1024,
        "over-budget export allocated {temporary_peak} temporary bytes after input construction"
    );
}
