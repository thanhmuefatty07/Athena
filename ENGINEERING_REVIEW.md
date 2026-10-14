# Athena — báo cáo nâng cấp và kiểm chứng lõi

Đợt thực hiện: 14–15/10/2026, giờ Asia/Saigon. Snapshot ban đầu: `8de5709e6c47fd57713ca821131579b2e8c25a36`. Đã rà soát toàn bộ mã/config/dependency đang có và triển khai các thay đổi tại nơi sở hữu invariant. Đây là nâng cấp của reference core hiện tại; các dịch vụ tương lai trong blueprint vẫn là kế hoạch.

**Theo chỉ dẫn mới nhất:** các sửa đổi mã này là draft chưa chốt áp dụng. Vòng hiện tại chỉ lập kế hoạch nghiên cứu trong CORE_RESEARCH_PLAN.md; agent khác kiểm chứng Kaggle trước quyết định triển khai. Không chạy thêm build/test hoặc viết thêm mã lõi trong vòng lập kế hoạch.

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
| aae.rs | JSON ghi qua sink giới hạn byte, kể cả escaping/newline/end; lỗi flush không trả envelope dở dang. Content hash incremental giữ byte stream join-by-newline; buffer hash/JSON mỗi loại 8 KiB, không tạo joined page String. |
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


