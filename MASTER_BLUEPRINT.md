# ATHENA V2 — MASTER TECHNICAL BLUEPRINT / IMPLEMENTATION REFERENCE

**Ngày khóa thiết kế:** 14/10/2026; cập nhật phạm vi nghiên cứu 18/10/2026
**Bản:** 2.0.5-research-decisions
**Ngôn ngữ triển khai lõi:** Rust 1.99.1, edition 2024

**Trạng thái chứng cứ:** Lõi tham chiếu đã build bằng Rust 1.99.0 trên Windows và Kaggle Linux x86_64. Study 2.1 có raw CPU, process peak RSS trên corpus links và đối chiếu tuple RNG; phán quyết nằm trong `kaggle_results/v21-run-004/research-decision.json` (trong đó `cpu_core` đã được dỡ bỏ cổng REJECT sau khi tối ưu hóa AAE serializer/hasher, đạt `READY_FOR_SCOPED_REVIEW`), diễn giải hiện hành trong `CORE_RESEARCH_PLAN.md` và `CHECKPOINT.md`. **Chưa đạt trạng thái production-verified**: chưa đo hệ thống triển khai, live network/browser/harness end-to-end, allocator/containment tổng quát, fuzz, Loom/Miri hay ARM64. Audit cũ không thay advisory gate cho tuple mới.

**Ưu tiên hiện tại của người dùng:** lập kế hoạch và định hướng nghiên cứu; chốt triển khai sau khi agent khác cung cấp bằng chứng. [CORE_RESEARCH_PLAN.md](CORE_RESEARCH_PLAN.md) quy định stack Rust, các phương án so sánh, Kaggle và protocol kiểm chứng. Các sửa đổi mã hiện có là draft chưa được chốt; kết quả Windows không thay kết quả Kaggle. Không lấy roadmap dài hạn bên dưới làm lệnh bắt đầu coding.

**Phạm vi bổ sung ngày 15/10/2026:** được sửa harness nghiên cứu để khắc phục sai lệch đo lường; người dùng tự chạy benchmark Kaggle. [Protocol v2 và hướng dẫn chạy](kaggle_experiments/README.md) là hợp đồng thực nghiệm hiện tại. Kết quả legacy không tự duyệt candidate; thay production core cần quyết định theo từng nhóm bằng chứng.

**Phạm vi đã thống nhất ngày 16/10/2026:** core trước, chỉ mục web riêng để sau. Nghiên cứu cách phối hợp tìm nguồn, đọc/trích xuất/crawl và truy cập nguồn được phép, lấy Exa/Firecrawl/Bright Data làm tham chiếu năng lực; bổ sung **Search as Code (SaC)** của Perplexity để so sánh cách agent lập workflow qua SDK trong sandbox. [CORE_RESEARCH_PLAN.md](CORE_RESEARCH_PLAN.md) mục 3.4–3.7/6.6 quy định định hướng, mục **9–13** quy định giao việc: U01–U10, owner/consumer map, WP00–WP08, task/workflow contracts, oracle/panel, spec/resource/trust và gate G0–G6. Agent nhận đúng một scope, giữ baseline và evidence identity, không suy blueprint thành lệnh triển khai toàn bộ. Đây là **[PROPOSED]**, chưa phải API/SDK/runner đã triển khai hoặc quyết định gọi các dịch vụ ngoài.

> Mục tiêu: công cụ truy xuất web được cấp phép, phục vụ AI agent, tối thiểu chi phí trên **mỗi kết quả đúng và đủ bằng chứng**, hỗ trợ freshness có thể quan sát, định dạng đầu ra có cấu trúc, ưu tiên hiệu suất hệ thống end-to-end. Không lấy việc vượt CAPTCHA, giả mạo fingerprint, lấy clearance cookie, quét proxy mở hoặc né rate limit làm cơ chế kinh doanh.

## 0. Phân biệt bằng chứng

- **[STD]**: điều kiện từ đặc tả được đối chiếu tài liệu gốc (RFC, WHATWG, Rust docs, OWASP).
- **[DERIVED]**: toán học suy ra chính xác *từ đầu vào giả định đã ghi*.
- **[IMPLEMENTED]**: có mã trong workspace; không tự suy ra đã kiểm thử. Kết quả test cụ thể nằm trong báo cáo kiểm chứng.
- **[VERIFIED-OFFLINE]**: đã chạy kiểm tra được nêu rõ trên môi trường/corpus ghi trong báo cáo; không suy rộng thành bảo đảm production.
- **[PROPOSED]**: mô-đun hoặc thuật toán chưa triển khai hoàn chỉnh.
- **[MEASURE]**: số liệu chỉ được điền sau khi benchmark thật và lưu artefact tái lập.

Không dùng nhãn “chính xác tuyệt đối” cho biến ngẫu nhiên, hiệu năng nền mạng, giá cloud biến thiên, hay suy luận nhân quả từ quan sát thiếu mẫu. Có thể chính xác tuyệt đối về **phép tính có điều kiện**, không thể tuyệt đối về **đầu vào chưa biết**.

## 1. North-star product contract

### 1.1 Hệ thống làm gì

**[PROPOSED] Hợp đồng sản phẩm đích; không phải toàn bộ tính năng của reference core hiện tại.**

Athena nhận URL đã được policy cho phép hoặc search candidates đã được xác thực, trả về kết quả structured `aae/1`, bao gồm: nguồn và thời gian quan sát; nội dung liên quan câu hỏi; thứ tự DOM có thể truy hồi; bảng giữ được ngữ nghĩa `rowspan/colspan`; ánh xạ links và action identifiers; hash nội dung; trạng thái “live fetch”, “origin revalidated”, “cache” được phân biệt minh bạch.

Athena **không** cam kết: toàn bộ web đều truy cập được; nội dung đổi mỗi mili-giây được thấy tức thời; mọi site có `ETag`; mọi bảng malformed đều không mất thông tin; có thể lấy data đăng nhập / API riêng bằng quyền không được cấp; DOM sẽ luôn nhỏ hơn Markdown theo token; mọi tác vụ đạt độ trễ <15 ms.

### 1.2 API hợp đồng tối thiểu

```http
POST /v1/retrieve
Content-Type: application/json
Authorization: Bearer <customer_token>

{
  "url": "https://docs.example.org/topic",
  "query": "What changed in the CPU requirements?",
  "representation": "aae/1",
  "freshness": {"mode":"origin_validation", "max_age_ms":0},
  "limits": {"deadline_ms":12000,"output_tokens":1600,"max_origin_bytes":4194304},
  "tenant_partition":"opaque-tenant-id"
}
```

Kết quả trả kèm `fetch_started_at`, `response_observed_at`, `validation_method`, `origin_status`, `from_cache`, `etag`, `vary_keys`, `content_hash`, `selection_version`, `source_policy_id`, `deadline_hit` và `errors`. Các timestamp phục vụ provenance, không được dùng để khẳng định đồng bộ thời gian máy chủ gốc tới mili-giây.

### 1.3 Bốn freshness modes khác nhau

