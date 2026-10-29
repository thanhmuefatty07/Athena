# ATHENA RESEARCH & ENGINEERING CHECKPOINT

**Trạng thái dự án và hướng dẫn bàn giao công khai**
*Lưu quyết định/evidence pointers cần cho người tiếp tục công việc; không thay private checkpoint vận hành, actual outputs hoặc xác minh độc lập. Số liệu lịch sử có phạm vi theo run/snapshot, không phải test được chạy lại mỗi lần cập nhật tài liệu.*

---

## TRẠNG THÁI CHIẾN DỊCH CORE HIỆN TẠI — GCE LINUX VERIFICATION & LOCAL AUDIT 28/10/2026

- Nguồn trạng thái mới nhất là `CORE_RESEARCH_PLAN.md` §§19.132–19.162 cùng hai registry `cloud_experiments/core_decisions.json` và `cloud_experiments/library_decisions.json`. Các kết quả Study 2.1 bên dưới chỉ mô tả đúng bundle/run lịch sử; chúng không chứng minh readiness cho chiến dịch core hiện tại.
- Runtime local vừa xác nhận: Python 3.11.9, Rust/Cargo 1.99.1, Node v22.21.1. `campaign audit-decisions` trả `MAPPED`, kiểm đủ 12 tệp Rust core, 13 dependency, 13 hàng quyết định thư viện, và H09 khớp chính xác đủ 13 dependency; đây chỉ là kiểm tra cấu trúc, không phải phê duyệt thư viện hay bằng chứng hiệu năng.
- **Xác minh thực tế trên GCE Linux (`athena-native-01`, `us-central1-a`, Debian 13 `Linux-6.12.113+deb13-cloud-amd64`, Python 3.13.5, Rust/Cargo 1.99.1, Node v20.19.2)**:
  - Đã mở rộng persistent boot disk của `athena-native-01` từ 80 GiB lên **150 GiB**, chạy `bootstrap.sh` (`growpart` + `resize2fs` mở rộng `/` lên ~139.1 GiB trống, vượt ngưỡng `minimum_free_bytes = 94 GiB`), và chạy `research.py warm-cache ... --stage-sdk-artifacts` dựng sạch `core_probe`, `athena-mcp-rust-fixture`, `mcp-sdk-ts` (`node_modules`), và `lexbor-v3.0.0` (`semantic_parity` 6 case AAE/warning parity) hoàn toàn bên trong `/opt/athena/runs/cargo-target`.
  - **Full suite 8 module Python trên Linux đạt 278/278 PASS, 0 skip, 0 fail, 0 error** (cả chạy độc lập và bên trong `run-native` `unshare -rn` `harness_controls`), kiểm chứng đầy đủ các boundary Linux (`unshare --user --map-root-user --net --pid --fork --mount-proc --kill-child`, cgroup v2 `memory.max`, `PR_SET_PDEATHSIG` reaper, và symlink escape). Trên Windows local, bộ 8 module chạy **278 test (270 pass, 8 platform-gated skip, 0 fail/error)**.
  - **Hoàn tất chạy kiểm định có giới hạn `results/core-research-cloud-011`** (`study_id` `a6a502ed90460a153c04629dbdb2b6deec9b624915a02a6098292b8c7287917c`, `source_sha256` `b130aa66000c7e705f247c0bffbcf7997880c786f0d9d71bd9950a6d58700575`, `status: COMPLETE`, `current_phase: finished`, `overall_decision: HOLD`, 3 biến thể) và **chiến dịch mở rộng 11 biến thể `results/core-research-cloud-012`** (`study_id` `fc820c7ad32a879d8033bddb82cfe51e558f6bf3138992804b78832806de3b1a`, `source_sha256` `9a17bdf3c5e7d8eecbbc775a92e691218dffc23e4c82ed52c2929370b57e9160`, `status: COMPLETE`, `current_phase: finished`, `overall_decision: HOLD`):
    - `L01` (`l01-lexbor-semantic-parity.json`): `PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY` (`commit` `2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe`).
    - `L03` (`l03-mcp-sdk-preflight.json`): `PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT` (`128/128` task consumer arms, `76/76` scope controls, `22/22` fault/disconnect/concurrency controls qua `rust` và `typescript-sdk` trên cả `stdio` và `http`).
    - `WPT`, `I-HTML-ATTR-MEMBERSHIP` (đủ 10/10 cell) & `I-HTML-TABLE` (đủ 2/2 cell) trong `results/core-research-cloud-012`: cả 10 báo cáo WPT (`baseline` + 9 biến thể `html-attrs-*`) đạt `SEMANTICALLY_IDENTICAL` trên 1.733 case; `screen-interactions.json` đạt `COMPLETE_CELLS` cho cả `I-HTML-ATTR-MEMBERSHIP` (57 contrast trên 19 cohort) và `I-HTML-TABLE` (6 contrast trên `html-table-nested` và `html-table-spans`); `selection.json` chọn `html-attrs-modern-vector` (`CONTRACTS_PASS_WITHIN_SCOPE` trên 17 kịch bản service) và xác nhận qua 6 macroblock (`confirmation-html-attrs-modern-vector.json`: `PASS_PERFORMANCE` tại `attribute-boundary-128`, `attribute-duplicate-128`, `attribute-boundary-31/32/33/rss`, `attribute-duplicate-32/rss`; `HOLD` trên 32 cohort còn lại; `0 REJECT`).
    - `H07` (`h07-service-study/study-manifest.json` & `analysis.json`): `COMPLETE` đủ **24/24 block** (`6` cell: `ring`/`aws-lc` × `per-fetch`/`pool-short`/`pool-long`), trong `cloud-012` đạt `PASS_PERFORMANCE` ở `provider-per-fetch` và `HOLD` ở 5 phép so sánh còn lại (`performance_decision: PER_COMPARISON_ONLY_NO_GLOBAL_WINNER`).
    - Đã `export` và `acknowledge` qua `cloud_experiments.lifecycle` (`export-receipt.json`, `export-ack.json`, `sha256` `b50e2951852e9277796cc917af3e7be673533cd6bef232cb65d5781bb8b00f6f`, `95,609,244` byte, `copy_verified: true`), tải toàn bộ biên nhận về `results/core-research-cloud-012/`, và **tắt máy ảo `athena-native-01` (`status: TERMINATED`)**.
