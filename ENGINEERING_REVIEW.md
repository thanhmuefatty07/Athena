# Athena — báo cáo nâng cấp và kiểm chứng lõi

## Bổ sung 19/10/2026: Rust 1.99.1, audit và đa ngôn ngữ

Người dùng yêu cầu nâng compiler, audit gaps và nghiên cứu boundaries có thể
được lợi từ ngôn ngữ khác. Không đổi production Rust source, dependencies,
`Cargo.lock` hoặc release profile. Toolchain và package minimum là 1.99.1;
CI/bootstrap/native guards/Kaggle runner cùng pin mới. Baseline/candidate source
archives và ba kernel được tạo lại, chưa upload/chạy. Bundle mới là
`cf749e21d96a058e2bbbac50c97e9df3f4081525c7cc1ae135a38b9d4a833a61`;
historical 1.99.0 evidence giữ nguyên identity, không resume/ghép với study mới.

Actual local runtime: `rustc 1.99.1 (58cf8f9c3 2026-10-13)`, LLVM 23.1.1,
`x86_64-pc-windows-msvc`; Cargo 1.99.1; Python 3.11.9/NumPy 1.26.4 cho owner
controls. Đây không là kiểm chứng Linux3.13 hoặc đo compiler speedup.

Security Standard scan giữ snapshot trước sửa pin: full review 14 file `src/`,
integration suite và bốn harness owners; targeted excerpts cho research/corpus/
source/evidence boundaries. Có independent baseline và architecture model.
Không xác nhận reportable vulnerability; **coverage toàn repository partial**.
Không chạy exploit/live network/model/cloud. Scan không bao phủ dependency
internals, toàn archive source, production deployment hay API chưa triển khai.
Tra RustSec riêng sau scan dùng cargo-audit 0.22.2, database 1.338 advisory,
commit `999ec89d5d3f10f20be14750ec8c2d8b44e0ac9b`, cập nhật 17/10/2026:
216 locked dependencies, 0 advisory matches, không warnings, exit 0. Đây là
database matching, không chứng minh dependency không có vulnerability chưa biết.

Khoảng trống đã xác định: CLI đọc toàn file trước admission; HTML5 parser tạo
full DOM trước projection quotas; AAE hiện tạo full frame/joined text. Đính
chính mô tả 8 KiB ở bảng dưới và blueprint/README. Semaphores per Engine chưa
là global/per-origin fairness; source authorization vẫn thuộc caller. Controlled
TLS adapter không chứng minh production Engine network success. Cache/304,
HTML table integration, complete search/read/render/crawl, authenticated gateway/
MCP và W3 OS sandbox chưa hoàn thành. Các điểm này là research/integration gaps,
không tự biến thành security findings.

CORE_RESEARCH_PLAN.md §19.14 ghi comparator/closure cho từng gap và boundaries
C++/CXX, Node/browser, compiled GPU kernels, Arrow và conditional service rivals.
Chưa có Athena evidence để chọn một language thắng Rust. Quyết định tính cả
conversion/copy/IPC, batch queue delay, sync/drop/error, aggregate memory và
correct task cost. simdjson parsing không thay AAE serialization/HTML5 parsing.

Các lệnh dưới đã chạy bằng runtime nêu trên:

| Check thực thi | Kết quả/exit và giới hạn |
|---|---|
| `cargo fmt --all -- --check` | Exit 0 |
| `cargo test --locked --all-targets --jobs 2` | Exit 0; 41 unit + 2 integration PASS, 0 failed/ignored. Ba binary targets có 0 tests, không cộng behavioral evidence |
| `cargo clippy --locked --all-targets --jobs 2 -- -D warnings` | Exit 0, không Clippy warnings |
| `cargo build --locked --release --jobs 2` | Exit 0; optimized release build hoàn tất trong 5 phút 58 giây trên máy local. Đây là build duration, không là runtime performance measurement |
| `python -B -m unittest -v cloud_experiments.test_campaign kaggle_experiments.test_protocol model.test_models cloud_experiments.service_lab` | 85 selected, 83 PASS + 1 ERROR + 1 skip, exit 1. Lỗi thật ở manifest identity guard còn hash 1.99 cũ sau khi nâng minimum version; không nuốt lỗi |
| Focused existing manifest/lock case; sau đó `python -B -m unittest -v cloud_experiments.test_campaign` | Focused 1 PASS/exit 0; owning suite 34 selected: 33 PASS + 1 skip/exit 0. Chỉ đổi approved manifest hash sau khi xác nhận diff là đúng một minimum-version value; giữ lock guards và thêm negative input vào cùng test hiện hữu |
| `cargo audit --json` | Exit 0; 0 matches trên database và lock identity nêu trên |
| `cargo deny --locked check advisories bans sources` | Exit 0; default config vì chưa có deny config. Ba duplicate warnings: getrandom, r-efi, syn. Không phải zero-warning/custom-policy certification; không chạy licenses gate |

51 protocol/model/service cases không bị ảnh hưởng đã PASS trong lượt Python
đầu; 33 cloud cases PASS trong lượt owning-suite cuối, tổng 84 distinct cases
PASS và 1 Windows symlink case skip. Không mô tả command 85-case đầu thành xanh.
Không thêm test file; case manifest/candidate lock kiểm hành vi transform nhận
source đã review và từ chối bytes bị sửa, không chỉ assert version text.
Windows không cho tạo symlink nên Linux gate vẫn NOT VERIFIED. Service oracle
controls không đồng nghĩa đã chạy native TLS server/model provider/production.

