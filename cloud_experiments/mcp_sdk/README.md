# Isolated MCP to Rust core integration fixture

This owner checks a local MCP tool call from the pinned Rust `rmcp` and official
TypeScript server SDK candidates into the same compiled Athena `core_probe`.
The fixture uses small synthetic HTML in `core_fixtures.json` and frozen agent
task inputs in `task_inputs.json`; it performs no external network request and
is not a representative web corpus.

Research source/spec bundles include this fixture's 14 reviewed source,
lock and support files. Installed SDKs, Cargo targets and bytecode stay outside
the bundle. The delivered-source checks execute its Python validators and owned-process observer and
reject a missing core fixture. This supplies preflight inputs; it does not add
a full L03 campaign executor or verify the target Node/Linux environment.

For each `tools/call`, both SDK arms pass the selected fixture JSON through
stdin to `core_probe export - 0` and return the Rust core's AAE JSONL. The child
process receives a sanitized environment, has a five-second deadline, and is
limited to 128 KiB of fixture input and returned output. The independent driver
also invokes the same core probe once per fixture, validates the AAE frame
structure, URL, observation time, expected extracted text and link, then
requires each SDK result to match those bytes exactly. This proves wrapper to
core wiring and transport preservation for these fixtures. The shared core
probe is still the producer and is not an independent oracle for parser
correctness or content hashes.

The long fixture includes Vietnamese text, an accented Latin word and a joined
emoji sequence. Its hand-authored expected text and the exact direct-core AAE
comparison protect valid Unicode while malformed producer UTF-8 is rejected.

The protocol sequence checks strict MCP `2026-07-28` discovery, the required
`fixture_case` schema (`long` and `short`), core outputs for both fixture sizes,
missing and invalid arguments, legacy `initialize`
rejection, and—over loopback Streamable HTTP—bearer authentication, Origin
rejection, request-size limits and stateless reconnect. Round-trip timings are
diagnostics only; this preflight cannot select a faster SDK.

The short HTTP fixture call supplies a progress token and requires the matching
`notifications/progress` event before its terminal response. The HTTP disconnect
control observes that event, closes the loopback client socket and requires the
owned core child to exit within two seconds. The Rust handler observes the
request cancellation token and kills then waits for the child; the Node adapter
forwards the Web response stream to the socket with backpressure and passes
disconnect cancellation to the child. This is local direct-child behavior,
not a latency or proxy-resilience benchmark.

For each selected transport, the same driver additionally exercises both SDKs
with four controlled producer failures: stdout one byte above 128 KiB before
EOF, invalid UTF-8, a missing final newline, and a producer that exceeds the
five-second operation deadline. When HTTP is selected, the driver also runs two
caller-disconnect controls and four bounded-concurrency controls across both
SDKs (`admission_saturation` with `ATHENA_MCP_MAX_INFLIGHT=1` plus post-disconnect
slot recovery, and `sibling_disconnect_isolation` with `ATHENA_MCP_MAX_INFLIGHT=2`
proving that a third concurrent call is rejected without spawning a child and
that disconnecting one active caller terminates only its own child while the
sibling request completes with a validated AAE frame). Selecting both transports
therefore requires four real-core SDK/transport combinations, 16 producer-failure
controls, two HTTP caller-disconnect controls, and four HTTP concurrency controls
(22 failure/disconnect/concurrency controls total). Both SDKs read
`ATHENA_MCP_MAX_INFLIGHT` at startup (default `2`, permitted integer range
`1..=8`, failing closed at startup on invalid values) and reject over-limit
`tools/call` admissions before spawning `core_probe` with a completed tool error
(`CORE_PROBE_ADMISSION_SATURATED`). The driver rejects preflight if a
producer-failure control lacks its expected completed tool-error result or if
the recorded direct child is still running when that error arrives. Disconnect
controls instead require the child to exit after the socket is closed. Individual
control failures are recorded before the full panel is rejected.

The Rust bridge enforces lock-free atomic admission (`InflightLimiter`), reads
bounded chunks and rejects excess before waiting for EOF; on operation failure
or cancellation it terminates and reaps a still-running direct child before
releasing the admission permit. The Node bridge enforces in-flight admission,
retains only in-budget chunks, rejects invalid UTF-8 without replacement, and on
failure or cancellation destroys child `stdin`/`stdout` pipes and settles its
promise as soon as the direct child exits so inherited grandchild pipes cannot
wedge admission permits. Both return `resultType: complete` with `isError: true`
and the same bounded error labels.
The child operation deadline stays five seconds; the Node HTTP socket has
seven seconds and the independent RPC caller eight, allowing timeout feedback
before an outer timeout closes the channel. These budgets do not guarantee
kernel termination under every OS failure.
The byte caps bound retained payload length; they do not measure or hard-limit
total-process memory.

Controlled producers run from an owned temporary directory, receive sanitized
environments, write at most 131,073 stdout bytes and use a 12-second self-exit
loop after reading stdin. Cleanup observes their recorded PID until exit;
those PID files are used only for observation. The Windows observer queries
the owned process exit code, and its unit check observes a real child both
alive and after exit. The Linux process-existence path and escaped descendants
have not been verified on the target host.

