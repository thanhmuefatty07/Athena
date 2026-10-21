# Nghiên cứu core trên Google Cloud

Runner mở rộng hiện tại là `research.py`, protocol **`athena-core-research/2`**.
Đọc [hướng dẫn protocol 2](#protocol-2-bộ-nghiên-cứu-mở-rộng) trước khi chạy mới.
Các phần native/1 dưới đây mô tả study cũ, được giữ để tái lập evidence cũ;
không dùng thông số hoặc verdict của native/1 để đại diện cho protocol 2.

Phần bổ sung **CPU-first / phối hợp ngôn ngữ** nằm ở
[kế hoạch mục 19.15](../CORE_RESEARCH_PLAN.md#1915-nghiên-cứu-phối-hợp-ngôn-ngữ-cho-core-trên-cloud--19102026):
L01 parser C/Rust, L02 browser controller, L03 Rust/TypeScript MCP/SDK;
L04 typed-batch handoff, L05 model inference CPU và L06 service runtime chỉ
admit khi có consumer/profile/gold phù hợp. Đây là thiết kế, **chưa có executor
cho L01–L06**; native/2 hiện sinh 204 variants, 12 whole-path factorial panels,
một panel H06 stage-only và mixed-strength coverage trên 13 whole-path factors
(kế hoạch 19.19). GPU/training/index vẫn
deferred. Sáu interaction thiết kế `I-LANG-*` trong decision map chưa là cells
được native runner thực thi. Giữ đầy đủ H01–H14 và các capability còn thiếu.
Không dùng `MAPPED` làm readiness; phải triển khai adapter/oracle/collector,
Linux gates và pilot recovery/cost trước campaign mới. Gate Linux hiện hành
chạy 76/76 test không skip; Actions audit đồng thời đạt Rust fmt/test/Clippy/
release build. Hai resource-bound regressions mới và source archive được
đối chiếu trên commit reviewed; việc ghi `source.tgz` dùng byte cap và publication
nguyên tử để lỗi giữa chừng không biến archive dở dang thành input tiếp theo; đây là prelaunch evidence, chưa phải benchmark
toàn matrix, provider inference hay production readiness. VM đã được dừng,
disk/evidence/cache được giữ lại. Dự trù 48–96 giờ cũ cần reforecast sau pilot
cho scope bổ sung.

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
`deny.toml` là policy dependency duy nhất cho graph Cargo hiện tại; nó ghim
cargo-deny 0.20.2 qua CI và kiểm tra advisories, bans, licenses và sources.
Ba biểu thức license cũ dùng dấu `/` được bind bằng các file license đã chuẩn
hóa và hash xxHash32; kết quả policy không thay thế source-license review hoặc
MSRV/transitive compatibility review.
Registry `language_panels` theo dõi riêng L01–L06: L01–L03 hiện là bắt buộc nhưng
chưa triển khai; L04–L06 đang không được admit có điều kiện và kèm lý do/gate.
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
vẫn xuất hiện như blocker `NOT_IMPLEMENTED`, L04–L06 là non-admission có điều
kiện. Experiment execution và decision sufficiency **NOT_EVALUATED**. Không dùng
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
python -B -m unittest cloud_experiments.test_campaign kaggle_experiments.test_protocol model.test_models -v
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
gửi tên/version crate công khai; partial/error giữ NOT VERIFIED, không tự cập nhật.

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

- Sinh 205 variants: 203 cấu hình trước đó, một H06 CSS-selector comparator và một H06 scope-header comparator.
  Gồm I-AAE 48 cell (writer × hash × reserve), I-RANK
  6 cell (lookup × selection), I-PROJECT 4 cell (fusion × link lookup), cùng
  prepared state, logical table và build factors. Thêm I-HASH-LIBRARY 12,
  I-PARSER-LIBRARY 12, I-JSON-LIBRARY 24, I-LIBRARY-SYSTEM 18, I-JSON-CPU 4 và
  I-JSON-PROJECT 4 cell; thêm I-QUERY-PROJECT 12, I-COMPILER 12 và
  I-JSON-HASH-CPU 16. Các panel dùng chung cell trùng nên không cộng số cell
  thành số binary. `experimental_design.py` còn chọn 96 cell có bao phủ đầy đủ
  823 tuple hợp lệ: mọi cặp trên 13 axis, tám nhóm ba yếu tố có cơ chế và một
  nhóm writer/hash/lookup/projection bốn yếu tố. Ba tuple Sonic/streaming không
  tương thích được ghi rõ. 203 cell trước đó đã qua kiểm phép biến đổi source;
  H06 selector arm là stage-only trên cùng parser/grid và được nối vào build,
  oracle, fixture và cohort. H06 scope-header arm thêm liên kết `scope=col/row`
  trên logical grid với ưu tiên `headers`, từ chối ID trùng/tham chiếu thiếu và
  oracle literal độc lập; đây vẫn là stage-only, không phải WHATWG đầy đủ hay
  production integration. Parser DOM-node cap nay được kiểm trước mỗi lần
  `HtmlTreeSink` cấp node; sau khi chạm ngưỡng, runner ngừng đưa các chunk HTML
  tiếp theo vào parser. Input được cấp theo chunk UTF-8 16 KiB để giới hạn phần
  parser nội bộ còn xử lý sau khi báo vượt ngưỡng. Tổng logical grid-slot budget
  được truyền vào `expand_with_slot_limit` và kiểm trước từng lần tăng hàng;
  peak RSS, sức chứa Vec, bộ nhớ attribute/tokenizer và tổng memory vẫn
  NOT VERIFIED. **Chưa có bằng chứng build/runtime/benchmark
  toàn matrix**.
  `spec.native_design`, `screen-cross-coverage.json` và inventory ghi ranh giới
  này; covering array không cho phép suy ra interaction không confounded hoặc
  tuyên bố đã thử hết 248.832 điểm Cartesian trước constraints.
- 112 cohort mỗi split: pipeline/export, query 1/3/8/32/128 từ, hit density,
  novelty, parser/links/Unicode/HTML repair, prepared build/query/invalidation,
  logical grid và output-budget rejection 64 KiB/512 KiB/4 MiB. Seed holdout
  tạo byte mới; chưa có corpus web/domain holdout đại diện production.
- H03 hiện có `PreparedRank` trong Rust core với token-budget admission, revision
  check và AAE export byte-parity. `Engine::new` và
  `with_pending_limit` vẫn giữ đường one-shot; research arm phải chọn rõ
  `LexicalMode::Prepared`, khi đó state được xây một lần trong source flight
  rồi dùng lại cho các representation query đang cùng flight. Đây là closure
  integration/correctness cục bộ, không phải quyết định hiệu năng. Chi phí build,
  retained allocator overhead, eviction và break-even trên các lượt query tuần
  tự vẫn phải đo bằng campaign.
- 33 cohort bổ sung có vai trò cụ thể: giới hạn byte/nodes/link đúng ngưỡng và
  vượt ngưỡng, hash 63/64/65 và 1023/1024/1025 byte, nội dung giả framing,
  Unicode không tự chuẩn hóa, subtree phải bỏ, nearest-owner, query bất lợi
  và link scaling. Literal oracle kiểm tra role/text/link hoặc đúng loại lỗi;
  baseline parity không thay thế oracle này.
- `libraries.py` khóa đúng manifest/lock và kiểm tra feature thực sự được
  Cargo bật. `library_decisions.json` giữ consumer/gate của 12 dependency
  trực tiếp; `library_candidates.json` ghi provenance 17 package được thêm
  trong lock Sonic. Truy vấn advisory không chứng minh không có lỗ hổng.
- Ba lock trong `locks/` là đầu vào comparator: Sonic 0.5.10, Scraper lean,
  Sonic + lean. Lean giữ `errors`, chỉ bỏ CLI và hai package `getopts` /
  `unicode-width`. Các profile này dùng cùng bản vá RustSec của lock chính
  (`rustls-webpki 0.103.16`, `webpki-root-certs 1.0.10`) và đã qua metadata/
  advisory gate riêng. Không nâng version dependency sẵn có ngoài bản vá.
  Sonic chỉ hỗ trợ
  writer `frame`; ghép với direct/buffered/reuse bị từ chối rõ ràng.
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
- Sau khi H03 thêm prepared-state vào `src/aae.rs` và `src/rank.rs`, frozen
  source digests và H02 transformation region đã được cập nhật cùng owning
  regression: mọi contender khai báo phải materialize được từ đúng core hiện
  tại trước khi build. Đây là kiểm tra tính chạy được của comparator, không phải
  bằng chứng performance.
- H10 build receipt hiện ghi nhận `rustc -Vv`, `cargo -Vv`, channel trong
  `rust-toolchain.toml`, profile environment và `rustflags`; resume từ binary
  cũ bị từ chối nếu receipt compiler/profile khác. Receipt này khóa danh tính
  build, nhưng chưa chứng minh profile nào thắng về runtime, RSS, startup hay
  portability.
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
Control ghi số source leaders và đúng vai trò/denominator/recovery; tắt source
flight phải bị oracle bác bỏ. TLS chưa chắc hoàn tất tại thời điểm hủy.
Không dùng các control này như benchmark service p99 hoặc production Engine
permitted-public-DNS/TLS integration. Long soak/topology/HTTP2/3 vẫn chưa kiểm.
Các lệnh OpenSSL cục bộ dùng để nhận diện phiên bản và tạo TLS fixture giới hạn
stdout+stderr gộp ở 64 KiB mỗi lệnh; thời hạn lần lượt là 5 giây và 10 giây.

`agent_lab.py` và `agent_tasks.json` chứa F01–F08, W0 rules/W2 batching,
actual native AAE producer, full/compact+expand, scope/revision/expiry của handle
và oracle facts/units/citations độc lập. Native producer timeout và early-leader-exit
được cô lập trong process-group POSIX; Windows dùng Job Object với
`KILL_ON_JOB_CLOSE` và đã có hai regression boundary trên host phát triển. Hành vi
trên target cloud, sandbox và cross-runtime vẫn chưa được xác minh. Consumer AAE cũng từ chối frame xen kẽ và
link ID không liên tục; content hash BLAKE3 vẫn cần oracle độc lập. W1 chỉ gọi model sau khi chọn rõ model,
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
đã được nhận dạng. Nhãn này ghi nhận quan sát trong tiến trình, không chứng minh
độc lập danh tính model/provider hay tính xác thực của kết quả. Các test stub
chỉ là test doubles trong process, không chứng minh kết quả provider thật.
F04 đã có comparator scope-header bounded trên logical grid, cùng units/locators và
literal oracle; nó chưa phải inferred-header WHATWG đầy đủ hoặc integration với
consumer/task. Đúng vài cell chưa đủ task PASS.
**W3/general generated-code sandbox chưa triển khai**; không chạy code model.
Live model quality, MCP transport và deployment cost/SLO chưa được xác nhận.

Trước khi biên dịch contender, runner đối chiếu byte chính xác của `Cargo.toml`,
`Cargo.lock` và lock profile đã duyệt. Chỉ sau đó `cargo fetch --locked` mới tải
crate vào Cargo home riêng không chứa credential/config; `test`, `clippy` và
`build` chạy trong user/network namespace với negative control cho địa chỉ
metadata GCE và egress công cộng, đồng thời dùng Cargo offline. Nếu namespace
không tách biệt hoặc một kết nối kiểm soát được, runner dừng trước lệnh build.
Service unit để home Rust read-only. Đây mới là control ở mức tiến trình; việc
namespace hoạt động trên VM mục tiêu vẫn **NOT VERIFIED** cho tới khi cloud
preflight thực chạy, và nguồn bằng chứng vẫn chia sẻ OS authority.

### Bootstrap Rust

`bootstrap.sh` gọi `rustup_bootstrap.py`, tải `rustup-init` 1.29.1 theo kiến
trúc x86_64 hoặc aarch64, rồi so SHA-256 với giá trị đã ghim trong source trước
khi chạy installer dưới tài khoản `athena-research`. Hash lấy từ tệp checksum
chính thức của Rust cho [x86_64](https://static.rust-lang.org/rustup/archive/1.29.1/x86_64-unknown-linux-gnu/rustup-init.sha256)
và [aarch64](https://static.rust-lang.org/rustup/archive/1.29.1/aarch64-unknown-linux-gnu/rustup-init.sha256).
Giới hạn tải là 64 MiB; checksum sai, redirect hạ xuống HTTP hoặc kiến trúc
không hỗ trợ đều dừng bootstrap. Rust toolchain cài sau đó vẫn là 1.99.1. Đây
là kiểm tra integrity bằng digest đã review trong repository; `rustup-init`
không được xác minh chữ ký GPG tại runtime. Installer chạy qua bounded process
runner với timeout 30 phút và tổng stdout/stderr tối đa 2 MiB; timeout hoặc tràn
đầu ra sẽ dừng tiến trình và không đánh dấu cài thành công. Hai lệnh `apt-get`
đầu script nay dùng timeout cố định 30 phút và kill-after 30 giây. Sau đó
bootstrap chọn `python3.13` hoặc `python3` từ các đường dẫn hệ thống tuyệt đối
(chỉ khi nó thực sự báo 3.13), kiểm tra được `numpy` import trước khi cài Rust,
rồi dùng đúng interpreter đó cho toàn bộ script; image khác Python 3.13 dừng
ngay với mã lỗi riêng. Các bước
`rustup component add` và kiểm tra compiler sau đó cũng dùng wrapper cùng giới
hạn. Output của apt chưa có hard cap trước khi Python được cài. Bootstrap Linux
thật chưa được chạy trên VM.

### Lệnh thực của protocol 2

Chuẩn bị hỗ trợ Python 3.11+; native cloud runner yêu cầu Linux/Python 3.13,
Rust 1.99.1 đúng pin, GNU time, NumPy và OpenSSL CLI cho TLS fixtures.

```text
python -B -m unittest cloud_experiments.test_campaign kaggle_experiments.test_protocol model.test_models cloud_experiments.service_lab -v
python -B -m cloud_experiments.contenders
python -B -m cloud_experiments.research inventory
python -B -m cloud_experiments.research prepare results/research-input --wall-seconds <bounded-seconds>
python3 -B -m cloud_experiments.research run-native <source> <spec.json> <output> --pilot-only --budget-file <reviewed-budget.json>
python3 -B -m cloud_experiments.research run-native <source> <spec.json> <output> --resume --budget-file <same-reviewed-budget.json>
python3 -B -m cloud_experiments.research analyze <output> <variant> <new-analysis.json>
python3 -B -m cloud_experiments.research run-service <service_probe> <new-output>
python3 -B -m cloud_experiments.research run-agent <core_probe> <new-output> --policy W2 --representation compact
```

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
riêng đã gắn hash key. Không retry/fallback tự động. Catalog chỉ chứng minh model
được liệt kê; inference, tool support, billed cost và quota thực vẫn cần xác nhận
từ provider. Operator acknowledgement không được coi là xác minh giá. Không ghi
key vào archive, tham số, log hay VM image. OpenRouter còn
là tùy chọn tường minh với `--provider openrouter` và `OPENROUTER_API_KEY`.
`run-agent` trả exit 2 khi task chưa đủ contract hoặc provider chặn; đó là bằng
chứng cần giữ, không sửa expected để xanh. Output mới không ghi đè output cũ.

### Tài nguyên, launch và diễn giải

Protocol 2 sở hữu output directory được chỉ định, source-only bundle ≤16 MiB,
mỗi fixture JSON ≤8 MiB, mỗi trường HTML ≤4 MiB, tổng fixture JSON ≤16 MiB,
raw ≤12 GiB, mỗi collection block ≤256 MiB. Các ngưỡng fixture được kiểm tra
trong forecast và trước khi tạo thư mục/ghi fixture; ID cũng phải là thành phần
tên tệp ASCII giới hạn trước khi dùng làm đường dẫn. Các ngưỡng này không giới
hạn bộ nhớ vật lý của parser hay filesystem. Mỗi block paired
chỉ xuất hiện sau khi đủ hai arm và receipt đã được fsync; resume ràng buộc nguồn,
binary, fixture, settings và session, rồi bỏ staging dang dở. Dự trù build/cache
16 GiB, archive export tối đa 16 GiB; cần ít nhất 48 GiB trống trước
chạy và giữ reserve 2 GiB. Đây là watchdog/admission cho output thuộc runner,
không phải filesystem quota cho mọi tiến trình. Chỉ tái dùng cache tương thích;
không tự xóa evidence, fixture hay dữ liệu tích lũy chưa xác định owner.

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
không phải billing cap; STOP vẫn giữ disk có chi phí. Forecast quá hạn phải HOLD
và thiết kế phân việc/giới hạn mới, không cắt gates hoặc samples sau khi thấy kết quả.

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
rights, byte/path bounds và holdout rules tại plan 18.4. Không cung cấp manifest
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
checks, process-tree cleanup on POSIX and Windows Job Objects, export receipts, supervisor retry state and
NIM request gating. These controls do not implement every H01–H14 owner or make a
broad campaign ready. Exact Linux/Python 3.13.5 and pinned Rust 1.99.1 checks,
service installation, Linux target-host descendant cleanup, disk admission and interrupted
run export still require current-snapshot cloud verification. Preparation checks
do not launch the benchmark campaign.

### Continuing a long confirmation analysis

`run-native` now uses atomic manifest publication through `ResearchRun`. An
application failure records the phase at failure; systemd `ExecStopPost` writes
an outcome receipt and the supervisor attempts to export finalized failures.
That path still needs target-host verification under termination/OOM. A successful pilot or screen-only
operator does not establish completed confirmation. Historical operator 010
finished its explicit screen-only scope; it did not crash after an hour.

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

JSON metadata uses a uniquely owned same-directory staging file, file fsync and
atomic replacement. Commit failure cleans only that staging file and leaves the
previous published JSON. Normal/cancelled process checks do not attest power
failure durability. Checkpoint hashes detect accidental corruption under the
trusted-runner assumption; same-OS result authenticity remains NOT VERIFIED.

These controls have local Windows/Python 3.11 coverage, including a killed real
child, a fresh-process restart and ManagedRun descendant cleanup through a
Windows Job Object. The Windows-specific checks live in
`test_windows_lifecycle.py` so the Linux no-skip suite does not turn a
platform-specific check into false platform evidence. A Linux
descendant-memory/reaping check now exists in the lifecycle suite. POSIX lock
behavior and the modified native entry point still require Linux/Python 3.13
target verification before a large job.
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
unprivileged research user. Also set and read back an absolute GCE STOP deadline
derived from the reviewed budget, including analysis/export/shutdown reserves.
VM stop retains the persistent disk; retrieval may require restarting it with a
fresh explicitly bounded retrieval window. Never delete retained evidence merely
because a local archive or outcome receipt exists.

One VM is prepared and currently stopped; a comprehensive campaign is **NOT
READY**. See plan 19.17–19.19 and the private checkpoint for exact whole-core
obligations, bounded-preflight evidence and cost assumptions. Remaining gaps
include missing language/browser/MCP adapters; H06 full inferred-header
semantics, representative table corpus and F04 integration; independent corpus,
aggregate load/recovery controls, verified billing facts, NIM inference
eligibility and the combined campaign executor. Native/service/agent commands
are separate subset entry points. An installed unit, mapped decision or passing
subset is not whole-core research evidence. No broad launch is authorized until
the user approves the exact reviewed launch receipt.
