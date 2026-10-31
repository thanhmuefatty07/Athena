# ATHENA RESEARCH & ENGINEERING CHECKPOINT

**Trạng thái dự án và hướng dẫn bàn giao công khai**
*Lưu quyết định/evidence pointers cần cho người tiếp tục công việc; không thay private checkpoint vận hành, actual outputs hoặc xác minh độc lập. Số liệu lịch sử có phạm vi theo run/snapshot, không phải test được chạy lại mỗi lần cập nhật tài liệu.*

---

## TRẠNG THÁI CHIẾN DỊCH CORE HIỆN TẠI — HIGH-EFFICIENCY EXPERIMENTAL DESIGN, FORECAST CALIBRATION, GCE LINUX `cloud-014` (`H01`/`H09`) & `cloud-015` (`H01`/`H02`/`I-RANK`/`H06`/`H14`/`H07`), DIALECTIC SYNTHESIS ROUND (`§19.166`) & CI CLOSURE 31/10/2026

- Nguồn trạng thái mới nhất là `CORE_RESEARCH_PLAN.md` §§19.132–19.166 cùng hai registry `cloud_experiments/core_decisions.json` và `cloud_experiments/library_decisions.json`. Các kết quả Study 2.1 bên dưới chỉ mô tả đúng bundle/run lịch sử; chúng không chứng minh readiness cho chiến dịch core hiện tại.
- Runtime local vừa xác nhận: Python 3.11.9, Rust/Cargo 1.99.1, Node v22.21.1. `campaign audit-decisions` trả `MAPPED`, kiểm đủ 12 tệp Rust core, 13 dependency, 13 hàng quyết định thư viện, và H09 khớp chính xác đủ 13 dependency; đây chỉ là kiểm tra cấu trúc, không phải phê duyệt thư viện hay bằng chứng hiệu năng.
- **Thiết kế thực nghiệm hiệu suất cao (`cloud_experiments/experimental_design.py`) và hiệu chuẩn dự báo/bootstrap (`cloud_experiments/research.py`)**:
  - `high_efficiency_design(target_quality=0.95)` & `evaluate_design_efficiency`: nén không gian thực nghiệm 225 biến thể xuống **88 biến thể** (`include_single_arms=True, enforce_higher_order=False`, **giảm 60.89% khối lượng build/screen**, chất lượng tổng hợp **98.30%** với `main_effect_coverage_ratio = 100.0%` trên `36/36` mức, `single_arm_retention_ratio = 100.0%` trên `36/36` nhánh đơn, `pairwise_coverage_ratio = 100.0%` trên `591/591` tuple 2-way, `mechanism_group_coverage_ratio = 95.06%` trên `828/871` tuple hỗn hợp [`237/280 = 84.64%` tuple 3/4-way, `41/139 = 29.50%` ô panel]) hoặc **54 biến thể** (`include_single_arms=False, enforce_higher_order=False`, **giảm 76.00% khối lượng**, chất lượng tổng hợp **98.30%**). Khi bật `enforce_higher_order=True` (`target_quality=0.95`), thiết kế chọn **116 biến thể** (`include_single_arms=True`, **giảm 48.44%**) hoặc **83 biến thể** (`include_single_arms=False`, **giảm 63.11%**) đạt đồng thời `100.0%` (`591/591`) tuple 2-way, `95.00%` (`266/280`) tuple cơ chế bậc 3/4 (`98.39%` `857/871` hỗn hợp), `higher_order_composite_quality_score = 98.31%` (trung bình điều hòa 4 thành phần `RT-04`), và chất lượng tổng hợp **99.46%**; với `target_quality=1.0`, thiết kế chọn **130 biến thể** (**giảm 42.22%**; **97 biến thể / giảm 56.89%** khi `include_single_arms=False`) để phủ đủ `100.0%` (`871/871` hỗn hợp, `280/280` bậc 3/4, `coverage_status: "COVERED"`). `whole_path_core_panel()` báo cáo minh bạch đồng thời cả `"efficiency_within_active_subspace"` và `"efficiency_vs_full_design"` so với toàn bộ 225 biến thể (`RT-03`).
  - `whole_path_core_panel()` & `forecast_native`: sửa lỗi ước lượng thừa ~41x trong `forecast_native` bằng cách lưu `case_pilot_ns` theo từng case trong `pilot.json` (tách biệt các case `whole_path` 30–500 µs khỏi các case parser `attribute-heavy-8192` ~15 ms, đồng thời suy giảm an toàn về trung bình các case hợp lệ nếu `case_pilot_ns` chỉ có một phần), chuẩn hóa mẫu số số lần chạy pilot từ `26.0` sang `52.0` (`2 blocks * 2 arms * (1 warmup + 12 iterations) = 52`) và `confirm_spawn_mult = design["confirm"]["blocks"] / 2.0` (`3.0` tương ứng `12 / 4` lần spawn mỗi case, `RT-NEW-02`), và khử trùng lặp cả số lượng confirm case (`owner_cases`) lẫn chi phí thời gian confirm (`owner_confirm_costs`) theo chủ sở hữu quyết định (`maximum_confirm_cases = 60`, `maximum_family = 240`, `maximum_bootstrap_draws = 960,000` cho `cloud-014`; `maximum_confirm_cases = 223`, `maximum_family = 892`, `maximum_bootstrap_draws = 3,568,000` cho `cloud-015`, khóa bất biến `RT-05` bao gồm `combined_finalist` trong `freeze_confirmation`); vector hóa `bounded_bootstrap` (`batch_size = 256`, `_WEIGHT_CACHE`, `coarse_T`, `k_bits = count.bit_length()` với rejection sampling khớp 100% `Random.randrange` cả khi `count` là lũy thừa của 2, và `int32` binary search cố định bước trên lát cắt liên tiếp `prefix[1:]`, giữ nguyên 100% kết quả bit-for-bit) và nâng ngưỡng dịch chuyển log trong `service_study._positive_log` lên `math.log(value) + 1024.0` để bảo toàn miền dương nghiêm ngặt cho mọi số thực dương hữu hạn IEEE-754 xuống tới `5e-324` (`RT-NEW-03`).