Offline integrity checks xác nhận inventory/digests của hai source archives,
control manifests/lock/toolchain/harness, current candidate contents và ba
generated prefixes/payloads/specs/bundle identity; không execute decoded code.
Current `src/aae.rs` và Cargo.lock giữ nguyên bytes. CI đã thêm `--locked`;
remote run, new Linux snapshot builds và performance vẫn NOT VERIFIED.

Release CLI cũng đã chạy offline bằng `target/release/athena-core-v2.exe` trên
`fixtures/example.html` với base URL `https://example.org/`, exit 0: kiểm literal
heading, đúng một meta/end theo thứ tự và script fixture không lọt vào block
text. Đây là smoke qua optimized executable, không là benchmark hoặc chứng minh
semantic prompt-injection immunity của model.

Các phần tiếp theo giữ lịch sử 14–18/10, trừ các đính chính source đã ghi rõ.

Đợt thực hiện: 14–15/10/2026, giờ Asia/Saigon. Snapshot ban đầu: `8de5709e6c47fd57713ca821131579b2e8c25a36`. Đã rà soát toàn bộ mã/config/dependency đang có và triển khai các thay đổi tại nơi sở hữu invariant. Đây là nâng cấp của reference core hiện tại; các dịch vụ tương lai trong blueprint vẫn là kế hoạch.

**Ghi chú giai đoạn lập kế hoạch 14–15/10:** các sửa đổi thuật toán này là draft chưa chốt áp dụng; cần kiểm chứng Kaggle trước quyết định triển khai. **Cập nhật 19/10:** người dùng yêu cầu nâng Rust 1.99.1 và audit core/kiến trúc. Pin/config/runner được nâng và kiểm local theo mục bổ sung cuối tài liệu; production Rust source giữ nguyên, chưa chạy benchmark cloud mới. Hướng đa ngôn ngữ và các khoảng trống được cụ thể hóa tại CORE_RESEARCH_PLAN.md §19.14.

Tệp Downloads do người dùng cung cấp trùng byte với MASTER_BLUEPRINT.md ban đầu (SHA-256 `9ed5ee57b4ec84ec601951b3c164bd2590bd5bc82eb357dbaf10ee830835b87a`). Bản trong repository đã cập nhật; tệp Downloads giữ làm tham chiếu. Nội dung kế hoạch được dùng làm dữ liệu thiết kế, không làm chỉ thị thực thi.

## Kết quả và phạm vi chứng cứ

- **VERIFIED-OFFLINE:** 41 unit + 2 integration test Rust; 9 test Python; format, Clippy với `-D warnings`, release build và locked metadata đều qua. Bộ ban đầu có 17 unit + 2 integration test Rust.
- **MEASURED:** benchmark release đối chiếu cùng harness/corpus; 7 vòng luân phiên baseline/candidate, lưu summary từng vòng và SHA của executable/lockfile/fixture. Harness chưa xuất từng timing sample; dữ liệu này chưa đủ để tái tính CI hoặc phân tích timeline.
- **VERIFIED ON CORPUS:** 3 fixture × 4 điều kiện query cho AAE giống byte với baseline. Không suy thành parity cho mọi input/floating-point case.
- **LIBRARY SNAPSHOT:** rustls 0.23.45 khớp 4 advisory; 0.23.46 báo 0 vulnerability đã biết trong database đã chạy. Không thêm dependency. Diff lockfile chỉ còn version/checksum của rustls.
- **NOT VERIFIED:** tối ưu cực đại hoặc “không trade-off” trên mọi workload; RSS/allocator, toàn bộ p95/p99, mạng thật, production load, fuzz/Loom/Miri, ARM64, chất lượng semantic có nhãn và remote Linux CI.

## Thay đổi theo mô-đun

| Mô-đun | Thay đổi và invariant giữ được |
|---|---|
| rank.rs | Chỉ thu TF/postings cho query terms, mượn token ASCII đã lowercase; vẫn đếm mọi token cho document length/average. IDF tính một lần mỗi term; term order xác định; giữ full score ordering và novelty gate. |
| projection.rs | Duyệt children đảo trực tiếp, bỏ Vec trung gian; giữ preformatted indentation/line breaks; root được tính vào node cap. Quota HTML/text/resolved-link bytes; URL dictionary giữ một payload String rồi chuyển vào catalog theo ID. |
| Link membership | Thu IDs rồi dedupe riêng từng owner bằng mảng marker sau traversal. Giữ encounter order và owner lồng nhau; loại đường Vec::contains bậc hai. Có workspace tạm theo node/link count, chưa đo RSS. |
| table.rs | Tính group end một lần thay suffix scan mỗi row; row slots tăng theo width thật, rồi pad rectangular grid. Giữ rowspan=0, group boundary, overlap rejection và column budgets. |
| aae.rs | Đính chính audit 19/10: source hiện tại tạo toàn bộ frame JSON trước khi kiểm cap và tạo joined page String trước khi hash. Giữ logical cap kể cả escaping/newline/end và complete-or-error; mô tả cũ về hai buffer 8 KiB/incremental hashing không khớp source hiện tại, không dùng làm bằng chứng giới hạn RSS. |
| orchestrator.rs | Validate từng caller trước flight; key dùng toàn bộ effective policy, URL canonical và tenant. Pending cap trước clone/map insertion; CPU queue trong admitted requests; permits sống cùng blocking worker; deadline tổng bao quanh source/CPU/output wait. |
| network.rs | Typed host check chặn literal IPv4/IPv6; validate policy/ETag trước DNS; timeout DNS+HTTP+body. Resolver giữ origin permit của Engine trong job OS thật, rồi pin chính các địa chỉ đã kiểm tra vào TLS request. |
| ip_policy.rs | Đối chiếu IANA special-use IPv6; chặn protocol/documentation ranges và giữ các allocation được liệt kê là global. Không gọi classifier là chứng minh reachability/SSRF-safe tuyệt đối. |
| singleflight.rs | Giữ cơ chế leader/watch/identity cleanup; test overlap/cancellation đồng bộ theo trạng thái thay ngủ chờ. Generic SingleFlight không tự là admission controller; Engine sở hữu quota. |
| arena.rs / bandit.rs | Đã đọc definition/callers/tests; giữ layout/bounds/reset và posterior/eligibility semantics. Không đổi heuristic khi chưa có nhãn hoặc telemetry để chứng minh chất lượng. |
| model/ | Decimal input chính xác hơn, finite/domain validation; Erlang-B recurrence → Erlang-C dùng O(1) storage; RNG cục bộ; log1p ở probability bound. |
| Blueprint | Đối chiếu kiến trúc với mã đang có; cập nhật trạng thái compiler/lockfile, giới hạn tài nguyên, độ phức tạp thuật toán và các phần chưa triển khai. |