1. `origin_fetch`: mỗi flight thành công phải phát một request tới endpoint đích được phép. Một flight có thể phục vụ nhiều caller đồng thời; không bảo đảm *mỗi caller* đã phát request riêng.
2. `origin_validation`: gửi conditional GET khi đủ HTTP cache semantics, hoặc GET bình thường; dùng 304 và validator theo RFC 9110/9111. Nếu không có dữ liệu hợp lệ thì không thể phục hồi body từ 304.
3. `bounded_staleness`: cho phép trả cache dưới giới hạn `max_age`, có thể background revalidate theo chính sách. **Không dùng** khi caller yêu cầu origin validation.
4. `best_effort`: degrade được khai báo công khai; về bản chất không thể đồng nhất với “100% live”.

**Giao kèo cực hạn:** không bao giờ gắn cờ `live` cho cache chưa được xác nhận hoặc cho truy xuất trùng flight không khớp partition và representation.

## 2. Các lỗi quan trọng của blueprint trước và cách sửa

| Đặc tả cũ | Vì sao chưa đúng | Thiết kế V2 |
|---|---|---|
| TLS/JA4/WAF 1 lần vượt rồi 1.000 req chắc chắn | session hợp lệ tùy WAF, hệ thống, UA, cookies và nhiều nhân tố; không có tính phổ quát | standard HTTP pooling, phép truy cập được cấp phép, auth/session riêng cho tenant |
| Gộp 50 request từ 0–200ms = đúng 1 origin call | flight hoàn tất trước lúc request thứ 50 đến sẽ tạo flight mới | exact active-flight map, quan sát overlap thực tế |
| HTTP 304 luôn chứng minh nội dung thời gian thực tuyệt đối | validator/cache và TTL semantics phức tạp | freshness mode + validator correctness + provenance |
| SSR JSON trên 75% website | không có mẫu độc lập hỗ trợ tỷ lệ | điều tra theo từng nền tảng; fallback parser/browser đúng quyền |
| IPv6 /64 miễn phí = vô hạn request | cấp địa chỉ không đồng nghĩa được định tuyến / được phép né giới hạn | dùng IP được hạ tầng cấp và policy cho phép; không phụ thuộc IP rotation |
| Free proxy TLS chống 100% MITM | proxy có thể chặn, quan sát metadata, không bảo đảm trust chain tuyệt đối | proxy hợp đồng được audit; mặc định direct no_proxy |
| 32-byte node = zero-copy parser | HTML5 decode entities và fix tree cần chuyển đổi | html5ever/scraper parse đúng, projection copy text có kiểm soát |
| Chi phí $14.88/1M | tổng đúng của giả định thiếu chi phí | full cost model phân tách fixed/variable, failed attempts, browser, egress |
| Thompson Sampling là ratio(E[p]) | không lấy mẫu phân phối Beta | sampled Beta với gating và phân biệt heuristic vs tối ưu có chứng minh |
| `watch::broadcast` singleflight đúng tuyệt đối | leader election race + receiver lag/lifecycle | atomic map flight; `watch` giữ giá trị cuối; test cancellation |

## 3. Architecture và failure boundaries

**[PROPOSED]** Sơ đồ dưới đây là ranh giới của sản phẩm đích. Các thành phần có mã thực tế được phân biệt trong mục 4.2; sơ đồ không phải yêu cầu triển khai đồng thời tất cả thành phần.

Ở vòng core hiện tại, input là URL hoặc search candidates có provenance qua adapter/corpus đã xác định. Planner lựa chọn đọc, mở rộng hoặc dừng theo yêu cầu chất lượng/freshness và budget; không mặc định kích hoạt mọi năng lực. Kiến trúc SDK/SaC so sánh workflow cố định, agent gọi từng tool, chương trình batch viết sẵn và chương trình do agent sinh trên cùng core primitives. MCP/direct SDK là adapter ngoài; subcall vẫn qua cùng policy/admission/budget. Agent kiểm soát kế hoạch, còn quyền truy cập, byte/resource caps và cancellation được core thực thi. Chưa triển khai sandbox hay search index từ mô tả này.

```text
Agent SDK / REST / gRPC / MCP
          |
          v
Identity + Tenant Entitlements + Usage Ledger (append-only)
          |
          v
Policy Plane: allowlist + robots.txt + budgets + source contracts
          |
          v
Scheduler: deadline, fair queues, admission control, rate policies
          |
          v
Singleflight by canonicalized Request Identity
          |
          v
Fetch Planner: live vs validated vs bounded-cache
    | direct TLS       | HTTPS conditional        | Authorized browser
    | pinned DNS       | HTTP validator cache     | isolated context
    +------------------+--------------------------+
          |
          v
Bounded Decoder -> HTML5 Parser / JSON Adapter
          |
          v
Compact 32-byte DOM Projection + Tables + Semantic Blocks
          |
          v
BM25 -> optional embed/rerank -> evidence selector / MMR
          |
          v
AAE/1 JSONL stream + links + state hash + provenance
          |
          v
Agent; telemetry -> Cost Ledger / Traces / SLO Dashboard
```

### 3.1 Ranh giới lõi hiện tại và điều kiện tách crate

```text
athena/
├── Cargo.toml / Cargo.lock          # Một crate athena-core-v2
├── src/
│   ├── network.rs / ip_policy.rs    # URL, DNS pinning, TLS, bounded body
│   ├── orchestrator.rs              # Effective identity, admission, deadlines
│   ├── singleflight.rs              # Active-flight lifecycle
│   ├── arena.rs / projection.rs     # HTML5 projection, ownership, byte budgets
│   ├── table.rs / rank.rs           # Grid invariants, lexical ranking
│   ├── aae.rs                       # Bounded serialization, hashes, provenance
│   ├── bandit.rs                    # Eligible-tier sampling
│   └── bin/bench_core.rs            # Offline stage comparison
├── model/                           # Cost, queueing, probability, benchmark models
└── fixtures/                        # Offline input
```

**[IMPLEMENTED]** Giữ một crate với ownership rõ theo module. Chưa có bằng chứng việc tách thành chín crate cải thiện runtime hoặc giải quyết một consumer độc lập. Chỉ tách khi có hợp đồng tái sử dụng độc lập, nhu cầu cô lập feature/dependency hoặc vòng đời phát hành riêng; số crate theo nhu cầu, không theo chỉ tiêu cố định. Module boundary không tự tạo process/security isolation cho browser tương lai.

Source flight dùng chung fetch/parse cho các query khi canonical URL, tenant và toàn bộ effective policy giống nhau; representation flight sở hữu query/output riêng. Admission nằm trong Engine, permit của công việc blocking đi cùng chính worker. Parser sở hữu quota dữ liệu chiếu; AAE sở hữu quota serialization. Ranh giới này giữ kiểm tra tại nơi tạo/giữ tài nguyên, không đẩy invariant sang từng caller.

### 3.2 Request identity: khi nào được Singleflight

Không được dùng `u64 hash(URL)` làm toàn bộ khoá. Request key phải chứa ít nhất: method, canonical URL (query intact), tenant/auth partition, cookie/session identity hash, `Accept`, `Accept-Language`, variant `Vary`, freshness mode và validator policy, output representation, query/selection parameters, security/compliance policy version. Không gộp request có khác nhau về cấp phép hay user-specific content. Tránh lưu plaintext token/cookie trong key hoặc log.

Khóa có thể lưu dưới dạng struct typed rồi hash bằng hasher cho map, nhưng **không** dùng riêng hash 64-bit để đồng nhất danh tính nếu không kiểm chứng collision. Một `HashMap<RequestKey, Flight>` với `Eq + Hash` giữ equality semantics an toàn hơn.