- **Tinh chỉnh zero-tradeoff các biến thể ứng viên (`cloud_experiments/contenders.py` & `cloud_experiments/core_probe.rs`)**:
  - `H01` (`aae-direct`, `aae-reuse` trong `contenders.py`): sửa lỗi `_writer()` trước đó xóa cứng `BufWriter` khỏi `use std::io::{self, BufWriter, Write};` ngay cả khi `hash="joined"` vẫn dùng `BufWriter::with_capacity(8192, &mut content)` tại dòng 94 của `src/aae.rs` (ảnh hưởng 16/225 biến thể trong `native_design()`); nay chỉ lược bỏ `BufWriter` khi không còn tham chiếu `BufWriter::` sau toàn bộ phép biến đổi trên `src/aae.rs`, được kiểm chứng trên toàn bộ 225 biến thể trong `contenders._self_test()` và biên dịch chạy thật `cargo test --test aae_output_budget` + `cargo clippy -- -D warnings`.
  - `H06` (`table-sparse` trong `contenders.py` & single-parse DOM sharing `RT-NEW-01` trong `core_probe.rs`): khôi phục `expand_with_slot_limit(rows: &[Row], max_rows: usize, max_cols: usize, max_slots: usize) -> Result<Grid, GridError>`, theo dõi `row_widths` và `initialized_slots` lũy tiến trong vòng lặp đặt ô trước khi ghi `occupied: Vec<BTreeMap<usize, usize>>` (bảo toàn đúng thứ tự lỗi `TooManySlots` so với `Overlap` như `src/table.rs` và được kiểm chứng trực tiếp bằng `rustc --edition=2024 --test` trong `contenders._self_test()`); bổ sung `parse_projection_and_table_cells`, `scraper_projection_nodes`, `extract_structured_table_cells_from_document` và bộ nhớ đệm `Fixture.table_cells` trong `core_probe.rs` để các lượt xuất có bảng (`include_table_cells: true`) chỉ phân tích cây DOM HTML5 đúng một lần.
  - `H02` (`rank-select`, `rank-binary`, `rank-linear` trong `contenders.py` — `RT-NEW-05` + `cloud-015` crossover): bổ sung nhánh tuyến tính `limit == 1` (`max_by`), khử trùng lặp bằng `Cow<'_, str>` `lowercase`, đảo thứ tự kiểm tra độ dài từ trước khi hạ chữ hoa thành `if !(min_len..=max_len).contains(&word.len()) && word.is_ascii() { continue; }` (tránh bỏ sót các ký tự UTF-8 đa byte đổi độ dài khi `to_lowercase()` như `İ` `U+0130` -> `i\u{307}` đồng thời kiểm tra cận độ dài $O(1)$ trước `is_ascii()`), bổ sung bộ lọc bloom 256-bit trên stack cho byte đầu/cuối (`first_mask: [u64; 4]`, `last_mask: [u64; 4]`), và bổ sung cơ chế tra cứu lai thích ứng trong `lookup == "binary"` (`term_ids: Option<HashMap<&str, usize>>` khi `terms.len() > 8 && chunks.len() > 1`) để giữ nguyên tốc độ binary search không cấp phát `HashMap` cho truy vấn 1–8 từ và truy vấn thưa 1-chunk trong khi triệt tiêu suy giảm $O(|D| \log |Q|)$ trên truy vấn dày đặc 32–128 từ.
  - `H03` (`prepared` trong `core_probe.rs`) & `H04`/`H05` (`projection-fused`): cộng dồn trực tiếp trên `postings.get_mut` không cấp phát `BTreeMap` theo từng chunk, tính `norm = k1 * (1.0 - b + b * self.lengths[i] as f64 / avg_len)` trực tiếp tại điểm cộng điểm, cấp phát trước `String::with_capacity(chunk.text.len())` trong `collapse_ws`, và bỏ qua quét `last_owner` khi `chunk.links.len() <= 1`.
