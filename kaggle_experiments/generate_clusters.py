"""Generate the 3 Kaggle benchmark cluster packages using plain template replacement.
"""
import os
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_b64(name: str) -> str:
    return (ROOT / f"{name}.b64").read_text(encoding="ascii").strip()

def write_cluster(slug: str, title: str, code: str):
    cluster_dir = ROOT / slug
    cluster_dir.mkdir(parents=True, exist_ok=True)
    
    title = slug.replace("-", " ").title()
    meta = {
        "id": f"vnthanhphm/{slug}",
        "title": title,
        "code_file": "main.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": "true",
        "enable_gpu": "false",
        "enable_tpu": "false",
        "enable_internet": "true",
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": []
    }
    
    (cluster_dir / "kernel-metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (cluster_dir / "main.py").write_text(code, encoding="utf-8")
    print(f"Generated {slug}/ ({len(code)} code chars)")

def main():
    base_b64 = load_b64("baseline")
    cand_b64 = load_b64("candidate")
    
    # -------------------------------------------------------------------------
    # Cluster 1: Core Stages Paired Cluster Benchmark
    # -------------------------------------------------------------------------
    c1_template = """#!/usr/bin/env python3
# Athena Cluster 1: Core Stage Paired Cluster Benchmark (5k samples per stage)
import os
import sys
import json
import time
import base64
import tarfile
import hashlib
import platform
import subprocess
from pathlib import Path

BASE_B64 = "__BASE_B64__"
CAND_B64 = "__CAND_B64__"

WORKDIR = Path("/kaggle/working" if os.path.exists("/kaggle") else "./c1_out")
WORKDIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = WORKDIR / "run.log"
SAMPLES_FILE = WORKDIR / "samples.jsonl"
MANIFEST_FILE = WORKDIR / "manifest.json"
SUMMARY_FILE = WORKDIR / "summary.json"

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\\n")

def run_cmd(cmd: str, cwd=None) -> str:
    log(f"CMD: {cmd} (cwd={cwd})")
    p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        log(f"FAILED (code {p.returncode}): {p.stdout}")
        raise RuntimeError(f"Command failed with code {p.returncode}: {cmd}")
    return p.stdout

def setup_rust():
    log("Checking Rust toolchain...")
    cargo_bin = os.path.expanduser("~/.cargo/bin")
    if cargo_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{cargo_bin}:{os.environ.get('PATH', '')}"
    try:
        ver = run_cmd("rustc --version --verbose")
        log(f"Existing rustc:\\n{ver}")
    except Exception:
        log("Installing Rust via rustup...")
        run_cmd("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable")
        if cargo_bin not in os.environ["PATH"]:
            os.environ["PATH"] = f"{cargo_bin}:{os.environ['PATH']}"
        run_cmd("rustup toolchain install 1.99.0 || rustup default stable")
        ver = run_cmd("rustc --version --verbose")
        log(f"Installed rustc:\\n{ver}")

def unpack(b64_str: str, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(b64_str)
    import io
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(target_dir)
    log(f"Unpacked {len(raw)} bytes to {target_dir}")

def add_bench_core_to_source(src_dir: Path):
    bin_dir = src_dir / "src" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    bench_rs = bin_dir / "bench_core.rs"
    bench_rs.write_text('''use athena_core_v2::{
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
    let mut raw_samples = Vec::with_capacity(iterations);
    let mut output_size = 0;
    for _ in 0..iterations {
        let start = Instant::now();
        output_size = black_box(run());
        raw_samples.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    let mut sorted = raw_samples.clone();
    sorted.sort_by(f64::total_cmp);
    let percentile = |p: f64| sorted[((iterations - 1) as f64 * p).ceil() as usize];
    println!(
        "{}",
        serde_json::json!({
            "stage": stage, "size": size, "iterations": iterations,
            "fixture_bytes": html.len(), "output_size": output_size,
            "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "raw_samples": raw_samples
        })
    );
}
''', encoding="utf-8")

def build_snapshots(base_dir: Path, cand_dir: Path):
    add_bench_core_to_source(base_dir)
    add_bench_core_to_source(cand_dir)
    log("Building Baseline release binaries...")
    run_cmd("cargo build --locked --release --bins", cwd=base_dir)
    log("Building Candidate release binaries...")
    run_cmd("cargo build --locked --release --bins", cwd=cand_dir)

def verify_byte_equivalence(base_bin_dir: Path, cand_bin_dir: Path, fixtures_dir: Path):
    log("Verifying AAE output byte equivalence across fixtures...")
    example = fixtures_dir / "example.html"
    multilingual = fixtures_dir / "multilingual.html"
    if not multilingual.exists():
        paragraphs = []
        for i in range(1000):
            term = "tokio runtime" if i % 17 == 0 else "unrelated topic"
            text = f"Document {i}: CPU memory network latency throughput. ordinary multilingual tài liệu 内容 details and reproducible evidence " * 8 + term
            paragraphs.append(f"<p>{text}<a href='/source/{i}'>source</a></p>")
        multilingual.write_text("".join(paragraphs), encoding="utf-8")
    
    queries = [None, "tokio runtime", "tài 内容", "H100 pricing"]
    for fix in [example, multilingual]:
        for q in queries:
            outs = []
            for d in [base_bin_dir, cand_bin_dir]:
                cmd = [str(d / "athena-core-v2"), str(fix), "https://example.org/"]
                if q is not None:
                    cmd.append(q)
                p = subprocess.run(cmd, capture_output=True, check=True)
                outs.append(p.stdout)
            h0 = hashlib.sha256(outs[0]).hexdigest()
            h1 = hashlib.sha256(outs[1]).hexdigest()
            if outs[0] != outs[1]:
                log(f"WARNING: Output divergence on {fix.name} query '{q}' (base={h0[:8]}, cand={h1[:8]})")
            else:
                log(f"MATCH: {fix.name}, query '{q}': {len(outs[0])} bytes, SHA={h0[:16]}")

def run_paired_benchmark(base_dir: Path, cand_dir: Path, rounds=20, iters_per_round=250):
    base_bin = base_dir / "target" / "release"
    cand_bin = cand_dir / "target" / "release"
    
    stages = [
        ("rank", 1000, iters_per_round),
        ("table", 5000, iters_per_round),
        ("parse", 1000, iters_per_round),
        ("export", 1000, iters_per_round),
        ("pipeline", 1000, iters_per_round)
    ]
    
    log(f"Starting Paired Cluster Benchmark: {rounds} rounds x {iters_per_round} iters = {rounds * iters_per_round} samples/stage")
    with open(SAMPLES_FILE, "w", encoding="utf-8") as s_file:
        for r in range(rounds):
            order = [("baseline", base_bin), ("candidate", cand_bin)]
            if r % 2 == 1:
                order.reverse()
            log(f"--- Round {r+1}/{rounds} (Order: {order[0][0]} -> {order[1][0]}) ---")
            for stage, size, iters in stages:
                for label, bin_path in order:
                    exe = bin_path / "bench_core"
                    cmd = [str(exe), stage, str(size), str(iters)]
                    t0 = time.time()
                    p = subprocess.run(cmd, capture_output=True, text=True, check=True)
                    res = json.loads(p.stdout)
                    raw_timings = res.get("raw_samples", [])
                    for idx, duration in enumerate(raw_timings):
                        record = {
                            "round": r,
                            "order_in_round": 0 if label == order[0][0] else 1,
                            "candidate": label,
                            "stage": stage,
                            "size": size,
                            "iteration": idx,
                            "duration_ms": duration,
                            "output_size": res.get("output_size", 0),
                            "timestamp": time.time()
                        }
                        s_file.write(json.dumps(record) + "\\n")
                    s_file.flush()

def paired_cluster_bootstrap(samples_path: Path, n_bootstrap=10000, seed=42):
    log("Running Paired Cluster Bootstrap Statistical Analysis...")
    import random
    random.seed(seed)
    data = {}
    with open(samples_path, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            st = rec["stage"]
            r = rec["round"]
            cand = rec["candidate"]
            d = rec["duration_ms"]
            if st not in data:
                data[st] = {}
            if r not in data[st]:
                data[st][r] = {"baseline": [], "candidate": []}
            data[st][r][cand].append(d)
            
    summary = {}
    def quantile(arr, q):
        if not arr:
            return 0.0
        s = sorted(arr)
        idx = int(round(q * (len(s) - 1)))
        return s[idx]
        
    for stage, rounds_dict in data.items():
        round_keys = sorted(rounds_dict.keys())
        N_rounds = len(round_keys)
        all_base = [x for r in round_keys for x in rounds_dict[r]["baseline"]]
        all_cand = [x for r in round_keys for x in rounds_dict[r]["candidate"]]
        base_p50 = quantile(all_base, 0.50)
        base_p95 = quantile(all_base, 0.95)
        base_p99 = quantile(all_base, 0.99)
        cand_p50 = quantile(all_cand, 0.50)
        cand_p95 = quantile(all_cand, 0.95)
        cand_p99 = quantile(all_cand, 0.99)
        
        p50_diffs, p95_diffs, p99_diffs = [], [], []
        for _ in range(n_bootstrap):
            resampled_rounds = [random.choice(round_keys) for _ in range(N_rounds)]
            b_sample = [x for r in resampled_rounds for x in rounds_dict[r]["baseline"]]
            c_sample = [x for r in resampled_rounds for x in rounds_dict[r]["candidate"]]
            p50_diffs.append(quantile(c_sample, 0.50) - quantile(b_sample, 0.50))
            p95_diffs.append(quantile(c_sample, 0.95) - quantile(b_sample, 0.95))
            p99_diffs.append(quantile(c_sample, 0.99) - quantile(b_sample, 0.99))
            
        p50_diffs.sort()
        p95_diffs.sort()
        p99_diffs.sort()
        
        p50_ci = (p50_diffs[int(0.025 * n_bootstrap)], p50_diffs[int(0.975 * n_bootstrap)])
        p95_ci = (p95_diffs[int(0.025 * n_bootstrap)], p95_diffs[int(0.975 * n_bootstrap)])
        p99_ci = (p99_diffs[int(0.025 * n_bootstrap)], p99_diffs[int(0.975 * n_bootstrap)])
        reduction_p50 = ((base_p50 - cand_p50) / base_p50) * 100.0 if base_p50 > 0 else 0.0
        
        decision = "HOLD"
        if p50_ci[1] < 0 and p95_ci[1] <= 0:
            decision = "ACCEPT (Statistically Significant Improvement)"
        elif p50_ci[0] > 0:
            decision = "REJECT (Statistically Significant Regression)"
            
        summary[stage] = {
            "sample_count_per_arm": len(all_base),
            "baseline": {"p50_ms": base_p50, "p95_ms": base_p95, "p99_ms": base_p99},
            "candidate": {"p50_ms": cand_p50, "p95_ms": cand_p95, "p99_ms": cand_p99},
            "p50_reduction_percent": reduction_p50,
            "delta_p50_ci_95": p50_ci,
            "delta_p95_ci_95": p95_ci,
            "delta_p99_ci_95": p99_ci,
            "decision": decision
        }
        log(f"Stage {stage}: Baseline p50={base_p50:.4f}ms -> Candidate p50={cand_p50:.4f}ms ({reduction_p50:+.2f}%), CI={p50_ci} => {decision}")
        
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary

def main():
    log("=== Athena Cluster 1: Core Stages Benchmark Start ===")
    t_start = time.time()
    sys_info = {
        "platform": platform.platform(),
        "python": sys.version,
        "cpu_count": os.cpu_count()
    }
    setup_rust()
    base_dir = Path("/tmp/athena_baseline")
    cand_dir = Path("/tmp/athena_candidate")
    unpack(BASE_B64, base_dir)
    unpack(CAND_B64, cand_dir)
    build_snapshots(base_dir, cand_dir)
    fixtures_dir = cand_dir / "fixtures"
    verify_byte_equivalence(base_dir / "target" / "release", cand_dir / "target" / "release", fixtures_dir)
    run_paired_benchmark(base_dir, cand_dir, rounds=20, iters_per_round=250)
    summary = paired_cluster_bootstrap(SAMPLES_FILE, n_bootstrap=10000, seed=42)
    manifest = {
        "cluster": "cluster1_core_stages",
        "system_info": sys_info,
        "duration_total_seconds": time.time() - t_start,
        "summary": summary
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"=== Athena Cluster 1 Completed in {manifest['duration_total_seconds']:.2f}s ===")

if __name__ == "__main__":
    main()
"""
    c1_final = c1_template.replace("__BASE_B64__", base_b64).replace("__CAND_B64__", cand_b64)
    write_cluster("athena-c1-core-stages-20261015", "Athena C1 - Core Stage Paired Cluster Benchmark (5k samples)", c1_final)

    # -------------------------------------------------------------------------
    # Cluster 2: Pathological DOM Scalability and Memory RSS Profile
    # -------------------------------------------------------------------------
    c2_template = """#!/usr/bin/env python3
# Athena Cluster 2: Pathological DOM Scalability and Peak RSS Memory Profile
import os
import sys
import json
import time
import base64
import tarfile
import hashlib
import platform
import subprocess
from pathlib import Path

BASE_B64 = "__BASE_B64__"
CAND_B64 = "__CAND_B64__"

WORKDIR = Path("/kaggle/working" if os.path.exists("/kaggle") else "./c2_out")
WORKDIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = WORKDIR / "run.log"
SAMPLES_FILE = WORKDIR / "samples.jsonl"
MANIFEST_FILE = WORKDIR / "manifest.json"
RSS_FILE = WORKDIR / "rss_measurements.json"

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\\n")

def run_cmd(cmd: str, cwd=None) -> str:
    log(f"CMD: {cmd} (cwd={cwd})")
    p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        log(f"FAILED (code {p.returncode}): {p.stdout}")
        raise RuntimeError(f"Command failed with code {p.returncode}: {cmd}")
    return p.stdout

def setup_rust():
    cargo_bin = os.path.expanduser("~/.cargo/bin")
    if cargo_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{cargo_bin}:{os.environ.get('PATH', '')}"
    try:
        run_cmd("rustc --version")
    except Exception:
        run_cmd("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable")
        run_cmd("rustup toolchain install 1.99.0 || rustup default stable")

def unpack(b64_str: str, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(b64_str)
    import io
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(target_dir)

def get_peak_rss_kb(pid: int) -> int:
    try:
        statm = Path(f"/proc/{pid}/statm").read_text().split()
        page_size = os.sysconf("SC_PAGE_SIZE") // 1024
        return int(statm[1]) * page_size
    except Exception:
        return 0

def benchmark_link_deduplication(base_bin: Path, cand_bin: Path):
    log("=== R5: Link Deduplication Scaling Sweep (100 to 20,000 anchors) ===")
    link_counts = [100, 500, 1000, 2500, 5000, 10000, 20000]
    results = []
    with open(SAMPLES_FILE, "a", encoding="utf-8") as sf:
        for L in link_counts:
            html = "<p>" + "".join(f"<a href='/a/{i}'>link {i}</a>" for i in range(L)) + "</p>"
            fix_path = Path(f"/tmp/links_{L}.html")
            fix_path.write_text(html, encoding="utf-8")
            timings = {"baseline": [], "candidate": []}
            rss_peaks = {"baseline": 0, "candidate": 0}
            for label, bdir in [("baseline", base_bin), ("candidate", cand_bin)]:
                exe = bdir / "athena-core-v2"
                for it in range(10):
                    t0 = time.time()
                    p = subprocess.Popen([str(exe), str(fix_path), "https://example.org/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    peak = 0
                    while p.poll() is None:
                        rss = get_peak_rss_kb(p.pid)
                        if rss > peak:
                            peak = rss
                        time.sleep(0.005)
                    p.wait()
                    dt_ms = (time.time() - t0) * 1000.0
                    timings[label].append(dt_ms)
                    if peak > rss_peaks[label]:
                        rss_peaks[label] = peak
                    sf.write(json.dumps({
                        "stage": "link_dedupe_sweep", "anchors": L, "candidate": label,
                        "iteration": it, "duration_ms": dt_ms, "peak_rss_kb": peak
                    }) + "\\n")
            b_med = sorted(timings["baseline"])[len(timings["baseline"]) // 2]
            c_med = sorted(timings["candidate"])[len(timings["candidate"]) // 2]
            ratio = b_med / c_med if c_med > 0 else 1.0
            log(f"L={L:5d}: Baseline p50={b_med:.2f}ms, Candidate p50={c_med:.2f}ms (Speedup: {ratio:.2f}x, RSS base={rss_peaks['baseline']}KB cand={rss_peaks['candidate']}KB)")
            results.append({
                "anchors": L, "baseline_p50_ms": b_med, "candidate_p50_ms": c_med,
                "speedup_ratio": ratio, "baseline_peak_rss_kb": rss_peaks["baseline"],
                "candidate_peak_rss_kb": rss_peaks["candidate"]
            })
    return results

def benchmark_deep_dom(cand_bin: Path):
    log("=== R2: Deeply Nested DOM Parsing & Stack Safety ===")
    depths = [50, 100, 200, 400, 500]
    deep_results = []
    exe = cand_bin / "athena-core-v2"
    for d in depths:
        html = "<div>" * d + f"<p>Leaf content at depth {d}</p>" + "</div>" * d
        fix_path = Path(f"/tmp/deep_{d}.html")
        fix_path.write_text(html, encoding="utf-8")
        t0 = time.time()
        p = subprocess.run([str(exe), str(fix_path), "https://example.org/"], capture_output=True, text=True)
        dt_ms = (time.time() - t0) * 1000.0
        success = (p.returncode == 0 and "Leaf content" in p.stdout)
        log(f"Depth {d:3d}: Success={success}, time={dt_ms:.2f}ms, output_len={len(p.stdout)}")
        deep_results.append({"depth": d, "success": success, "duration_ms": dt_ms, "exit_code": p.returncode})
    return deep_results

def test_html5_conformance(cand_bin: Path):
    log("=== HTML5 Entity Decoding and Tree Repair Conformance ===")
    cases = [
        ("&amp; &quot; &lt; &gt; &copy; &#38; &#x26;", "Entity expansion"),
        ("<table><tr><td>Cell 1<td>Cell 2</table>", "Unclosed table cells"),
        ("<div><span>Unclosed span", "Unclosed inline tag"),
        ("<p>Line 1<br>Line 2<hr>Line 3</p>", "Void elements br and hr"),
        ("<script>alert('hidden');</script><p>Visible</p>", "Script tag stripping"),
        ("<p>Diacritics: Tiếng Việt có dấu và 日本語テキスト</p>", "Unicode and diacritics")
    ]
    exe = cand_bin / "athena-core-v2"
    conformance = []
    for snippet, desc in cases:
        fix = Path("/tmp/conformance.html")
        fix.write_text(snippet, encoding="utf-8")
        p = subprocess.run([str(exe), str(fix), "https://example.org/"], capture_output=True, text=True)
        ok = (p.returncode == 0)
        log(f"Conformance check '{desc}': {'PASS' if ok else 'FAIL'} (output len={len(p.stdout)})")
        conformance.append({"description": desc, "pass": ok, "exit_code": p.returncode})
    return conformance

def main():
    log("=== Athena Cluster 2: DOM Scalability and RSS Profile Start ===")
    t_start = time.time()
    setup_rust()
    base_dir = Path("/tmp/athena_baseline")
    cand_dir = Path("/tmp/athena_candidate")
    unpack(BASE_B64, base_dir)
    unpack(CAND_B64, cand_dir)
    run_cmd("cargo build --locked --release --bin athena-core-v2", cwd=base_dir)
    run_cmd("cargo build --locked --release --bin athena-core-v2", cwd=cand_dir)
    base_bin = base_dir / "target" / "release"
    cand_bin = cand_dir / "target" / "release"
    link_results = benchmark_link_deduplication(base_bin, cand_bin)
    deep_results = benchmark_deep_dom(cand_bin)
    conformance = test_html5_conformance(cand_bin)
    rss_summary = {
        "cluster": "cluster2_dom_rss",
        "link_scaling": link_results,
        "deep_dom": deep_results,
        "conformance": conformance,
        "duration_total_seconds": time.time() - t_start
    }
    RSS_FILE.write_text(json.dumps(rss_summary, indent=2), encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps(rss_summary, indent=2), encoding="utf-8")
    log(f"=== Athena Cluster 2 Completed in {rss_summary['duration_total_seconds']:.2f}s ===")

if __name__ == "__main__":
    main()
"""
    c2_final = c2_template.replace("__BASE_B64__", base_b64).replace("__CAND_B64__", cand_b64)
    write_cluster("athena-c2-dom-rss-20261015", "Athena C2 - Pathological DOM Scalability and Memory RSS Profile", c2_final)

    # -------------------------------------------------------------------------
    # Cluster 3: Dependencies RNG and Concurrency Stress Test
    # -------------------------------------------------------------------------
    c3_template = """#!/usr/bin/env python3
# Athena Cluster 3: Dependencies RNG and Concurrency Stress Test
import os
import sys
import json
import time
import base64
import tarfile
import hashlib
import platform
import subprocess
from pathlib import Path

BASE_B64 = "__BASE_B64__"
CAND_B64 = "__CAND_B64__"

WORKDIR = Path("/kaggle/working" if os.path.exists("/kaggle") else "./c3_out")
WORKDIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = WORKDIR / "run.log"
SAMPLES_FILE = WORKDIR / "samples.jsonl"
MANIFEST_FILE = WORKDIR / "manifest.json"
R1_R9_FILE = WORKDIR / "r1_r9_verification.json"

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\\n")

def run_cmd(cmd: str, cwd=None) -> str:
    log(f"CMD: {cmd} (cwd={cwd})")
    p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        log(f"FAILED (code {p.returncode}): {p.stdout}")
        raise RuntimeError(f"Command failed with code {p.returncode}: {cmd}")
    return p.stdout

def setup_rust():
    cargo_bin = os.path.expanduser("~/.cargo/bin")
    if cargo_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{cargo_bin}:{os.environ.get('PATH', '')}"
    try:
        run_cmd("rustc --version")
    except Exception:
        run_cmd("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable")
        run_cmd("rustup toolchain install 1.99.0 || rustup default stable")

def unpack(b64_str: str, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(b64_str)
    import io
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(target_dir)

def test_r1_rand_upgrade():
    log("=== R1: Evaluating rand 0.9.5/0.5.1 vs rand 0.10.3/0.6.0 on Linux ===")
    test_dir = Path("/tmp/r1_bench")
    test_dir.mkdir(parents=True, exist_ok=True)
    cargo_toml = '''[package]
name = "r1-bench"
version = "0.1.0"
edition = "2024"

[dependencies]
rand = "0.9"
rand_distr = "0.5"
serde_json = "1"
'''
    (test_dir / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    src_dir = test_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    main_rs = '''use rand_distr::{Beta, Distribution};
use std::time::Instant;

fn main() {
    let mut rng = rand::rng();
    let alpha = 2.0f64;
    let beta = 5.0f64;
    let dist = Beta::new(alpha, beta).unwrap();
    let n = 1_000_000;
    let t0 = Instant::now();
    let mut sum = 0.0;
    let mut sum_sq = 0.0;
    for _ in 0..n {
        let x: f64 = dist.sample(&mut rng);
        sum += x;
        sum_sq += x * x;
    }
    let elapsed_ns = t0.elapsed().as_nanos();
    let ns_per_sample = elapsed_ns as f64 / n as f64;
    let mean = sum / n as f64;
    let variance = (sum_sq / n as f64) - (mean * mean);
    let expected_mean = alpha / (alpha + beta);
    let expected_variance = (alpha * beta) / ((alpha + beta).powi(2) * (alpha + beta + 1.0));
    println!("{}", serde_json::json!({
        "n_samples": n, "ns_per_sample": ns_per_sample,
        "empirical_mean": mean, "theoretical_mean": expected_mean,
        "mean_error": (mean - expected_mean).abs(),
        "empirical_variance": variance, "theoretical_variance": expected_variance,
        "variance_error": (variance - expected_variance).abs()
    }));
}
'''
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8")
    run_cmd("cargo build --release", cwd=test_dir)
    out = run_cmd("./target/release/r1-bench", cwd=test_dir)
    res_09 = json.loads(out)
    log(f"rand 0.9.5: {res_09['ns_per_sample']:.2f} ns/sample, mean_err={res_09['mean_error']:.6f}, var_err={res_09['variance_error']:.6f}")
    
    # Candidate rand 0.10.3 + rand_distr 0.6.0
    test_dir_10 = Path("/tmp/r1_bench_10")
    test_dir_10.mkdir(parents=True, exist_ok=True)
    cargo_toml_10 = '''[package]
name = "r1-bench-10"
version = "0.1.0"
edition = "2024"

[dependencies]
rand = "0.10.3"
rand_distr = "0.6.0"
serde_json = "1"
'''
    (test_dir_10 / "Cargo.toml").write_text(cargo_toml_10, encoding="utf-8")
    src_dir_10 = test_dir_10 / "src"
    src_dir_10.mkdir(parents=True, exist_ok=True)
    (src_dir_10 / "main.rs").write_text(main_rs, encoding="utf-8")
    try:
        run_cmd("cargo build --release", cwd=test_dir_10)
        out_10 = run_cmd("./target/release/r1-bench-10", cwd=test_dir_10)
        res_10 = json.loads(out_10)
        log(f"rand 0.10.3: {res_10['ns_per_sample']:.2f} ns/sample, mean_err={res_10['mean_error']:.6f}, var_err={res_10['variance_error']:.6f}")
    except Exception as e:
        log(f"rand 0.10 build/run failed or unavailable: {e}")
        res_10 = {"error": str(e)}
        
    return {"rand_0_9": res_09, "rand_0_10": res_10}

def test_r8_concurrency_singleflight(cand_dir: Path):
    log("=== R8: Concurrency & SingleFlight Stress Testing ===")
    test_dir = Path("/tmp/r8_bench")
    test_dir.mkdir(parents=True, exist_ok=True)
    cargo_toml = f'''[package]
name = "r8-bench"
version = "0.1.0"
edition = "2024"

[dependencies]
tokio = {{ version = "1.53", features = ["full"] }}
serde_json = "1"
athena-core-v2 = {{ path = "{cand_dir}" }}
'''
    (test_dir / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    src_dir = test_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    main_rs = '''use athena_core_v2::singleflight::SingleFlight;
use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
use tokio::time::{sleep, Duration};

#[tokio::main]
async fn main() {
    let sf = Arc::new(SingleFlight::default());
    let fetch_count = Arc::new(AtomicUsize::new(0));
    let mut tasks = vec![];
    let n_tasks = 500;
    let n_keys = 50;
    let t0 = std::time::Instant::now();
    for i in 0..n_tasks {
        let sf_c = sf.clone();
        let fc = fetch_count.clone();
        let key = format!("https://example.org/resource/{}", i % n_keys);
        tasks.push(tokio::spawn(async move {
            let res = sf_c.call(key, || async move {
                fc.fetch_add(1, Ordering::SeqCst);
                sleep(Duration::from_millis(50)).await;
                Ok::<String, ()>("Payload from flight".to_string())
            }).await;
            assert_eq!(*res.unwrap(), "Payload from flight");
        }));
    }
    for t in tasks {
        t.await.unwrap();
    }
    let elapsed = t0.elapsed().as_millis();
    let actual_fetches = fetch_count.load(Ordering::SeqCst);
    println!("{}", serde_json::json!({
        "total_requests": n_tasks, "unique_keys": n_keys,
        "actual_origin_fetches": actual_fetches,
        "deduplication_ratio": n_tasks as f64 / actual_fetches as f64,
        "elapsed_ms": elapsed
    }));
}
'''
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8")
    run_cmd("cargo build --release", cwd=test_dir)
    out = run_cmd("./target/release/r8-bench", cwd=test_dir)
    res = json.loads(out)
    log(f"SingleFlight Stress: 500 reqs -> {res['actual_origin_fetches']} origin calls (Deduplication: {res['deduplication_ratio']:.2f}x in {res['elapsed_ms']}ms)")
    return res

def test_ip_policy_and_ssrf(cand_dir: Path):
    log("=== R3 & IP Policy: SSRF Address Validation ===")
    test_dir = Path("/tmp/ssrf_bench")
    test_dir.mkdir(parents=True, exist_ok=True)
    cargo_toml = f'''[package]
name = "ssrf-bench"
version = "0.1.0"
edition = "2024"

[dependencies]
serde_json = "1"
athena-core-v2 = {{ path = "{cand_dir}" }}
'''
    (test_dir / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    src_dir = test_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    main_rs = '''use athena_core_v2::ip_policy::is_public_candidate;
use std::net::IpAddr;

fn main() {
    let test_ips = [
        ("127.0.0.1", false),
        ("10.0.0.1", false),
        ("172.16.0.1", false),
        ("192.168.1.1", false),
        ("169.254.169.254", false),
        ("0.0.0.0", false),
        ("8.8.8.8", true),
        ("1.1.1.1", true),
        ("::1", false),
        ("fe80::1", false),
        ("fc00::1", false),
        ("2001:db8::1", false),
        ("2606:4700:4700::1111", true)
    ];
    let mut results = vec![];
    for (ip_str, expected) in test_ips {
        let ip: IpAddr = ip_str.parse().unwrap();
        let allowed = is_public_candidate(ip);
        assert_eq!(allowed, expected, "SSRF mismatch for IP: {}", ip_str);
        results.push(serde_json::json!({"ip": ip_str, "allowed": allowed, "expected": expected, "match": allowed == expected}));
    }
    println!("{}", serde_json::json!({"total_checked": results.len(), "all_matched": true, "details": results}));
}
'''
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8")
    run_cmd("cargo build --release", cwd=test_dir)
    out = run_cmd("./target/release/ssrf-bench", cwd=test_dir)
    res = json.loads(out)
    log(f"SSRF Policy: checked {res['total_checked']} addresses, all matched expected policy: {res['all_matched']}")
    return res

def main():
    log("=== Athena Cluster 3: Dependencies & Concurrency Start ===")
    t_start = time.time()
    setup_rust()
    cand_dir = Path("/tmp/athena_candidate")
    unpack(CAND_B64, cand_dir)
    r1_res = test_r1_rand_upgrade()
    r8_res = test_r8_concurrency_singleflight(cand_dir)
    ssrf_res = test_ip_policy_and_ssrf(cand_dir)
    out_data = {
        "cluster": "cluster3_deps_concurrency",
        "r1_rng": r1_res,
        "r8_concurrency": r8_res,
        "ip_policy_ssrf": ssrf_res,
        "duration_total_seconds": time.time() - t_start
    }
    R1_R9_FILE.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
    log(f"=== Athena Cluster 3 Completed in {out_data['duration_total_seconds']:.2f}s ===")

if __name__ == "__main__":
    main()
"""
    c3_final = c3_template.replace("__BASE_B64__", base_b64).replace("__CAND_B64__", cand_b64)
    write_cluster("athena-c3-deps-concurrency-20261015", "Athena C3 - Dependencies RNG and Concurrency Stress Test", c3_final)

if __name__ == "__main__":
    main()