API grounding: [Tokio 1.54.1 child termination](https://docs.rs/tokio/1.54.1/tokio/process/struct.Child.html),
[Node 22.21.1 child close/kill semantics](https://nodejs.org/download/release/v22.21.1/docs/api/child_process.html#event-close),
[MCP tool execution errors](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling),
and [Windows exit observation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess).

The additional `athena_search_task` (`task_id`, `query`),
`athena_extract_document` (`task_id`, `source_id`, `revision`), and
`athena_expand_handle` (`task_id`, `source_id`, `revision`, `handle`) tools
invoke `core_probe task-search - 0`, `core_probe task-export - 0`, and
`core_probe task-expand - 0` respectively. A common Rust resolver selects the
frozen search candidates, HTML, URL, query and observation time, enforces the
supervisor-bound task and tenant plus exact task query or document
tenant/revision (and for `athena_expand_handle`, the BLAKE3-bound compact handle
token `ev_<hex>`), rejects HTTP-denied, orphan-304, or unsearchable-task input,
and emits structured `table_cell` AAE frames for top-level HTML tables
(excluding nested-table descendants). Callers cannot supply HTML, an output
limit, principal or task binding. The typed native requests reject duplicate and
unknown fields and are limited to 4 KiB with identifiers at most 64 UTF-8 bytes
(queries at most 256 UTF-8 bytes/chars and handle tokens at most 128 UTF-8
bytes). The CLI principal fields are trusted supervisor inputs; the CLI itself
is not a multi-user authorization boundary. The SDK tools never expose those
fields.

`task_inputs.json` is a reviewed input-only projection of `agent_tasks.json`
(containing task documents and search candidate passages without expected
document passages, gold answers, action requirements or scorer). The driver runs
the existing W2 replay consumer in full and compact modes; actual task search
candidate retrieval, document reads, and compact handle expansions cross the
selected SDK and native Rust `core_probe`. The existing independent literal gold
checks facts, citations, revision, freshness, required reads, denied reads,
unnecessary actions, and F04 HTML table row/column/group/value/unit associations
(`128/128` arms satisfy `contract_satisfied == true`, with F04
`table_contract_status == "SATISFIED"`). Each SDK/transport also receives 19
direct wrong-task, wrong-principal, foreign-document, wrong-revision,
absent-source, denied-HTTP, orphan-304, tampered-handle, wrong-query,
unsearchable-task, and caller-scope-override calls across search, extraction,
and handle expansion (`76/76` scope controls total); all must return a bounded
data-free completed tool denial or schema rejection. Successful same-scope
search/read/expansion calls distinguish actual access enforcement from a server
that denies everything. Evidence bytes, answers and quality outcomes must agree
across SDK arms.

By default the driver selects all 16 frozen tasks and both representations.
`--task-id` may repeat to select an explicit subset, which is recorded in the
report. Deliberate abstentions on direct tasks with only denied sources can make
zero native calls and cannot be counted as SDK extraction trials, while all
query tasks execute `athena_search_task` across both full and compact modes.
Canonical JSON request and response bytes and RPC counts are separated by
representation; SDK startup, discovery and total process lifetime are separate
process diagnostics. They exclude actual HTTP headers, network costs and model
usage and are not a statistical cost or speed comparison.

Workflow orchestration and scoring are still deterministic Python replay
components. This does not verify live search/crawl, cancellation through proxies
or network failure, sustained offered-load throughput, real-model quality/cost,
independent result authenticity, Linux process-tree cleanup or target-cloud
behavior. It does not close L03 or `I-LANG-AGENT`, and it cannot support a
production dependency decision.

## Local preflight

Build each arm from its reviewed lockfile. Do not regenerate or update either
lockfile during preflight:

```text
cargo +1.99.1 test --locked --offline --features research-probes --bin core_probe
cargo +1.99.1 build --release --locked --offline --features research-probes --bin core_probe
cargo +1.99.1 build --release --locked --offline --manifest-path cloud_experiments/mcp_sdk/rust/Cargo.toml
npm ci --ignore-scripts --no-audit --no-fund --prefix cloud_experiments/mcp_sdk/typescript
node --check cloud_experiments/mcp_sdk/typescript/server.mjs
python -B -m unittest -v cloud_experiments.mcp_sdk.test_driver
```

Then run with the installed Node runtime and all three compiled executables:

```text
python -B cloud_experiments/mcp_sdk/driver.py --rust-bin <absolute-path-to-athena-mcp-rust-fixture> --node <absolute-path-to-node> --typescript-server <absolute-path-to-server.mjs> --core-probe <absolute-path-to-core_probe> --transport both
```

The driver creates one random local bearer token per MCP process run, strips
known model-provider API keys from server environments, binds HTTP servers to
loopback and applies protocol startup/RPC/EOF deadlines. Windows execution
does not establish Linux process-tree isolation; that and the outstanding L03
requirements above remain `NOT VERIFIED`.
