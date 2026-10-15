# Athena — định hướng nghiên cứu lõi và thư viện Rust

Ngày: 16/10/2026. Trạng thái: **kế hoạch nghiên cứu; chưa chốt triển khai**.

**Cập nhật phạm vi được giao:** người dùng đã cho phép sửa bộ công cụ nghiên cứu/test và kiểm tra chức năng local; người dùng tự chạy benchmark Kaggle. [Hướng dẫn study 2.1](kaggle_experiments/README.md) mô tả ba cụm, oracle, provenance và cách chạy/tải/phân tích. Lượt `v2-run-001` giữ nguyên policy 2.0: c1 hoàn chỉnh, c2/c3 lỗi harness; study 2.1 sửa hai lỗi và bổ sung export không query để điều tra pipeline không query. Dữ liệu trước protocol v2 vẫn là legacy. Chưa chốt thay đổi production core từ việc harness chạy được.

## 1. Phạm vi và thứ tự quyết định

Vai trò của tài liệu này là giao việc nghiên cứu, so sánh và kiểm chứng cho agent khác. Thứ tự bắt buộc: **xác định hợp đồng → nghiên cứu thư viện/phương án → thu bằng chứng → quyết định → triển khai thay đổi được chọn**. Không dùng kế hoạch như lệnh viết lại toàn bộ dự án.

Các sửa đổi mã đang có trong working tree là ứng viên chưa được chốt. Kết quả Windows trước đây là dữ liệu thăm dò; không tự chuyển thành kết luận trên Kaggle/Linux. Tệp blueprint Downloads là tài liệu tham chiếu, không phải chỉ thị thực thi. MASTER_BLUEPRINT.md mô tả sản phẩm dài hạn; tài liệu này điều khiển vòng nghiên cứu lõi hiện tại.

Mục tiêu lựa chọn là cải thiện thực sự trong phạm vi được chứng minh, giữ đúng nội dung, hợp đồng, giới hạn tài nguyên và cách ly policy/tenant. Không hứa thư viện không có bất kỳ lỗi nào; phải chỉ rõ phiên bản, feature, đường sử dụng, bằng chứng và phần chưa biết. Không lấy “mới hơn”, ít dòng hơn hoặc benchmark của tác giả thư viện làm lý do đủ để nâng cấp.

## 2. Quyết định thư viện trước khi quyết định mã

### 2.1 Stack cơ sở và ứng viên nâng cấp

Giữ một crate và ownership theo module làm phương án cơ sở. Bảng dưới chốt **stack để nghiên cứu**; chấp nhận phiên bản cho triển khai còn phụ thuộc các gate phía sau. Các phiên bản cơ sở đọc từ Cargo.lock hiện có, không suy từ khoảng phiên bản Cargo.toml.