### 3.3 Cancellation, scheduling, load shedding

- Deadline tổng cố định, chia ngân sách: DNS, connect, TLS, origin, bytes, parse, rank, serialize. Không reset deadline sau retry.
- Global and per-origin concurrency; token bucket/robots delay; fair queue per tenant; bounded pending queue và `429/503 + Retry-After` có đo lường.
- Leader cancel: chọn 1 trong 2 semantics, test rõ. Reference code hiện làm *cancel followers and unblock a new flight*; bản production có thể detach worker nhưng phải kiểm soát tài nguyên khi tất cả followers drop.
- Cache and flights must not bypass policy gate: authorization checked before entering shared pool.
- Backpressure propagated from output consumer; max result bytes và output tokens được enforced trước cấp memory lớn.
- Origin retry chỉ với lỗi transitory đủ điều kiện, retry bounded + exponential backoff + jitter, tuyệt đối không retry 403/429 theo kiểu né giới hạn.

## 4. Rust implementation — phần lõi tham chiếu hiện có trong `src/`

### 4.1 Dependency strategy

| Thành phần | V2 baseline | Tại sao | Khi nào đổi |
|---|---|---|---|
| Rust | 1.99.1, edition 2024 | pin hiện hành; evidence 1.99.0 giữ riêng | local gates theo engineering review; Linux/performance cần evidence mới |
| Runtime | Tokio 1.53.x | cancellation, async sockets, sync primitives | so sánh `monoio` chỉ khi profile chỉ ra hạn chế |
| HTTP | Reqwest 0.13.5 + rustls | TLS và HTTP abstraction mature, DNS override | Hyper custom connector khi cần bounded DNS-aware connection cache |
| HTML | scraper 0.27.0 + html5ever 0.39.0 | chuẩn HTML5 recovery; TreeSink adapter đếm node trước cấp phát | direct-to-arena/parser rival chỉ sau parity, resource và performance evidence |
| Node projection | Rust `Vec<Node>` 32 bytes | locality, bounded indexing | SoA khi profile chứng minh throughput tăng |
| Shared bytes | `bytes` | share immutable byte buffers không copy theo cloning | giới hạn use case theo lifetime và actual buffers |
| Dedupe | `std::Mutex<HashMap>` + `tokio::watch` | leader election atomic, dễ kiểm toán | sharded locks khi contention proven |
| Hash | BLAKE3 | fast content hash, reproducible bytes | chọn hash khác nếu compliance yêu cầu |
| Lexical ranking | in-page BM25 | CPU-only, transparent | neural ONNX nếu quality gain vượt cost |
| Serialization | serde_json JSONL | unambiguous escaping, SDK-friendly streaming | binary/wire format sau đo portability |
| Stats model | rand_distr Beta | Thompson sampling thật | hierarchical contextual bandit khi đủ telemetry |
| Observability | `tracing`/OTel (proposed) | end-to-end attribution | sampling policy khi log cost tăng |

**Dependency pinning:** `Cargo.lock` đã tồn tại và được dùng với `--locked`. `html5ever = 0.39.0` là direct edge cho TreeSink adapter; crate này vốn đã nằm trong graph qua scraper nên không thêm package version. Đợt 14–15/10/2026 cập nhật riêng rustls `0.23.45 → 0.23.46` để xử lý bốn advisory RustSec; audit sau cập nhật báo 0 vulnerability đã biết trong database tại thời điểm chạy. `cargo deny` đã kiểm tra advisories/bans/sources; license inventory không thay thế duyệt tuân thủ pháp lý. Không suy từ semver hay audit sạch thành tương thích/an toàn tuyệt đối.

**Vòng nghiên cứu tiếp theo:** giữ graph hiện tại làm comparator; khảo sát rand `0.10.3` + rand_distr `0.6.0` như một nhóm tương thích, bounded TreeSink behavior và transport reuse là các thí nghiệm riêng. Danh mục đủ 12 direct dependencies, rival và gate chốt nằm trong mục 2–3 của CORE_RESEARCH_PLAN.md. Không chọn parser/runtime mới trước kết quả kiểm chứng.

### 4.2 Mô-đun có mã thực tế và nhiệm vụ

| File | Chức năng | Điều kiện đã xử lý | Điều còn thiếu |
|---|---|---|---|
| `src/network.rs` | HTTPS/443 allowlist + DNS pinning + body cap | reject redirects/proxy/IP literals; validate policy/ETag trước DNS; timeout cả DNS+HTTP+body | pooled custom connector, robots enforcement, legacy charset, compressed decoding, egress firewall |
| `src/singleflight.rs` | đồng bộ cùng key | cancel guard, wake followers, result retention | LRU/cap active key, leader detached mode, metrics, load shedding |
| `src/arena.rs` | index-based DOM 32 bytes | range checking, stable buffer lifetime | bounded preallocation telemetry, adaptive layout |
| `src/projection.rs` | HTML5 parse → compact projection | TreeSink node-admission cap before node allocation; chunked input; byte/output/link quota; preformatted fidelity; linear dedupe; single URL dictionary | hard RSS/allocator cap, attributes/tokenizer memory caps, metadata/a11y attrs, table structural integration, encoding sniffing, parser RSS isolation, lost-content eval |
| `src/table.rs` | grid 2D spans | group ends tính một lần, sparse rows, overlap và quota columns | WHATWG parity for malformed overlapping and nested tables |
| `src/rank.rs` | lexical BM25 scoring | query-only postings; giữ full document lengths/DF/IDF; term order xác định | labeled relevance evaluation, chunk merging and cross-encoder |
| `src/aae.rs` | JSONL AAE serialization | logical byte cap kể cả escaping/newline/end; source hiện tạo full frame và joined text, chưa chứng minh hard temporary-memory cap | bounded intermediate writer/hash cần đo parity/RSS; streaming to socket, token budget, diff, incremental skeleton semantics |
| `src/orchestrator.rs` | two-layer source/representation flights | effective-policy keys, pending admission, CPU quota sống theo worker, deadline tổng | persistent cache, source authorization, tenant fairness, distributed routing |
| `src/bandit.rs` | adaptive TierSampler | discounted Beta draws, eligibility and USD/attempt | posterior calibration, failover Bellman, nonstationary evaluation |
| `src/ip_policy.rs` | address classifier | many common special-use network exclusions | registry synchronization + firewall, full validation |

### 4.3 DOM memory layout & complexity proof

```rust
#[repr(C)]
pub struct Node {
    pub parent: u32,
    pub first_child: u32,
    pub last_child: u32,
    pub next_sibling: u32,
    pub source_start: u32,
    pub source_len: u32,
    pub attributes_id: u32,
    pub tag: Tag,    // #[repr(u8)]
    pub flags: u8,
    pub depth: u16,
}
const _: () = assert!(std::mem::size_of::<Node>() == 32);
```

**Byte size:** `7 * 4 + 1 + 1 + 2 = 32`. Nếu 50.000 node, payload node là `1.600.000 bytes` = `1.526 MiB`; số này không tính raw HTML, parser tree, decoded text, attributes, Vec capacity, overhead allocator. `#[repr(C,align(32))]` không tự làm nhanh hơn, phải profile `perf`, cache-misses và IPC. `append_child` cập nhật `last_child` O(1), nhưng append front load và parser construction vẫn chi phối nhiều ca. HTML entities (`&amp;`) bắt buộc decode, nên không thể quảng cáo toàn bộ DOM là “zero-copy từ HTTP response”.