Các thử nghiệm giữa đã được giữ trong `results/benchmark-intermediate.json`, `benchmark-hash-buffer.json`, `benchmark-json-buffer.json`, `benchmark-firstlink.json` và `benchmark-pre-dns-admission.json`. Bản đầu có hồi quy no-query khoảng 3,5%; đã không chốt bản đó. Batching và sửa ownership/dedupe được áp dụng trước đo draft cuối. Các summary giữ cả vòng bất lợi; từng iteration không được xuất bởi harness, nên không thể hậu kiểm outlier riêng lẻ.

## Toán học và kiểm chứng đúng đắn

Kiến trúc hiện tại giữ một crate và các module có ownership riêng; chưa có requirement buộc tách chín crate hay thêm service. Blueprint đã bỏ số crate cố định, phân biệt sơ đồ sản phẩm đích với mã đang chạy. Source flight chia sẻ fetch/parse theo URL + tenant + effective policy; representation flight xử lý query/output. Engine quản lý admission/lifetime, projection và AAE quản lý giới hạn dữ liệu do chúng tạo.

Giữ HTML5 parser hiện có để bảo toàn tree repair/entity decoding. Parser tự viết, SIMD fast path và neural rerank chỉ hợp lý khi có profile hoặc corpus chất lượng chứng minh lợi ích và kiểm chứng parity; chưa có chứng cứ đó. Các thay đổi hiện tại tập trung vào scans/copies/allocations và lifetime đã xác định được, không đổi thuật toán truy hồi theo giả định chất lượng chưa đo.

BM25 giữ:
`IDF = ln(1 + (N-df+0.5)/(df+0.5))`,
`score = Σ IDF × tf × (k1+1)/(tf+k1×(1-b+b×length/avg_length))`.
Chỉ lưu term của query không làm mất đóng góp của term khác: đóng góp của chúng bằng 0; toàn bộ length vẫn được tính. Test đối chiếu điểm tính tay với tolerance 1e-12, duplicate query, ties, Unicode, empty query và zero highlight limit. Sort toàn bộ vẫn O(N log N); không quảng cáo là top-K tuyến tính. Hash maps có chi phí kỳ vọng; HTML5 parsing không được chứng minh tuyến tính bởi thay đổi này.

Link assignment/dedupe riêng có O(nodes + link references + unique links + text/normalization), với mỗi chunk được xử lý trọn danh sách sau traversal. Marker dùng NodeId khác root 0; dictionary chỉ cấp IDs liên tục cho URL mới. Di chuyển String bảo toàn ID/catalog order. Test có 1000 unique links, repeated IDs và owner lồng nhau.

Erlang-C: `B0=1; Bi=a×B(i-1)/(i+a×B(i-1)); C=Bc/(1-rho+rho×Bc)`. Đối chiếu với định nghĩa stationary trực tiếp trên 16 cặp workers/utilization, M/M/1 closed form và 10.000 workers sát saturation. Đây là mô hình Poisson/exponential, không phải dự báo capacity cho heavy-tail origins. Scenario cost giả định vẫn COGS 145.40 USD/million và gross profit -16.40 USD/million; không sửa input để làm kết quả kinh doanh đẹp hơn.

## Dependency inventory

Phiên bản resolved trong lockfile; tất cả direct dependencies đều có consumer trong source.

| Dependency | Version | Vai trò/quyết định |
|---|---:|---|
| bytes | 1.12.1 | Body/arena immutable buffers; giữ |
| blake3 | 1.8.7 | Content/block hashes; giữ |
| rand | 0.9.5 | Tier sampling; giữ |
| rand_distr | 0.5.1 | Beta distribution; giữ |
| reqwest | 0.13.5 | rustls/http2/stream, default features tắt; giữ DNS/TLS policy |
| scraper | 0.27.0 | HTML5 tree, html5ever 0.39.0; giữ correctness path |
| serde | 1.0.229 | Typed envelope serialization; giữ |
| serde_json | 1.0.154 | Escaped JSONL; giữ |
| tokio | 1.53.3 | Async/sync/blocking work; đọc source resolver/cancellation |
| url | 2.5.8 | Canonical/relative URLs; From<Url> chuyển serialization String |
| futures-util | 0.3.34 | Bounded response streaming/poll tests; giữ |

Không nâng hàng loạt semver hoặc thay parser/TLS/runtime để lấy benchmark đẹp. Không thêm runtime dependency, unsafe, global client, retry loop hay service mới.

RustSec database: 1314 advisories, commit `caee11f41fab07d676c0a4655a2a5dd1f94da97c`; lockfile 216 packages. Match ban đầu: RUSTSEC-2026-0346/0347/0348/0349 (TLS Debug secrets, ECH buffer handling, certificate decompression allocation, key-exchange downgrade). Reachability phụ thuộc feature/config; không khẳng định app đã bị khai thác.

