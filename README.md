# Athena V2 — verified reference core

[MASTER_BLUEPRINT.md](MASTER_BLUEPRINT.md) describes the product architecture. [ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md) records the 14 October 2026 source/dependency review, local verification, measured comparisons and remaining evidence gaps.

The current implementation is a Rust reference library and offline CLI: pinned HTTPS policy, cancellation-aware singleflight, 32-byte DOM arena, HTML5 projection, table-span grid, lexical BM25, AAE JSONL and a cost-aware tier sampler. Python models cover hypothetical Decimal costs, M/M/c queues, synthetic coalescing and probability intervals. This is not yet a deployed crawler service.

## Verification

The current toolchain and minimum Rust version are 1.99.1. CI, cloud bootstrap, native study guards and newly generated Kaggle snapshots use that pin. Historical 1.99.0 results retain their original identities; they are not measurements of 1.99.1. The committed lockfile is unchanged. See the engineering review for completed upgrade checks and the research plan §19.14 for the audit and language-boundary decisions. Remote Linux CI, production networking, fuzz/Loom/Miri, ARM64 and general peak RSS containment remain unverified.

```bash
cargo fmt --all -- --check
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --release
python -m unittest model.test_models
cargo audit --json
cargo deny --locked check advisories bans sources
```

## Run and measure

For the user-run Kaggle core/dependency study, follow [the v2 runbook](kaggle_experiments/README.md). It separates preparation, explicit kernel launch, raw-evidence download and analysis; historical Kaggle reports remain legacy. Preparing scripts never starts a cloud run.

```bash
cargo run --locked --release --bin athena-core-v2 -- fixtures/example.html https://example.org/ benchmark
cargo run --locked --release --bin bench_html -- fixtures/example.html https://example.org/ 100 benchmark
cargo run --locked --release --bin bench_core -- rank 1000 100
python model/cost_model.py model/inputs.json
python model/queueing.py
python model/singleflight_sim.py
python model/statistics.py
python -m model.benchmark results/baseline results/candidate --rounds 7 --output results/benchmark-comparison.json
```

Use module execution for `model.benchmark`: it imports Python's standard `statistics`, while this repository also has `model/statistics.py`. The comparison requires preserved baseline/candidate release executables and lockfiles. They are available in this workspace under ignored `results/`; a clean clone must build both snapshots with the same toolchain/profile and copy the identical `bench_core` harness into the baseline before building. See the review for the starting revision, fixture hashes, measurements and rejected intermediate versions. Benchmarks are warm offline CPU measurements, not production network latency.

## Resource and compatibility contracts

- `parse_html` keeps its signature and applies default `ParseLimits`: HTML, decoded text and unique resolved URL bytes each 4 MiB; each resolved URL 8192 bytes; the requested node budget includes the arena root. Use `parse_html_with_limits` to configure these budgets explicitly.
- Preformatted chunks preserve line breaks and indentation. Other chunks retain whitespace normalization and word truncation. Link IDs retain encounter order and per-owner deduplication, including nested content blocks.
- `export_jsonl` keeps its signature with a 16 MiB escaped JSONL cap including newlines/end frame. `export_jsonl_with_limit` returns a complete envelope or an error. The current implementation serializes a whole frame before checking its size and joins projected text before hashing. Earlier 8 KiB intermediate-buffer claims do not describe this source. Byte limits are logical data limits, not total allocator/RSS limits.
- `Engine::new` admits at most 32 times the configured worker count, including coalesced followers. `Engine::with_pending_limit` configures that cap; zero denies admission. Overload and total caller deadline are explicit errors. Started DNS/parser/export blocking work keeps its corresponding permits until it exits; cancellation does not forcibly terminate it. The low-level `fetch_https` API delegates concurrency admission to its caller.
- Source/representation keys include effective host and fetch limits, timeouts, policy label and tenant partition. Every caller still requires upstream source authorization before entering the engine.

Inputs exceeding new budgets intentionally return errors. New `FetchError`/`PipelineError` variants require updating exhaustive downstream matches; no external consumers were available to compile. The original AAE schema is retained; exact output equivalence is checked on the benchmark corpus, not promised for every floating-point ranking case.

## Integration gates

The HTML5 parser still constructs its tree before projection node/text quotas. Measure parser RSS/CPU under malformed input and establish containment before exposing untrusted production traffic. Persistent caching/freshness, robots/source authorization, per-tenant fairness, browser isolation, streaming backpressure, token budgets, billing and operations remain planned.

The dialer creates a client per validated DNS resolution. A pooled connector requires verified DNS/IP/policy lifetime semantics and live socket tests. The IP classifier is a conservative filter backed by registry review; production egress isolation remains necessary. The table module handles an isolated row model; a research-only HTML table traversal/selector comparator now exists under `cloud_experiments`, while inferred header semantics and production/task integration remain open. The bandit remains a heuristic requiring labeled evaluation against deterministic baselines.