**Giới hạn hiện có:** `ParseLimits` mặc định HTML/text/link bytes mỗi loại 4 MiB, URL đã resolve 8192 byte, 250.000 output arena nodes, 250.000 HTML parser nodes và 1200 từ/chunk. Parser giới hạn node tại `TreeSink` trước khi node được cấp; sau khi chạm ngưỡng, phần input HTML tiếp theo không được gửi vào parser. `Engine` lấy HTML/text/link budgets và cùng giới hạn node parser/output từ fetch policy, dùng 1000 từ/chunk; pending mặc định `32 × max(1, origin_capacity)`, có `with_pending_limit` để cấu hình. `export_jsonl` mặc định cap JSONL 16 MiB; `export_jsonl_with_limit` cho quota riêng. Caller bị quá tải/deadline trả lỗi; worker blocking đã bắt đầu vẫn giữ quota đến khi thực sự kết thúc. Đã kiểm thử biên và input khuếch đại URL.

**Giới hạn còn phải chứng minh/tích hợp:** node quota không phải tổng RSS/capacity allocator; byte cap hiện được kiểm khi nội dung đã được đọc vào bộ nhớ. Cần đo peak và concurrency trên Linux, đánh giá attributes/tokenizer/projection allocations, và cô lập parser nếu yêu cầu deployment cần hard process-memory kill. Input vượt parser-node budget nay trả lỗi có chủ đích; dùng API limits rõ ràng khi tăng quota, rồi đo lại workload. Benchmark mỗi giới hạn bằng pathological fixtures, không dựa vào một file đẹp.

### 4.3.1 Kiểm soát độ phức tạp extraction

Reference `src/projection.rs` gán mỗi text node cho **content block gần nhất** khi duyệt mảng node theo preorder. Thu link IDs theo encounter order rồi xử lý riêng từng owner bằng mảng marker theo global link ID; owner lồng nhau không làm mất trạng thái dedupe. Với `N` nodes, `L` unique links, `T` byte văn bản và `W` chi phí normalize, riêng assignment + dedupe là `O(N + L + T + W)`; không tính HTML5 parsing hay URL dictionary hashing. URL dictionary chuyển quyền sở hữu chuỗi vào Vec theo ID, không clone payload cho hai catalog. Trường hợp chunk cực dài phải dùng quota output và tokenizer thật; `max_words_per_chunk` không tương đương ngân sách token.

### 4.4 Singleflight state transition / linearization

```text
ABSENT --atomic Mutex.entry--> RUNNING
RUNNING --sender.send_replace(Done)--> DONE
RUNNING --leader future cancelled--> CANCELLED
DONE/CANCELLED --remove_if_same_identity--> ABSENT
```

Điểm tuyến tính tạo flight phải **ở trong mutex HashMap**. Follower subscribe trong flight và đọc snapshot cuối trước khi chờ `changed`; không dùng `broadcast(1)` rồi đoán mọi follower nhận được. `send_replace` giữ giá trị ngay cả khi không còn receiver. Nếu follower đến sau khi entry bị remove, request **mới** hợp lệ vì Singleflight không phải cache.

Reference ở `src/singleflight.rs` đã qua test 32 callers, sequential non-caching và leader cancellation có follower; test đồng bộ theo trạng thái thay cho ngủ chờ. Generic `SingleFlight` không tự là admission controller: `Engine` lấy pending quota trước khi vào flight và partition theo toàn bộ effective fetch policy. `cargo test` đã chạy; Loom vẫn là gate production chưa thực hiện.

### 4.5 DNS-to-connect security

```rust
let answers = tokio::net::lookup_host((host, 443)).await?;
// validate all resolved addresses; reject invalid/special/private
let client = reqwest::Client::builder()
    .https_only(true)
    .no_proxy()
    .redirect(reqwest::redirect::Policy::none())
    .resolve_to_addrs(host, &validated_addrs)
    .build()?;
```

**Điểm chính:** sau khi kiểm tra địa chỉ IP, kết nối phải đi tới **chính địa chỉ đã kiểm tra**, trong khi hostname gốc vẫn được dùng cho SNI, HTTP Host và certificate validation. Không trust DNS rebinding, DNS trả cả public + private, redirect cross-origin hay proxy env var. Xây firewall deny-all egress ngoài cổng HTTPS tới IP công cộng, và block metadata endpoints. IANA registry thay đổi theo thời gian nên `is_public_candidate` chỉ là conservative filter ban đầu, **không phải xác nhận absolute SSRF-safe**. Cần áp dụng network namespace/policy kiểm soát socket thật.

**Performance trade-off:** baseline tạo client theo mỗi DNS-validated fetch → mất lợi thế connection pooling. Phase 2 phải triển khai resolver-aware connection cache với TTL và key `(host, IP, TLS policy, tenant privacy context, cert config, policy epoch)`; thử nghiệm riêng trên HTTPS/2 và HTTP/3, so sánh p50/p95, TLS handshakes/request và memory/connection. Tuyệt đối không đổi về một global client có second DNS resolution thiếu kiểm tra chỉ để lấy latency đẹp.

### 4.6 HTTP freshness / caching implementation requirements

Lưu bản đại diện với `cache_key` đã tính `Vary`, nguyên tắc dữ liệu riêng user, `Cache-Control`/`no-store`, `ETag`, `Last-Modified`, `Content-Encoding`, cache status và origin observation times. HTTP 304 phải làm mới header được yêu cầu bởi RFC và giữ body trước đó trong cache phù hợp; nếu không có bản hợp lệ, trả lỗi validator inconsistency hoặc fresh GET, không trả JSON rỗng như một trang đã xác thực.

Cache `no-store` không được lưu, `private` không được phục vụ liên tenant, `Vary: *` không dùng làm shared cache; `Set-Cookie`, Authorization, language variants và cookies cần strict partition. Với `origin_validation`, lần đó vẫn phải có network interaction trừ các yêu cầu cùng flight đang hoạt động. `last_state_hash` là optimization cho consumer, không phải bằng chứng origin đã thực sự giữ nguyên.

### 4.7 HTML table algorithm

HTML table không phải ma trận đã rectangle hóa. Chuẩn có `rowspan=0`, `colspan` tối đa 1000, `rowspan` tối đa 65534, `thead/tbody/tfoot`, groups, nested tables, malformed overlaps. Matrix mapping giữ `CellId` và `header associations` thay vì chỉ sao chép text, nhất là đối với báo cáo tài chính.

Reference code `src/table.rs` xử lý span và group trong một grid bounded, không tuyên bố thay thế đầy đủ thuật toán WHATWG. **Hướng nghiên cứu tiếp theo:** thiết kế extraction `tr/td/th`, `scope`, `headers`, caption và thứ tự nhóm; chọn cases theo invariant và counterexample, sinh variants trong memory/scratch thay vì tạo hàng nghìn file. Đối chiếu browser DOM API / WHATWG conformance khi có môi trường phù hợp. Nếu lỗi overlap, định nghĩa `TableError`/fallback rõ ràng; chưa tuyên bố căn hàng chuẩn xác 100%.

### 4.8 AAE/1 protocol (machine-readable)