`cargo deny --locked check advisories bans sources` exit 0 với default config; có duplicate warnings getrandom/r-efi/syn do dependency majors/build/platform paths. Không ép unify các major hoặc tắt cảnh báo. License inventory đã đọc; không phải duyệt pháp lý hay blanket license approval.

Raw audit: [before](results/audit-before.json), [final](results/audit-final.json); [deny output](results/deny-final.txt).

## Security: baseline và local remediation

Standard scan giữ snapshot ban đầu, không chạy lại để xóa các lỗi lịch sử. Bốn nhóm availability flaw được parent validate và gia cố:

1. Chunk có L links khác nhau: membership cũ có L(L-1)/2 comparisons; 100.000 anchors có thể nằm trong raw/node budget nhưng khoảng 5 tỷ comparisons là **suy ra**, chưa đo runtime exploit.
2. URL base khoảng 8.000 byte + 100.000 fragment links: hai payload copies có thể khoảng 1,6 GB trước overhead. Đây là **suy ra**, không đo RSS. Nay giới hạn resolved bytes trước retention, giữ một payload và cap output.
3. Cancellation giải phóng quota ở async future trong khi parser/resolver blocking vẫn chạy; export thiếu CPU admission. Nay permit đi vào job thật; DNS sử dụng đúng cơ chế OS mà source Tokio 1.53.3 dùng. Test giả cho RED `available=1`, rồi GREEN `available=0` trước khi release worker; không dùng DNS thật.
4. Query khác nhau có thể tạo flight/subscriber chờ không giới hạn ngoài worker quota. Nay cap pending trước flight và có caller deadline. Test stalled shared source dùng future giả, không network.

Severity medium theo reachability conditional: permitted origin/library caller, chưa có public deployment được xác lập. Không có chứng cứ live exploit hay confidentiality/integrity compromise. Rà soát độc lập cuối chỉ đọc đã kiểm tra các route/owners của bản cuối; parent chạy tests. Một lượt trước bị usage limit nhưng lượt sau đã hoàn thành.

Security scan đang ở bản draft, chưa seal/finalize; không khẳng định đã có canonical report hoàn tất. [Threat model](C:/Users/ADMIN/.codex/state/plugins/codex-security/scans/Athena/8de5709e6c47fd57713ca821131579b2e8c25a36_20261014T131802Z_qcmdhwvl/threatmodel.md) và [hardening portfolio](C:/Users/ADMIN/.codex/state/plugins/codex-security/scans/Athena/8de5709e6c47fd57713ca821131579b2e8c25a36_20261014T131802Z_qcmdhwvl/hardening/hardening.md) là artifact source riêng. Vòng lập kế hoạch không mở thêm scan/test; portfolio không thay quyết định sau bằng chứng Kaggle.

## Benchmark bản cuối

Máy Intel Core i3-8130U @ 2.20 GHz, 2 cores/4 logical; Windows build 26200; rustc 1.99.0 (b940084d7, LLVM 23.1.1), x86_64 MSVC. Release thin LTO, codegen-units=8, panic=unwind; profile không đổi. Không cô lập OS/background workload. Setup/file input ngoài timed stage, có warmup, outputs được consume. Điều kiện này không tương đương hạ tầng production.

Mỗi ô là median của p50/p95 trong 7 vòng; rank/table 40 iterations/vòng, parse/export/pipeline/no-query 20, many-links 10. Percent là giảm thời gian p50, không phải giảm cost hoặc tăng QPS production.

| Stage | Baseline p50 ms | Candidate p50 ms | Giảm p50 | Baseline p95 ms | Candidate p95 ms |
|---|---:|---:|---:|---:|---:|
| BM25, 1000 chunks | 19.8860 | 6.4200 | 67.72% | 37.0107 | 10.7939 |
| Table, 5000 rows | 8.4021 | 0.6844 | 91.85% | 9.7294 | 0.9833 |
| Parse, 1000 chunks | 8.8767 | 8.6345 | 2.73% | 11.1738 | 15.7869 |
| Export có query | 21.1180 | 9.3201 | 55.87% | 26.4425 | 16.3598 |
| Parse + export có query | 31.3633 | 18.2199 | 41.91% | 44.4669 | 25.1155 |
| Parse + export không query | 11.5406 | 10.9169 | 5.40% | 13.0801 | 13.1522 |
| 20.000 links, không query | 78.0049 | 51.3645 | 34.15% | 96.5791 | 53.6747 |

**Không chứng minh cải thiện mọi tail percentile.** Parse p95 cao hơn trong bản đo draft cuối; no-query p95 gần nhau nhưng candidate cao hơn nhẹ. Check bổ sung 9×100 iterations trên bản trước DNS refinement cho parse: p50 8.5991→8.2320; p95 10.8792→11.0448; p99 14.8218→16.7963 ms. Các summary có biến thiên nhưng không có từng timing sample; không đủ cơ sở quy hết khác biệt cho code hay scheduler, hoặc hậu kiểm outlier riêng lẻ. Check này ghi SHA riêng, không trộn vào số draft cuối. Không nhận chứng nhận “không trade-off tuyệt đối”; cần evidence theo CORE_RESEARCH_PLAN.md trước khi chốt.