- **Xác minh thực tế trên GCE Linux (`athena-native-01`, `us-central1-a`, Debian 13 `Linux-6.12.113+deb13-cloud-amd64`, Python 3.13.5, Rust/Cargo 1.99.1, Node v20.19.2) và GitHub Actions CI (`rust-reference-ci`)**:
  - Đã mở rộng persistent boot disk của `athena-native-01` từ 80 GiB lên **150 GiB**, chạy `bootstrap.sh` (`growpart` + `resize2fs` mở rộng `/` lên ~139.1 GiB trống, vượt ngưỡng `minimum_free_bytes = 94 GiB`), và chạy `research.py warm-cache ... --stage-sdk-artifacts` dựng sạch `core_probe`, `athena-mcp-rust-fixture`, `mcp-sdk-ts` (`node_modules`), và `lexbor-v3.0.0` (`semantic_parity` 6 case AAE/warning parity) hoàn toàn bên trong `/opt/athena/runs/cargo-target`.
  - **Full suite 9 module Python (bao gồm `model.test_benchmark`) đạt 284/284 PASS, 0 skip, 0 fail, 0 error trên Linux CI** (và trong `run-native` `unshare -rn` `harness_controls`), kiểm chứng đầy đủ các boundary Linux (`unshare --user --map-root-user --net --pid --fork --mount-proc --kill-child`, cgroup v2 `memory.max`, `PR_SET_PDEATHSIG` reaper, và symlink escape). Trên Windows local, bộ 9 module chạy **284 test (276 pass, 8 platform-gated skip, 0 fail/error)** cùng **71 Rust tests** (`--locked --offline --features research-probes`).
  - **Hoàn tất chuỗi 5 chiến dịch kiểm định có giới hạn trên GCE Linux**: `results/core-research-cloud-011` (`study_id` `a6a502ed90460a153c04629dbdb2b6deec9b624915a02a6098292b8c7287917c`, 3 biến thể), `results/core-research-cloud-012` (`study_id` `fc820c7ad32a879d8033bddb82cfe51e558f6bf3138992804b78832806de3b1a`, 11 biến thể), `results/core-research-cloud-013` (`study_id` `b191b47549650647f107fa796d86341e7cbd8c07736dd021abb522ead63faaf7`, 5 biến thể), `results/core-research-cloud-014` (`study_id` `7178a62ec726de8ce9ff634c46f2a8db73474cc5e542c0c6d28b136cab8d70b5`, 7 biến thể), và **chiến dịch xác nhận 13 biến thể `H01` + `H02` (`I-RANK` 6/6 cell) + `H06` (`I-HTML-TABLE` 2/2 cell) + `H14` `results/core-research-cloud-015`** (`study_id` `ac204648746e0d2f3b0e570e68d013eba7f532d0ffcbf7add5abbdeffab08252`, `freeze_id` `6711b76b7874a03e498fdd48493af06781247bff04436e6e798285adc93ff87e`, `status: COMPLETE`, `current_phase: finished`, `overall_decision: HOLD`):
    - `L01` (`l01-lexbor-semantic-parity.json`): `PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY` (`commit` `2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe`).
    - `L03` (`l03-mcp-sdk-preflight.json`): `PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT` (`128/128` task consumer arms, `76/76` scope controls, `22/22` fault/disconnect/concurrency controls qua `rust` và `typescript-sdk` trên cả `stdio` và `http`).
    - `WPT`, `I-HTML-ATTR-MEMBERSHIP` (10/10 cell trong `cloud-012`), `I-RANK` (6/6 cell trong `cloud-015`, `COMPLETE_CELLS`, `438` contrasts) & `I-HTML-TABLE` (2/2 cell trong `cloud-012`, `cloud-013`, `cloud-014`, và `cloud-015`, `COMPLETE_CELLS`, `6` contrasts): báo cáo WPT đạt `SEMANTICALLY_IDENTICAL` trên 1.733 case; `confirmation-html-attrs-modern-vector.json` (`cloud-012`) đạt `PASS_PERFORMANCE` trên 6 cohort CPU/RSS và `HOLD` trên 32 cohort còn lại (`0 REJECT`).
    - **Held-out Confirmation trong `results/core-research-cloud-013`** (`288,000` bootstrap draws, `family_size: 72`, `6` macroblock):
      - `confirmation-prepared.json` (`H03`, `prepared`): **`PASS_PERFORMANCE`** trên `prepared-8` (**nhanh hơn 1.71x**, $\Delta p_{50} = [-792557, -724108]$ ns; RSS `HOLD`) và `prepared-32` (**nhanh hơn 5.89x**, $\Delta p_{50} = [-6204272, -5928908]$ ns; `prepared-32/rss`: `REJECT`, $\Delta p_{50} = [48, 256]$ KiB); `REJECT` trên `prepared-1`, `prepared-2`, và `prepared-invalidation` — xác lập ngưỡng hòa vốn thực nghiệm của `H03` nằm giữa 2 và 8 truy vấn trên mỗi tài liệu đã chuẩn bị.
      - `confirmation-table-sparse.json` (`H06`, `table-sparse`): `REJECT` trên cả 3 cohort `table-narrow` (CPU & RSS), `table-wide` (CPU), và `table-spans` (CPU) — bác bỏ thực nghiệm `Vec<BTreeMap<usize, usize>>` so với lưới đặc `Vec<Vec<Option<usize>>>` của baseline.
      - `confirmation-prepared+table-sparse.json` (`H03 + H06 + H14 + I-SYSTEM`): ứng viên kết hợp đa chủ sở hữu vượt qua toàn bộ 17 kịch bản hợp đồng dịch vụ TLS (`CONTRACTS_PASS_WITHIN_SCOPE`), đạt `PASS_PERFORMANCE` trên `prepared-8` và `prepared-32`, `HOLD` trên `html-table-spans` và `html-table-nested`, và `REJECT` trên `prepared-1/2/invalidation` cùng `table-narrow/wide/spans`.
    - **Screening & Held-out Confirmation toàn đường ống `H01`/`H09` trong `results/core-research-cloud-014`** (`960,000` bootstrap draws, `family_size: 240`, `24` blocks × `250` iterations across `60` whole-path cohorts):
      - **Loại bỏ tại cổng `cargo test` trước khi đo (`REJECTED_WITHIN_SCOPE` / `ORACLE_REJECTED`)**: `lib-json-sonic` (`sonic-rs 0.5.10`) bị bác bỏ bởi `tests/aae_output_budget.rs` (`over_budget_export_does_not_materialize_escaped_frame_or_joined_page_copy`) vì `sonic_rs::to_vec` cấp phát toàn bộ frame 17.4 MiB vào `Vec<u8>` (`temporary_peak >= 1 MiB`) trước khi `Output::write` kiểm tra `max_output_bytes`.
      - **`confirmation-lib-blake3-pure.json` (`H01` + `H09`)**: vượt qua toàn bộ 17 kịch bản dịch vụ TLS (`CONTRACTS_PASS_WITHIN_SCOPE`, SHA-256 `cf813ae44bf8db13e1dc0fd6b1e195bae874bc2fe2dd70b2912a0d4f0b440c46`), đạt `PASS_PERFORMANCE` trên `18/60` cohort RSS (`HOLD` trên `42/60` RSS, `0` RSS `REJECT`), nhưng bị **`REJECT` trên `29/60` cohort CPU** (`HOLD` trên `31/60` CPU; `performance_decision: "REJECT"`), khẳng định các tập lệnh SIMD/ASM của `blake3` mặc định là thiết yếu cho tốc độ băm/xuất AAE trên Xeon Broadwell x86_64.
    - **Screening & Held-out Confirmation 13 biến thể `H01` + `H02` (`I-RANK`) + `H06` (`I-HTML-TABLE`) + `H14` trong `results/core-research-cloud-015`** (`3,568,000` bootstrap draws, `family_size: 892`, `24` blocks × `250` iterations):
      - **Screening hoàn chỉnh toàn bộ 9 nhánh đơn `H01` và 6 ô `I-RANK` (`H02`)**: cả 13 biến thể trong `cloud_015_variants` vượt qua toàn bộ bài test `tests/aae_output_budget.rs` và oracle vi sai (`rejected_variants: {}`). Trên 60 cohort `H01`, thứ hạng screening là `["aae-batched4096", "aae-direct", "aae-buffered", "aae-batched16384", "aae-smallreserve", "aae-reuse"]` (trung vị `1.0213x` đến `1.2617x`); trên 73–126 cohort `H02`, thứ hạng screening là `["rank-binary+rank-select", "rank-binary", "rank-linear+rank-select", "rank-select", "rank-linear"]` (với `rank-binary+rank-select` trung vị `0.9988x`, `rank-linear+rank-select` trung vị `0.9904x`).
      - **`confirmation-aae-batched4096.json` (`H01`, `60` cohort)**: vượt qua 17 kịch bản TLS (`CONTRACTS_PASS_WITHIN_SCOPE`), đạt `PASS_PERFORMANCE` trên `2/60` cohort RSS (`pipeline-duplicate_links-q3/rss`, `link-scaling-4096/rss`), `HOLD` trên `58/60` RSS và `42/60` CPU, và `REJECT` trên `18/60` cohort CPU (`hash-article`, `hash-large`, `pipeline-article-plain`, `pipeline-article-q3`; `performance_decision: "REJECT"`), khép lại thực nghiệm rằng không nhánh đơn `H01` nào vượt qua `BufWriter<Output>` + single-slice `blake3` SIMD mặc định trên toàn bộ 60 cohort.
      - **`confirmation-rank-binary+rank-select.json` (`H02`, `73` cohort)**: vượt qua 17 kịch bản TLS (`CONTRACTS_PASS_WITHIN_SCOPE`), đạt **`PASS_PERFORMANCE` trên `13/73` cohort CPU** (`export-article-q3` **nhanh hơn 1.22x**, $\Delta p_{50} = [-80602, -73126]$ ns; `rank-1-dense` **nhanh hơn 1.60x**; `selection-1-dense` **nhanh hơn 1.67x**; `selection-3-dense` **nhanh hơn 1.63x**; `rank-8-dense` **nhanh hơn 1.55x**; `selection-8-dense` **nhanh hơn 1.59x**; `rank-32-sparse` **nhanh hơn 1.72x**; `selection-32-sparse` **nhanh hơn 1.69x**; cùng `rank-1-novelty`, `selection-3-sparse`, `rank-3-novelty`, `rank-8-sparse`, `selection-8-sparse`) và `2/73` cohort RSS, `HOLD` trên `48/73` CPU và `71/73` RSS, nhưng bị `REJECT` trên `12/73` cohort CPU có 32–128 từ truy vấn dày đặc (`rank-32-dense`, `selection-32-dense`, `rank-128-dense`, `selection-128-dense`; `performance_decision: "REJECT"`), định lượng chính xác đường ranh giới chuyển đổi giữa `rank-binary+rank-select` (vượt trội ở 1–8 từ truy vấn dày đặc và 3–32 từ truy vấn thưa) so với `BTreeMap` + full-sort của baseline (vượt trội ở 32–128 từ truy vấn dày đặc).
      - **`confirmation-aae-batched4096+rank-binary+rank-select.json` (`H01 + H02 + H14 + I-AAE + I-SYSTEM`, `90` cohort)**: ứng viên kết hợp đa chủ sở hữu vượt qua toàn bộ 17 kịch bản dịch vụ TLS (`CONTRACTS_PASS_WITHIN_SCOPE`, SHA-256 `dafbe479f2b3ca39377aa5b01dfc21b6290a6ab41338b43cea099b6564e53a68`), đạt **`PASS_PERFORMANCE` trên `15/90` cohort CPU** (bao gồm `pipeline-large-q3` **nhanh hơn 1.25x**, $\Delta p_{50} = [-1335834, -1089987]$ ns; `export-large-q3` **nhanh hơn 1.29x**, $\Delta p_{50} = [-615887, -547313]$ ns; `export-article-q3` **nhanh hơn 1.27x**, $\Delta p_{50} = [-93165, -86862]$ ns; `rank-128-sparse` **nhanh hơn 1.77x**, $\Delta p_{50} = [-9139, -8875]$ ns) và `3/90` cohort RSS, `HOLD` trên `52/90` CPU và `86/90` RSS, và `REJECT` trên `23/90` cohort CPU cùng `1/90` cohort RSS (`pipeline-large-q3/rss`; `performance_decision: "REJECT"`).
    - `H07` (`h07-service-study/study-manifest.json` & `analysis.json`): `COMPLETE` đủ **24/24 block** (`6` cell: `ring`/`aws-lc` × `per-fetch`/`pool-short`/`pool-long`), trong `cloud-015` đạt **`PASS_PERFORMANCE` trên cả `provider-per-fetch` và `provider-pool-short`** trên toàn bộ 5 chỉ số latency/CPU/RSS (`HOLD` trên 4 phép so sánh còn lại; `performance_decision: "PER_COMPARISON_ONLY_NO_GLOBAL_WINNER"`).
    - Đã `export` và `acknowledge` qua `cloud_experiments.lifecycle` cho `cloud-012` (`sha256` `b50e2951852e9277796cc917af3e7be673533cd6bef232cb65d5781bb8b00f6f`), `cloud-013` (`sha256` `2fe946aa66273c129bdedb7df10eaf727175671ac7236df89b85920ccf078051`), `cloud-014` (`sha256` `c6c4836588591df5cf5966e734499e4c7b0a55ae54dede24d4d706f0dc8c4f3f`), và **`cloud-015`** (`sha256` `3c8466f36240a13cb9db99ecf18d2f3ac6f60b97dedaafd5c900c328ea0e597e`, `154,205,802` byte, `copy_verified: true`), tải toàn bộ 22 biên nhận về `results/core-research-cloud-015/`, và **tắt máy ảo `athena-native-01` (`status: TERMINATED`)**.