Mỗi dòng là JSON hợp lệ; giữ version và frame type độc lập, không nhét lại URL dài vào context nếu SDK/MCP hỗ trợ link resolver. Ví dụ:

```jsonl
{"type":"meta","version":"aae/1","source_url":"https://example.org/p","fetched_at_unix_ms":0,"content_hash":"<full-blake3>","total_blocks":2,"parse_warnings":0}
{"type":"block","id":"<blake3>","role":"heading","text":"GPU Pricing","links":[],"highlight":false}
{"type":"block","id":"<blake3>","role":"paragraph","text":"H100 hourly $2.49","links":[0],"highlight":true}
{"type":"link","id":0,"url":"https://example.org/buy"}
{"type":"end","block_count":2}
```

**JSONL thay `@META` raw:** text của trang độc hại có thể cố chèn `@ACTION`, `@DIFF`, hoặc chuỗi newline fake; `serde_json` escape text khiến toàn bộ message được parse thành phần nội dung, không thành câu lệnh điều khiển. SDK chỉ giải quyết link ID xuất phát từ bảng đã kiểm tra, không tự động click URL, không đưa secret vào context.

**Sửa cho production:** giữ `full_hash` immutable, `block_id` ổn định qua chèn sibling (hash content + normalized path có caveat), `page_revision`, canonical locators, SDK action scope và signature; dùng length-delimited frames hoặc JSONL frame cap, CRC nếu transport cần, gắn xác nhận `end` để phân biệt stream ngắt và thành công. Bảo vệ AAE trước prompt injection bằng trust boundary ở tầng Agent, không chỉ escape JSON.

### 4.9 Query-aware retrieval

`src/rank.rs` giữ công thức BM25/tokenizer trong phạm vi một trang; chỉ thu TF/postings cho term của query nhưng vẫn đếm mọi token để chuẩn hóa độ dài. Term được sort/dedup, IDF tính một lần mỗi term. Full ranking vẫn có sort `O(N log N)`; đây không phải thuật toán top-K tuyến tính hay semantic model. Đã đối chiếu điểm tính tay và Unicode; parity byte toàn bộ AAE được kiểm trên corpus ghi trong báo cáo, không cam kết mọi trường hợp floating-point có thứ tự như bản cũ. Trong top-32 rerank, cần phân biệt:

- **Bi-encoder:** mã hóa query và chunks độc lập, cosine/dot product; có thể lưu và tái dùng document vectors nếu freshness/cache cho phép. `INT8 DOT D=384` mô tả phần similarity *sau embedding*, không phải chi phí chạy Transformer.
- **Cross-encoder:** đưa cặp `(query, passage)` qua model để tính score; không thể được thay thế bằng dot-product vector `i8`.
- **MMR:** đa dạng hóa passages; tune λ trên nhãn answer accuracy, không áp dụng mặc định mọi trường hợp cần exact answer.

Bài toán tối ưu truy xuất dưới token budget:

\[
\max_{x_i\in\{0,1\}}\sum_i x_i U_i(q) - \lambda \sum_i x_i T_i
\quad\text{subject to}\quad \sum_i x_i T_i\le B.
\]

Chưa biết `U_i` cho tới khi có labels. Với mọi lựa chọn nén output, đo `answer recall@budget`, exact numeric cell correctness, citation-grounding, cross-page false positives, task success và end-to-end cost. Nếu chỉ token rẻ mà accuracy giảm, cần rollback.

## 5. Toán học tối ưu hệ thống và yêu cầu chứng minh

### 5.1 COGS đầy đủ

Cho `N` requests người dùng trong kỳ, `F` fixed compute/instance, `E` public egress, `L` logging/storage/monitoring, `S` security and support alloc, `A_k` origin attempts phương thức `k`, `b_k` billable GB, `g_k` USD/GB, `c_k` USD/attempt compute/proxy, `X` miscellaneous:

\[
\mathrm{COGS}_{1M}=\frac{10^6}{N}\left(F+E+L+S+X+\sum_k[A_k c_k+b_k g_k]\right).
\]

Không gán `A_k/N` trực tiếp từ tỉ lệ “có thể dùng session” hay “trang web có SSR”. **Failed attempts tính tiền**, browser warm-up, proxy double-billing, DNS calls, 304 header bytes và retries đều cần vào ledger.

Công cụ số học: `model/cost_model.py`, `model/inputs.json` (Python Decimal). Với **giả định** 1M req/tháng, fixed `$120+$10`, egress `200GB×$0.05`, paid proxy `1.35GB×$4`, browser cost giả định $0, ta có **COGS `$145.40/1M`** và gross profit âm `$16.40` nếu giá `$129/1M`. Đây là **illustrative scenario**, không phải giá thật. Định giá 79–129 không thể chốt khi chưa có số liệu theo cohort.

Độ chính xác số học đến nhiều chữ số không thể sửa tình trạng thiếu đầu vào. Cần dashboard per-request cost attribution, query cohort / site mix và confidence interval của cost per successful result.

### 5.2 Singleflight không có mức tiết kiệm cố định

Với `N` arrivals, thời điểm `t_i`, key `k_i`, fetch window `w_i`, số origin calls là số leader của các cụm arrivals có cùng key **và** time overlap. Không được suy ra bằng tỷ lệ cố định `25%`. Mô phỏng Poisson tổng `2.000 arrivals/s`, latency không đổi 80 ms trong `model/singleflight_sim.py` (seeded, 100k arrivals):

| Distinct URL keys | Joined fraction (ví dụ synthetic) | Origin calls / client |
|---|---:|---:|
| 100 | ~61.6% | ~38.4% |
| 1,000 | ~13.9% | ~86.1% |
| 10,000 | ~1.6% | ~98.4% |

Các con số này hoàn toàn không dự đoán Athena; chúng chứng minh workload popularity và overlap mới quyết định savings. Load thử phải dùng **trace arrivals thực**, giữ bursts, hot-key skew, locale/tenant partitions và browser long-tail.

### 5.3 Latency decomposition

\[
T_{e2e}=T_{queue}+T_{DNS}+T_{connect}+T_{TLS}+T_{origin}+T_{body}+T_{parse}+T_{rank}+T_{serialize}+T_{client}.
\]

Các thành phần có tương quan; không suy `p95(total)=sum(p95(component))`. Báo `p50,p90,p95,p99,p99.9` thực nghiệm theo cohort, deadline expiration, success rate, bytes, CPU cycles, memory high-watermark. Đo time-to-first-valid-evidence riêng time-to-final-envelope và time-to-first-action thành công.

### 5.4 Queueing / peak load

Nếu giả định arrivals Poisson λ requests/s, mỗi worker service time exponential với rate μ, `c` workers, utilization `ρ=λ/(cμ)<1`, Erlang-C cho xác suất phải đợi `P_W`:

\[
P_W=\frac{\frac{a^c}{c!}\frac{1}{1-\rho}}{\sum_{n=0}^{c-1}\frac{a^n}{n!}+\frac{a^c}{c!}\frac{1}{1-\rho}},\quad a=\lambda/\mu.
\]

\[
P(W_q>t)=P_W e^{-(c\mu-\lambda)t},\qquad
E[W_q]=P_W/(c\mu-\lambda).
\]

