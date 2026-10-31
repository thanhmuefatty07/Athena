# Nghiên cứu core trên Google Cloud

Runner mở rộng hiện tại là `research.py`, protocol **`athena-core-research/2`**.
Đọc [hướng dẫn protocol 2](#protocol-2-bộ-nghiên-cứu-mở-rộng) trước khi chạy mới.
Các phần native/1 dưới đây mô tả study cũ, được giữ để tái lập evidence cũ;
không dùng thông số hoặc verdict của native/1 để đại diện cho protocol 2.

Phần bổ sung **CPU-first / phối hợp ngôn ngữ** nằm ở
[kế hoạch mục 19.15](../CORE_RESEARCH_PLAN.md#1915-nghiên-cứu-phối-hợp-ngôn-ngữ-cho-core-trên-cloud--19102026):
L01 parser C/Rust, L02 browser controller, L03 Rust/TypeScript MCP/SDK;
L04 typed-batch handoff, L05 model inference CPU và L06 service runtime chỉ
admit khi có consumer/profile/gold phù hợp. L01 hiện có adapter nghiên cứu
riêng và suite sáu ca. Thay đổi dual-parse để giữ nguyên cảnh báo Rust đã pass
6/6 trên Windows x64 bằng Lexbor/C shim build sẵn đã pin; chưa có clean native
rebuild. Suite kiểm tra đúng count và AAE bytes. L01 chưa phải executor trong
native/2 và chưa xác minh Linux. L03 hiện có fixture MCP Rust/TypeScript riêng,
common driver và local synthetic preflight; vẫn chưa đóng language panel native/2,
Linux, IAM/replay hoặc cost. L02 và L04–L06 chưa có executor; `lib-scraper-modern`
chỉ là parser Rust candidate, không phải so sánh C/Rust của L01. Native/2 hiện sinh 225
variants, gồm panel H04 kiểm tra membership thuộc tính HTML; giữ 12 whole-path
factorial panels, panel H06 stage-only và mixed-strength coverage trên 13 factors
(kế hoạch 19.19). GPU/training/index vẫn
deferred. Sáu interaction thiết kế `I-LANG-*` trong decision map chưa là cells
được native runner thực thi. Giữ đầy đủ H01–H14 và các capability còn thiếu.
Không dùng `MAPPED` làm readiness; phải triển khai adapter/oracle/collector,
Linux gates và pilot recovery/cost trước campaign mới. Lượt mới nhất chỉ chạy
bounded Linux preflight trên source đã khóa: 113 tests không skip, 3 composer
checks, 43 Rust baseline tests và format/Clippy/release/shell gates; một lần
đọc catalog NIM HTTP 200, không benchmark hay inference. Ghi chú VM đã dừng là
trạng thái tại thời điểm lượt chạy đó, không phải inventory hiện tại. GCE
preflight ngày 2026-10-24 đã chuẩn bị một VM nghiên cứu Debian 13/Python 3.13.5,
E2-standard-8 (8 vCPU/32 GiB) với boot `pd-standard` 200 GB; guest báo
119,167,983,616 byte trống trên root filesystem (~111.0 GiB), vượt gate 48 GiB.
Bootstrap đã hoàn tất trên VM mới: Python 3.13.5, NumPy 2.2.4 và Rust 1.99.1
được xác nhận; marker bootstrap tồn tại. Bản source đã giải nén khớp 78/78 hash
trong `spec.json`. Một transient systemd unit với `MemoryMax=2 GiB` đọc lại đúng
`memory.max=2147483648`, xác nhận guest hỗ trợ giới hạn cgroup; unit supervisor
của campaign chưa được cài. Không có `active-job.json`, benchmark hay inference
nào chạy. Sau preflight, VM mới cũng đã `TERMINATED` để tránh phí compute khi chưa
chạy nghiên cứu; cả ba boot disks vẫn được giữ. SSH/RDP chỉ cho phép từ
dải IAP 35.235.240.0/20; ICMP chỉ từ mạng nội bộ 10.128.0.0/9; SSH qua IAP đã
kết nối thành công. Runner service account có quyền Secret Accessor ở đúng
secret NIM; không đọc giá trị secret và chưa xác minh model/provider.

Giá on-demand niêm yết chính thức tại Singapore cho E2-standard-8 là $0.26804568
mỗi giờ ([Google Cloud VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose));
120 ngày liên tục tương đương $771.97 tiền compute, chưa gồm disk/network. Đây là
giá niêm yết, không phải hóa đơn thực tế. GCP Billing Budgets API từ chối quyền
đọc; credit, chi tiêu hiện tại, ưu đãi và hard billing enforcement vẫn NOT
VERIFIED. Không có benchmark, provider inference hay campaign nào được khởi chạy.
Các VM, firewall và IAM đã được thay đổi để chuẩn bị hạ tầng; Linux gates, thiết
lập supervisor campaign và launch receipt riêng vẫn cần hoàn tất trước khi bắt đầu.
Dự trù
48–96 giờ cũ vẫn chưa được đo lại trên source hiện tại.

Lexbor v3.0.0 has a separate C canary and Rust FFI adapter under
`cloud_experiments/lexbor/` and `cloud_experiments/lexbor_adapter/`. The adapter
copies the completed parser tree through a small C ABI, then uses Athena's
feature-gated projection builder and table grid. Its six-case HTML5 repair test
now requires exact Rust `parse_warnings` values and byte-identical AAE output.
The research-only output path first obtains the count from Athena's bounded
html5ever parser, drops that parsed tree, and then parses with Lexbor. Lexbor's
native diagnostic arrays remain separate: table foster parenting reports 2
there while Rust reports 5; duplicate attributes report 1 there while Rust
reports 3. This preserves the serialized Rust contract by doing a second parse,
not by mapping or masking Lexbor diagnostics. The second parse must be included
in every complete-path timing and memory comparison; no performance benefit is
implied.
The shim currently sums Lexbor's separate tree-builder and tokenizer error
arrays. Tree errors carry an error ID and token byte range; the pinned
html5ever path sends tokenizer/tree-builder diagnostics through the same Rust
sink counter. An opt-in diagnostic trace now records at most 256 raw events
(source, error ID, description and input-byte range) and sets `truncated` when
the cap is crossed; ordinary `parse_tree` calls do not request or copy this
trace. A boundary test feeds 300 stray end tags and verifies storage stops at
256 while aggregate parser counts match the untraced parse. In the table case,
Lexbor records only ID 4 (`unexpected token in initial mode`, bytes 1–6) and
ID 29 (`characters in table text`, bytes 12–15), while Rust reports five
warnings. In the duplicate-attribute case, Lexbor records only ID 4 at bytes
1–2, while Rust reports three. The pinned Lexbor document setup enables
`keep_duplicate`, so its tokenizer's duplicate-error path is skipped in this
mode; its raw arrays omit the two duplicate-attribute warnings in this fixture.
The table trace also shows different diagnostic granularity, so raw Lexbor
array lengths cannot be translated directly into Rust counts. The current
candidate preserves the Rust count with a second bounded parse. Any lower-cost
reconstruction remains unimplemented and would need to generalize over an
independent corpus without fixture-specific constants.
This prototype is not a cell in the 225-variant matrix and is not a production
dependency. The Linux command
`bash cloud_experiments/lexbor/build_and_smoke.sh /absolute/fresh/scratch`
downloads a SHA-pinned source ZIP, builds static Lexbor, runs the C canary, then
builds and runs the Rust adapter suite. It needs Rust/Cargo 1.99.1, CMake, a C
compiler, `curl`, `unzip`, `sha256sum`, GNU `timeout`, and at least 8 GiB free in
a new scratch directory outside the checkout. It preserves that scratch and its
logs on success or failure. The strict dual-parser adapter suite now passes 6/6
on Windows x64 with the existing pinned Lexbor v3.0.0 library and C shim; the
test run compiled the changed Rust code and verified exact AAE output for its
six fixtures. Adapter Clippy with warnings denied also passes. This reuses the
prior native build; it is not a clean C rebuild. WSL2 cannot start and CMake/a
C compiler are absent, so a fresh native build and Linux/GCE, RSS, allocation
ceilings, concurrency and comparative performance remain `NOT VERIFIED`.
`max_parser_nodes` is checked while collecting the final
tree, after Lexbor has already parsed and allocated it; this is not a hard parser
memory cap. The public allocator hook remains process-wide.

Đây là runner nghiên cứu riêng, không thay protocol Kaggle 2.1 và không tự sửa
core production. `CORE_RESEARCH_PLAN.md` mục 9–13 vẫn là hợp đồng tổng thể.
Mục 15 là thiết kế mở rộng H01–H14/interaction/DAG/model thật; **runner native/1
trong thư mục này chưa triển khai thiết kế đó**. Không launch lại lệnh dưới rồi
gọi là đã nghiên cứu toàn core. Các kết quả native/1 giữ nguyên protocol/policy.
Quyền chạy cloud của người dùng thay yêu cầu chờ người dùng launch trong WP07/08;
không thay điều kiện xét duyệt kết quả hay cho phép push/merge.

## Đối chiếu danh mục quyết định

`core_decisions.json` map H01–H14 vào source/direct dependencies, alternatives,
oracles, closure và gaps; mục 16 của kế hoạch quy định khi nào evidence đủ dùng.
Registry `language_panels` theo dõi riêng L01–L06: L01 có prototype Windows
nhưng panel vẫn `NOT_IMPLEMENTED` vì chưa có executor native/2 và Linux
evidence; L02 chưa có executor. L03 có fixture MCP Rust/TypeScript riêng, nhưng panel vẫn
`NOT_IMPLEMENTED` ở full-panel scope vì chưa có executor native/2, Linux/cancel-load,
production identity/replay và cost evidence. L04–L06 chưa được admit có điều kiện
và kèm lý do/gate.
Đây là trạng thái chuẩn bị có máy kiểm tra, không phải executor hay kết quả đo.
Các alternative/gap có tiền tố `PLANNED Lxx` và interaction `I-LANG-*` vẫn là
backlog thiết kế 19.15; `implementation_status` và `harness` hiện hữu không xác
nhận adapter đã có code. Không thêm thư viện chưa tích hợp vào `dependencies`.
Chạy kiểm cấu trúc bằng command thực:

```text
python -B -m cloud_experiments.campaign audit-decisions
```

Thiếu source/dependency hoặc graph/owner sai: exit 2, `INCOMPLETE`. Exit 0 và
`MAPPED` chỉ xác nhận inventory/design và đủ sáu panel L đã được map; L01–L03
vẫn block readiness cho tới khi có record chạy được và được kiểm tra, L04–L06
là non-admission có điều kiện. Experiment execution và decision sufficiency **NOT_EVALUATED**. Không dùng
nó thay comparator/oracle/pilot hoặc
gọi là broad launch đã được duyệt. `prepare/run/analyze` của native/1 giữ semantics
cũ và chưa enforce broad-campaign readiness. Tests ở owning `test_campaign.py`.

## Câu hỏi và phạm vi

1. R7: cách ghi JSON (`frame_string`/`bounded_vec`), cấp phát (`estimate`/`zero`)
   và băm nội dung (`joined`/`incremental`) tạo tám cấu hình AAE. B1 là đúng core
   dirty hiện có, gồm cấu hình `111`; bảy cell còn lại được sinh trong scratch.
   Đối chứng `previous_aae` chỉ thay AAE bằng HEAD, giữ các source khác, lock,
   toolchain, benchmark và profile chung.
2. R4: `rank_binary` thay lookup query term từ HashMap bằng binary search trên
   cùng tập term đã sort; giữ tokenization, phép tính, thứ tự, novelty và API.
   Nếu shortlist đầu là một cell AAE, đo thêm tổ hợp cell đó × binary lookup.
   Không dùng việc ít dòng hoặc có tên thuật toán hay hơn làm bằng chứng thắng.
3. R1–R9: chạy lại các gates liên quan trên Linux và khóa graph phụ thuộc;
   giữ khoảng trống workflow/access/SDK/model/index trong coverage ledger. Chưa
   có comparator thì không thể kết luận thư viện/kiến trúc đó đã tốt nhất.

Screen dùng 8 shape: tiny/article/large/Unicode/JSON escapes/unique links/deep
DOM/repeated links; có query và không query, export/pipeline, thêm rank.
Đây là fixture tổng hợp chạy code thật, không phải phân phối web production.
Confirmation dùng seed khác đã khóa trước, 6 cohort quan trọng trên export
article, pipeline large và pipeline escaped, mỗi cohort có/không query.
Seed mới giảm việc chọn theo mẫu timing; không chứng minh tổng quát sang domain mới.
Ngoài các cohort đó, cả hai native split đều có dải workload adversarial
`attribute-heavy-{1024,2048,4096,8192}`: một start tag với số thuộc tính duy
nhất tăng theo log scale và cùng oracle đoạn văn literal. Mỗi fixture bị chặn
riêng ở 4 MiB HTML và 8 MiB JSON; tổng JSON của hai split bị chặn ở 16 MiB.
Đây không phải corpus web đại diện. Một lần chạy feasibility Windows với 8,192
thuộc tính, 250 mẫu và 10 warmup mất 49.9 giây trong một process; không có arm
đối chứng nên không phải kết quả so sánh hiệu năng. Ca 16,384/32,768 thuộc tính
ở cùng cấu hình và ca 252,602 thuộc tính chỉ một lượt parse đều chạm timeout
180 giây. Parser `html5ever` 0.39.0 hiện quét danh sách thuộc tính đã nhận để
loại tên trùng, khiến dải thuộc tính duy nhất có đường đi xấu nhất bậc hai
([source tokenizer đã pin](https://docs.rs/crate/html5ever/0.39.0/source/src/tokenizer/mod.rs)).
Peak RSS/tokenizer memory và hành vi Linux/GCE vẫn NOT VERIFIED.

## Oracle và dữ liệu

| Yêu cầu | Defect được phân biệt | Oracle và boundary | Giới hạn |
|---|---|---|---|
| AAE giữ hợp đồng | Sai framing/hash/escaping/links/order/cap | Existing literal content fixtures, hash probe, root cap tests, byte parity qua serializer thật | Hash probe dùng cùng BLAKE3 dependency; không là kiểm định mật mã độc lập |
| Rank giữ toán/thứ tự | Sai df/length/query dedup/tie/Unicode | Independent Python formula, hand-calculated Rust tests, byte parity rank outputs | Corpus hữu hạn; không đánh giá neural/semantic relevance |
| Table đúng | Sai rowspan=0/group/overlap/budget | Existing literal logical-grid probe và Rust/integration tests | Chưa có HTML→header grid integration |
| Evidence đúng cặp | Thiếu/trùng/sai arm/order/fingerprint/reuse screen | Validator được thách thức bằng dữ liệu sai; raw ns/offset/phase/block/variant | Process output được tin cậy; chống giả mạo OS NOT VERIFIED |
| Concurrency/recovery | Double execution, cancellation giải phóng quota quá sớm | Existing real Tokio tests cho SingleFlight/Engine/DNS worker | Không là distributed/load/TLS production test |
| Bound tài nguyên | Temporary output lớn dù cap nhỏ | API oversized-Projection negative cases; fresh-process RSS, external cgroup/deadline | Process peak gồm setup; không là allocation count hay hard per-request bound |

Không xóa failure hoặc đổi expected để lấy PASS. Một gate hỏng dừng wave, giữ
diagnostic nhỏ nhất và toàn bộ evidence đang cần. Không chạy untrusted SaC code,
không mang API key, Kaggle key, gcloud credential hay `.git` lên VM.

## Thiết kế đo và dừng

Screen: 6 paired blocks × 30 iterations/arm/case, arm AB/BA cân bằng, case order
shuffle theo seed. Các rival được randomize thứ tự bằng seed. Tất cả interval
screen là exploratory; số đo giữ theo thời gian. Không suy causal interaction CI
từ các cell chạy ở thời điểm khác nhau. Finalist tổ hợp cần confirmation full path.
Chọn tối đa hai arm qua geometric mean của tỷ lệ p50 trên **mọi** screen cohort;
chỉ arm đủ tất cả correctness/cases được xét. Đây là heuristic sàng lọc, không
là acceptance hay trade-off giữa latency/RSS/quality.

Confirmation: 20 blocks × 250 CPU samples/arm/case; mỗi block có thêm fresh
process RSS. p50/p95/p99 CPU và p50 RSS; không tính p99 từ 20 RSS. Family cố định
M=48 cho 2 finalist × 6 cases × 4 metrics, α=.05, paired whole-block bootstrap
192,000 draws, ít nhất 100 draws kỳ vọng/mỗi đuôi Bonferroni. Reuse statistical
functions 2.1, nhưng cloud rows đi qua validator cloud, không giả phiên bản 2.1.
Giữ gate drift |lag-1|≥.5, RSS maximum bất lợi → HOLD, CPU CI lower>0 → REJECT,
mọi protected CI upper≤0 và một upper<0 mới có PASS_PERFORMANCE theo phạm vi.
Kết quả cuối luôn cần review; không tự đưa vào production.

Dừng khi panel hữu hạn xong, gate hỏng, hoặc hết thời hạn. Một wave xong chỉ khóa
được quyết định của các rival đã đo. Nếu còn hotspot và rival có cơ sở, lập wave
kế tiếp với oracle/spec mới; không dùng ngân sách vô hạn hoặc tuyên bố tối ưu toàn cục.

## Chạy và phục hồi

Python chuẩn bị hỗ trợ 3.11+; môi trường confirmation là Debian 13/Python 3.13,
Rust 1.99.1 đúng pin. Các lệnh dưới là parser thực của runner:

```text
python -B -m unittest cloud_experiments.test_campaign cloud_experiments.test_lifecycle kaggle_experiments.test_protocol model.test_models cloud_experiments.service_lab cloud_experiments.mcp_sdk.test_driver -v
python -B -m cloud_experiments.campaign prepare results/cloud-native-input
python3 -B -m cloud_experiments.campaign run /opt/athena/input /opt/athena/spec.json /opt/athena/runs/native-001
```

`prepare` fail nếu destination đã tồn tại; không ghi đè bundle cũ. Source-only
archive có inventory/hash; source checkout và dữ liệu Kaggle cũ được giữ nguyên.
Mỗi variant dùng source work nhỏ và cache Cargo chung, chỉ giữ executable cần
đối chiếu. Cache chỉ reuse trong cùng compiler/lock/flags/profile của wave.

Với protocol 1/native-only setup, VM dùng project/mạng riêng, IAP SSH, không gắn
service account/scopes, root supervisor và user nghiên cứu không đặc quyền.
Quy tắc này không áp dụng cho protocol 2 W1 đọc NIM key từ Secret Manager: đường
đó cần service account riêng với quyền accessor giới hạn đúng secret. Bootstrap
cần mạng để tải Debian packages/Rust/crates từ nguồn chính thức. OSV query chỉ
gửi tên/version crate công khai. Mỗi batch tối đa 100 crate đi qua HTTP worker chỉ
cho phép đúng `POST https://api.osv.dev/v1/querybatch`, không proxy/redirect,
timeout tối đa 45 giây nhưng không vượt deadline còn lại của campaign, và phản hồi
tối đa 4 MiB. Status khác 200, timeout, phản hồi thiếu hoặc có pagination/error
đều giữ `NOT VERIFIED`; không tự cập nhật dependency.

Owner scratch là wave này: `/opt/athena/runs/`; expected cache/build ≤12 GiB,
evidence ≤250 MiB, source ≤16 MiB. Trước build phải còn ≥20 GiB. Systemd enforce
job ≤5h, MemoryMax=12G, TasksMax=256 và giới hạn mỗi file 32 GiB; disk VM 50 GB giới hạn
tổng filesystem nhưng không là quota chỉ cho job. VM tự STOP sau tối đa 6h từ
khi khởi động, hoàn tất job sẽ yêu cầu shutdown sau 5 phút. Disk vẫn tính phí
sau STOP và được giữ để đọc kết quả; không tự xóa evidence/cache/source.

Source/runner kiểm soát quá trình test nên `result_authenticity=NOT VERIFIED`.
Hash bảo vệ byte sau capture, không xác nhận assertion thật sự chạy. Cần đọc
command/exit/transcript/assertions và kết quả denied effects khi review.

Sau gián đoạn, đọc `job-outcome.json`, trạng thái systemd và manifest thật.
RUNNING/timeout/partial không là PASS. Không tự resume bằng append mẫu cũ hoặc
đổi spec; khởi tạo study/output riêng khi inputs thay đổi. Mốc quay lại được
ước lượng sau pilot và gồm thời gian build/setup/analysis; không dùng chỉ tổng
raw operation duration làm thời gian hoàn tất.

Nguồn API: [Google VM time limit](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[OSV querybatch](https://google.github.io/osv.dev/post-v1-querybatch/),
[serde_json upstream serializer](https://github.com/serde-rs/json/blob/master/src/ser.rs).
Code mới dùng các API dependency có trong source đã cài của lock; compiler và
runtime gates trên VM phải hoàn tất trước khi diễn giải benchmark.

## Protocol 2: bộ nghiên cứu mở rộng

`research.py` điều phối, `contenders.py` sinh các comparator trong scratch,
`core_probe.rs` đo code Rust thật. Python không thay thế core Rust. Không sửa
`src/`, Cargo.lock hay toolchain production khi chuẩn bị/chạy comparator.
Contract đầy đủ nằm ở `CORE_RESEARCH_PLAN.md` mục 15–18.

### Nội dung đã triển khai và giới hạn

- Sinh 225 variants, gồm baseline, H06 CSS-selector comparator, H04 HTML
  attribute-membership panel và parser Rust candidate `scraper 0.29.0` +
  `html5ever 0.40.1`.
  Gồm I-AAE 48 cell (writer × hash × reserve), I-RANK
  6 cell (lookup × selection), I-PROJECT 4 cell (fusion × link lookup), cùng
  prepared state, logical table và build factors. Thêm I-HASH-LIBRARY 12,
  I-PARSER-LIBRARY 16 (4 parser profile × projection × link lookup),
  I-JSON-LIBRARY 24, I-LIBRARY-SYSTEM 24 (3 BLAKE3 × 4 parser × 2 JSON), I-JSON-CPU 4 và
  I-JSON-PROJECT 4 cell; thêm I-QUERY-PROJECT 12, I-COMPILER 12 và
  I-JSON-HASH-CPU 16. Các panel dùng chung cell trùng nên không cộng số cell
  thành số binary. `experimental_design.py` còn chọn 96 cell có bao phủ đầy đủ
  871 tuple hợp lệ: mọi cặp trên 13 axis, tám nhóm ba yếu tố có cơ chế và một
  nhóm writer/hash/lookup/projection bốn yếu tố. Ba tuple Sonic/streaming không
  tương thích được ghi rõ. Cả 225 biến thể qua kiểm tra biến đổi manifest/lock
  tĩnh; việc này không tương đương build toàn matrix. 96 cell covering design
  giữ nguyên dưới giới hạn 256; coverage không chứng minh execution hoặc
  optimality. Không suy ra đã thử hết 331.776 điểm Cartesian trước constraints.
  H06 selector arm là stage-only trên cùng parser/grid và được nối vào build,
  oracle, fixture và cohort. Parser DOM-node cap nay được kiểm trước mỗi lần
  `HtmlTreeSink` cấp node; sau khi chạm ngưỡng, runner ngừng đưa các chunk HTML
  tiếp theo vào parser. Input được cấp theo chunk UTF-8 16 KiB để giới hạn phần
  parser nội bộ còn xử lý sau khi báo vượt ngưỡng. Tổng logical grid-slot budget
  được truyền vào `expand_with_slot_limit` và kiểm trước từng lần tăng hàng;
  peak RSS, sức chứa Vec, bộ nhớ attribute/tokenizer và tổng memory vẫn
  NOT VERIFIED. **Chưa có bằng chứng build/runtime/benchmark
  toàn matrix**.
  H04 thêm đúng 10 cell parser-only: hai cặp khóa Scraper/html5ever nhân năm
  chiến lược Vec/HashSet/lazy threshold. Trước khi đo, runner thu report WPT
  từng ca cho baseline và chín arms, rồi yêu cầu tree digest và warning count
  của mỗi indexed arm khớp vector cùng phiên bản parser. Baseline hiện có 105
  tree mismatches và 482 warning-count deltas trên 1.733 ca document; đây là
  quan sát baseline, không phải pass WPT tuyệt đối. Modern-vector được đối chiếu
  riêng với baseline hiện hành để ghi nhận thay đổi phiên bản. Thay đổi output
  do indexed arm làm arm bị loại trước clippy/release/timing. Report-only không
  thay thế test chẩn đoán nghiêm ngặt zero-mismatch và không khẳng định full
  WHATWG conformance. Source snapshot giữ nguyên từng byte của fixture/data
  (kể cả CRLF và bare-CR); chỉ source text được chuẩn hóa newline để ổn định
  hash giữa các hệ điều hành.
  `spec.native_design`, `screen-cross-coverage.json` và inventory ghi ranh giới
  này; covering array không cho phép suy ra interaction không confounded hoặc
  tuyên bố đã thực thi toàn Cartesian.
- 112 cohort mỗi split: pipeline/export, query 1/3/8/32/128 từ, hit density,
  novelty, parser/links/Unicode/HTML repair, prepared build/query/invalidation,
  logical grid và output-budget rejection 64 KiB/512 KiB/4 MiB. Seed holdout
  tạo byte mới; chưa có corpus web/domain holdout đại diện production.
- 33 cohort bổ sung có vai trò cụ thể: giới hạn byte/nodes/link đúng ngưỡng và
  vượt ngưỡng, hash 63/64/65 và 1023/1024/1025 byte, nội dung giả framing,
  Unicode không tự chuẩn hóa, subtree phải bỏ, nearest-owner, query bất lợi
  và link scaling. Literal oracle kiểm tra role/text/link hoặc đúng loại lỗi;
  baseline parity không thay thế oracle này.
- `libraries.py` khóa đúng manifest/lock và kiểm tra feature thực sự được
  Cargo bật. `library_decisions.json` giữ consumer/gate của 12 dependency
  trực tiếp; `library_candidates.json` ghi provenance 17 package record mới
  trong lock Sonic. Graph Linux của Sonic bật thêm 19 crate nodes so với
  baseline; `bumpalo 3.20.3` và `simdutf8 0.1.5` đã có trong lock baseline nhưng
  trước đó chưa active trong graph đã lọc target. Truy vấn advisory không chứng
  minh không có lỗ hổng.
- Năm lock trong `locks/` là đầu vào comparator: Sonic 0.5.10, Scraper lean,
  Sonic + lean, parser modern, và Sonic + parser modern. Lean giữ `errors`, chỉ
  bỏ CLI và hai package `getopts` / `unicode-width`. Parser modern là cặp khóa
  riêng, thay các phiên bản trong parser graph. `cargo-audit 0.22.2` không báo
  advisory áp dụng hoặc warning trong root, năm profile này và lock Lexbor theo
  RustSec snapshot `21d909e4c8bd4e14c7aea58bf980c881a0053ab1` (1.352
  advisories; 2026-10-24). Kết quả chỉ áp dụng cho bảy lockfile đã quét, không
  chứng minh an toàn source-level hay tương thích production. Kiểm tra archive
  local xác nhận 19 crate-node của Sonic khớp checksum Cargo.lock; 17 archive
  và manifest khớp provenance đã ghi, cả 19 có manifest license và tệp license.
  Bảy crate không khai báo `rust-version`; nội dung notice bên thứ ba và nghĩa
  vụ phân phối chưa được rà soát. MSRV/license đầy đủ, Linux build/performance/
  parity còn mở; candidate chưa được phép vào dependency production. Sonic chỉ
  hỗ trợ writer `frame`; ghép với direct/buffered/reuse bị từ chối rõ ràng.
- Mỗi lock ứng viên phải dùng được cho cả TLS AWS-LC và Ring vì hai profile
  dùng chung lock. Kiểm tra trước build chạy `cargo metadata --locked` với
  target Linux và đúng hai bộ feature của harness; test thư viện kiểm tra lock
  có cả hai cạnh provider. Đây chỉ là bằng chứng resolve/feature graph, không
  thay cho build, test hay benchmark Linux.
- H07 dùng process con riêng cho từng ô; source path xác nhận mỗi probe khởi
  tạo provider trước khi dựng Reqwest client. Rustls 0.23.46 giữ `CryptoProvider`
  mặc định ở cấp process; ứng dụng nhúng có thể cài provider trước Athena và
  làm khác backend trong cùng một binary. Graph profile cô lập chọn riêng
  AWS-LC hoặc Ring, nhưng không chứng minh cấu hình của host harness; tích hợp
  nhúng vẫn `NOT VERIFIED` cho tới khi host chốt quyền sở hữu provider và
  feature graph.
- ID tổ hợp được chuẩn hóa theo registry; các panel không build/đo lại cùng
  bộ factor chỉ vì thứ tự tên khác. `screen_plan` khóa cohort chung cho từng
  panel trước đo, bổ sung ca còn thiếu cho mọi corner và giữ phạm vi toàn bộ
  stage của build factors. Report kiểm đủ cohort đã khai báo, không tự lấy
  intersection sau khi thấy kết quả. Bootstrap giữ nguyên mẫu/estimand/CI;
  interval path chỉ giữ exact tail order statistics thay vì toàn bộ draws.
- Gates gồm test hiện có, independent native controls, canonical parity,
  Clippy và build release thật. Một tổ hợp nhiều yếu tố chỉ là comparator;
  source của baseline và comparator đều được băm sau formatting, kể cả probe
  được inject. Không suy đúng chỉ từ file/binary tồn tại hay exit 0.
- Mọi cell screening được interleave theo cùng macroblock/case; AB/BA cân
  bằng, CPU pin và warmup giữ rõ. Báo cáo tương tác dùng log quantile theo
  block, có cả three-way term khi đủ cell. Các contrast này chỉ exploratory,
  không phải CI xác nhận tương tác. Thiếu cell giữ `INCOMPLETE`.
- Shortlist theo owner và cohort bất lợi nhất, không dùng trung bình toàn cục
  che tail regression. Quy tắc ghép union các factor tương thích được khai báo
  trước: comparator ghép phải qua oracle và controlled service gate, rồi có
  confirmation riêng. Không cộng các speedup. Factor xung đột làm dừng việc
  ghép union; từng finalist vẫn phải qua service gate riêng. Prepared-only và
  logical-grid-only giữ boundary riêng, không nhận là service capability.
- Confirmation khóa selected tuple/binary/input/assignment/family trước đo;
  24 blocks × 250 iterations/arm/cohort, RSS bằng process mới mỗi block. Phân
  tích đọc raw shards thực, kiểm đủ cặp/count/order/fingerprint/RSS, đúng freeze,
  source và binary. Không nhận JSON nhóm sẵn tùy ý hay đổi alpha sau screening.
  Bonferroni family bao gồm cả contender ghép; bootstrap đủ Monte Carlo tail
  resolution là điều kiện tính toán, không bảo đảm độ chính xác thống kê.
- Bootstrap giữ cùng estimand/seed với reference, chia batch bounded memory và
  tìm weighted order statistic bằng prefix counts/binary search ở shape lớn.
  Có đối chiếu exact với reference độc lập, gồm ties và endpoint quantiles.
  Resource pilot đo shape phân tích thật; các số được lặp để profile tài nguyên
  **không** được đưa vào scientific observations hay tính sample size độc lập.

`service_lab.py` + `service_probe.rs` có 17 scenario kiểm tính đúng qua TLS origin
thật, reqwest đã khóa và actual core parse/AAE/SingleFlight: pool partition,
stored-body/304/update/revocation, query-specific representation, admission,
deadline/drain, cancellation và TLS denial. `load` dùng offered arrivals độc lập;
`journey` dùng bước phụ thuộc. `cancel_leader`/`cancel_follower` poll actual futures
đến trạng thái pending để xác nhận join trước hủy; không dựa vào sleep 2 ms/20 ms.
Lệnh CLI chạy scenario cũng yêu cầu cùng lớp cô lập Linux; nó fail closed nếu
không tạo được namespace.
Control ghi số source leaders và đúng vai trò/denominator/recovery; tắt source
flight phải bị oracle bác bỏ. TLS chưa chắc hoàn tất tại thời điểm hủy.
Không dùng các control này như benchmark service p99 hoặc production Engine
permitted-public-DNS/TLS integration. Long soak/topology/HTTP2/3 vẫn chưa kiểm.
Các lệnh OpenSSL cục bộ dùng để nhận diện phiên bản và tạo TLS fixture giới hạn
stdout+stderr gộp ở 64 KiB mỗi lệnh; thời hạn lần lượt là 5 giây và 10 giây.

`agent_lab.py` và `agent_tasks.json` chứa F01–F08, W0 rules/W2 batching,
actual native AAE producer (gồm `task-search`, `task-export`, `task-expand` qua `core_probe` và MCP SDK tools `athena_search_task`, `athena_extract_document`, `athena_expand_handle`), full/compact+expand, scope/revision/expiry của handle
và oracle facts/units/citations độc lập. W1 chỉ gọi model sau khi chọn rõ model,
đặt request/token/byte/time cap và truyền cờ `--confirm-provider-no-charge`.
Cờ này xác nhận operator đã kiểm tra riêng giá của đúng model và tài khoản; runner
không kiểm chứng được giá NIM. NIM catalog chỉ xác nhận model được liệt kê, nên
báo cáo luôn ghi `pricing=NOT_VERIFIED_BY_PROVIDER` và cost UNKNOWN nếu provider
không trả số liệu. OpenRouter kiểm tra giá qua catalog riêng. 429/503 dừng, không
retry hoặc chuyển model trả phí. Provider quota và usage/cost không được trả về
phải giữ UNKNOWN, không ghi thành 0.
Ở client boundary, mọi transport được tiêm vào, kể cả callback bọc qua hàm khác,
đều được coi là có thể gọi mạng thật và phải qua cả cờ live lẫn xác nhận phí.
W1 kiểm tra lại hai cờ này trên mỗi lần chạy, kể cả khi nhận client dựng sẵn.
Mọi transport được tiêm vào hoặc bị thay thế trước lần dispatch mang nhãn
`INJECTED_*_HTTP` và thêm boundary gap; chúng không được tính là `actual_model`.
`actual_model` chỉ được bật sau HTTP 200 và usage hợp lệ qua transport mặc định
đă được nhận dạng. Nhãn này ghi nhận quan sát trong tiến trình, không chứng minh
độc lập danh tính model/provider hay tính xác thực của kết quả. Các test stub
chỉ là test doubles trong process, không chứng minh kết quả provider thật.
F04 yêu cầu HTML table header/group integration (`table_cells` trích xuất qua `core_probe` đáp ứng đủ `F04-screen` và `F04-confirm` cho `RustProbeProducer` và `SdkTaskProducer`; synthetic Python producer mặc định không tạo `table_cells` nên chỉ đúng vài fact chưa đủ task PASS).
**W3/general generated-code sandbox chưa triển khai**; không chạy code model.
Live model quality, MCP transport và deployment cost/SLO chưa được xác nhận.

Trước khi biên dịch contender, runner đối chiếu byte chính xác của `Cargo.toml`,
`Cargo.lock` và lock profile đã duyệt. Chỉ sau đó lệnh fetch chính xác theo
target x86_64 Linux mới tải crate vào Cargo home riêng không chứa
credential/config; `test`, `clippy` và `build` chạy trong user/network namespace với negative control cho địa chỉ
metadata GCE và egress công cộng, đồng thời dùng Cargo offline. Nếu namespace
không tách biệt hoặc một kết nối kiểm soát được, runner dừng trước lệnh build.
Service unit để home Rust read-only. Đây mới là control ở mức tiến trình; việc
namespace hoạt động trên VM mục tiêu vẫn **NOT VERIFIED** cho tới khi cloud
preflight thực chạy, và nguồn bằng chứng vẫn chia sẻ OS authority.

Từ revision nghiên cứu 25/10/2026, mọi tiến trình con vẫn chạy trong user,
mount, PID và network namespace riêng bằng util-linux unshare; chỉ loopback
được bật. Preflight kiểm tra namespace identity cùng metadata/public TCP
negative controls. Cargo fetch giờ dùng đúng `fetch --offline --locked
--target x86_64-unknown-linux-gnu`, cùng network-denied namespace; `cargo
metadata` cũng nhận `--offline` qua adapter riêng của cloud campaign, giữ nguyên
runner và bundle Kaggle đã đóng băng. Không còn ngoại lệ Cargo được phép ra mạng.

Trước baseline build hoặc phép đo, runner kiểm tra offline mọi lock profile
được lên lịch (cùng profile `lib-rustls-ring` bắt buộc của full campaign).
Cache thiếu hoặc sai lock làm preflight dừng, không fallback sang tải mạng.
Cache nguồn phải được chuẩn bị riêng trước khi chạy tại
`<output-parent>/cargo-target/cargo-home`; cache này không nằm trong source
bundle. Config/credentials của Cargo trong workspace và Cargo home vẫn bị từ
chối. Rust target ngoài x86_64 Linux cũng bị từ chối. Đây là bằng chứng source
và test cục bộ; user namespace, egress và cache thật trên GCE vẫn
**NOT VERIFIED** cho tới preflight Linux mục tiêu.

H07 dựng origin TLS loopback và probe trong cùng một worker được cô lập để
chặn metadata/Internet nhưng vẫn giữ test network cục bộ. Nếu không tạo được
namespace, worker phải trả về lỗi chưa thực thi và xác nhận thư mục tạm đã
được dọn. Kiểm tra Linux trên GCE, khả năng user namespace của image và kiểm
tra descendant thực tế vẫn **NOT VERIFIED**; bằng chứng cùng máy không thay
thế collector độc lập. Chi tiết và ranh giới còn lại nằm ở CORE_RESEARCH_PLAN
§19.117. Chiến dịch vẫn HOLD, chưa chạy Cloud/Kaggle.

### Bootstrap Rust

`bootstrap.sh` gọi `rustup_bootstrap.py`, tải `rustup-init` 1.29.1 theo kiến
trúc x86_64 hoặc aarch64, rồi so SHA-256 với giá trị đã ghim trong source trước
khi chạy installer dưới tài khoản `athena-research`. Hash lấy từ tệp checksum
chính thức của Rust cho [x86_64](https://static.rust-lang.org/rustup/archive/1.29.1/x86_64-unknown-linux-gnu/rustup-init.sha256)
và [aarch64](https://static.rust-lang.org/rustup/archive/1.29.1/aarch64-unknown-linux-gnu/rustup-init.sha256).
Giới hạn tải là 64 MiB; checksum sai, redirect hạ xuống HTTP hoặc kiến trúc
không hỗ trợ đều dừng bootstrap. Rust toolchain cài sau đó vẫn là 1.99.1. Đây
là kiểm tra integrity bằng digest đã review trong repository; `rustup-init`
không được xác minh chữ ký GPG tại runtime. Tải xuống chạy trong worker có giám
sát, timeout tổng 10 phút và tổng stdout/stderr tối đa 2 MiB; tiến trình cha
kiểm tra hash trên tệp đã tải trước khi chạy. Installer chạy qua bounded process
runner với timeout 30 phút và tổng stdout/stderr tối đa 2 MiB; timeout hoặc tràn
đầu ra sẽ dừng tiến trình và không đánh dấu cài thành công. Hai lệnh `apt-get`
đầu script nay dùng timeout cố định 30 phút và kill-after 30 giây; các bước
`rustup component add` và kiểm tra compiler sau đó cũng dùng wrapper cùng giới
hạn. Output của apt chưa có hard cap trước khi Python được cài. Bootstrap Linux
thật chưa được chạy trên VM.

### Lệnh thực của protocol 2

Chuẩn bị hỗ trợ Python 3.11+; native cloud runner yêu cầu Linux/Python 3.13,
Rust 1.99.1 đúng pin, GNU time, NumPy và OpenSSL CLI cho TLS fixtures.

H07 now has a frozen six-cell AWS-LC/ring × client-lifetime collector in
`service_study.py`. It runs 24 randomized blocks × 256 sequential requests per
cell over controlled loopback HTTP/1.1, checks actual client/handshake counts,
and records p50/p95/p99 latency, whole-process CPU/request and peak RSS. Its
120,000-draw paired analysis covers 30 Bonferroni-adjusted intervals. The native
runner requires NumPy from the same Python 3.13 interpreter and records its
version; bootstrap checks this before marking setup complete. A resource-only
profile and provider contract gates precede broad measurements, with runtime
and evidence reserves included in admission. This H07 collector and profile
have not run on Linux/cloud; production DNS/Engine, HTTP/2/3 and independent
result authenticity remain NOT VERIFIED. No provider winner is claimed.

```text
python -B -m unittest cloud_experiments.test_campaign cloud_experiments.test_lifecycle kaggle_experiments.test_protocol model.test_models cloud_experiments.service_lab cloud_experiments.mcp_sdk.test_driver cloud_experiments.test_wceb cloud_experiments.test_wceb_runner -v
python -B -m cloud_experiments.contenders
python -B -m cloud_experiments.research inventory
python -B -m cloud_experiments.research prepare results/research-input --wall-seconds <bounded-seconds>
python3 -B -m cloud_experiments.research warm-cache <source> <spec.json> <output-parent>/cargo-target --stage-sdk-artifacts
python3 -B -m cloud_experiments.research run-native <source> <spec.json> <output> --pilot-only --budget-file <reviewed-budget.json>
python3 -B -m cloud_experiments.research run-native <source> <spec.json> <output> --resume --budget-file <same-reviewed-budget.json>
python3 -B -m cloud_experiments.research analyze <output> <variant> <new-analysis.json>
python3 -B -m cloud_experiments.research run-service <service_probe> <new-output>
python3 -B -m cloud_experiments.research run-agent <core_probe> <new-output> --policy W2 --representation compact
```

## WCEB local extraction adapter

`wceb.py` supports local analysis of the public Web Content Extraction Benchmark
v1.0 at pinned commit `62ff86d12ea72c80c31fb810ff1a724fad687bea`. The committed
`wceb_v1_tree_manifest.json` contains only repository paths, Git blob hashes and
byte sizes; it does not contain page HTML or annotation text. `load_wceb(root)`
checks a complete local tree against that inventory, verifies annotation and
compressed-HTML bytes, bounds decompression, checks the local directory boundary,
and records cross-split domain/HTML overlap. `load_html` rechecks the page before
returning it. The symlink-escape fixture is present, but Windows denied symlink
creation (`WinError 1314`), so that boundary is not verified on this host. The
adapter does not download, stage or upload corpus bytes and is not connected to
campaign launch. The bounded native scoring runner is `wceb_runner.py`; it
requires the complete pinned dataset to already exist at a local path on the
machine that runs it. The full expanded corpus is 111,728,111 bytes and cannot
fit the `research prepare` source bundle cap of 16 MiB. No cloud transfer/mount
procedure is implemented or authorized by this harness. The pinned corpus was
checked against the current `core_probe` limits (4 MiB HTML and 8 MiB JSON
fixture): 2,005/2,008 pages fit both size limits (1,494 dev, 511 public test);
the other three dev pages are retained in the score as `NOT_ADMITTED_HTML_CAP`,
never silently omitted. A successful native parse and score join are possible
only on pages admitted by the current probe. This is extraction-quality evidence,
not a Rust performance benchmark and not evidence for search recall/ranking,
provider/proxy routing, cache/freshness, concurrency, or agent task
completion/cost.

For a dev pass, provide the Rust baseline plus no more than 15 candidate
binaries. Each candidate is scored against identical WCEB files and annotation
hashes. Completed 16-page blocks are committed to `collection/` with content
digests, so `--resume` reuses matching blocks and reruns only an incomplete
block. The managed-run wall bound is at most 18,000 seconds (the existing
five-hour campaign ceiling); the separate pinned-tree integrity scan is bounded
to 1,800 seconds. Per-page native calls time out after at most 60 seconds. The output root allows 64 MiB of result rows and
64 MiB of command metadata, reserves 1 GiB of free disk, caps one AAE output at
16 MiB and total AAE bytes at 512 MiB per binary. These are application checks,
not OS-enforced quotas. Reports include extraction metrics and a paired
domain-cluster bootstrap with 50,000 draws and Bonferroni family-wise intervals
for F1 and the two snippet rates. Timing/RSS are process-level diagnostics
including launch and JSON I/O; they do not isolate parser speed.
Disk-reserve, storage-guard, metadata-capacity, and collection-capacity errors
abort the study rather than becoming page-level missing predictions; ordinary
child timeout, exit, invalid AAE, and per-page output-limit results remain
visible as missing outcomes.

After reviewing dev output, freeze one or two candidates. The `test` run requires
that immutable freeze, its dev report, and exactly the frozen binary hashes.
WCEB's test split is public, so it is not an independent secret holdout. Neither
the runner nor its in-process receipts authenticate execution independently of
the measured host OS; report authenticity remains `NOT VERIFIED`.

Example commands (the WCEB dataset must be pre-staged after a separate rights review;
binary paths come from `research run-native`'s frozen output):

```text
python3 -B -m cloud_experiments.wceb_runner run --dataset /data/wceb-v1 --out /opt/athena/runs/wceb-dev --split dev --baseline /opt/athena/runs/native-001/bins/baseline/core_probe --candidate scraper=/opt/athena/runs/native-001/bins/scraper/core_probe --wall-seconds 18000
python3 -B -m cloud_experiments.wceb_runner freeze --dev-report /opt/athena/runs/wceb-dev/report.json --out /opt/athena/runs/wceb-dev/freeze.json --candidate scraper
python3 -B -m cloud_experiments.wceb_runner run --dataset /data/wceb-v1 --out /opt/athena/runs/wceb-test --split test --baseline /opt/athena/runs/native-001/bins/baseline/core_probe --candidate scraper=/opt/athena/runs/native-001/bins/scraper/core_probe --wall-seconds 18000 --freeze-from /opt/athena/runs/wceb-dev/freeze.json --dev-report /opt/athena/runs/wceb-dev/report.json
```

`score_aae_stream` validates each bounded AAE/1 result and requires an exact
source-URL match when the annotation has one. If an annotation lacks a URL, the
envelope is still checked but the row is marked `aae_source_identity_verified: false`;
it is omitted from domain summaries. Explicit-null and missing `main_content`
references are separately reported and excluded from primary word-F1; snippet
rates and official-compatible diagnostics remain available. The public test
split and its known overlap are explicitly reported; it is not a secret holdout.
Reports include per-domain and per-page-type sample counts and a strict test
subset with neither a dev-seen hostname nor duplicate HTML. They include the
publisher-declared CC-BY-4.0 license and attribution.
Text-prediction admission counts UTF-8 in at most 16,384-code-point chunks
(64 KiB worst-case temporary) and stops at the item/aggregate byte budget; it
does not create a full-size encoded copy before admission.

```text
python -B -m unittest cloud_experiments.test_lifecycle cloud_experiments.test_wceb cloud_experiments.test_wceb_runner -v
```

The publisher declares the dataset, including cached HTML, under CC-BY-4.0; the
module records attribution. The publisher's authority over each third-party
page's content has not been independently verified. The pinned real archive was
validated locally in temporary storage and then removed; it was not uploaded to
cloud. Extraction quality, Rust throughput/RSS and campaign admission remain
unverified.

Muốn W1 với NVIDIA NIM, thêm `--policy W1 --provider nim --model <provider/model>
--request-cap <N> --confirm-provider-no-charge`. Chỉ dùng cờ này sau khi tự kiểm
tra điều kiện giá hiện hành cho model và tài khoản; runner không xác minh được
giá hay quota. Trên Compute Engine, đặt biến không bí mật
`NVIDIA_API_KEY_SECRET_RESOURCE` thành resource name đầy đủ của SecretVersion.
Runner lấy OAuth token từ metadata server, đọc đúng secret đã chỉ định trong bộ
nhớ, rồi truyền trực tiếp vào client; không đặt giá trị key vào command line,
environment, file, log hay archive. Service account của VM cần quyền
`roles/secretmanager.secretAccessor` chỉ trên secret đó. `NVIDIA_API_KEY` vẫn là
đường cấu hình tường minh cho môi trường operator/local và được ưu tiên nếu đặt.
Adapter giới hạn tiến trình dùng chung tối đa 30 request/60 giây, cách nhau ít
nhất 2,05 giây, một request tại một thời điểm; HTTP 429 ghi cooldown vào state
riêng đã gắn hash key. Không retry/fallback tự động. Adapter hiện có preflight
`GET /v1/models`. Một checkpoint cũ ghi nhận một catalog GET NIM đã xác thực trả
HTTP 200, nhưng không lưu đủ request/response identity để kết luận cho mọi model
hoặc tài khoản. [Tài liệu hosted API của NVIDIA](https://docs.api.nvidia.com/nim/reference/llm-apis)
xác nhận `POST /v1/chat/completions`; `GET /v1/models` được nêu trong
[tài liệu NIM container tự host](https://docs.nvidia.com/nim/large-language-models/latest/reference/api-reference.html).
Vì vậy giữ lần catalog thành công như một quan sát đơn lẻ; mức cam kết endpoint
hosted, inference, tool support, giá và quota thực vẫn NOT VERIFIED. Catalog
không chứng minh inference hoặc miễn phí. Operator acknowledgement không được
coi là xác minh giá. Không ghi key vào archive, tham số, log hay VM image. OpenRouter còn
là tùy chọn tường minh với `--provider openrouter` và `OPENROUTER_API_KEY`.
Khóa quota liên tiến trình hiện từ chối ngay lượt W1 thứ hai khi một lượt khác
đang giữ khóa; nó không xếp hàng. Lượt bị từ chối chưa dispatch request tới
provider, nhưng panel đó kết thúc FAILED. Vì vậy chỉ chạy một lệnh `run-agent`
W1 cho mỗi VM/key tại một thời điểm; khóa cục bộ không giới hạn các VM khác,
máy cá nhân hay ứng dụng ngoài Athena.
`run-agent` trả exit 2 khi task chưa đủ contract hoặc provider chặn; đó là bằng
chứng cần giữ, không sửa expected để xanh. Output mới không ghi đè output cũ.
Deadline `max_wall_seconds` của task được truyền vào model preflight và từng lượt
inference. Nếu `complete()` phải kiểm tra lại model admission, phần kiểm tra đó
cũng dùng cùng deadline với lượt inference kế tiếp. OpenRouter chia một deadline
đơn điệu cho các bước catalog/key; NIM kiểm tra lại thời gian sau khi ghi state
quota và trước provider transport; OpenRouter tính lại phần thời gian còn lại sau
khi serialize body. Cả hai adapter từ chối dispatch nếu deadline đã hết ở lần
kiểm tra cuối trước transport.
Regression Windows dùng transport giả xác nhận các nhánh hết hạn không phát
inference. Một kiểm soát đa tiến trình cũng giữ request NIM giả đang bay: tiến
trình thứ hai phải bị từ chối trước transport; biến thể có chủ ý bỏ khóa phải
dispatch được, và request bình thường chạy lại sau khi khóa được nhả. Linux/GCE,
quota giữa nhiều VM và việc provider hủy xử lý sau disconnect vẫn NOT VERIFIED.

### Tài nguyên, launch và diễn giải

Protocol 2 sở hữu output directory được chỉ định, source-only bundle ≤16 MiB,
mỗi fixture JSON ≤8 MiB, mỗi trường HTML ≤4 MiB, tổng fixture JSON ≤16 MiB,
evidence ≤16 GiB, WPT report ≤2 MiB mỗi file/≤24 MiB tổng, mỗi collection
block ≤256 MiB. Các ngưỡng fixture được kiểm tra
trong forecast và trước khi tạo thư mục/ghi fixture; ID cũng phải là thành phần
tên tệp ASCII giới hạn trước khi dùng làm đường dẫn. Các ngưỡng này không giới
hạn bộ nhớ vật lý của parser hay filesystem. Mỗi block paired
chỉ xuất hiện sau khi đủ hai arm và receipt đã được fsync; resume ràng buộc nguồn,
binary, fixture, settings và session, rồi bỏ staging dang dở. Dự trù build/cache
16 GiB, mỗi artifact export tối đa 19 GiB; cần ít nhất 94 GiB trống trước
chạy và giữ reserve 2 GiB. Forecast dự trù tối đa ba artifact export theo ba
lần thử supervisor: bản hiện tại và hai bản cũ, gồm cả tệp export tạm còn lại
sau gián đoạn. Forecast kiểm tra tổng evidence/build/metadata/
analysis/export/retained-export/fixture cộng reserve; RAM admission lấy cực đại giữa 2×peak lớn
hơn của native pilot/H07 và bound collector, rồi cộng reserve RAM. H07 peak phải
đo được. Đây là watchdog/admission cho output thuộc runner, không phải filesystem
quota hay giới hạn vật lý cho mọi tiến trình. Chỉ tái dùng cache tương thích;
không tự xóa evidence, fixture hay dữ liệu tích lũy chưa xác định owner.

Source snapshot collector áp trần tổng 16 MiB lên bytes đầu vào trước khi nạp
thêm từng file vào bộ nhớ; mỗi file bị giới hạn 8 MiB. Mỗi lần đọc bị chặn ở
`min(8 MiB, phần còn lại)+1 byte` để phát hiện vượt mức, và collector từ chối
trước khi mở file tiếp theo nếu đã dùng hết ngân sách tổng. Chuẩn hóa newline
có thể làm archive nhỏ hơn; giới hạn admission áp dụng cho bytes nguồn thô.
Khi duyệt thư mục, collector bỏ qua `target/` và `__pycache__/` trước khi mở
file, nên cache build/bytecode không tiêu hao ngân sách hoặc bị đọc vào bộ nhớ.
Hai lệnh chuẩn bị bundle còn kiểm kê `cloud_experiments/` theo danh sách tệp
được duyệt trong `campaign.py`; tệp lạ, tệp thiếu hoặc link sẽ làm preflight
dừng trước khi đọc nội dung. Khi thêm nguồn nghiên cứu mới, phải cập nhật danh
sách này sau review để tránh đóng gói nhầm khóa, log hay dữ liệu cục bộ.
Danh sách hiện gồm 14 tệp nguồn, fixture và lockfile của adapter MCP
`mcp_sdk/`; `target/`, `node_modules/` và bytecode được bỏ qua. Tệp lạ trong
thư mục MCP cũng làm preparation dừng trước khi đọc nội dung. Gói giải nén
chạy đủ 19 ca hiện có của `mcp_sdk.test_driver`, bao phủ response MCP, AAE,
fixture đầu vào không chứa đáp án, nhãn evidence và vòng đời child process.
`research.py` cũng đưa
`cloud_experiments.mcp_sdk.test_driver` vào `harness_controls` với deadline 600 giây;
suite 19 ca đã chạy trên Python 3.11/3.13 cục bộ. Đây vẫn chỉ unit/synthetic
preflight; không thay thế benchmark L03, target Linux/GCP, IAM/replay, hay quyết
định SDK production.
Việc chuẩn bị bundle dựng `source.tgz` và `spec.json` trong thư mục staging cùng
thư mục cha, chỉ đổi tên thành output sau khi dựng xong; lỗi giữa chừng tự dọn
staging, không để output dở dang chặn lần chạy lại. Thư mục output đã tồn tại
vẫn bị từ chối, không ghi đè bundle cũ.

Unit systemd được render với `RuntimeMaxSec=infinity`, memory/process caps và
retry giới hạn; runner giữ deadline bền vững theo budget và chừa dự phòng cho
export/shutdown. Dự toán export đang dùng giả định 1 MiB/s cộng 10 phút; đây
chưa phải tốc độ đo, nên cần thay bằng throughput pilot trước admission đầy đủ.
Supervisor cố xuất manifest COMPLETE/FAILED khi kết thúc có kiểm soát. SIGKILL,
mất host hoặc hết disk vẫn có thể cắt lần ghi đang dở; chỉ block đã commit mới
được dùng lại. Ước lượng phải gồm build, confirmation, analysis, export và
shutdown, đối chiếu giá/credit/free capacity/deadline thực tế. Budget JSON cần
`remaining_usd`, `ceiling_usd`, `reserve_usd`, `spent_usd`, `hourly_usd`,
`storage_usd`, `free_bytes`, `deadline_seconds`, `memory_bytes`,
`enforced_memory_bytes`, `pilot_memory_bound_bytes`, và các facts
`price_verified`, `credit_verified`, `enforcement_verified`. Chỉ đặt verified khi
đã kiểm thực tế; không sao chép một JSON toàn true để vượt admission. Admission
so cả chi phí forecast lẫn chi phí của toàn bộ `deadline_seconds` theo hourly
price và storage khai báo, sau khi trừ reserve; thời gian bootstrap/egress và
chi phí lưu disk kéo dài phải nằm trong đầu vào dự phòng. Admission không phải
billing cap GCP; STOP vẫn giữ disk có chi phí. Forecast quá hạn phải HOLD
và thiết kế phân việc/giới hạn mới, không cắt gates hoặc samples sau khi thấy kết quả.

Supervisor state now stores the SHA-256 of the exact budget JSON bytes that
established its deadline. Resume rejects a changed or legacy unbound budget
before dispatch; the supervisor rechecks the bytes immediately before native
execution and passes that same snapshot through admission and manifest binding.
This detects accidental/stale budget substitution, not same-OS-authority
tampering; execution authenticity remains `NOT VERIFIED`.

Managed check receipts are scoped to the process session that produced them.
After a supervisor restart, a previous `PASS` does not skip its check action;
the owner runs again and validates its resumable artifacts before reuse.

`inventory`, map đủ H01–H14, pilot COMPLETE, native controls PASS hoặc service
controls PASS đều không tự đóng cả chiến dịch. Native runner chưa điều phối đầy
đủ H09/H11–H14, dependency tuple rivals, corpus đại diện, W3 hay cloud topology.
`overall_decision=HOLD`, `decision_sufficiency=NOT_EVALUATED` giữ các khoảng trống
này rõ ràng. Tính toàn vẹn byte có kiểm hash; authenticity của execution vẫn
`NOT VERIFIED` vì runner và collector cùng quyền OS.

### Mốc thực nghiệm ngày 17/10/2026

Pilot cloud 003 của snapshot cũ đã hoàn tất baseline build/gates và 75 cohort,
900 raw CPU observations; **không có broad screening/confirmation**. Service
validation cùng snapshot có 16/17 scenario PASS, leader-cancel FAIL do không bảo
đảm follower kịp join. Failure/exit 2 được giữ. Bản nguồn hiện tại sửa bằng control
pending/join xác định và local positive/negative behavior checks; phải xác nhận
lại bản này trên Linux. Không gán PASS của snapshot cũ cho source mới.
Hai live free-model attempts nhận HTTP 503/429; chưa có dữ liệu chấm model quality.
Nguồn mới có 79 cohort và các sửa đổi loader/scheduler/analysis sau pilot 003;
không gọi 900 observations cũ là kết quả benchmark cho toàn bộ bản mở rộng này.

Validation snapshot **004** sau sửa đã hoàn tất trên Debian 13/Python 3.13.5,
Rust 1.99.0: 73 Python controls và 3 source-composition controls PASS, không skip;
baseline và tổ hợp 8 factor đều qua 49 Rust tests, format/Clippy/release và 79
canonical/oracle cohort mỗi cấu hình. Controlled service có 17/17 PASS. Negative
source-flight-off chạy exit 0 nhưng bị oracle bác bỏ đúng joined-flight/outcome
contract. W0/W2 × full/compact có 14/16 task hoàn thành mỗi cell; hai F04 thiếu
HTML table capability vẫn bị chặn, không chuyển thành PASS. Raw artifacts và
supervisor exit 0 đã được đọc/đối chiếu; export được kiểm size/SHA-256.
**Đây là validation tính đúng, không là comparative performance confirmation**;
verdict tổng thể tiếp tục HOLD. Snapshot 004 không chạy model thật hay W3.

Template lịch sử `finish.sh` sau validation 004 dùng transient timer để hoãn chính lệnh
poweroff; tránh chặn SSH trong cửa sổ tải artifact. Lý do là timed shutdown tạo
`/run/nologin` trước khi tắt máy ([Debian shutdown](https://manpages.debian.org/trixie/systemd-sysv/shutdown.8.en.html)).
Bash syntax và actual systemd timer với marker thay poweroff đã được kiểm trên
Linux; marker xuất hiện sau 3.162 giây, không có nologin. Đây là kiểm mechanism
defer, không phải thực thi nguyên template poweroff. Không gán thay đổi template
hoặc cập nhật tài liệu sau đó vào source snapshot 004 đã khóa.

Protocol 2 hiện dùng exact retained-tail bootstrap intervals: cùng paired draws
và CI ranks, chỉ giữ tối đa 1024 values/tail/quantile. Full sorted distributions
vẫn có cho independent/reference checks. Xem plan 18.6; thay đổi này tối ưu
analyzer, không phải bằng chứng Rust core đã nhanh hơn.

Corpus có thể nhập qua `prepare DEST --corpus-manifest MANIFEST`; xem schema,
dataset revision, license evidence, rights, byte/path bounds và holdout rules tại plan 18.4.
Manifest hiện dùng `athena-research-corpus/2`; loader kiểm tra cấu trúc, bytes/hash/path,
URL và việc `domain_group` khớp hostname chuẩn hóa, nhưng không xác minh độc lập
quyền pháp lý hoặc revision upstream.
Không cung cấp manifest
thì vẫn ghi NOT_PROVIDED, không tự gọi synthetic fixtures là corpus thực tế.

Điểm vào chọn công việc nghiên cứu là [CORE_RESEARCH_PLAN.md mục 19](../CORE_RESEARCH_PLAN.md#19-bản-đồ-bài-toán-và-nghiên-cứu-để-quyết-định-nâng-cấp-core--18102026):
source gaps, feasible acquisition combinations, H01–H14/F01–F08 và các tương tác
có cơ chế. Những executor, pool/cache, selected/reference representation và
sandbox adapters được đề xuất ở đó chưa tự thành cell hoặc CLI option hiện có.
Mỗi campaign phải bàn giao quyết định theo consumer và actual affected closure;
không dùng test count, registry size hoặc exploratory timing làm adoption gate.
Chưa có benchmark hoặc cloud launch mới từ lần cập nhật định hướng này.

Kế hoạch toàn core vẫn ở CORE_RESEARCH_PLAN.md mục 19.13–19.15. Dự trù 48–96 giờ
là proposal cũ, chưa là measured ETA cho implementation hiện tại. Runner không
còn hard cap 1 giờ cho pilot hay 6 giờ mặc định; design cho phép deadline tối đa
120 ngày, còn budget đã kiểm và cận trên export/shutdown quyết định thời gian thật.
Đây cũng chưa xác nhận khả năng chạy 120 ngày: Google Cloud, tiền còn lại,
throughput export và recovery phải được xác minh trước campaign. Rust đã pin
1.99.1; Linux runtime/performance cần evidence mới. Chưa có benchmark campaign
mới được khởi chạy.

### Current implementation and target-host verification

Current research source has durable collection journals, resume and storage
checks, process-tree cleanup on POSIX, export receipts, supervisor retry state and
NIM request gating. These controls do not implement every H01–H14 owner or make a
broad campaign ready. Linux/Python 3.13 and pinned Rust 1.99.1 checks,
service installation, descendant process cleanup, disk admission and interrupted
run export still require current-snapshot cloud verification. Preparation checks
do not launch the benchmark campaign.

### Continuing a long confirmation analysis

`run-native` now uses atomic manifest publication through `ResearchRun`. An
application failure records the phase at failure; systemd `ExecStopPost` writes
an outcome receipt and the supervisor attempts to export finalized failures.
That path still needs target-host verification under termination/OOM. A successful pilot or screen-only
operator does not establish completed confirmation. Historical operator 010
finished its explicit screen-only scope; it did not crash after an hour.

The supervisor persists one absolute campaign deadline across restarts, bounds
the archive-export child by its reserved export deadline, and does not rebase an
expired run. Expired resume finalization is idempotent so the outer error handler
does not duplicate the terminal failure log. Local Windows coverage passes;
Linux/systemd/process-group and cloud-filesystem behavior remain **NOT VERIFIED**.

Managed exports now use `output-evidence-<manifest-sha256>.tgz`. A finalized
failed attempt and its recovered completion have distinct immutable archives;
repeating an export for the same finalized manifest reuses its verified bytes.
Read the current `export_archive_name` in the supervisor state or `archive_name`
in `export-receipt.json` instead of assuming the legacy fixed filename. A local
lock serializes managed export admission and publication. At most three matching
published or partial export artifacts may be retained; the legacy fixed archive
counts toward that limit. Capacity exhaustion preserves all existing artifacts
and stops new export publication. This is an application guard, not a filesystem
quota. Transfer receipt/ack files remain outside the immutable artifact map; an
ack for an older SHA-256 does not verify a newer archive. Supervisor exit 0 now
requires `VERIFIED_LOCAL_ARCHIVE`, not a still-running or missing manifest.
Prepare a new frozen source/spec bundle after this change; do not reuse an older
56 GiB admission snapshot as the current design.

On resume, the 94 GiB envelope includes storage already allocated to that exact
managed run, its owned `cargo-target` cache and its managed export artifacts.
Source, other run directories, linked entries and other filesystems receive no
credit. The scan counts single-link regular files at most once, using the lesser
of logical length and allocated `st_blocks × 512`; sparse holes, hard links and
unavailable block metadata receive no credit. The reviewed capacity remains an
upper bound, and physical free space must still preserve the disk reserve.
Scanning is limited to 200,000 entries and the remaining command/campaign
deadline. A resume with no export snapshot slot fails before expensive work.
`disk_admission` records the free/credited/admitted byte counts. This avoids
charging already-retained run bytes twice in startup admission; it does not
establish Linux filesystem behavior from the Windows unit checks. The allocation
fields follow the [Python 3.13 OS documentation](https://docs.python.org/3.13/library/os.html#os.stat_result.st_blocks).

After **complete frozen confirmation collection**, the existing command is also
the resume command:

```text
python -B -m cloud_experiments.research analyze RUN_DIRECTORY VARIANT REPORT.json
```

Each completed case commits CPU/RSS analysis to
`REPORT.analysis-progress.json`; `REPORT.analysis.lock` prevents simultaneous
analyzers for that output. After process termination, run the same command with
the same source, runtime and evidence. A partial case is recomputed; completed
cases are reused only with matching raw/freeze/settings/source/runtime identity
and valid checkpoint digests. Changing inputs requires a separate output, rather
than forcing a stale checkpoint to load. Progress metadata is capped at 8 MiB;
that is not a total process memory or disk quota.

The final report is published only after all cases are present and retains the
full frozen simultaneous family, seed, drift/RSS/rejection rules and an explicit
`analysis_context`. A resumed analysis must equal uninterrupted analysis; it
must not hide a late adverse case. The raw input loader hashes the bytes it
parses instead of rereading the complete shard into memory. Numeric outputs and
required raw CPU/RSS pairing are unchanged.

Atomic collection receipts are also analysis inputs: `evidence.receipt_inputs`
records SHA-256 of the exact receipt bytes consumed, and the analysis evidence
records the environment binding. A receipt change invalidates a cached analysis
checkpoint even when the CPU/RSS sample bytes remain unchanged. Destination
validation rejects output, progress and lock paths inside `collection/` or
`bins/` before creating a lock or checkpoint there. Required manifest, freeze,
raw sample, receipt and binary files cannot be overwritten by analysis output.
The owner checks verify both byte preservation and unchanged committed-block
directory contents after rejection.

Standalone analysis also uses the lifecycle byte limits for manifest (16 MiB)
and frozen design (8 MiB). The shared reader consumes chunks of at most 64 KiB,
so a small input does not preallocate its maximum allowed size. Selected CPU
rows are capped per cohort at the frozen two-arm block/iteration count across
all shards; the first excess row is rejected before later rows are decoded.
Existing strict JSON, CPU/RSS pairing and the streaming-shard memory gate stay
in force. These are input bounds, not a measured total-process RSS guarantee.

JSON metadata uses a uniquely owned same-directory staging file, file fsync and
atomic replacement. Commit failure cleans only that staging file and leaves the
previous published JSON. Normal/cancelled process checks do not attest power
failure durability. Checkpoint hashes detect accidental corruption under the
trusted-runner assumption; same-OS result authenticity remains NOT VERIFIED.

These controls have local Windows/Python 3.11 coverage, including a killed real
child and a fresh-process restart. A Linux descendant-memory/reaping check now
exists in the lifecycle suite. POSIX lock behavior and the modified native entry
point still require Linux/Python 3.13 target verification before a large job.
Incomplete timing collection is rejected, not automatically resumed or mixed
across sessions. Compute windows may be longer or split by phase only after
actual capacity, credit and supervisor/VM lifetime admission; the global study
wall bound is not automatically extended by analysis recovery.

### Prelaunch remediation, 2026-10-20

The current harness fixes completed-job identity reuse, atomic archive
publication without replacement, registered-combination service admission,
malformed model tool names, per-credential NIM gate ownership and unsent-request
accounting. Provider and Secret Manager HTTP now run in a cancellable child:
credentials use an anonymous pipe, with fixed endpoints, no proxy/redirect/retry,
bounded input/output and kill/wait on the parent's deadline. This adds startup
cost to provider control calls; it is outside Rust core timing. Controlled
loopback checks cover header/body drips, a pre-connect stall and real worker
reaping. Linux workers also bind to the request owner's lifetime and have an
independent alarm; killing a parent cannot leave an active worker. Windows
preparation has a finite self deadline and does not claim Linux parent-death
semantics. Research subprocesses receive a fixed runtime environment allowlist
and only the five declared `ATHENA_RESEARCH_*` compile-time selectors; arbitrary
namespaced values and provider/GCP/Kaggle credentials are stripped. The owning
lifecycle regression checks both secret removal and selector retention with
synthetic values. Provider-side cancellation after disconnect remains NOT VERIFIED.

Install both root-owned units produced by `lifecycle render-unit MEMORY_BYTES --source FROZEN_SOURCE`
and `lifecycle render-poweroff-unit` on systemd >=254. The poweroff unit runs a
fixed system command after terminal success/failure; `RestartMode=direct`
preserves intermediate retries. Bind the unit to the frozen source directory;
preserve any existing checkout at `/opt/athena/current`. `finish.sh` records the process outcome as the
unprivileged research user. Before starting the campaign, choose a future
absolute UTC terminationTime no later than the cost-derived cutoff and 120 days
ahead. Set instanceTerminationAction=STOP; read back terminationTime and
terminationTimestamp.
Use the absolute time because GCP recalculates maxRunDuration from the latest
start after a stop/restart. The guest supervisor deadline must end earlier to
leave time for analysis, export, shutdown, and a margin. Verify the final export
and its checksum before expiry. Compute Engine can begin termination up to 30
seconds after the configured time. STOP preserves the VM and its checkpoint,
but its persistent disks continue to accrue charges. Include that storage cost
and a bounded retrieval/cleanup window in the reviewed budget; delete the VM or
evidence disk only after the exported result checksum is verified. This does not
cap project-wide spending or prevent creating another resource. The current
project has no VM on which to verify these settings, so the campaign stays on
HOLD.

See [GCE VM runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[disk deletion behavior](https://docs.cloud.google.com/compute/docs/disks/modify-persistent-disk),
and plan §19.139. Never delete retained evidence merely because a local archive
or outcome receipt exists.

Read-only inventory of the configured project on 2026-10-26 returned no rows
for Compute Engine instances, persistent disks, snapshots, or reserved
addresses. The VM and three-disk inventory above records the 2026-10-24
preflight and is historical; no cloud resource was changed during this audit.
This inventory does not verify the billing balance or charges from other
services.

The comprehensive campaign is **NOT READY**. See plan 19.17–19.19 and the
private checkpoint for exact whole-core obligations, bounded-preflight evidence
and cost assumptions. Remaining gaps include missing language/browser/MCP
adapters; H06 semantic header association, representative table corpus and F04
integration; independent corpus, aggregate load/recovery controls, verified
billing facts, NIM inference eligibility and the protocol-2 whole-core campaign
executor. The legacy
`campaign run` command is only a scoped AAE/rank study with a fixed five-hour
deadline and no resume path; it does not satisfy the whole-core obligations.
Native/service/agent commands
are separate subset entry points. An installed unit, mapped decision or passing
subset is not whole-core research evidence. No broad launch is authorized until
the user approves the exact reviewed launch receipt.

## 2026-10-26 — Financial gate clarification

E2-standard-8 in Singapore is listed at $0.26804568/hour. The 120-day design ceiling would cost $771.97 in compute alone; it is not a run estimate or an approved spend. If the user's approximate $250 were actually available, it would cover about 38.86 days of compute before disk, network, bootstrap, reserve, or other charges. Current balance and usable credits remain unverified.

Google's current spend-cap eligibility list covers Gemini API, Agent Platform, Cloud Run, and Cloud Run functions, not Compute Engine. The documentation also says caps do not stop ongoing fixed resource charges and enforcement is not instantaneous. A spend-cap budget is therefore not a VM cost cap. The application admission gate checks forecast and deadline costs from supplied reviewed facts, but is not an external cloud kill switch. Before launch, verify current budget facts and install/read back an absolute UTC GCE terminationTime with STOP, including export and shutdown reserves. No such deadline or campaign supervisor is currently verified.

Sources: [E2 VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose); [spend-cap eligibility and limits](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## 2026-10-27 — Pre-isolation Cargo cache warmup, cache-isolated SDK/Lexbor staging, expanded technical_coverage receipts, and offline L01/L03/WPT child-env closure

Because `run-native` executes every Cargo and Python child inside a network-denied Linux namespace (`unshare --user --map-root-user --net --pid --fork --mount-proc --kill-child`) with a scrubbed child environment (`lifecycle._RESEARCH_CHILD_ENV`) and requires `cargo fetch --offline --locked --target x86_64-unknown-linux-gnu` before compilation, the target VM must warm the external Cargo cache (`<output-parent>/cargo-target/cargo-home`) and stage the local MCP SDK and Lexbor v3.0.0 dependencies inside `<output-parent>/cargo-target` while network access is still permitted during host preparation:

1. **Warm offline Cargo cache and stage local SDK + Lexbor v3.0.0 artifacts inside `<cache-root>`**:
   ```text
   python3 -B -m cloud_experiments.research warm-cache /opt/athena/current /opt/athena/spec.json /opt/athena/runs/cargo-target --stage-sdk-artifacts
   ```
   `warm-cache` verifies the frozen `source` against `spec.json`, rejects any `.cargo` config or credentials in the workspace or cache root, materializes each unique planned lock profile (including unpacking `cloud_experiments/vendor/html5ever-0.39.0.crate` and `cloud_experiments/vendor/html5ever-0.40.1.crate` for the attribute-index lockfiles) plus `cloud_experiments/mcp_sdk/rust` and `cloud_experiments/lexbor_adapter` inside temporary scratch directories under `cargo-target`, fetches all crates online, immediately verifies every profile with `cargo fetch --offline --locked` and `libraries.offline_dependency_metadata`, and (when `--stage-sdk-artifacts` is set) stages all prebuilt artifacts inside `<cache-root>` (`<cache-root>/release/core_probe`, `<cache-root>/mcp-sdk-rust-target/release/athena-mcp-rust-fixture`, `<cache-root>/mcp-sdk-ts/typescript/server.mjs` + `node_modules` via `npm ci --ignore-scripts --no-audit --no-fund`, `<cache-root>/lexbor-v3.0.0`, and `<cache-root>/lexbor-adapter-target` via `cloud_experiments/lexbor/build_and_smoke.sh` with `ATHENA_LEXBOR_ARCHIVE_CACHE` and `ATHENA_LEXBOR_ADAPTER_TARGET_DIR`) without touching the read-only `source` tree, and reports `cache_bytes = lifecycle.tree_bytes(cache)`.
2. **In-harness and standalone L01 (Lexbor v3.0.0), L03 (MCP SDK), WPT child-environment execution, and expanded `technical_coverage` receipts**:
   - `lifecycle._RESEARCH_CHILD_ENV` explicitly preserves non-secret build/test environment variables (`ATHENA_WPT_REPORT_ONLY`, `ATHENA_LEXBOR_ADAPTER_TARGET_DIR`, `ATHENA_LEXBOR_ARCHIVE_CACHE`, `ATHENA_LEXBOR_WORK_ROOT`, `ATHENA_LEXBOR_SOURCE_DIR`, `ATHENA_LEXBOR_BUILD_DIR`, `ATHENA_LEXBOR_LIB_DIR`, `ATHENA_MCP_RUST_BIN`, `ATHENA_MCP_TS_SERVER`, `ATHENA_CORE_PROBE_BIN`, `ATHENA_OFFLINE`, `NODE_PATH`, plus Windows MSVC toolchain discovery variables) while continuing to strip provider credentials (`NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `KAGGLE_KEY`). Forwarding `ATHENA_WPT_REPORT_ONLY=1` ensures `_run_wpt_report` emits `ATHENA_WPT_REPORT_JSON=...` and exits `0` during `build-baseline` and `I-HTML-ATTR-MEMBERSHIP` builds instead of panicking on known `html5ever` WPT mismatches.
   - During `run-native`, `_run_harness_controls` and `_run_staged_language_panel_checks` automatically discover the `<output-parent>/cargo-target` staged artifacts before falling back to `source`, execute `l03_mcp_sdk_preflight` (`output/l03-mcp-sdk-preflight.json`) and `l01_lexbor_semantic_parity` (`output/l01-lexbor-semantic-parity.json`) offline inside `unshare -rn`, and `_update_technical_coverage` records structured `SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED` (`runnable_verified: False`) receipts across executed factorial interaction panels (`screen-interactions.json`), `screen-cross-coverage.json` (`I-NATIVE-CROSS-COVERAGE`), `screening.json` (`screened_variants` across `H01–H14`), `selection.json` (`H14`, `I-SYSTEM`), `h07_service_study` (`H07`, `H08`, `I-ACCESS`, `I-SYSTEM`), `L01` (`I-LANG-PARSE`, `I-HTML-TABLE`), and `L03` (`I-LANG-AGENT`, `I-AGENT`) while preserving `readiness(design, run.manifest["technical_coverage"])["status"] == "INCOMPLETE"`.
   - `cloud_experiments/bootstrap.sh` installs `cloud-guest-utils` and `e2fsprogs`, canonicalizes `/dev/disk/by-*` root device symlinks via `realpath -e`, expands the root partition and filesystem (`growpart` + `resize2fs`) when the boot persistent disk is resized, and verifies `node >= 20`. Read-only `gcloud compute instances describe athena-native-01 --zone=us-central1-a --project=athena-core-research-261017` on 2026-10-27 confirmed `status=TERMINATED`, `machineType=e2-standard-4`, and boot disk `athena-native-01` (`diskSizeGb=80`, `autoDelete=false`, `debian-13-trixie`); resizing the disk so free space exceeds the 94 GiB `minimum_free_bytes` admission floor remains an explicit operator pre-launch step.
   - Standalone L01 smoke: `bash cloud_experiments/lexbor/build_and_smoke.sh /opt/athena/runs/lexbor-smoke`.
   - Standalone L03 preflight (`--node` defaults to `node` on `PATH`; `--ts-server` is accepted as an alias for `--typescript-server`; `--out` writes the JSON receipt):
     `python3 -B -m cloud_experiments.mcp_sdk.driver --rust-bin /opt/athena/runs/cargo-target/mcp-sdk-rust-target/release/athena-mcp-rust-fixture --ts-server /opt/athena/runs/cargo-target/mcp-sdk-ts/typescript/server.mjs --core-probe /opt/athena/runs/cargo-target/release/core_probe --out /opt/athena/runs/mcp-sdk-preflight.json`.
3. **High-efficiency experimental design (`experimental_design.py`), per-case pilot forecast calibration (`case_pilot_ns`), `contenders._writer` `BufWriter` import fix, exact bootstrap parity (`k_bits = count.bit_length()`), and GCE Linux campaigns `results/core-research-cloud-011`..`015` (`CORE_RESEARCH_PLAN.md` §§19.161–19.165)**:
   - `experimental_design.high_efficiency_design(target_quality=0.95, include_single_arms=True, enforce_higher_order=False)` reduces the 225-variant space to **88 variants** (**60.89% reduction**, **98.30% composite quality** with `36/36 = 100.0%` main-effect levels, `36/36 = 100.0%` single-arm retention, `591/591 = 100.0%` 2-way pairwise tuple retention, and `828/871 = 95.06%` mixed-strength tuple retention [`237/280 = 84.64%` 3/4-way higher-order mechanism tuples, `41/139 = 29.50%` factorial panel cells]) or **54 variants** (`include_single_arms=False, enforce_higher_order=False`, **76.00% reduction**, **98.30% composite quality**). With `enforce_higher_order=True` (`target_quality=0.95`), selects **116 variants** (**48.44% reduction**; **83 variants / 63.11% reduction** when `include_single_arms=False`) achieving `591/591 = 100.0%` 2-way pairwise, `266/280 = 95.00%` 3/4-way mechanism (`857/871 = 98.39%` mixed-strength), and **99.46% composite quality**; with `target_quality=1.0, enforce_higher_order=True`, selects **130 variants** (**42.22% reduction**; **97 variants / 56.89% reduction** when `include_single_arms=False`) for `871/871 = 100.0%` mixed-strength and `280/280 = 100.0%` 3/4-way tuple coverage (`coverage_status: "COVERED"`). `experimental_design.whole_path_core_panel()` defines the bounded `H01`/`H09`/`I-AAE`/`I-HASH-LIBRARY`/`I-JSON-LIBRARY` whole-path core panel (`cloud_014_variants`, 7 variants) and the 13-variant `H01` + `H02` (`I-RANK` 6 cells) + `H06` (`I-HTML-TABLE` 2 cells) panel (`cloud_015_variants`, 13 variants).
   - `research.forecast_native` uses per-case `pilot["case_pilot_ns"]` iteration durations (with fallback to mean valid per-case duration when `case_pilot_ns` is partial) + subprocess invocation overhead and deduplicates both confirm case counts and confirm wall-time costs per decision owner (avoiding the ~41x overestimation when `attribute-heavy-8192` pilot durations were uniformly applied to 60 fast `whole_path` cohorts), while `bounded_bootstrap` / `_bootstrap_batches` uses `batch_size=256`, `_WEIGHT_CACHE`, vectorized `coarse_T`, `k_bits = count.bit_length()` with unified rejection sampling (exact parity with `Random.randrange` across power-of-two and non-power-of-two block counts), and `int32` fixed-step binary search on a contiguous `prefix[1:]` slice with 100% bit-for-bit identical interval bounds.
   - `contenders.py` strips `BufWriter` from `use std::io::{self, BufWriter, Write};` in `src/aae.rs` only when no `BufWriter::` references remain after all factor transformations (fixing compilation of the 16 variants in `native_design()` with `writer in {"direct", "reuse"}` and default `hash="joined"`).
   - Completed bounded GCE Linux runs `results/core-research-cloud-011` (`H04` `html-attrs-hash`), `results/core-research-cloud-012` (10-cell `I-HTML-ATTR-MEMBERSHIP` + 2-cell `I-HTML-TABLE`, confirming `html-attrs-modern-vector`), `results/core-research-cloud-013` (`H03` `prepared`, `H06` `table-sparse`, and multi-owner `prepared+table-sparse`), `results/core-research-cloud-014` (`H01`/`H09` whole-path screening and 60-cohort / 960,000-draw confirmation of `lib-blake3-pure` + `tests/aae_output_budget.rs` oracle rejection of `lib-json-sonic`), and `results/core-research-cloud-015` (13-variant `H01` + 6-cell `I-RANK` `H02` + 2-cell `I-HTML-TABLE` `H06` screening and 3,568,000-draw confirmation of `aae-batched4096`, `rank-binary+rank-select`, and multi-owner `aae-batched4096+rank-binary+rank-select`), with `athena-native-01` returned to `TERMINATED`.