- Registry nghiên cứu khai báo 14 giả thuyết H, 24 tương tác I (22 bắt buộc; `I-LANG-BATCH` và `I-LANG-MODEL` có điều kiện, chưa nhận vào), 225 biến thể native và 14 panel native. Vì các lượt chạy `cloud-011`..`cloud-015` là các chiến dịch xác minh có giới hạn theo từng nhóm (`SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED`, `runnable_verified: False`), readiness tổng thể vẫn giữ `INCOMPLETE` / `HOLD` cho đến khi chạy toàn bộ lưới 225 biến thể.
- Đối chiếu dependency hiện tại: 13/13 dependency trực tiếp trong `Cargo.toml` có hàng nghiên cứu tương ứng, không thừa/thiếu key; đây chỉ là độ phủ registry, chưa phải quyết định chọn hoặc phê duyệt thư viện.
- Người dùng xác nhận không có bộ dữ liệu Kaggle và giao tôi chọn. Chọn WPT đã ghim và fixture tổng hợp trong repo cho correctness/throughput có kiểm soát. WCXB có tệp license CC-BY-4.0 nhưng quyền cấp phép từng trang chưa được xác nhận; umi-golden ghi rõ quyền nội dung trang thuộc tác giả gốc. Chưa chấp nhận hoặc tải hai bộ này; extraction trên web thực/search relevance còn `NOT VERIFIED`. `kaggle.json` chỉ là thông tin xác thực; không đọc hoặc đưa khóa vào bundle.
- VM `athena-native-01` hiện đã được đưa về trạng thái **`TERMINATED`** (`e2-standard-4`, persistent boot disk 150 GiB `debian-13-trixie`, `autoDelete: false`) ngay sau khi hoàn tất thu thập biên nhận `results/core-research-cloud-015/`, đảm bảo không phát sinh chi phí compute nhàn rỗi. Xem `CORE_RESEARCH_PLAN.md` §§19.138, 19.160–19.165 và `cloud_experiments/README.md`.