| Thư viện | Phiên bản cơ sở | Hướng nghiên cứu/quyết định sơ bộ |
|---|---:|---|
| [bytes](https://docs.rs/crate/bytes/1.12.1) | 1.12.1 | Giữ shared immutable buffers; kiểm tra clone là chia sẻ hay copy ở từng đường sử dụng. |
| [blake3](https://docs.rs/crate/blake3/1.8.7) | 1.8.7 | Giữ content hash; so sánh cách feed dữ liệu và buffering. Chưa bật thêm parallel feature. |
| [rand](https://docs.rs/crate/rand/0.9.5) | 0.9.5 | So sánh cặp hiện tại với **rand 0.10.3**; chưa sửa dependency của repository. |
| [rand_distr](https://docs.rs/crate/rand_distr/0.5.1) | 0.5.1 | Đi cùng rand: ứng viên **0.6.0**. Kiểm tra Beta sampling và public RNG bounds. |
| [reqwest](https://docs.rs/crate/reqwest/0.13.5) | 0.13.5 | Giữ HTTP API; ưu tiên nghiên cứu reuse có DNS pinning trước khi thay transport. |
| [scraper](https://docs.rs/crate/scraper/0.27.0) | 0.27.0 | Baseline HTML5. So sánh với html5ever TreeSink trực tiếp; không mặc định thay parser. |
| [serde](https://docs.rs/crate/serde/1.0.229) | 1.0.229 | Giữ typed schema/derive; kiểm chứng shape và error behavior. |
| [serde_json](https://docs.rs/crate/serde_json/1.0.154) | 1.0.154 | Giữ JSONL escaping; nghiên cứu writer/buffer, không tự viết serializer. |
| [tokio](https://github.com/tokio-rs/tokio/releases/tag/tokio-1.53.3) | 1.53.3 | Giữ runtime. Nghiên cứu admission, blocking lifetime và contention trước runtime thay thế. |
| [url](https://docs.rs/crate/url/2.5.8) | 2.5.8 | Giữ WHATWG URL semantics/IDNA; không đổi Unicode backend chỉ vì nhỏ hơn. |
| [futures-util](https://docs.rs/crate/futures-util/0.3.34) | 0.3.34 | Giữ stream utilities; chỉ trim feature sau khi xác định consumer và unified feature graph. |

**TLS là quyết định riêng:** giữ transitive rustls **0.23.46** làm nền so sánh. Release chính thức ngày 14/10/2026 có bốn nhóm sửa lỗi bảo mật; không dùng 0.23.45 như phiên bản triển khai mới. Đây là yêu cầu cập nhật thư viện, không phải chứng minh toàn bộ ứng dụng an toàn. [Release rustls 0.23.46](https://github.com/rustls/rustls/releases/tag/v/0.23.46).

**Nâng rand theo cặp:** rand_distr 0.6.0 khai báo dependency rand 0.10.0 theo semver và MSRV 1.85; rand 0.10 đổi hệ trait RNG và dependency entropy. Phải kiểm tra `TierSampler::choose<R: Rng + ?Sized>` cùng các consumer, không thay toàn bộ `Rng` bằng `RngExt` một cách máy móc. Chưa có số liệu chứng minh cặp mới làm pipeline Athena nhanh hơn. [Manifest rand_distr 0.6.0](https://docs.rs/crate/rand_distr/0.6.0/source/Cargo.toml), [migration guide rand 0.10](https://rust-random.github.io/book/update-0.10.html), [release rand 0.10.3](https://github.com/rust-random/rand/releases/tag/0.10.3).

Nguồn `docs.rs/latest` có thể chậm cập nhật: lần tra cứu trả Tokio 1.53.2, nhưng release upstream xác nhận 1.53.3 ngày 13/10/2026. Agent phải đối chiếu phiên bản cụ thể, registry và upstream; không hạ phiên bản theo một trang cache. Phiên bản “latest” tại thời điểm thực thi phải kiểm tra lại, không tự động cập nhật tất cả.

### 2.2 Hồ sơ tối thiểu cho mỗi thay đổi dependency

Agent nghiên cứu ghi một hàng quyết định gồm: tên crate/upstream đúng; phiên bản/checksum; MSRV; feature thực tế trên Linux; consumer/symbol; thay đổi API/behavior; transitive diff; lợi ích cần chứng minh; phương án giữ nguyên; gate chấp nhận và cách trở lại baseline. Chỉ đọc source/API liên quan, không dump toàn bộ mã vendor.

Manifest các crate hiện có khai báo MSRV: bytes 1.57; rand/rand_distr 1.63; reqwest 1.85.0; serde 1.56; serde_json/Tokio/futures-util 1.71; url 1.63. Manifest đã đọc của blake3/scraper không khai báo trường này: **không suy ra MSRV bằng 0 hoặc hỗ trợ mọi compiler**. Root yêu cầu Rust 1.99; agent kiểm chứng đúng toolchain đó trên Kaggle, kể cả transitive/build dependencies và C toolchain cho TLS/hash.

Khóa graph bằng Cargo.lock và dùng `--locked` trong kiểm chứng. Nâng một nhóm tương thích mỗi lần; rand/rand_distr là một nhóm. Không ép hợp nhất major transitive, bỏ Unicode validation, đổi crypto provider hoặc tắt feature trước khi xác định tác động. Audit không có advisory đã biết không thay thế kiểm chứng consumer.

## 3. Câu hỏi kiến trúc và các hướng cần so sánh

| Ưu tiên | Câu hỏi/phương án | Bằng chứng cần thu | Điều kiện dừng hoặc loại |
|---|---|---|---|
| R1 | Cặp rand hiện tại so với 0.10.3/0.6.0 | Compile public bounds; Beta domain/moments; RNG/entropy behavior; dependency/build delta; thời gian sampling | Chỉ có lợi ích “mới hơn”, consumer hỏng, sai distribution hoặc thêm burden không có lợi ích cụ thể. |
| R2 | scraper → projection hiện tại so với html5ever TreeSink ghi vào arena trực tiếp | HTML5 tree parity; text/links/owner fidelity; CPU, allocations, peak RSS; khả năng thực thi quota | Không hỗ trợ đầy đủ tree repair, template, reparenting hoặc quota/error path; không có lợi ích đo được. |
| R3 | reqwest client theo fetch so với cache client hữu hạn theo policy/address; Hyper connector là phương án tiếp theo | TLS handshakes/request; cold/warm latency; socket peer; DNS/policy generation; idle/active memory | Reuse vượt tenant/policy, địa chỉ chưa kiểm tra, stale authorization, cache không có cap hoặc không kiểm chứng được socket. |
| R4 | BM25 query-only so với index lexical dùng lại theo source; full sort so với partial selection | Score oracle; DF/length đúng; tie/order contract; p50/p95/p99 theo query/chunk count; index amortization | Index tốn hơn ở one-shot; partial top-K làm đổi thứ tự các block hoặc làm mất score cần cho novelty. |
| R5 | Link ownership/dedupe và text assembly | Scaling theo anchors/unique URLs/nested owners; encounter order; URL/text bytes; allocations | Chỉ nhanh trên anchors lặp; owner lồng nhau sai; temporary storage vượt quota hoặc không có RSS evidence. |
| R6 | Table grid hiện tại và tích hợp extraction/header semantics | Row groups, rowspan=0, colspan, overlap, nested tables; reference đúng độc lập; memory theo occupied slots | Chỉ đối chiếu text đẹp; trộn parser repair với grid semantics; chưa giữ header association. |
| R7 | JSON/hash buffering so với writer trực tiếp; streaming là hợp đồng riêng | Exact JSONL/hash byte stream; escaping/budget/end; CPU, RSS, first-result timing | Bỏ flush error/end frame; đổi hash; streaming trả thành công dở dang; claimed TTFB không được đo. |
| R8 | Admission/lifetime hiện tại; phân shard map hoặc resolver async chỉ khi cần | Pending/worker bounds; cancel/timeout races; wait/hold time; contention profile; actual job completion | Permit mất trước worker exit, follower bypass quota, queue tăng không giới hạn; thêm runtime/lock/service theo phỏng đoán. |
| R9 | Tokenization/chunk selection đa ngôn ngữ; neural rerank chỉ là nhánh có điều kiện | Corpus có nhãn; evidence recall/answer quality; Unicode boundaries; inference+ranking cost | Đổi semantics mà không có nhãn; quảng cáo INT8 dot-product như toàn bộ inference; GPU không có workload cần nó. |

**Thứ tự nghiên cứu:** khóa inventory/compatibility và R1 trước; xử lý khoảng trống đo lường; ưu tiên R2/R5/R7 cho CPU/memory, R3 cho network; R4/R6 theo hợp đồng nội dung. R8 chỉ thay cấu trúc khi profile hoặc failure trace chỉ ra nguyên nhân. R9 cần dữ liệu chất lượng trước dependency ML. Không triển khai đồng thời chín hướng.

### 3.1 Parser: ba phương án, ba phạm vi khác nhau

P0 giữ scraper/html5ever và tối ưu projection; đây là comparator correctness. Source hiện tại dùng `Html`/`Node`, chưa dùng CSS `Selector`; có cơ sở nghiên cứu giảm lớp wrapper/dependency, nhưng chưa có bằng chứng giảm runtime.

P1 dùng html5ever 0.39.0 với TreeSink riêng. Phải giữ HTML5 repair, template contents, sibling insertion, text merging và reparenting. `create_element`/`append` không trả `Result`, nên **không giả định** gắn counter vào TreeSink là tự có early abort/quota an toàn. Agent phải chứng minh error/termination strategy trước prototype được chọn. Chưa chứng minh được thì giữ P0. [TreeSink 0.39.0](https://docs.rs/html5ever/0.39.0/html5ever/tree_builder/trait.TreeSink.html).

P2 khảo sát lol_html như fast path có phạm vi hoặc streaming analysis, không xem là thay thế DOM tương đương. Memory limit của rewriter đo gần đúng các internal buffers; không bao toàn bộ handler/output/RSS. Cần kiểm tra version cụ thể và semantics bailout/partial output; không mở chế độ graceful bailout rồi coi kết quả chưa hoàn tất là thành công. [MemorySettings](https://docs.rs/lol_html/latest/lol_html/struct.MemorySettings.html).

Quyết định parser phải tách ba tính chất: conformance của parser, fidelity của extraction và giới hạn tài nguyên. Một tính chất đạt không chứng minh hai tính chất còn lại.

### 3.2 Transport: reuse giữ đúng quyền truy cập

So sánh T0 client-per-fetch với T1 client cache hữu hạn. Cache identity phải phản ánh host/port, tập IP vừa được kiểm tra, effective policy, cấu hình TLS và privacy context. Phân biệt active/idle connection, policy epoch và invalidation; không đồng nhất DNS cache với HTTP content freshness. OS getaddrinfo hiện tại không cung cấp authoritative DNS TTL: không bịa TTL từ API này. Resolver khác là một thí nghiệm riêng.

Reqwest có connection pooling; feature/config phải đọc theo 0.13.5. Mặc định hệ proxy/redirect khác cấu hình Athena; giữ explicit no_proxy, không redirect, hostname certificate validation và DNS-to-connect pinning. Thử header limits riêng: body byte cap không tự giới hạn tổng headers. Chỉ chọn Hyper/custom connector nếu T1 không đáp ứng hợp đồng hoặc profile chứng minh overhead đáng kể. [Reqwest 0.13.5](https://docs.rs/reqwest/0.13.5/reqwest/), [ClientBuilder](https://docs.rs/reqwest/0.13.5/reqwest/struct.ClientBuilder.html).

### 3.3 Toán học phục vụ quyết định

- BM25: oracle kiểm tra DF/IDF, mọi document length và normalization; bỏ lưu non-query terms không được bỏ đếm chúng. Full ordering còn cần cho AAE thì top-K không tương đương hợp đồng hiện tại.
- Link membership: scan Vec cho L anchors khác nhau tạo L(L−1)/2 comparisons. Đo cả repeated/unique/nested owners và byte hashing; không gọi toàn bộ HTML parsing là tuyến tính.
- Beta: với α,β>0, mean=α/(α+β), variance=αβ/((α+β)²(α+β+1)). Seed/sequence và distribution correctness là hai tiêu chí khác nhau; không đòi sequence giống nhau khi chưa có hợp đồng đó.
- Floating point: công thức tương đương trên số thực chưa bảo đảm byte/tie order của f64. Với phép cộng finite, round-to-nearest, không overflow/underflow, bound γₙ=nu/(1−nu), u=2⁻⁵³, giúp định trước tolerance theo scale. Oracle kiểm tra riêng ordering gần tie; không nới tolerance sau khi thấy FAIL.
- Chi phí index/reuse: ghi setup cost, per-query cost và số query/source cần amortize. Không lấy benchmark nhiều query áp cho mọi one-shot fetch.

Không cần thêm toán phức tạp nếu nó không thay đổi quyết định hoặc tạo được một gate kiểm chứng.

## 4. Chứng cứ hiện có và khoảng trống cần lấp

Đo Windows trước đây cho median p50 theo vòng: rank 19.8860→6.4200 ms; table 8.4021→0.6844 ms; pipeline có query 31.3633→18.2199 ms. Đây là tín hiệu ưu tiên nghiên cứu, chưa phải quyết định áp dụng.

Parse p95 trong cùng đợt là 11.1738→15.7869 ms; pipeline không query p95 13.0801→13.1522 ms. Không bỏ các số này khi trình bày cải thiện. Chưa có peak RSS/allocator hoặc số liệu Kaggle.

**Thiếu bằng chứng quan trọng:** `bench_core` và `bench_html` hiện chỉ xuất percentiles/summary; chúng không xuất từng iteration. Trường `raw` trong report hiện chứa summary từng vòng, không phải raw timing samples. Không thể dựng lại CI hoặc timeline từ dữ liệu này. Agent bổ sung instrument vào harness hiện có trước vòng confirmatory; không tạo một bộ benchmark song song rồi gọi là tương đương.

Tách ba comparator: B0 source ban đầu với cùng security patch/tuple thư viện được chọn làm nền; B1 working-tree draft hiện có; B2 một ứng viên thay đổi cụ thể. Ghi đầy đủ diff của B0 so với HEAD và của từng B2. Không trộn thay đổi source, RNG, compiler, allocator và release flags trong một lần so sánh. B0 là reference lịch sử, không phải cấu hình mặc nhiên hợp lệ để triển khai.

## 5. Giao việc cho agent khác

| Vai trò | Đầu việc | Đầu ra và giới hạn |
|---|---|---|
| Agent nghiên cứu thư viện/kiến trúc | Đối chiếu manifest/lock/upstream; hoàn thành matrix R1–R9; chỉ ra lựa chọn và rival có thể bác bỏ nó | Một decision table trong kế hoạch, sources cụ thể, UNKNOWN rõ ràng. Chưa sửa mã production. |
| Agent kiểm chứng correctness/compatibility | Khóa oracle/hợp đồng; chạy existing checks; khảo sát candidate trong scratch workspace khi đã có experiment spec | Logs thực, expected differences định trước, failing counterexample tối thiểu. Không “sửa” oracle để candidate PASS. |
| Agent đo performance | Thiết kế/run paired benchmark Kaggle; lưu raw samples/RSS/environment; tính CI và phân tích failure | Một run directory với bằng chứng; không đổi production source hoặc chạy nhiều benchmark cạnh tranh CPU cùng lúc. |
| Agent review quyết định | Đọc source diff, lock/feature diff và bằng chứng từ các vai trò trên | ACCEPT / REJECT / HOLD theo gate; không dùng lời xác nhận của implementer thay dữ liệu. |

Có thể một agent đảm nhiệm nhiều vai trò, nhưng phải giữ comparator/oracle và dữ liệu độc lập với lựa chọn muốn chứng minh. Nghiên cứu độc lập có thể song song; đo trên một máy phải tuần tự. Tài liệu này không tự khởi chạy agent hoặc Notebook.

Code Review connector hiện có phục vụ CI diagnostics của PR; khi chưa có PR, không ghi rằng connector đã review source cục bộ. GitHub dùng để đọc upstream/source đúng ref. Codex Security findings và dependency audit là input cho gate; chúng không thay thế compatibility/performance checks.

## 6. Hướng dẫn kiểm chứng trên Kaggle

### 6.1 Chuẩn bị đúng môi trường và snapshot

Tệp người dùng xác định đã tìm thấy ở **`C:\Users\ADMIN\Downloads\kaggle (2).json`**; mới xác định tên/đường dẫn, chưa đọc hoặc kiểm chứng credential. Xem nó là credential candidate cho bước cấu hình, không phải test corpus. Kaggle CLI tìm tệp tên `kaggle.json` trong config directory; agent dùng bản sao riêng với KAGGLE_CONFIG_DIR khi cần, không ghi đè credential sẵn có, không đưa khóa vào Notebook/source/log/dataset. [Kaggle CLI configuration source](https://github.com/Kaggle/kaggle-cli/blob/main/src/kaggle/api/kaggle_api_extended.py).

Chọn **CPU Notebook** cho Rust lexical/parser/serialization trước. GPU chỉ là experiment riêng nếu R9 có model/corpus phù hợp. Kaggle hỗ trợ Python Notebook trên CPU/GPU images; Rust được build/run như process từ Notebook, không giả định Kaggle có notebook language Rust hay có sẵn Rust 1.99. [Kaggle docker-python](https://github.com/Kaggle/docker-python/blob/main/README.md).

Agent cần xác định Notebook slug, machine shape/image thực tế và quyền internet. Notebook/source/corpus dự án đặt private; kernel metadata khác credential JSON. Dùng đúng schema CLI hiện tại và ghi phiên bản công cụ; không bịa resource quota, CPU model hoặc image digest chưa đọc được. [Kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md).

Đóng gói source/corpus theo allowlist. Bao gồm thay đổi working tree và file nguồn có chủ đích chưa tracked; loại .git, credentials, bytecode, target và results cũ. Commit SHA một mình không định danh dirty workspace: ghi source-file hashes, diff và generator/fixture hashes. Windows .exe không dùng làm comparator Linux; build cả hai snapshot cùng toolchain/profile trên Kaggle.

Chuẩn bị dependencies ngoài khoảng đo. Capture rustc/cargo -Vv, target, compiler/linker/C tools, Cargo.lock hash, unified features, release flags, CPU flags/quota/affinity, RAM và image/session metadata có thể đọc. Trường không có dữ liệu ghi UNKNOWN. Không đổi Rust 1.99/edition hoặc bật native CPU/PGO để cứu một candidate không compile.

### 6.2 Không sinh test files bừa

Ưu tiên test đang có trong module/integration và `model/test_models.py`; dùng `src/bin/bench_core.rs`, `src/bin/bench_html.rs`, `model/benchmark.py` làm điểm đo hiện có. Những lệnh sau dành cho agent kiểm chứng, **chưa được thực thi trong vòng lập kế hoạch này**:

```text
cargo metadata --locked --format-version 1
cargo tree --locked -e features
cargo fmt --all -- --check
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --release --bins
python -m unittest model.test_models
python -m model.benchmark <baseline_directory> <candidate_directory> --rounds 7 --output <run_directory>/comparison.json
```

Các path trong dấu <> là placeholders, phải thay bằng directory thực. Lệnh benchmark hiện tại chỉ là bước thăm dò; chưa đáp ứng raw-sample/CI protocol bên dưới. Dùng `python -m model.benchmark` để tránh `model/statistics.py` shadow thư viện standard khi chạy script trực tiếp. Các check chỉ chạy nơi module đó thuộc snapshot; không báo baseline đã qua một test không tồn tại ở baseline.

Chỉ thêm regression test khi có invariant chưa được test hoặc counterexample cụ thể. Ưu tiên thêm case vào module đang sở hữu invariant. Trước mỗi file test mới, ghi: invariant nào, existing test vì sao không đủ, oracle độc lập ở đâu, và vì sao cần file riêng. Không thêm test lặp implementation, snapshot sinh từ chính candidate, sleeps đoán scheduling, access internet thật cho unit test hoặc dependency mới chỉ để tăng số lượng case.

Generator tạo nhiều input trong memory/scratch; chỉ persist một minimal failing fixture khi cần tái hiện bug. Harness/instrument của experiment ở scratch hoặc sửa điểm đo hiện có theo phạm vi được giao; không đưa comparator adapters vào production.

### 6.3 Matrix correctness tối thiểu theo invariant

| Nhóm | Case cần có | Oracle/gate |
|---|---|---|
| HTML/text | entities, preformatted, malformed nesting, template/foreign content, deep/wide DOM, đa ngôn ngữ | Tree/reference và expected content độc lập; không chỉ count node/output length. |
| Links | unique/repeated/nested owner, long base + fragments, URI edge, quota−1/quota/quota+1 | Encounter order, ownership, URL validity và exact byte budget. |
| BM25 | empty/duplicate query, ties, non-ASCII, short/long chunks, corpus reuse | Công thức/oracle độc lập, document lengths/DF, specified floating tolerance và output ordering. |
| Tables | rowspan=0, group edges, colspan/rowspan limits, overlaps, nested table, headers | Grid/cell ownership đúng; expected error/fallback định trước. Không lấy parser DOM đúng suy ra grid đúng. |
| AAE/hash | escaping UTF-8/newline, exact limits kể cả end, long writes, flush/error | JSON schema/framing/hash oracle; byte parity khi semantics phải giữ; expected fixes định trước. |
| Concurrency | overlapping callers, late caller, leader/follower cancellation, DNS/parse/export timeout | Barrier/state transitions, pending counts và permit giữ đến actual job exit. Không “ngủ đủ lâu chắc đã chạy”. |
| Network/TLS | mixed public/private DNS, literals, bad policy/ETag, redirects/proxy, certificate identity, connected IP | Tách pure/mocked contract và actual socket/TLS evidence; không gộp thành một PASS. |
| RNG upgrade | α/β boundaries, sample finite/support, moments, seeded reproducibility contract, eligibility | Oracle analytic và sampling plan định trước; không đòi entropy/sequence behavior chưa có hợp đồng. |

Không tắt public-IP guard hoặc certificate verification của production để chạy localhost fixture. Kaggle thiếu quyền/network setup cho actual socket containment thì ghi **NOT VERIFIED**, không đổi mock thành bằng chứng TLS/egress thật. Pathological case chạy với supervisor/time/memory budget; timeout/OOM là kết quả thất bại hoặc HOLD, không bỏ khỏi báo cáo.

### 6.4 Protocol đo tránh kết quả sai

1. Khóa experiment spec trước chạy: candidate ID, input cohorts, oracle, metric chính, secondary metrics, warm/cold mode, seeds, workload limits, repetitions và stop budget. Tách discovery corpus dùng chọn hướng khỏi confirmation corpus dùng chốt.
2. Cùng fixture/query/limits, toolchain/profile/flags và harness cho mỗi cặp. Consume result, kiểm correctness ngoài khoảng đo. Equal output sizes không chứng minh nội dung tương đương.
3. Chạy tuần tự baseline/candidate theo block order cân bằng được chọn trước. Không chạy compile, package download hoặc benchmark khác đồng thời. Ghi warmup riêng; không bỏ outlier/failed request vì số liệu xấu.
4. Bổ sung **raw timings theo thứ tự gốc**, round/case/candidate IDs, status và output fingerprint vào harness hiện có. Tính quantile trên bản sao; không chỉ lưu mảng đã sort hoặc percentiles. Ghi rõ CPU time/wall time; không gọi CPU offline benchmark là network request latency.
5. Peak RSS đo mỗi case trong process mới, cùng input/setup. Ghi rõ RSS bao gồm process/setup/workspace; không dùng cumulative high-water mark nhiều stage để giả làm peak riêng từng stage. Allocations đo riêng khi instrument có thể làm đổi timing.
6. Screening dùng workload nhỏ để loại phương án sai hoặc quá tốn; không gọi screening là acceptance. Confirmation trên các finalist và đường quan trọng: mặc định thiết kế 20 paired blocks × 250 samples/nhánh/cohort = 5.000 samples. Budget không đủ thì giảm phạm vi công bố hoặc HOLD, không tự hạ gate rồi PASS.
7. Báo p50/p95/p99 theo cohort và sample count. Với n=20, xác suất thấy ít nhất một sample vượt population p99 chỉ khoảng 1−0.99²⁰≈18,2% nếu iid; không đủ chốt tail. 5.000 samples cũng không tự bảo đảm precision hoặc independence trên VM; cần kiểm tra drift/autocorrelation từ raw data.
8. Tính CI trên paired blocks, giữ cấu trúc workload; không coi mọi iteration tương quan là sample độc lập. Study 2.1 dùng paired-cluster bootstrap, resample nguyên cặp round baseline/candidate, **30.000 draws, seed 42**; tính lại delta quantile trên samples cùng cohort. Family gồm **57 intervals toàn bộ ba cụm** (c1=21, c2=28, c3=8), hai phía với α=0,05 và Bonferroni α/(2M) cho mỗi đuôi. Khóa policy `paired-rounds/2.1` trước chạy: lag-1 trong block là diagnostic; lag-1 giữa median round mỗi nhánh hoặc delta quantile từng cặp có trị tuyệt đối ≥0,5 → HOLD. Đây là screening, không chứng minh độc lập. CI bootstrap xấp xỉ; Bonferroni không sửa được interval thiếu coverage. Quá ít paired blocks → HOLD. RSS/RNG chỉ công bố median block/process và maximum quan sát khi phù hợp, không bịa p99 từ 20 phép đo. Policy/family cũ 2.0 (54 intervals) giữ nguyên cho dữ liệu cũ; không hồi tố PASS.
9. Muốn claim không hồi quy: mặc định tolerance ε=0 theo yêu cầu. Với metric chi phí/latency/memory, bound trên của delta candidate−baseline phải không dương trong phạm vi thống kê được nêu, hoặc có bounded proof thích hợp. Không tự cho phép hồi quy 3–5%. CI rộng hoặc straddles 0 → **HOLD**, không suy “không significant” thành tương đương.
10. Báo cả failure rate, budget rejections và lượng nội dung trả đúng. Candidate trả ít nội dung, thất bại sớm hoặc bỏ validation không được ghi là nhanh hơn cho cùng nhiệm vụ. Không khẳng định optimum/no-tradeoff toàn cục từ hữu hạn workloads.

### 6.5 Bằng chứng gọn, có thể kiểm lại

Mỗi run có một directory với ba artifact chính: `manifest.json` ghi snapshots/versions/environment/spec, commands/exits/check statuses, summaries và hashes; `samples.jsonl` giữ từng measurement/status; `run.log` giữ output chẩn đoán thực. Không xuất credentials hay full page contents không cần thiết vào log. File comparison thăm dò hiện có được xem là dữ liệu legacy, không thay ba artifact confirmatory này.

PASS chỉ khi command thật chạy, đúng target/candidate và exit/result đã ghi. Phân biệt PASS, FAIL, NOT RUN, NOT VERIFIED; unsupported tool không chuyển thành PASS. Không dùng `|| true`, disable test, retry tới lúc green, ghi log giả hay copy kết quả của phiên bản khác. Nếu rerun để điều tra, giữ lần lỗi và lý do; không chọn lần đẹp làm kết luận.

## 7. Khi nào chốt thư viện và mã

Agent reviewer chốt từng nhóm theo thứ tự:

1. **Identity/API:** upstream, registry/version, actual feature graph và consumer contract đã được xác định; Linux build/runtime probe có chứng cứ hoặc HOLD.
2. **Correctness/security/resource:** invariants không yếu đi; expected semantic changes có spec trước; failure/cancellation/byte bounds đạt. Không dùng benchmark bù một gate hỏng.
3. **Fitness:** có lợi ích cụ thể cho Athena; không dùng release fixes của API Athena không gọi làm bằng chứng lợi ích đường lõi.
4. **Performance:** raw comparison đáp ứng protocol và phạm vi không hồi quy; chất lượng nội dung giữ được. Trường hợp chưa đủ chứng cứ giữ baseline hoặc HOLD.
5. **Simplicity/compatibility:** diff nhỏ nhất đủ đáp ứng; không kéo dependency/framework mới thiếu nhu cầu; rollback và affected consumers rõ ràng.

Đầu ra quyết định là một bảng `candidate → ACCEPT/REJECT/HOLD → evidence → residual unknown → implementation scope`. ACCEPT chỉ cho phạm vi được đo/chứng minh. Sau đó mới ghi kế hoạch implementation: symbol/consumer cần sửa, semantic diff, regression cần giữ và check bắt buộc. Không bắt đầu viết mã production cho một candidate HOLD.

## 8. Điều chưa được chốt

Kaggle Notebook slug/image/CPU quota, credential validity, Linux evidence, raw timing protocol, parser RSS và nhánh nâng RNG chưa được xác minh. Parser mới, pooled transport, top-K, neural rerank và feature trimming là hướng nghiên cứu có rival/gate; chưa là quyết định triển khai. Security scan đang là draft và bằng chứng source riêng; không thay kết quả Kaggle và không được diễn giải thành chứng nhận an toàn tuyệt đối.

Vòng tiếp theo: người dùng kiểm screening 2.1 trước, sau đó confirmation cả ba cụm cùng bundle, giữ nguyên artifacts và trả bằng chứng. Agent phân tích theo từng phạm vi rồi mới chốt core/dependency. `export_no_query` parse sẵn ngoài timer, dùng cùng input với pipeline không query; export có query vẫn bao gồm ranking. Chênh lệch giữa các median không tự chứng minh phân rã chi phí. Việc sửa harness được phép không mở rộng thành migration core hoặc tự khởi chạy Kaggle.