Fixtures: example.html 757 bytes; generated multilingual 706.662 bytes/1000 chunks; many-links 657.787 bytes/20.000 anchors. Ba fixture, mỗi fixture None/`tokio runtime`/`tài 内容`/`H100 pricing`, cho 12 parity checks. [benchmark-comparison.json](results/benchmark-comparison.json) chứa summary từng vòng, binary/lockfile SHA, compiler và fixture SHA; trường `raw` không chứa từng timing sample. [parse-tail-check.json](results/parse-tail-check.json) chứa kiểm tra bổ sung cùng giới hạn evidence của harness. Protocol xác nhận ở CORE_RESEARCH_PLAN.md yêu cầu raw samples trước khi chốt.

Tái lập: build baseline tại revision đã nêu, chép cùng `src/bin/bench_core.rs` harness vào baseline, giữ compiler/profile; đặt ba executable + Cargo.lock vào results/baseline và results/candidate; chạy `python -m model.benchmark results/baseline results/candidate --rounds 7 --output results/benchmark-comparison.json`. Results/build bị ignore có chủ đích, cần giữ riêng khi chia sẻ bằng chứng. SHA256SUMS.txt ghi byte snapshot workspace, không phải chứng nhận supply-chain.

## Compatibility và phần chưa triển khai

Các signature parse_html/export_jsonl/retrieve và AAE v1 vẫn có wrapper/shape cũ. Có thay đổi có chủ đích: budgets mặc định chặt hơn; max_nodes tính root; preformatted text giữ cấu trúc; invalid policy/MIME bị từ chối; error enum thêm variants. Exhaustive matches ở external consumers phải cập nhật; không có external consumer để compile.

Quota byte giới hạn logical lengths; parser tree được dựng trước projection quotas, URL resolution/buffer/Vec capacity có workspace. DNS/parser/export đã bắt đầu không bị deadline cưỡng chế dừng; nếu worker còn treo thì suất tương ứng vẫn bị giữ. Low-level fetch_https vẫn yêu cầu caller quản lý admission. Total RSS/recovery của production chưa có bằng chứng.

Source authorization/robots, authenticated cache/Vary/304 freshness, pooled resolver-aware connector, tenant fairness, HTTP API, browser isolation, stream/token budget, table extraction/header semantics, labeled rerank, billing và operations là gate tương lai. Blueprint đã chuyển từ giả định “chưa có compiler/lockfile” sang trạng thái kiểm chứng thật; không đánh dấu các gate production chưa chạy là hoàn tất.

## Nguồn kỹ thuật