- Registry nghiên cứu khai báo 14 giả thuyết H, 24 tương tác I (22 bắt buộc; `I-LANG-BATCH` và `I-LANG-MODEL` có điều kiện, chưa nhận vào), 225 biến thể native và 14 panel native. Vì lượt chạy `cloud-012` là chiến dịch xác minh có giới hạn 11 biến thể (hoàn tất 100% các cell của `I-HTML-ATTR-MEMBERSHIP` và `I-HTML-TABLE` cùng `L01`, `L03`, `H07`), `technical_coverage` ghi nhận `SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED` (`runnable_verified: False`) và readiness tổng thể vẫn giữ `INCOMPLETE` / `HOLD` cho đến khi chạy toàn bộ lưới 225 biến thể.
- Đối chiếu dependency hiện tại: 13/13 dependency trực tiếp trong `Cargo.toml` có hàng nghiên cứu tương ứng, không thừa/thiếu key; đây chỉ là độ phủ registry, chưa phải quyết định chọn hoặc phê duyệt thư viện.
- Người dùng xác nhận không có bộ dữ liệu Kaggle và giao tôi chọn. Chọn WPT đã ghim và fixture tổng hợp trong repo cho correctness/throughput có kiểm soát. WCXB có tệp license CC-BY-4.0 nhưng quyền cấp phép từng trang chưa được xác nhận; umi-golden ghi rõ quyền nội dung trang thuộc tác giả gốc. Chưa chấp nhận hoặc tải hai bộ này; extraction trên web thực/search relevance còn `NOT VERIFIED`. `kaggle.json` chỉ là thông tin xác thực; không đọc hoặc đưa khóa vào bundle.
- VM `athena-native-01` hiện đã được đưa về trạng thái **`TERMINATED`** (`e2-standard-4`, persistent boot disk 150 GiB `debian-13-trixie`, `autoDelete: false`) ngay sau khi hoàn tất thu thập biên nhận `results/core-research-cloud-012/`, đảm bảo không phát sinh chi phí compute nhàn rỗi. Xem `CORE_RESEARCH_PLAN.md` §§19.138, 19.160–19.162 và `cloud_experiments/README.md`.

---

## 1. THÔNG TIN PHIÊN BẢN & MÃ NGUỒN HIỆN HÀNH


## Corpus decision — 26/10/2026