---

## 1. THÔNG TIN PHIÊN BẢN & MÃ NGUỒN HIỆN HÀNH


## Corpus decision — 26/10/2026

The user confirmed there is no Kaggle dataset and delegated selection. The planned campaign will use the already-pinned WPT cases and repository-owned synthetic fixtures for deterministic parser correctness, concurrency/resource bounds, and throughput. WCXB has a CC-BY-4.0 license file but its HTML was captured from public pages and page-level authority was not verified; umi-golden explicitly says the HTML copyright remains with original authors. Neither third-party corpus was admitted or downloaded. Real-page extraction quality, live provider routing, and search relevance remain NOT VERIFIED. See CORE_RESEARCH_PLAN.md §19.136.

- **Repository**: [https://github.com/thanhmuefatty07/Athena](https://github.com/thanhmuefatty07/Athena) (nhánh `main`)
- **Commit kết quả Study 2.1**: [`525d031`](https://github.com/thanhmuefatty07/Athena/commit/525d0312e332cb1798bc0fb700acbfa5ee945276)
  *(`feat(kaggle): execute study 2.1 screening and confirmation runs, pull telemetry and compute decision matrix`)*
- **CI hiện hành (`rust-reference-ci` trên `origin/main`)**: [Run ID `41203870765`](https://github.com/thanhmuefatty07/Athena/actions/runs/41203870765) (cùng [Run ID lịch sử `38807741840`](https://github.com/thanhmuefatty07/Athena/actions/runs/38807741840) cho Study 2.1) đạt `SUCCESS` trên cả 2 job `audit` (Rust `fmt`, `clippy --locked --all-targets --features research-probes`, 71 Rust tests, `SHA256SUMS.txt` 186/186, `campaign audit-decisions`, `contenders.py` self-test, 9 Python test suites) và `linux-platform-gates` (Ubuntu 24.04 unprivileged userns, Lexbor v3.0.0 build & smoke, Linux boundary gates, và 283/283 Python tests với `0` skip).
- **Giới hạn kiểm chứng hiện hành**: YAML CI tại commit lịch sử `525d031` gọi Cargo chưa kèm `--locked`, trong khi `.github/workflows/ci.yml` hiện hành đã enforce `--locked --features research-probes` và zero-skip Linux harness trên toàn bộ 9 module Python (283 tests).
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