Code `model/queueing.py` dùng log-gamma/log-sum-exp tránh tràn factorial. Đây là **mô hình lý tưởng M/M/c**; browser/rendering có tail nặng và arrival bursts nên phải kiểm tra bằng discrete-event simulation / replay + empirical quantiles. Ví dụ λ=50/s, μ=10/s/worker: c=4 overload (ρ=1.25), c=8 ρ=0.625, P(wait)≈0.167, p95 wait≈40.25 ms; c=16 ρ=0.3125. Không diễn giải đây là năng lực Athena đo được.

### 5.5 Reliability confidence

Với `s` successes trên `n` phép Bernoulli độc lập, Wilson 95% interval là phép đánh giá sơ bộ; nếu không có failures, cận dưới Clopper-Pearson một phía mức 95% là `(0.05)^(1/n)`. Để cận dưới `≥0.999` khi 0 fails cần **2.995 trials độc lập mỗi cohort**; 99.99% cần khoảng 29.956. Đối với request trùng source/domain hoặc ngày, bootstrap phải theo cluster/domain/time, tránh CI quá lạc quan. Code `model/statistics.py` kèm sẵn.

### 5.6 Bandit and fallback policy

Đặt `p_{d,k,t}` xác suất thành công tại domain `d`, method `k`, thời điểm `t`. Discounted Beta với half-life `h`: `α'=1+2^{-Δt/h}(α−1)+success`, `β'=1+2^{-Δt/h}(β−1)+failure`. Đây là adaptive heuristic, **không phải Bayesian posterior chính xác** khi môi trường non-stationary. Sample `\tilde p~Beta(α',β')`, chọn eligible arm theo cost proxy có đơn vị nhất quán:

\[
\hat J_k=\frac{c_k^{USD/attempt}+\mu^{USD/ms} E[L_k^{ms}]}{\max(\tilde p_k,\varepsilon)}.
\]

Nhưng khi cho phép fallback, tối ưu chính xác phải giải problem state-dependent:

\[
V(s)=\min_{k\in eligible(s)}\left[c_k+\mu E[L_k]+(1-p_k)E[V(s'|k,fail)]\right]
\]

sau khi đặt quy tắc dừng, error penalty và cost caps. `src/bandit.rs` chỉ tham chiếu cho sampler (không phải router production), cần offline policy evaluation IPS / doubly robust với propensity logged, đo regret/violation under distribution shift, exploration guardrail, baseline A/B gating.

## 6. Performance engineering: thứ tự tối ưu bắt buộc

### 6.1 Stage 0 correctness profile

Đặt fixed reproducible corpus; 8 nhóm: static docs, tables, JavaScript hydration, multilingual Vietnamese/Chinese, malformed HTML, huge pages, redirects, 304, error/429. Verify output against browser DOM/label set. Vết benchmark lưu hardware, CPU governor, kernel, cargo.lock, git SHA, model versions, timers, warm-up durations.

### 6.2 Stage 1 eliminate avoidable copies and allocations

- `Bytes` reuse trong fan-out; không copy body `Vec<u8>` vô cớ. `BytesMut` grow + cap; `serde_json` string output có cap. `Arena` reserve dựa lịch sử size theo bucket, không reserve vô hạn từ attacker input.
- Link dictionary intern strings, bounded per-page. Chọn `Box<str>` khi immutable, indices `u32` khi nodes < 2³²; use `SmallVec` chỉ nếu nhỏ có telemetry, không chạy theo macro benchmarks.
- Không tự thêm `#[inline(always)]` mọi nơi; release build, codegen and PGO/phases benchmark phải chứng minh cải thiện.
- CPU blocking parse dùng `spawn_blocking` + semaphore riêng, tránh pool unbounded; memory reservations trước khi nhận request.
- Giải nén có giới hạn **sau khi decompress**, ratio cap, CPU cap để chống zip bomb. Reference MVP chỉ nhận identity encoding để đơn giản hóa an toàn; production thêm streaming decompressor.

### 6.3 Stage 2 connection reuse (lợi ích lớn hơn SIMD node)

Baseline `network.rs` build client per pinned resolution đảm bảo không DNS rebind lại nhưng tăng TLS setup. Thực hiện *safe pooled connector* với resolver policy inside dialer: on connection creation resolve candidate IP, validate, connect pinned; keep socket until TTL/lifetime/tenant boundary; relaunch validation on new connections. Benchmark HTTP/1.1 vs HTTP/2 by actual server negotiation, H2 max concurrent streams, flow-control/window stalls, idle closing, origin request caps. Không ép browser JA4 để bypass.

### 6.4 Stage 3 parser fast path with fallback

Thu thập phân phối thật của tag, whitespace, script/data variants. SIMD pre-scanner có thể tìm dấu `__NEXT_DATA__`/script tags nhanh, nhưng không thể thay thế tokenizer chuẩn nếu phải decode HTML entities, context-sensitive escapes, malformed nesting. Fast path cho JSON embedding phải kiểm tra schema, content length, parse JSON, provenance và parity với full parse; fallback khi mismatch. Mọi đường đi có kiểm soát và version.

### 6.5 Stage 4 ML accuracy-first

Khi BM25 không đủ: index selective BM25, top-K (tune 8/16/32/64), bi-encoder embedding batches, INT8 ONNX CPU, optional cross-encoder only top-smallK; MMR when beneficial. Measure `tokens_in`, `tokens_out`, inference walltime, per-language recall, accuracy, hallucinations, GPU idle resource. AVX-512 VNNI *nếu CPU hỗ trợ* và quant model hợp lệ; runtime dispatch AVX2/NEON fallback; nén `i8` dot không tương đương full inference speed.

### 6.6 Stage 5 cross-platform operational mode

Rust code core Linux/macOS/Windows; Linux network/containers optimized via optional feature flags. Không assume `SO_REUSEPORT`, `io_uring`, `IP_FREEBIND`, cgroups hoặc SIMD flags có trên mọi OS. Bộ benchmark cung cấp portable path, isolate Linux-specific code behind `cfg` và integration test bằng network namespaces.

## 7. Threat model & security gates

1. **SSRF**: cross-cloud metadata, internal service, DNS rebinding, redirect, IPv4-mapped IPv6, integer/hex IP notation, special-use registries, HTTP proxy ENV. Mitigation: strict URL parse, scheme/port allow, normalized exact hosts, every DNS answer validated, **pinned connect**, no redirects, no proxy, egress ACL.
2. **Prompt injection**: HTML/JSON nguồn là untrusted. Never elevate extracted instructions into control plane; separate tool result from system instructions; escape framing; hyperlink actions require explicit caller permission.
3. **Cross-tenant leak**: flight keys, cache partitions, browser cookies, screenshots and network traces must isolate users.
4. **Resource exhaustion**: decompression bombs, huge spans, deeply nested DOM, billions of elements, slowloris and infinite media streams. Bound byte/timeout/node/depth/table/LLM output concurrency.
5. **Supply-chain**: `cargo audit`, cargo deny, license allowlist, SBOM/SLSA attest, cargo.lock, dependency provenance.
6. **Legal/policy**: robots protocol RFC 9309, contracts, data privacy, explicit authorization for authenticated APIs; no stealth WAF bypass or open-proxy pools.
7. **Observability privacy**: hash/tokenize sensitive ids and URLs, redact query params, never log cookies/secret, retention policies, tenant segregation.
8. **Usage billing**: immutable events with idempotency key, state machine for retry, separately account billable calls vs attempts; never double charge joined followers without explicit contract.

**Stop-ship security conditions:** any SSRF reach internal IP under attacker input; cross-tenant singleflight contamination; TLS cert verification off; source content can invoke tool actions; unbounded decompressed memory; action resolver accepts non-http schemes; secrets in logs.

## 8. Measurement plan / SLO / launch gates

### 8.1 Metrics with explicit denominators

- `fetch_success_rate = successful_200_or_valid_304 / all_admitted_fetches` (separately parser-success and answer-success).
- `correctness = correct_answer_with_citation / evaluated_gold_tasks`; separate extraction-only tasks and agent-action tasks.
- `p50/p95/p99` per HTTP class, geography, source type, failure, time to first AAE frame, full completion.
- `coalescing_factor = admitted_client_requests / actual_origin_attempts` by key and cohort; report attempts revalidation separately.
- `cpu_ms/success`, `peak_rss`, `allocations/KB`, `cache_hit_rate`, `304_rate`, `bytes_wire`, `bytes_decompressed`, `source_block_rate`.
- `COGS/successful_answer`, `$ / 1M requests`, `$ / 1M successful result`, revenue gross margin, retry distribution and incident rate.
- `freshness_error`: difference between origin-controlled change timestamp and Athena observation, only measurable on **controlled origin** where truth is known.

### 8.2 Staged performance gates (initial targets, not promises)

| Gate | Suggested acceptance objective | How to measure |
|---|---|---|
| HTML conformance | ≥99.9% structure parity on curated fixtures *before* considering rollout | tree comparison against browser references |
| Tables | 100% on valid fixture subset; malformed errors explicit | WHATWG fixture suite |
| Isolation | 0 observed cross-tenant leaks in targeted tests | adversarial tests |
| SSRF | 0 successful internal egress across suites | black-box fixture server/network namespace |
| Singleflight | 1 leader for fully overlapping same-key calls | concurrency stress + loom |
| Reliability | ≥99.9% baseline on defined permitted origin cohort | independent clustered analysis |
| Latency | empirically chosen p95 SLO per *cohort*, e.g. static sites <1500ms | real external test, stratified |
| AAE | ≥95% answer recall at target token budget | model/evaluator gold data |
| Billing | sum event ledger = reconciled invoice within tolerance | end-to-end reconciliation |
| Resource cap | no OOM even malformed inputs within specified admission budgets | fuzz/load tests |

Targets phải điều chỉnh sau feasibility testing; không tạo target p95=35ms toàn cầu rồi tuyên bố đạt khi đo localhost.

### 8.3 Benchmark matrix

- CPU: x86_64 AVX2; AVX-512/VNNI optional; ARM64 NEON; baseline generic.
- Sites: 100 controlled + 1,000 sampled permitted pages; fixed and live subsets; provenance and rights recorded.
- Inputs: HTML 10KB, 100KB, 1MB, 4MB; nesting 10,100,1000; links 0,100,10000; tables 1×1, 100×100, max spans.
- Concurrency: 1,8,64,256,1024 clients; hot-keys vs unique URLs; 3 regions; long-tail browser fallback.
- Rerank: lexical only, INT8 bi-encoder, float16/float32 reference, cross-encoder, MMR; compute QPS/p99 and accuracy.
- Run randomization: warm/cold cache separately; A/B run order randomized; ≥10 trials (more for tail); 95% bootstrap CIs clustered by URL.
- Stress tools: `cargo bench`, Criterion, flamegraph `perf`/`samply`, allocator stats, tokio-console, Linux eBPF; `cargo test`, proptest, libFuzzer, Loom, Miri (where supported).

### 8.4 Release criteria

**Alpha:** compiled + fmt + clippy + tests + fixtures, no SSRF open paths, valid AAE v1.  
**Beta:** enforced robots/rate policy, stable cache, distributed coordination, linked actions scope, ledger and dashboards.  
**RC:** 72-hour soak, budget exceed/429 policy, chaos tests, at least three production-like datasets, repeatable COGS and p95 with CI.  
**GA:** documented SLOs, incident runbooks, security review, rolling deployment, rollback, legal/compliance approval.

## 9. Execution roadmap prioritized by risk

### 9.0 Vòng đang hoạt động: nghiên cứu trước, triển khai sau

1. Agent nghiên cứu hoàn thành library/contract matrix và experiment spec trong CORE_RESEARCH_PLAN.md.
2. Agent khác kiểm correctness/compatibility và thu raw performance/RSS trên Kaggle theo protocol; không tạo test files theo số lượng hoặc báo PASS thiếu log.
3. Reviewer chốt từng candidate ACCEPT/REJECT/HOLD; giữ kết quả bất lợi và UNKNOWN.
4. Chỉ candidate ACCEPT mới có implementation scope cụ thể; sau đó agent được giao mới triển khai và kiểm chứng diff đó.

Study 2.1 là evidence đã lưu, không reset hoặc retag thành Study mới. Vòng nghiên cứu tiếp theo tập trung ablation AAE, tương tác giữa các tối ưu, workflow/SDK/SaC và chi phí thực ở harness. Các shard Kaggle có baseline/candidate trong cùng VM, có budget và scope riêng; model/harness thật và live access là gate riêng. Chỉ mục web riêng được hoãn theo quyết định người dùng; các phase dài hạn về sau không tự mở rộng phạm vi này.

Các phase dài hạn bên dưới là backlog tham khảo, không phải lệnh triển khai hoặc lịch đang chạy. Không mở rộng vòng hiện tại sang CI/deployment/billing/operations.

### Phase 0 — Build verifiability & baseline (days 1–5)

- Đã chạy local Rust 1.99 build/test/fmt/Clippy, dùng lockfile. Kết quả local Windows không thay thế kiểm chứng deployment hoặc nền tảng khác.
- Add standard fixtures and hashing, reproducible benchmarks, project-level CI artifacts, release profiles. Deliver: compile/test/fmt/clippy all pass.
- Implement PR benchmarking (throughput, p99, peak RSS) with no fraudulent speed claims.

### Phase 1 — Correct HTML/Arena/AAE (weeks 1–3)

- Harden projection, encoding detection, table integration against WHATWG, chunk fidelity.
- Đã có HTML/text/link/output byte quota và end frame. Tiếp theo đo parser RSS/allocator, tích hợp containment và telemetry trước khi mở server.
- Implement `last_state_hash` + semantic content diff with stable locators, no missed updates under controlled tests.
- Deliver: deterministic offline engine with gold fixture tests and robust source mappings.

### Phase 2 — Correct live transport / freshness (weeks 3–5)

- Domain policy/robots RFC 9309 implementation, safe DNS connector with shared pool, pinned IP and egress ACL.
- HTTP 304 cache validator engine under RFC 9111, `Vary`, `no-store`, authenticated partitions, staleness proof tests.
- Budgeted scheduler, fair queues, per-origin politeness, permissioned browser fallback (no WAF bypass); stream backpressure.
- Deliver: retriever with accurate freshness semantics and measured network timings.

### Phase 3 — Multi-agent fanout & API (weeks 5–7)

- Stateless REST/gRPC/MCP front-end, auth/rate/billing, tenant-scoped flight/cache, cancel policy, tracing and immutable event ledger.
- Distributed dedupe only when worth communication cost; local-first on hot single node, use redis/nats if tested.
- Deliver: horizontal scale under controlled failure injection.

### Phase 4 — Quality & CPU rerank (weeks 7–9)

- Label answer tasks, tune BM25/query segmentation, prototype ONNX INT8 bi-encoder and cross-encoder only where helpful.
- Adaptive token budget and AAE reorder; evidence recall/hallucinations vs cost. Streaming chunks only after validated final structural semantics.
- Deliver: quality/perf pareto frontier; select optimal model not trendy model.

### Phase 5 — Economic optimization & production hardening (weeks 9–12+)

- Source-mix cost curves at 1M,10M,100M requests, open-loop/closed-loop tests, cost ledger reconciliation.
- Fine-grained CPU profile, PGO and LTO tuning after real workloads, not blanket `inline(always)`.
- Security review / fuzz 72 hours / rollback drills / compliance / privacy / support docs.
- Deliver: production go/no-go with measurements, CIs, source hashes, reproducible reports.

**Ngày trong roadmap là khung phụ thuộc kỹ thuật, không phải thời gian cam kết giao hàng; thực tế phụ thuộc quy mô nhóm, scope và quyền truy cập sources.**

## 10. Detailed acceptance checklist

- [x] Rust 1.99.0 lockfile/build/test/Clippy local Windows x86_64 đã qua; remote CI cần chạy sau publication.
- [ ] Correctness fixtures compare source-parsed content with browser reference.
- [ ] `Node` size 32 bytes confirmed on x86_64 and aarch64.
- [ ] No `unsafe` code in project core without ADR and tests.
- [ ] Structural malformed HTML does not cause OOM or stack overflow.
- [ ] Table headers and scope preserved for financial comparisons.
- [ ] Fuzz `rowspan=0`, max colspan, overlaps and 100k-cell cutoff.
- [ ] URL scheme/host/port/userinfo/fragment validated before network.
- [ ] DNS resolution pinned to connection; every IP screened against registry.
- [ ] No auto-follow redirects; no system proxy env inheritance.
- [ ] Per-origin robots policy and rate limit enforced before connection.
- [ ] TLS cert verification cannot be disabled via customer config.
- [ ] `Cache-Control: no-store`, `Vary`, tenant privacy and validators covered.
- [ ] 304 without a stored valid body does not masquerade as fresh content.
- [ ] Same flight identity atomic and cancellation semantics documented.
- [ ] Flight and cache keys respect tenant/auth/source policy.
- [ ] HTTPS response decompression quotas enforced after decompression.
- [ ] AAE source content escapes framing; no implicit external actions.
- [ ] Live and cache/validated result provenance visible to caller.
- [ ] No secret/cookie/token in logs or input representation.
- [ ] Billing events idempotent across retry, join and timeout.
- [ ] P95/P99 measured per cohort with sample size and CIs.
- [ ] Cost measured per *successful* answer and reported separately from client calls.
- [ ] Meaningful accuracy comparison against raw HTML/Markdown baseline.
- [ ] Benchmark artifact tied to git SHA, compiler, flags, hardware and datasets.
- [ ] Soak, chaos, host outage, circuit breaker and rollout rollback passes.

## 11. Official documentation / verification references

1. [Rust 1.99.0 official release, 01 Oct 2026](https://blog.rust-lang.org/2026/10/01/Rust-1.99.0/).
2. [reqwest 0.13.5 Builder API](https://docs.rs/reqwest/0.13.5/reqwest/struct.ClientBuilder.html): `resolve_to_addrs`, `https_only`, redirect, no_proxy, timeout.
3. [reqwest client pooling](https://docs.rs/reqwest/0.13.5/reqwest/struct.Client.html).
4. [Tokio watch sender](https://docs.rs/tokio/latest/tokio/sync/watch/struct.Sender.html): `send_replace`, subscribers, cancellation.
5. [scraper HTML5 DOM](https://docs.rs/scraper/0.27.0/scraper/struct.Html.html).
6. [WHATWG HTML table formation](https://html.spec.whatwg.org/multipage/tables.html).
7. [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).
8. [IANA IPv4 special-use registry](https://www.iana.org/assignments/iana-ipv4-special-registry) and [IPv6 registry](https://www.iana.org/assignments/iana-ipv6-special-registry).
9. [RFC 9111 — HTTP caching](https://www.rfc-editor.org/rfc/rfc9111), [RFC 9110 — HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110).
10. [RFC 9309 — Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309.html).
11. [Notion/TOON token-notation investigation, arXiv May 2026](https://arxiv.org/html/2605.29676v1): token saving can trade off task accuracy.
12. [WebTaskBench web representations study (exploratory, not universally validated)](https://dbhurley.com/papers/plasmate-webtaskbench-paper.pdf): representation structure changes token volume and task latency; evaluate full task success.

---

### Final design decision

Athena không tối ưu bằng cách dựa vào “giao thức có lỗ hổng kinh tế”. Athena tối ưu bằng **giảm fetch thừa hợp lệ**, **giữ bằng chứng nguồn**, **reuse an toàn**, **chọn chính xác thông tin cần cho Agent** và **giảm chi phí cho một kết quả đúng**. Đây là con đường bền vững hơn để đạt throughput cao và chứng minh lợi thế thương mại. Tất cả code đã viết nằm trong `src/`; các module chưa implemented được đánh dấu rõ, không gắn nhãn hoàn tất giả.

Các comparator, native/service probes và agent research harness mới nằm riêng
ở `cloud_experiments/`. Trạng thái mã,
gaps và điều kiện bàn giao hiện tại nằm ở mục 17 của [kế hoạch nghiên cứu](CORE_RESEARCH_PLAN.md).
Không xem research prototype, pilot COMPLETE hay finite correctness controls là
production architecture/performance đã được chốt.

Nghiên cứu dependency/core được mở rộng tại CORE_RESEARCH_PLAN.md mục 18: locked library/feature tuples, interactions, causal denials và reviewed corpus intake. Các comparator nằm trong cloud_experiments; chưa phải lựa chọn production hay bằng chứng tối ưu toàn cục.

**Điểm vào nghiên cứu hiện hành — 18/10/2026:**
[CORE_RESEARCH_PLAN.md mục 19](CORE_RESEARCH_PLAN.md#19-bản-đồ-bài-toán-và-nghiên-cứu-để-quyết-định-nâng-cấp-core--18102026)
map các khoảng trống source vào H01–H14, F01–F08 và interaction obligations.
Phân biệt search-only với đọc/render khi cần; các trục freshness, connection,
session/proxy và representation có thể kết hợp, không là các tier loại trừ nhau.
Nghiên cứu state machine, demand-driven plan và sandbox SDK trên cùng primitives;
giữ AAE/1 compatibility riêng với negotiated selected/reference evidence.
Nguồn REA/Exa/Firecrawl/Bright Data/Perplexity đã được đối chiếu công khai;
chưa cài/chạy REA hoặc xác định thuật toán server không được phân phối.
Đây là kế hoạch quyết định, chưa là core upgrade hoặc kết quả benchmark mới.

Harness cloud đã bổ sung atomic manifest/JSON publication và resumable
confirmation analysis theo từng completed case; hướng dẫn tại
[cloud_experiments/README.md](cloud_experiments/README.md#continuing-a-long-confirmation-analysis).
Local process-kill/restart, stale-result rejection và statistical-output parity
đã có checks; target Linux và full performance/quality closure vẫn là gate riêng.
Không coi các sửa lifecycle của nghiên cứu là production-core speedup.