The user confirmed there is no Kaggle dataset and delegated selection. The planned campaign will use the already-pinned WPT cases and repository-owned synthetic fixtures for deterministic parser correctness, concurrency/resource bounds, and throughput. WCXB has a CC-BY-4.0 license file but its HTML was captured from public pages and page-level authority was not verified; umi-golden explicitly says the HTML copyright remains with original authors. Neither third-party corpus was admitted or downloaded. Real-page extraction quality, live provider routing, and search relevance remain NOT VERIFIED. See CORE_RESEARCH_PLAN.md §19.136.

- **Repository**: [https://github.com/thanhmuefatty07/Athena](https://github.com/thanhmuefatty07/Athena) (nhánh `main`)
- **Commit kết quả Study 2.1**: [`525d031`](https://github.com/thanhmuefatty07/Athena/commit/525d0312e332cb1798bc0fb700acbfa5ee945276)
  *(`feat(kaggle): execute study 2.1 screening and confirmation runs, pull telemetry and compute decision matrix`)*
- **CI lịch sử**: [Run ID `38807741840`](https://github.com/thanhmuefatty07/Athena/actions/runs/38807741840) tại commit kết quả Study 2.1 có status Success; logs đã được đối chiếu ở lượt review trước: 41 unit + 2 integration Rust tests, fmt/Clippy/release build hoàn tất. Không phải CI của phần tài liệu đang dirty.
- **Giới hạn kiểm chứng hiện hành**: YAML CI tại commit lịch sử `525d031` gọi Cargo chưa kèm `--locked` (hiện tại `.github/workflows/ci.yml` đã enforce `--locked --features research-probes` và zero-skip Linux harness); không gắn nhãn logs lịch sử đó là đã enforce lock. Các số đếm Python/model/protocol trong báo cáo cũ không thay selected test IDs/assertions và actual output của lượt chuẩn bị mới.
- **Trust**: logs/hashes là bằng chứng process/integrity dưới giả định runner được tin cậy; `result_authenticity: NOT VERIFIED` khi chưa có collector độc lập ngoài quyền OS của test. Xem mục 12.3 của kế hoạch.

---

## 2. TRẠNG THÁI KIỂM ĐỊNH ĐÁM MÂY KAGGLE (STUDY 2.1)

- **Giao thức Thực nghiệm**: Protocol Version `athena-research/2` | Policy `paired-rounds/2.1`
- **Mã Định Danh Bundle Xác Thực**: `968c02f335cacfb997ed4866d78de84e2e92e221bf96c30ba84167ed2675fe45`
- **Tài khoản Thực thi**: `vnthanhphm` (Kaggle Cloud Linux 6.18, Python 3.13, Rust 1.99.0)

### Tiến độ các Cụm Thực nghiệm:
| Cụm | Mục tiêu Thử nghiệm | Trạng thái Screening | Trạng thái Confirmation | Dữ liệu Lưu trữ Vĩnh viễn |
|---|---|:---:|:---:|---|
| **c1** | Các công đoạn Core Pipeline (Rank, Table, Parse, Export, Pipeline) | **COMPLETE** | **COMPLETE** | [`kaggle_results/v21-run-001/c1/`](kaggle_results/v21-run-001/c1/) |
| **c2** | Quy mô DOM Links & Đo Peak RSS bằng GNU `/usr/bin/time %M` | **COMPLETE** | **COMPLETE** | [`kaggle_results/v21-run-001/c2/`](kaggle_results/v21-run-001/c2/) |
| **c3** | Thẩm định Nâng cấp RNG `rand 0.10` & Kiểm tra Concurrency Singleflight | **COMPLETE** | **COMPLETE** | [`kaggle_results/v21-run-001/c3/`](kaggle_results/v21-run-001/c3/) |

---

## 3. MA TRẬN PHÁN QUYẾT KỸ THUẬT CHÍNH THỨC (RUN 4 - BUNDLE `3c4af54b...`)

Nguồn: [`kaggle_results/v21-run-004/research-decision.json`](kaggle_results/v21-run-004/research-decision.json)

| Phân hệ (Domain) | Phán quyết | Luận cứ & Dữ liệu Thực chứng (30.000 Bootstrap Draws, 57 Bonferroni Intervals) |
|---|:---:|---|
| **`cpu_core`** | **`HOLD`**<br>*(LIFTED FROM REJECT)* | • `table` tăng tốc **11.6x** ($0.72$ ms vs $8.34$ ms, $\Delta p_{50} = [-7.86, -7.43]$ ms).<br>• `rank` tăng tốc **3.0x** ($7.22$ ms vs $21.53$ ms, $\Delta p_{50} = [-14.83, -13.93]$ ms).<br>• `export` (có query) tăng tốc **2.48x** ($10.15$ ms vs $25.13$ ms, $\Delta p_{50} = [-15.36, -14.69]$ ms).<br>• `pipeline` (có query) tăng tốc **1.91x** ($17.79$ ms vs $34.07$ ms, $\Delta p_{50} = [-16.99, -15.64]$ ms).<br>• **`pipeline_no_query` đạt `PASS_PERFORMANCE`** ($10.18$ ms vs $12.08$ ms, $\Delta p_{50} = [-2.33, -1.56]$ ms, $\Delta p_{95} = [-3.45, -1.31]$ ms, $\Delta p_{99} = [-4.52, -0.73]$ ms — hoàn toàn $< 0$).<br>• **`export_no_query` đạt `HOLD`** ($2.51$ ms vs $2.60$ ms, $\Delta p_{50} = [-0.13, -0.03]$ ms, không còn bất kỳ suy giảm nào, triệt tiêu hoàn toàn cổng REJECT). |
| **`rng_upgrade`** | **`HOLD`** | Diagnostics Beta hữu hạn với family 2.240 checks, không chứng minh toàn bộ phân phối; graph tuple mới 206 packages. Fresh advisory review tương ứng vẫn **NOT_RUN**, không có cơ sở ghi “0 lỗ hổng”. CI performance chưa đủ chốt ưu thế; giữ tuple cơ sở `rand 0.9.5 / rand_distr 0.5.1`. |
| **`resource_controls`** | **`UNIT_SUPPORTED_INTEGRATION_HOLD`** | Unit regressions hỗ trợ hợp đồng SingleFlight/Engine và cancellation trong phạm vi đã kiểm. Chưa có bằng chứng live DNS/TLS/egress hoặc HTTP throughput; không lấy số gộp truy vấn từ legacy làm kết quả integration 2.1. |
| **`alternative_parser`** | **`NOT_RUN`** | Chưa có comparator cho TreeSink / lol_html. |
| **`transport_reuse`** | **`NOT_RUN`** | Chưa có socket pooling comparator. |
| **`overall_decision`** | **`HOLD`** | Sẵn sàng cho **Bounded Core Implementation Review** (`READY_FOR_SCOPED_REVIEW`); không triển khai tự động khi chưa qua manual engineering review. |

---

## 4. KHO LƯU TRỮ VĨNH VIỄN & CẤU TRÚC ARCHIVE CỦA ATHENA

Toàn bộ tài sản kỹ thuật được bảo toàn trên đĩa và Git theo cấu trúc:

```text
Athena/
├── engineering-constitution.md       # Hiến chương kỹ thuật bất biến (Điều 1 - 5)
├── CHECKPOINT.md                     # Bảng ghi trạng thái checkpoint hiện tại
├── MASTER_BLUEPRINT.md               # Thiết kế tổng thể V2 (500+ dòng)
├── CORE_RESEARCH_PLAN.md             # Kế hoạch nghiên cứu thực nghiệm R1 - R9
├── ENGINEERING_REVIEW.md             # Báo cáo phản biện kỹ thuật sâu
├── SHA256SUMS.txt                    # Bảng băm toàn vẹn SHA-256
├── kaggle_experiments/               # Bộ công cụ sinh bundle và runner Kaggle
│   ├── protocol.py                   # Logic cổng kiểm định và statistical bootstrap
│   ├── runner.py                     # Kịch bản thực thi trên container Kaggle
│   ├── bundle.json                   # Mã băm bundle hiện hành (v21-run-004)
│   └── snapshots_meta.json           # Mã băm baseline & candidate tarballs
└── kaggle_results/                   # Lưu trữ vĩnh viễn kết quả thực nghiệm
    ├── v21-screen-001/               # Dữ liệu screening 2.1
    ├── v21-run-001/                  # c1 70k + c2 70k CPU, 280 RSS và c3 32M draws
    ├── v21-run-002/                  # c1 confirmation 2
    ├── v21-run-003/                  # v21-run-003 intermediate telemetry
    └── v21-run-004/                  # Dữ liệu chốt xác thực Run 4: triệt tiêu REJECT gate
        ├── c1-analysis.json          # 70.000 mẫu c1, 30.000 bootstrap draws
        ├── c2-analysis.json          # 70.000 mẫu c2, 280 peak RSS samples
        ├── c3-analysis.json          # 32M beta draws, 8 regime
        └── research-decision.json    # cpu_core: HOLD (READY_FOR_SCOPED_REVIEW)
```

---

## 5. KẾ HOẠCH HÀNH ĐỘNG TIẾP THEO (NEXT ACTION)

1. **Thực hiện Scoped Review cho phân hệ `cpu_core`** theo `engineering-constitution.md` và `MASTER_BLUEPRINT.md`. Cổng `cpu_core` đã chính thức nâng từ `REJECT` lên `READY_FOR_SCOPED_REVIEW` (toàn bộ 7 chỉ số c1 đều $\le 0$).
2. **Tiến hành nghiên cứu WP02 (Parser Comparator)**: Xây dựng benchmark so sánh có kiểm soát giữa `scraper/html5ever` và `lol_html`/`TreeSink` bounded scratch comparator mà không làm suy giảm memory safety hay entity decoding.
3. **Tiến hành nghiên cứu WP03 (Transport & DNS Pooling)**: Thiết kế socket connection reuse có nhận biết chính sách egress IP và DNS timeout trước khi đo lường HTTP throughput thực tế.

---

## 6. TIẾN ĐỘ THỰC THI & KẾT QUẢ EMPIRICAL (STUDY 2.1 CONFIRMATION RUN 4)

- **Bundle ID Xác Thực**: `3c4af54b9c87c8017bb1f65eaa7a7665696fe994c36dfc2f3e36ee37b43b4872`
- **Các Kernel Kaggle hoàn tất**:
  - `vnthanhphm/athena-c1-core-stages-20261015-v21` (Version 4): `KernelWorkerStatus.COMPLETE`
  - `vnthanhphm/athena-c2-dom-rss-20261015-v21` (Version 4): `KernelWorkerStatus.COMPLETE`
  - `vnthanhphm/athena-c3-deps-concurrency-20261015-v21` (Version 4): `KernelWorkerStatus.COMPLETE`
- **Kết quả Thống kê Đối chứng (Paired Bootstrap, Bonferroni $\alpha = 0.05$)**:
  - `table`: Baseline $8.34$ ms vs Candidate $0.72$ ms (**11.6x nhanh hơn**, $\Delta p_{50} = [-7.86, -7.43]$ ms) — **PASS_PERFORMANCE**
  - `rank`: Baseline $21.53$ ms vs Candidate $7.22$ ms (**3.0x nhanh hơn**, $\Delta p_{50} = [-14.83, -13.93]$ ms) — **PASS_PERFORMANCE**
  - `export`: Baseline $25.13$ ms vs Candidate $10.15$ ms (**2.48x nhanh hơn**, $\Delta p_{50} = [-15.36, -14.69]$ ms) — **PASS_PERFORMANCE**
  - `pipeline`: Baseline $34.07$ ms vs Candidate $17.79$ ms (**1.91x nhanh hơn**, $\Delta p_{50} = [-16.99, -15.64]$ ms) — **PASS_PERFORMANCE**
  - `pipeline_no_query`: Baseline $12.08$ ms vs Candidate $10.18$ ms (**$1.90$ ms nhanh hơn**, $\Delta p_{50} = [-2.33, -1.56]$ ms) — **PASS_PERFORMANCE**
  - `export_no_query`: Baseline $2.60$ ms vs Candidate $2.51$ ms (**$0.08$ ms nhanh hơn**, $\Delta p_{50} = [-0.13, -0.03]$ ms) — **HOLD (REJECT LIFTED)**
  - `parse`: Baseline $9.33$ ms vs Candidate $9.16$ ms — **HOLD** (trung tính, chưa thay đổi parser)
  - `c2` Peak RSS: Giảm $-1.55$ MB trên DOM 10k links và giảm $-2.17$ MB trên DOM 20k links — **PASS_PERFORMANCE**
  - `c3` RNG: Tất cả 8 regime phân phối Beta đều nằm trong giới hạn sai số chấp nhận — **HOLD**
- **Kết luận Kỹ thuật**:
  Cổng tắc nghẽn hiệu năng duy nhất của Athena Core V2 tại AAE serializer/hasher đã được xử lý triệt để mà vẫn bảo toàn 100% mã an toàn (`#![forbid(unsafe_code)]`), bảo toàn 100% hàm băm kiểm chứng oracle, vượt qua toàn bộ 43 unit/integration tests và 37 protocol tests. Phân hệ `cpu_core` chính thức đủ điều kiện bước vào giai đoạn Review Triển khai Có Phạm vi.
