# Athena V2 — source-first audited implementation blueprint

**Primary deliverable:** [MASTER_BLUEPRINT.md](MASTER_BLUEPRINT.md), 500+ lines of Vietnamese technical design with requirements, math, security, performance plan, cited standards and go/no-go gates.

**Runnable-oriented Rust reference:** `src/` — network policy + pinned HTTPS, singleflight cancellation, 32-byte node arena, HTML5 projection, span grid, lexical BM25, AAE JSONL, pipeline and cost-aware tier sampler.

**Numerical models:** `model/` — editable example inputs, Decimal COGS model, queueing Erlang-C, synthetic coalescing trace, Wilson/Clopper-Pearson statistics.

## Verification status (must not be misrepresented)

The Python numerical programs have been run, and the inputs are labeled hypothetical. **No Rust compiler was present in the authoring environment, so the source has not yet passed cargo check, tests, clippy, fuzz or performance benchmarks.** Fix build errors before integrating. This is a *reference vertical slice* and an exhaustive architecture plan, not a finished commercial crawler. Browser sandbox, robots policy, persistent caching, streaming network AAE, distributed scheduling and billing remain to implement.

## Test on a machine with Rust toolchain

```bash
rustup toolchain install 1.99.0
cargo generate-lockfile
cargo fmt --all
cargo check --all-targets
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
cargo build --release
cargo run --release --bin athena-core-v2 -- fixtures/example.html https://example.org/ benchmark
cargo run --release --bin bench_html -- fixtures/example.html https://example.org/ 1000 benchmark
python model/cost_model.py model/inputs.json
python model/queueing.py
python model/singleflight_sim.py
python model/statistics.py
```

Commit `Cargo.lock` after validating dependencies and rerun CI with `--locked` once present. Pin GitHub Actions to audited SHAs before production.

## Production constraints

- Only access domains after source permission, robots, rate and privacy checks (upstream policy service is NOT implemented here).
- `src/network.rs` intentionally avoids connection pooling by constructing a client per pinned resolution. Do not bypass DNS-to-connect policy in pursuit of performance; implement a validated pooled connector.
- The current public-address classifier is defense-in-depth, not proof of public reachability; use IANA tables, egress firewall and network-isolation validation in production.
- AAE JSONL currently builds a `String`, not live network stream; query highlights are prioritized but the page is not yet compressed to a token budget.
- `src/table.rs` handles an isolated row-model; HTML table extraction, `scope/headers` and WHATWG conformance still need integration.
- The sampled cost-aware bandit is a heuristic; compare against deterministic baselines before routing production requests.