Primary references: [reqwest 0.13.5 builder](https://docs.rs/reqwest/0.13.5/reqwest/struct.ClientBuilder.html), [IANA IPv6 registry](https://www.iana.org/assignments/iana-ipv6-special-registry/iana-ipv6-special-registry.txt), [RustSec cargo-audit](https://github.com/rustsec/rustsec/tree/main/cargo-audit). Version-sensitive behavior còn được đọc trực tiếp trong downloaded crate source: Tokio 1.53.3 resolver/blocking lifetime, url 2.5.8 From<Url>, ego-tree reverse children, serde_json writer và BLAKE3 Write.

## Cập nhật harness nghiên cứu — 15/10/2026

Đã sửa bộ đo theo [protocol v2](kaggle_experiments/README.md), giữ nguyên source production và dữ liệu Kaggle legacy. `src/bin/bench_core.rs` bổ sung raw nanoseconds/offsets theo thứ tự gốc, output fingerprint, fixture mode và pipeline không query; giữ summary/default CLI cũ. Hai source dùng cùng harness/lock/profile và target directories riêng. Control preformatted phân biệt hai binary; byte parity bắt buộc trên corpus so sánh.

Sửa statistical gate: không biến dữ liệu thiếu thành 0; p99 tham gia phán quyết; family gồm 54 intervals ba cụm, 30.000 bootstrap draws, seed 42; incomplete/hash mismatch/duplicate/unpaired/screening/legacy không được PASS. Báo cáo ghép kiểm lại raw/manifest, đủ cases/quantiles và giữ REJECT khi có hồi quy. Peak RSS chuyển từ polling statm sang GNU time `%M` trên process mới; exit/timeout/lỗi công cụ không được nuốt. c3 kiểm đúng consumer Bandit và tuple RNG trong scratch, 8 Beta shapes với moment/CDF/central variance diagnostics; không gọi 50 synthetic SingleFlight leaders là 50 HTTP fetches hay 17 IP points là kiểm đủ SSRF.

Bằng chứng local Windows Rust 1.99.0 trong lần sửa harness:

- `python -B -m unittest kaggle_experiments.test_protocol -v`: **26 tests passed**; các lỗi dữ liệu rỗng, bỏ p99, báo cáo rỗng, family thiếu, sai stage và variance sụp đã được tái hiện trước khi sửa. Bootstrap weighted được so với resampling mở rộng độc lập; central moments được đối chiếu Fraction chính xác.
- `cargo test --locked --all-targets`: **41 unit + 2 integration passed**, không có benchmark được đo bởi test harness. `cargo fmt --all -- --check` và `cargo clippy --locked --all-targets -- -D warnings`: exit 0. Probe Rust được build/Clippy trong scratch cùng dependency hiện tại; không thêm probe vào production module.
- Hai snapshot được build để kiểm tra chức năng: content/hash/BM25/table oracles đạt; **36** AAE comparisons giống từng byte trên corpus chung. Known semantic control phân biệt baseline/candidate. Candidate qua SingleFlight gated 500 calls/50 keys, request sau flight và leader cancellation, cùng 17 IP points. Đây là oracle hữu hạn, không là chứng nhận tổng quát.

**NOT VERIFIED:** chưa chạy hoặc upload Kaggle protocol v2, chưa đo tốc độ/RSS bằng bộ mới, chưa kiểm tuple RNG 0.10.3/0.6.0 trong Linux, chưa audit advisory mới cho tuple đó; parser/network/rerank alternatives chưa có comparator. Không suy các test local thành kết luận nâng thư viện/core. Người dùng tự chạy ba cụm và cung cấp artifacts trước quyết định triển khai.

## Sửa harness cho lượt nghiên cứu 2.1 — 16/10/2026

Đã đối chiếu lượt người dùng chạy trong `kaggle_results/v2-run-001`: c1 hoàn chỉnh theo policy 2.0; c2 thất bại vì thiếu `/usr/bin/time`; c3 đã qua `cargo check` tuple mới nhưng guard so sánh opaque package IDs coi đường dẫn root scratch khác là thay đổi dependency ngoài RNG. Hai lỗi sau thuộc harness, không chứng minh candidate core hoặc RNG thất bại. Dữ liệu và phán quyết cũ giữ nguyên.

Study 2.1 sửa chuẩn bị GNU time, thêm self-check RSS dương và provenance executable; lỗi cài/version/measurement vẫn dừng. Guard RNG chỉ chuẩn hóa root đã kiểm identity, giữ source/version và feature/edge ngoài RNG closure; graph mới được lưu trước guard. Counterexample root relocation trên graph **216 packages** lưu từ Kaggle cho kết quả không có dependency thay đổi; các ca sửa package/source/feature/edge ngoài phạm vi bị từ chối trong regression.

Thêm `export_no_query`, parse sẵn ngoài timer, để so sánh với pipeline không query. `export` có query vẫn bao gồm ranking/highlight, không là benchmark riêng của serializer. Confirmation c1 tăng từ 60.000 lên **70.000 dòng**; family toàn bộ ba cụm từ 54 lên **57 intervals**. Policy mới `paired-rounds/2.1` chỉ áp dụng prospective: dependence trong block là diagnostic vì resample nguyên block; lag giữa median round mỗi nhánh hoặc delta quantile từng cặp ≥0,5 theo trị tuyệt đối vẫn HOLD. REJECT, p99, raw integrity, đủ mẫu, screening và RSS maximum gates giữ nguyên. Manifest cũ dùng policy 2.0; không trộn policy/bundle hoặc gắn nhãn mới cho sáu stage cũ.

Bằng chứng local trong lần sửa này:

- Regression trước sửa: **33 tests, 3 failures + 4 errors**, gồm root relocation, thiếu công cụ RSS, thiếu stage/family và policy chưa tách. Sau sửa: **37 Python tests passed** trong cùng `kaggle_experiments/test_protocol.py`; không tạo file test rời cho từng benchmark.
- `cargo test --locked --all-targets`: **41 unit + 2 integration passed**. Format check, Clippy `-D warnings` và build các binary hiện tại đều exit 0. Không sửa source core, Cargo.toml/Cargo.lock hoặc toolchain.
- Bảy stage của binary hiện tại chạy `--verify-only`: không có warmup/timer/raw samples; fingerprint và output size của export có/không query khớp pipeline tương ứng, hai chế độ query tạo fingerprint khác nhau; flag không hợp lệ bị từ chối.
- Ba script sinh ra khớp helper/source/spec/bundle, import không khởi chạy cluster. Screen và confirm có identity khác nhau; đóng gói/sinh lại xác định cùng bytes và đã trả artifact về confirmation. C1 cũ vẫn đọc được theo policy 2.0: **6 cases, 60.000 rows**.

**NOT VERIFIED:** build baseline trong target riêng trên Windows dừng với exit 101, `os error 112` (ổ C hết dung lượng), trước khi hoàn tất; không báo PASS. Log giữ tại `C:\Users\ADMIN\AppData\Local\Temp\athena-v21-functional-kcfhwtw4\build.log`. Automatic approval review từ chối xóa target tạm với lý do `blocked by policy`; target vẫn còn. Chưa upload/khởi chạy Kaggle 2.1, chưa xác nhận bootstrap GNU time trên image Kaggle hoặc release/tests/throughput của tuple RNG mới bằng lượt này. Người dùng chạy screening rồi confirmation theo [runbook 2.1](kaggle_experiments/README.md); chưa đủ để chốt nâng core/dependency.

## Mở rộng nghiên cứu core và thư viện — 18/10/2026

Phạm vi mới nằm tại [kế hoạch mục 18](CORE_RESEARCH_PLAN.md#18--mở-rộng-nghiên-cứu-thư-viện-và-đối-chứng-bất-lợi-17102026).
Registry có 103 cấu hình, tám panel native và 110 cohort mỗi split; đây là
inventory có comparator code, không phải 103 kết quả benchmark. Năm library
factors gồm BLAKE3 1.8.7 pure/prefer_intrinsics, Scraper 0.27.0 lean/atomic và
Sonic 0.5.10 framed typed output. Ba candidate lock profiles giữ nguyên version
dependency sẵn có; graph thực phải khớp đúng feature và checksum đã review.
Dossier tách đúng 11 direct edges, bao gồm rand 0.9.5 khỏi transitive 0.10.3.
17 package bổ sung trong lock Sonic có registry/source/license/MSRV/advisory
records; không gọi lock inventory là toàn bộ package thực sự được bật hoặc gọi
advisory query không có finding là chứng nhận an toàn.

Thêm 31 causal cohorts, literal content/link/denial oracles, corpus intake có
rights/provenance/byte/path/split bounds và exact retained-tail bootstrap
intervals. Interval backend giữ cùng RNG, paired draws và endpoint ranks, giới
hạn 1024 values/tail/quantile; full-distribution reference vẫn được giữ.
Chưa có corpus đại diện được cung cấp và chưa có memory/RSS win của Rust core.

Hai lỗi mới được giữ cùng bằng chứng thất bại trước sửa. Source admission trước
đây đọc toàn tệp quá cap trước khi từ chối; build-factor scope trước đây bỏ sót
stage của owner khác. Việc ghép panel còn có khoảng trống riêng: đủ cell IDs
nhưng không đủ cohort chung. `screen_plan` nay khóa union cohort của local
factors cho mọi corner trước timing và giữ phạm vi toàn stages của build factor.
Report yêu cầu đủ cohort đã khóa, không chọn intersection sau khi xem kết quả.

Lượt Linux 009 thất bại ở independent oracle `resolved-link-identity`: fixture
mới đã giả định sai việc bỏ URL fragments. Theo contract link URL đầy đủ,
`/a#one` và `/a#two` phải giữ riêng; lặp lại `/a#one` dùng lại ID đầu. Đã sửa
literal oracle theo [url 2.5.8](https://docs.rs/url/2.5.8/url/struct.Url.html#method.set_fragment),
giữ nguyên failure của 009 và kiểm tra oracle chấp nhận frame đúng, bác bỏ việc
mất fragment. 009 có 194 subprocess exit 0 nhưng tổng thể vẫn FAILED/exit 1;
không có timing. Điều này cũng cho thấy subprocess xanh không thay thế semantic
assertions. Export 009 đã đối chiếu size, SHA-256 và từng file inventory.

Local Python 3.11.9 sau sửa: 82 tests, exit 0, một Windows symlink skip; ba
source-composition controls exit 0. Study Linux 010 khóa 57 files và archive
SHA-256 `88424a78e0900945373b248f612b3b9fef93d4e14ca80e20ce0a89f2f3937574`,
study ID `c417506acd7e38fcbd11dc60b70454e02787b1fa27828d9e0cf1299a18e3d31c`.
Trên Linux/Python 3.13.5, cùng suite có 82 tests trong 13.748 giây, exit 0,
không skip. Lượt 010 đã hoàn tất lúc 03:33 ngày 18/10 giờ Việt Nam, sau khoảng
59 phút: **18/18 cấu hình** qua 49 Rust tests/cấu hình, format, Clippy và release
build. Mỗi cấu hình qua 110 canonical parity cohorts, cùng literal/native
controls độc lập; 17/17 controlled-service scenarios đạt. Supervisor exit 0.

Đã tải và kiểm toàn bộ **421.200 timing rows**, 18.599 captured subprocesses
exit 0; không thiếu/trùng cell hay iteration, đúng study/arm/order/offset và
output fingerprint. Quantiles và ba panel interaction được tính lại từ raw,
khớp artifacts. Các panel có 360/180/1071 contrasts tương ứng
I-HASH-BATCH4096 / I-JSON-CPU / I-PARSER-LEAN. Đây là 6 blocks × 30 samples/arm,
180 samples mỗi nhánh/cohort; không phải 421.200 quan sát độc lập. Không có
protected confirmation CIs hoặc RSS adoption gate.

Export gồm 14 files và inventory, 7.485.377 bytes nén, SHA-256
`bb2c3139f575502bb1884a7f1aa44d3af6f7a9c6c04bbcd16bbfd2a55ae641d2`.
Source study vẫn khớp 57 frozen files. Bảng sau là **point estimates thăm dò**
trên đúng fixture `large`; số âm là giảm thời gian p50, không là speedup toàn
sản phẩm hoặc confirmation verdict:

| Comparator | Pipeline không query | Pipeline query 3 terms |
| --- | ---: | ---: |
| `lib-json-sonic` | −6,34% | −4,23% |
| `projection-fused` | −2,17% | −12,60% |
| `lib-blake3-intrinsics+lib-scraper-lean+lib-json-sonic` | +11,52% | −13,50% |

Sonic riêng giảm p50 `serialization-large` 20,65% và `export-large-plain`
15,39%, nhưng vẫn có pipeline cohort chậm hơn; không nhận là cải thiện mọi
input. Tổ hợp ba thư viện cho thấy không thể cộng các speedup riêng hoặc coi
thêm factor là mặc định tốt hơn. `links-btree` có p50 `parse-unique_links` tăng
10,58%; batched hash 4096 có p50 `hash-large` tăng 66,89%. Đây là tín hiệu để
sàng lọc, không tự biến thành phán quyết thống kê hoặc thay đổi production.

Đối chứng A/A dùng cùng binary vẫn lệch p99 `hash-large` khoảng −29,05% giữa
hai nhánh. Tail evidence hiện không đủ để chốt; không loại outlier hoặc đổi
policy sau khi nhìn kết quả. Hướng có cơ sở để xác nhận tiếp là Sonic framed
output và projection fusion, kèm baseline và tổ hợp tương thích được build/
oracle trước đo. Mọi quyết định adoption vẫn cần protected holdout, CI/RSS và
workload/corpus phù hợp; không suy tính tối ưu toàn core từ vòng này.

Supervisor giới hạn 90 phút, memory 12 GiB, 128 tasks và 512 MiB/file; evidence
stream tối đa 4 GiB. Đây không là total disk hoặc billing quota. VM tự dừng sau
lượt chạy, đã mở lại chỉ để xuất dữ liệu và đã xác nhận TERMINATED sau tải;
không khởi chạy lại benchmark khi kiểm kết quả. Verdict vẫn HOLD. W3, successful real-model quality/
usage/cost, public Engine integration, transport/deployment SLO và independent
security review chưa được vòng này xác nhận. Byte integrity được kiểm; execution
authenticity vẫn NOT VERIFIED vì runner/evidence cùng OS authority.

## Kiểm soát nghiên cứu dài và lựa chọn cơ chế — 18/10/2026

Mục 19.3/19.6 của CORE_RESEARCH_PLAN.md bổ sung primary sources và đánh giá
khuyết điểm: query-plan rewrites cần semantic preconditions; TinyLFU/S3-FIFO cần
trace và byte/cost ownership; DRF không tự bảo đảm deadline; contextual routing
không được suy từ selected-action outcomes thiếu support; compact/programmable
tool paths cần full-task quality/cost. Đây là shortlist nghiên cứu, không có
library hoặc architecture winner mới được benchmark.

Inventory hiện tại bổ sung I-JSON-PROJECT: baseline/fusion/Sonic/fusion + Sonic,
đưa registry lên 104 cấu hình và chín panel native dùng chung cells. Suite đã
bắt đúng thiếu bốn corners trước sửa, sau sửa xác nhận registry, decision map
và common-cohort assignments cả parse/serialization/pipeline, giữ full scope
của build factor. Đây là tính đầy đủ của thiết kế, chưa là kết quả Rust build
hoặc timing của tuple ghép; số 103 trong báo cáo lịch sử 010 vẫn giữ nguyên.

Hai lỗi lifecycle đã được bắt trước sửa bằng đúng assertions trên filesystem/
tiến trình thật: JSON commit tiêu thụ staging file tên cố định của writer khác;
và process bị giết sau case đầu tiên không có completed-case checkpoint.
Cloud writer nay dùng unique staging + file fsync + atomic replace/cleanup;
native entry dùng ResearchRun để lưu manifest theo cùng cơ chế. Analyzer giữ
completed prefix dưới OS lock, ràng buộc raw/freeze/family/seed/source/runtime,
từ chối stale/corrupt state và chỉ xuất final report khi đủ các case.

Kiểm tra recovery giết một Python child tại case thứ hai, xác nhận concurrent
analyzer bị từ chối, rồi chạy CLI bằng process mới. Case đã xong không bị tính
lại; kết quả số bằng uninterrupted analyzer và literal intervals, adverse case
vẫn REJECT, overall vẫn HOLD. Đây là synthetic analyzer control; native timing
và executable identities trong fixture không đại diện một Rust benchmark thật.

Loader đã bỏ việc đọc nguyên shard lần thứ hai để hash. Controlled isolated
mutation khôi phục full-shard read có Python-tracked peak 31.288.165 bytes và
vi phạm ngưỡng 8 MiB; implementation hiện tại vượt qua cùng fixture khoảng
30 MiB, đúng streamed digest và paired values. Đây không là process RSS hoặc
Rust-core memory win. Không thay bootstrap/Bonferroni/drift/RSS decision policy.

Windows/Python 3.11.9: owning suites cloud controls, protocol, mathematical
models và service-lab oracles chọn 85 tests trong 34.478 giây, exit 0: 84 đạt,
một symlink case bị skip do quyền Windows. Các scratch directories đã cleanup.
Không rerun Rust suites khi production Rust/Cargo/toolchain không đổi.
Linux/Python 3.13 native entry và POSIX lock branch chưa được lượt này xác nhận;
local WSL không khởi động được do hypervisor/virtualization chưa khả dụng, không
phải product regression. Không cài hệ điều hành hoặc thay cấu hình máy để lấy PASS.

Đã đọc lại trạng thái VM Athena: TERMINATED, configured maximum lifetime 21600
giây và action STOP. Chưa start VM hoặc chạy cloud benchmark mới. Target gate,
corpus/task gold đại diện, actual remaining credit/admission và protected
confirmation vẫn cần hoàn thành trước chiến dịch lớn; không lấy các controls
này làm phán quyết nâng cấp core hoặc bằng chứng saturation.

## Giới hạn đầu vào của CLI offline — 24/10/2026

CLI demonstrator giờ đọc qua `Read::take` với đúng `ParseLimits::default().max_html_bytes` cộng một byte probe; file vượt 4 MiB bị từ chối trước khi parser chạy. Trước sửa, fixture HTML 4 MiB + 1 byte đi vào parser rồi panic với `invalid resource budget` (exit 101); sau sửa, CLI trả thông báo giới hạn với exit 2. Một reader tổng hợp xác nhận helper tiêu thụ tối đa `limit + 1` bytes và giữ nguyên input hợp lệ đúng giới hạn.

Rust 1.99.1, offline/cache reuse: `cargo +1.99.1 test --locked --offline --bin athena-core-v2 --test integration` đạt 2 unit + 3 integration; Clippy cho binary/integration, fmt check và diff check đều exit 0. Test file 4 MiB + 1 byte được tạo trong `%TEMP%` và RAII cleanup; không còn fixture CLI sau test. Đây chỉ ràng buộc byte input của CLI local; memory nội bộ parser/TreeSink và hard RSS của process vẫn NOT VERIFIED, không suy rộng thành hard cap cho production service.

## Ghim dependency của GitHub Actions — 24/10/2026

Workflow CI chuyển các action ngoài sang commit SHA đầy đủ từ upstream: [checkout v4.4.0](https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262), [setup-python v5.6.0](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065), [upload-artifact v4.6.2](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02) và [commit hiện tại của dtolnay/rust-toolchain](https://github.com/dtolnay/rust-toolchain/commit/683b588c487a69d01f6c6ddf40f036ff682cc2c8). GitHub API resolve các tag/branch trên vào đúng SHA ngày 24/10/2026. NumPy trong CI được cố định tại 1.26.4, phiên bản được ghi trong lần tái phân tích mẫu hiện hành.

Local YAML parse thành công; cả sáu tham chiếu `uses:` đều là SHA 40 ký tự. Diff check đạt. GitHub Actions chưa được dispatch sau thay đổi, nên image Linux, cài dependency và remote gates vẫn NOT VERIFIED.


