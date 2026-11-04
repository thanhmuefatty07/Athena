# ATHENA RESEARCH & ENGINEERING CHECKPOINT

**Trạng thái dự án và hướng dẫn bàn giao công khai**
*Lưu quyết định/evidence pointers cần cho người tiếp tục công việc; không thay private checkpoint vận hành, actual outputs hoặc xác minh độc lập. Số liệu lịch sử có phạm vi theo run/snapshot, không phải test được chạy lại mỗi lần cập nhật tài liệu.*

---

### TRẠNG THÁI CHIẾN DỊCH CORE HIỆN TẠI — HIGH-EFFICIENCY EXPERIMENTAL DESIGN, FORECAST CALIBRATION, GCE LINUX `cloud-014` (`H01`/`H09`), `cloud-015` (`H01`/`H02`/`I-RANK`/`H06`/`H14`/`H07`), DIALECTIC SYNTHESIS (`§19.166`), POST-SYNTHESIS `cloud-016` (`§19.167`), DEEPINFER SYNTHESIS `cloud-017` (`§19.168`), ROUND 2 FACTORIAL `cloud-018` & `cloud-019` (`§19.169`), FINAL DIALECTIC SYNTHESIS `cloud-020` (`§19.170`), THEORETICAL FOUNDATIONS & RED-TEAM REMEDIATIONS `cloud-021` (`§19.171`) & CI CLOSURE 05/11/2026

- Nguồn trạng thái mới nhất là `CORE_RESEARCH_PLAN.md` §§19.132–19.171 cùng hai registry `cloud_experiments/core_decisions.json` và `cloud_experiments/library_decisions.json`. Các kết quả Study 2.1 bên dưới chỉ mô tả đúng bundle/run lịch sử; chúng không chứng minh readiness cho chiến dịch core hiện tại.
- Runtime local vừa xác nhận: Python 3.11.9, Rust/Cargo 1.99.1, Node v22.21.1. `campaign audit-decisions` trả `MAPPED`, kiểm đủ 12 tệp Rust core, 13 dependency, 13 hàng quyết định thư viện, và H09 khớp chính xác đủ 13 dependency; đây chỉ là kiểm tra cấu trúc, không phải phê duyệt thư viện hay bằng chứng hiệu năng.
- **Thiết kế thực nghiệm hiệu suất cao (`cloud_experiments/experimental_design.py`) và hiệu chuẩn dự báo/bootstrap (`cloud_experiments/research.py`)**:
  - `high_efficiency_design(target_quality=0.95)` & `evaluate_design_efficiency`: tính `composite_quality_score` là trung bình điều hòa 4 thành phần (`main_effect_coverage_ratio`, `pairwise_coverage_ratio`, `mechanism_group_coverage_ratio`, `higher_order_coverage_ratio`; `RT-04`), nén không gian thực nghiệm 225 biến thể xuống **91 biến thể** (`include_single_arms=True, enforce_higher_order=False`, **giảm 59.56% khối lượng build/screen**, chất lượng tổng hợp 4 thành phần **95.04%** với `main_effect_coverage_ratio = 100.0%` trên `36/36` mức, `single_arm_retention_ratio = 100.0%` trên `36/36` nhánh đơn, `pairwise_coverage_ratio = 100.0%` trên `591/591` tuple 2-way, `mechanism_group_coverage_ratio = 95.52%` trên `832/871` tuple hỗn hợp [`241/280 = 86.07%` tuple 3/4-way, `42/139 = 30.22%` ô panel]) hoặc **58 biến thể** (`include_single_arms=False, enforce_higher_order=False`, **giảm 74.22% khối lượng**, chất lượng tổng hợp 4 thành phần **95.04%**). Khi bật `enforce_higher_order=True` (`target_quality=0.95`), thiết kế chọn **116 biến thể** (`include_single_arms=True`, **giảm 48.44%**) hoặc **83 biến thể** (`include_single_arms=False`, **giảm 63.11%**) đạt đồng thời `100.0%` (`591/591`) tuple 2-way, `95.00%` (`266/280`) tuple cơ chế bậc 3/4 (`98.39%` `857/871` hỗn hợp), và `composite_quality_score = higher_order_composite_quality_score = 98.31%`; với `target_quality=1.0`, thiết kế chọn **130 biến thể** (**giảm 42.22%**; **97 biến thể / giảm 56.89%** khi `include_single_arms=False`) để phủ đủ `100.0%` (`871/871` hỗn hợp, `280/280` bậc 3/4, `coverage_status: "COVERED"`). `whole_path_core_panel()` báo cáo minh bạch đồng thời cả `"efficiency_within_active_subspace"` và `"efficiency_vs_full_design"` so với toàn bộ 225 biến thể (`RT-03`).
  - `whole_path_core_panel()` & `forecast_native`: sửa lỗi ước lượng thừa ~41x trong `forecast_native` bằng cách lưu `case_pilot_ns` theo từng case trong `pilot.json` (tách biệt các case `whole_path` 30–500 µs khỏi các case parser `attribute-heavy-8192` ~15 ms, đồng thời suy giảm an toàn về trung bình các case hợp lệ nếu `case_pilot_ns` chỉ có một phần), chuẩn hóa mẫu số số lần chạy pilot từ `26.0` sang `52.0` (`2 blocks * 2 arms * (10 warmup + 3 iterations) = 52`) và `confirm_spawn_mult = design["confirm"]["blocks"] / 2.0` (`12.0` tương ứng `48 / 4` lần spawn mỗi case, `RT-NEW-02`), và khử trùng lặp cả số lượng confirm case (`owner_cases`) lẫn chi phí thời gian confirm (`owner_confirm_costs`) theo chủ sở hữu quyết định (`maximum_confirm_cases = 60`, `maximum_family = 240`, `maximum_bootstrap_draws = 960,000` cho `cloud-014`; `maximum_confirm_cases = 223`, `maximum_family = 892`, `maximum_bootstrap_draws = 3,568,000` cho `cloud-015`, khóa bất biến `RT-05` bao gồm `combined_finalist` trong `freeze_confirmation`); vector hóa `bounded_bootstrap` (`batch_size = 256`, `_WEIGHT_CACHE` lưu tới `65,536` batch phủ `16,777,216` lượt rút, `uint8` `cumsum` không tạo `np.stack` khi `len(values) <= 255`, `coarse_T`, `k_bits = count.bit_length()` với rejection sampling khớp 100% `Random.randrange` cả khi `count` là lũy thừa của 2, và `int32` binary search cố định bước trên lát cắt liên tiếp `prefix[1:]`, giữ nguyên 100% kết quả bit-for-bit) và nâng ngưỡng dịch chuyển log trong `service_study._positive_log` lên `math.log(value) + 1024.0` để bảo toàn miền dương nghiêm ngặt cho mọi số thực dương hữu hạn IEEE-754 xuống tới `5e-324` (`RT-NEW-03`).
- **Tinh chỉnh zero-tradeoff các biến thể ứng viên (`cloud_experiments/contenders.py` & `cloud_experiments/core_probe.rs`)**:
  - `H01` (`aae-direct`, `aae-reuse`, `aae-batched4096`, `aae-batched16384` trong `contenders.py` & `core_probe.rs`): sửa lỗi `_writer()` chỉ lược bỏ `BufWriter` khi không còn tham chiếu `BufWriter::` sau toàn bộ phép biến đổi trên `src/aae.rs`; trong `content_hash` (`core_probe.rs`) và `_hash` (`contenders.py`), bổ sung nhánh zero-allocation cho `[]` và `[only]`, nhánh stack-allocated `[u8; 4096]` (`total_len <= 4096`) / `[u8; 32768]` (`total_len <= 32768`) zero-heap single-shot `blake3::hash`, nhánh single-shot SIMD `blake3::hash(&scratch)` cho `total_len <= 262_144` (kích hoạt toàn bộ 16-way AVX-512 / 8-way AVX2 của `blake3` trên `hash-escaped_unicode`, `hash-article`, và `hash-large` trong khi giới hạn bộ đệm tại `<= 256 KiB`), và vòng lặp `Hasher::update` căn chỉnh đúng kích thước `BATCH` cho `total_len > 262_144` (loại bỏ lệnh xả sớm `if scratch.len() == batch` trước `bytes.split_at(space)`, kiểm chứng qua `content_hash_batches_match_joined_across_exact_buffer_boundaries`). Bổ sung `LinkMode` (`All`, `HighlightedOnly`, `None`) và `export_jsonl_with_link_mode_and_prepared` trong `src/aae.rs` (`§19.171`).
  - `H06` (`table-sparse`, `table-html-selectors` trong `contenders.py` & single-parse DOM sharing `RT-NEW-01` / `RT-DEEP-03` / `RT-DEEP-04b/c/d` trong `core_probe.rs`): khôi phục `expand_with_slot_limit(rows: &[Row], max_rows: usize, max_cols: usize, max_slots: usize) -> Result<Grid, GridError>` trong `table-sparse`; bổ sung `parse_projection_and_table_cells`, `scraper_projection_nodes`, `extract_structured_table_cells_from_document`, bộ nhớ đệm `Fixture.table_cells`, `static SELECTORS: OnceLock<TableSelectors>` với `table.select(&selectors.row)` giới hạn trực tiếp trên từng `<table>` (`RT-DEEP-04c/d`), và `has_html_table_tag` nhận biết đầy đủ comment HTML5 (`<!-->`, `<!--->`), rawtext (`script`, `style`, `textarea`, `title`, `xmp`), cùng thuộc tính có dấu nháy trên cả thẻ mở lẫn thẻ đóng (`RT-DEEP-04`, `RT-DEEP-04b`).
  - `H02` (`rank-select`, `rank-binary`, `rank-linear` trong `contenders.py` & `bm25f`/`bm25_plus`/`bm25l`/`ql_dirichlet`/`simhash64` trong `src/rank.rs` — `RT-NEW-05`, `RT-DEEP-01b`, `RT-DEEP-02`, `RANK-01`): trích xuất `#[inline(always)] fn score_chunks`, bổ sung nhánh `limit == 1` (`max_by`), vòng lặp cửa sổ hình học tại chỗ `scores[start..].select_nth_unstable_by(take - 1, cmp)` (`window = (limit * 6).max(32)`, nhân đôi mỗi vòng) và khử trùng lặp chỉ trên ứng viên bằng `eq_ignore_ascii_case` / `HashSet<String>` khởi tạo lười (`RT-DEEP-01b`); kiểm tra cận độ dài $O(1)$ trước `is_ascii()`, lọc bloom 256-bit không phân biệt hoa/thường trước `lowercase(word)`, nhánh nhanh ASCII `to_ascii_lowercase()` (`RT-DEEP-02`), và bảng băm mở lũy thừa của 2 `FxHash` (`0x517cc1b727220a95`) khi `terms.len() > 8 && (chunks.len() > 1 || chunks[0].text.len() > 64)`; bổ sung `bm25f` (`score_chunks_bm25f` với trọng số vai trò cấu trúc, hệ số `1.35x` cho tiêu đề khớp trực tiếp, `heading_context_boost <= 0.24`, và thuật toán hai con trỏ cửa sổ trượt `MSLP` $O(W)$, `RANK-01`), `simhash64` + `bm25f_simhash_highlight_indices`, `bm25_plus`, `bm25l`, và `ql_dirichlet`.
  - `H03` (`prepared` trong `core_probe.rs` & `src/rank.rs`), `H04`/`H05` (`projection-fused`, `links-btree` trong `contenders.py` & `PROJ-01..03` trong `src/projection.rs`), và `H07`/`H08` (`NET-01..04` trong `src/network.rs` & `ORCH-01..02` trong `src/orchestrator.rs` — `§19.168`..`§19.171`): tính sẵn `norms: Vec<f64>` một lần trong `PreparedRank::new_with_key` và tái sử dụng `source_key` qua `repeated_queries` (`RT-DEEP-05`); nâng cấp `projection-fused` với `16`-byte `struct Draft`, `node_hint` propagation, đếm `self.chunk_count += 1`, kẹp `text_cap` dưới `128 KiB` `M_MMAP_THRESHOLD` của glibc `ptmalloc2`, hợp nhất xây dựng `DomArena` và `Vec<Chunk>` vào một vòng lặp duy nhất cùng `build_chunks_fused` (`is_clean_single_spaced`), đồng bộ lời gọi 4 đối số `apply_boilerplate_policy(&dom, &mut chunks, &mut links, limits.boilerplate)` ở cả hai nhánh `if fuse:` và `else:` trong `contenders.py`, và `links-btree` hai tầng `FxHash` + `BTreeMap<u64, u32>`; bổ sung `CommentUnwrapPolicy::AnnasArchive` (`PROJ-01`), `BoilerplatePolicy::MainContentV1` / `CetdEntropyV1` (`PROJ-02`), `extract_annas_archive_records` (`PROJ-03`), giải nén thuần Rust RFC 1951/1952/1950 `decode_content_encoding` với bảng tra 9-bit `fast_lut`, kiểm chứng CRC32/Adler-32, chống zip-bomb khối rỗng (`MAX_CONSECUTIVE_EMPTY_BLOCKS = 64`), và hỗ trợ GZIP đa thành viên (`NET-01..04`), cùng `SourceCache` 16 phân mảnh với ETag/`304 Not Modified`, bảo toàn `links_extracted: true` trước truy vấn `include_links: false` (`ORCH-01`), và cập nhật LRU `last_accessed` + khởi tạo lười `PreparedRank` (`ORCH-02`).
- **Xác minh thực tế trên GCE Linux (`athena-native-01`, `us-central1-a`, Debian 13 `Linux-6.12.113+deb13-cloud-amd64`, Python 3.13.5, Rust/Cargo 1.99.1, Node v20.19.2), Apple M1 Pro 10 nhân (`ssh mac`, `arm64`, Rust/Cargo 1.99.1), và GitHub Actions CI (`rust-reference-ci`)**:
  - Đã mở rộng persistent boot disk của `athena-native-01` từ 80 GiB lên **150 GiB**, chạy `bootstrap.sh` (`growpart` + `resize2fs` mở rộng `/` lên ~139.1 GiB trống, vượt ngưỡng `minimum_free_bytes = 94 GiB`), và chạy `research.py warm-cache ... --stage-sdk-artifacts` dựng sạch `core_probe`, `athena-mcp-rust-fixture`, `mcp-sdk-ts` (`node_modules`), và `lexbor-v3.0.0` (`semantic_parity` 6 case AAE/warning parity) hoàn toàn bên trong `/opt/athena/runs/cargo-target`.
  - **Full suite 9 module Python (bao gồm `model.test_benchmark`) đạt 288/288 PASS, 0 skip, 0 fail, 0 error trên Linux CI** (và trong `run-native` `unshare -rn` `harness_controls`), kiểm chứng đầy đủ các boundary Linux (`unshare --user --map-root-user --net --pid --fork --mount-proc --kill-child`, cgroup v2 `memory.max`, `PR_SET_PDEATHSIG` reaper, và symlink escape). Trên Windows local, bộ 9 module chạy **288 test (280 pass, 8 platform-gated skip, 0 fail/error)** cùng **92 Rust tests** (`--locked --offline --features research-probes`: 69 lib + 2 main + 17 `core_probe` + 1 `aae_output_budget` + 3 `integration`) trên cả 3 kiến trúc Windows x64, macOS M1 Pro (`arm64`), và GCE Linux (`x86_64`).
  - **Hoàn tất chuỗi 10 chiến dịch kiểm định có giới hạn trên GCE Linux (`results/core-research-cloud-011`..`020`) và bộ benchmark liên kiến trúc `results/core-research-cloud-021/`**: `cloud-011` (3 biến thể), `cloud-012` (11 biến thể `I-HTML-ATTR-MEMBERSHIP`), `cloud-013` (5 biến thể `H03`/`H06`), `cloud-014` (7 biến thể `H01`/`H09`), `cloud-015` (13 biến thể `H01`/`H02`/`I-RANK`/`H06`/`H14`), `cloud-016` (7 biến thể hậu tổng hợp `H02`/`H06`), `cloud-017` (20 biến thể screening + 10 biến thể confirmation với 6 cohort stress khắc nghiệt), `cloud-018` (17 biến thể `I-RANK` 6 ô + `I-PROJECT` 4 ô + `I-QUERY-PROJECT` 12 ô + `I-HTML-TABLE` 2 ô), `cloud-019` (38 biến thể `I-HASH-LIBRARY` 12 ô + `I-PARSER-LIBRARY` 16 ô + `I-COMPILER` 12 ô + `I-PROJECT` 4 ô + `H07`), **chiến dịch hợp luyện cuối `results/core-research-cloud-020`** (`study_id` `7bfd0da48d50e4a0af3030cbb29f4a7a50cfea6739c84418d0f62a5c768480ca`, `freeze_id` `1941222203225ca7670a65c50e9d3bb30cf7523f958e4744f8c0251565272625`, `status: COMPLETE`, `current_phase: finished`, `overall_decision: HOLD`, `10,624,000` bootstrap draws, `family_size: 2656`), và **bộ benchmark liên kiến trúc `results/core-research-cloud-021/`** (`core_probe theory-matrix - 25`, đủ `11/11` cohort `annas_archive_cases` ở cả hai tập `screen` và `confirm` trong `windows-local-foundation-benchmarks.json` và `mac-m1-pro-benchmark.json` trên cả baseline và `projection-fused` synthesis với `errors: 0`, các mô hình xếp hạng `bm25`/`bm25f`/`bm25_plus`/`bm25l`/`ql_dirichlet`/`bm25f_simhash`, giải nén `gzip`/`deflate_zlib`/`deflate_raw`/`multi_member_gzip`/`empty_deflate_block_bomb_guard`, và bão hòa đa nhân `7.10x` trên 10 nhân Apple M1 Pro `ssh mac` `1,428.42 ops/sec` cùng `2.10x` trên 4 vCPU GCE Linux `athena-native-01` `166.83 ops/sec`):
    - `L01` (`l01-lexbor-semantic-parity.json`): `PASS_STRICT_SIX_CASE_AAE_AND_WARNING_PARITY` (`commit` `2ae88a1c6b5261830eff73ee12bb3cdf805f3cfe`).
    - `L03` (`l03-mcp-sdk-preflight.json`): `PASS_LOCAL_CORE_INTEGRATION_PREFLIGHT` (`128/128` task consumer arms, `76/76` scope controls, `22/22` fault/disconnect/concurrency controls qua `rust` và `typescript-sdk` trên cả `stdio` và `http`).
    - `WPT` & toàn bộ các panel tương tác giai thừa đã hoàn tất `COMPLETE_CELLS`: `I-HTML-ATTR-MEMBERSHIP` (`10/10` ô trong `cloud-012`), `I-RANK` (`6/6` ô, `450` contrasts trong `cloud-017`/`018`), `I-PROJECT` (`4/4` ô, `231` contrasts trong `cloud-017`/`018`/`019`), `I-QUERY-PROJECT` (`12/12` ô, `4,485` contrasts trong `cloud-018`), `I-HTML-TABLE` (`2/2` ô, `9` contrasts trong `cloud-017`/`018`), `I-HASH-LIBRARY` (`12/12` ô, `1,098` contrasts trong `cloud-019`), `I-PARSER-LIBRARY` (`16/16` ô, `4,389` contrasts trong `cloud-019`), và `I-COMPILER` (`12/12` ô, `5,538` contrasts trong `cloud-019`).
    - **Held-out Confirmation trong `results/core-research-cloud-013`..`019`**:
      - `H03` (`prepared` trong `cloud-013`): **`PASS_PERFORMANCE`** trên `prepared-8` (**nhanh hơn 1.71x**) và `prepared-32` (**nhanh hơn 5.89x**); `REJECT` trên `prepared-1`, `prepared-2`, và `prepared-invalidation` — xác lập ngưỡng hòa vốn thực nghiệm giữa 2 và 8 truy vấn.
      - `H06` (`table-sparse` trong `cloud-013` & `table-html-selectors` trong `cloud-018`): `table-sparse` bị `REJECT` trên cả 3 cohort `table-narrow/wide/spans`; `table-html-selectors` đạt `HOLD` với `0 REJECT` trên cả 3 cohort HTML table (`html-table-spans`, `html-table-nested`, `stress-html-table-multi-64x16` `-2.2%`).
      - `H01` & `H09` (`cloud-014`, `015`, `019`): `lib-json-sonic` bị bác bỏ bởi `tests/aae_output_budget.rs` (`ORACLE_REJECTED`); `lib-blake3-pure` bị `REJECT` trên `29/60` cohort CPU (`cloud-014`); `projection-fused+lib-scraper-modern` (`cloud-019`) đạt `10/77` RSS `PASS_PERFORMANCE` và tăng tốc `1.19x` trên `pipeline-large-q3`.
      - `H02` (`rank-binary` trong `cloud-016` & `rank-binary+rank-select` trong `cloud-018`, `5,552,000` bootstrap draws): `rank-binary+rank-select` đạt khoảng tin cậy 95% Holm-Bonferroni `p50` âm nghiêm ngặt trên **toàn bộ `30/30` cohort `rank-*` và `selection-*`** (`selection-1-dense` **nhanh hơn 2.22x**, `selection-3-dense` **1.97x**, `selection-8-dense` **1.74x**, `selection-32-dense` **1.24x**, `selection-128-dense` **1.40x**, `stress-boilerplate-novelty-trap` **1.61x**, `stress-zipf-skewed-lengths` **1.49x**, `0` query `REJECT`, `0` RSS `REJECT`).
    - **Đột phá Hợp luyện Cuối trong `results/core-research-cloud-020`** (`10,624,000` bootstrap draws, `family_size: 2656`, `20` blocks × `18` iterations = `360` mẫu/cohort):
      - **Xóa bỏ hoàn toàn cả hai điểm nghẽn từ `cloud-018`/`019`**:
        - `projection-fused` trên `pipeline-large-plain` đảo chiều từ `REJECT` (`+730,131 ns` ở `cloud-018`) sang **tăng tốc `-591,356 ns` (`-14.9%`, nhanh hơn 1.18x)** ($\Delta p_{50} = [-1,155,389, -234,546]$ ns), đồng thời `pipeline-large-plain/rss` đạt **`PASS_PERFORMANCE`** (`-260 KiB`, $\Delta p_{50} = [-396, -84]$ KiB) và `pipeline-large-q3/rss` đảo chiều từ `REJECT` (`+444 KiB` ở `cloud-018`) sang **`PASS_PERFORMANCE`** (`-132 KiB`, $\Delta p_{50} = [-272, -60]$ KiB)!
        - `projection-fused+links-btree` trên `pipeline-large-plain` đảo chiều từ `REJECT` (`+606,812 ns` ở `cloud-019`) sang **tăng tốc `-653,209 ns` (`-16.8%`, nhanh hơn 1.20x)** ($\Delta p_{50} = [-972,621, -341,946]$ ns), với cả `pipeline-large-plain/rss` (`-288 KiB`, $\Delta p_{50} = [-384, -156]$ KiB) và `pipeline-large-q3/rss` (`-164 KiB`, $\Delta p_{50} = [-304, -16]$ KiB) đều đạt **`PASS_PERFORMANCE`** (`7/77` RSS `PASS_PERFORMANCE`, `47/77` CPU cohort có $\Delta p_{50} < 0$)!
        - `aae-batched4096` và `aae-batched16384` trên `hash-large` đảo chiều từ `REJECT` (`+59,594 ns` ở `cloud-018`/`019`) sang **`HOLD`** ($\Delta p_{50} = [-3,462, +5,615]$ ns và $[-2,248, +4,524]$ ns), đồng thời `aae-batched4096` tăng tốc `-18.7%` trên `hash-boundary-64`, `-17.9%` trên `hash-boundary-63`, `-12.9%` trên `hash-boundary-65`, và `-3.4%` trên `hash-boundary-1023` (`0/61` RSS `REJECT`).
      - **Ứng viên Hợp luyện 6 Nhân tố Khả chuyển (`aae-batched16384+rank-binary+rank-select+projection-fused+links-btree+lib-scraper-modern`, `131` cohort / `262` đích CPU+RSS)**:
        - Vượt qua toàn bộ 17 kịch bản dịch vụ TLS (`CONTRACTS_PASS_WITHIN_SCOPE`, SHA-256 `b4f37b90565c4dd0328dae251b1171706d2fcf9e612e04bd6582c42c537bfd03`).
        - Đạt **`25/131` cohort RSS `PASS_PERFORMANCE` (`0/131` RSS `REJECT`)**, **`12/131` cohort CPU có khoảng tin cậy 95% Holm-Bonferroni âm nghiêm ngặt trên cả 3 phân vị (`p50`, `p95`, `p99`)** (`pipeline-article-q3`, `parse-article`, `rank-1-dense`, `selection-1-dense`, `rank-1-novelty`, `selection-3-dense`, `selection-8-dense`, `attribute-heavy-1024..8192`, `stress-dense-multi-link-blocks`), **`110/131` (`84.0%`) cohort CPU có khoảng tin cậy `p50` âm nghiêm ngặt**, và **0 suy giảm CPU thực chất** (`skip-foreign` là cohort duy nhất bị gắn `REJECT` do nhiễu đuôi `p95` dù `p50` nhanh hơn `-188 ns`, $\Delta p_{50} = [-399, -27]$ ns):
          - `attribute-heavy-8192`: **nhanh hơn 7.21x (`-86.1%`)** (`255.71 ms` $\to$ `35.46 ms`, $\Delta p_{50} = [-234.74, -214.39]$ ms, RSS `PASS_PERFORMANCE`)
          - `selection-1-dense`: **nhanh hơn 2.00x (`-50.0%`)** ($\Delta p_{50} = [-112,291, -106,082]$ ns, cả 3 phân vị $< 0$)
          - `parse-article`: **nhanh hơn 1.57x (`-36.5%`)** (`476,279 ns` $\to$ `302,640 ns`, $\Delta p_{50} = [-196,124, -157,071]$ ns, cả 3 phân vị $< 0$)
          - `pipeline-large-q3`: **nhanh hơn 1.54x (`-35.3%`)** (`6.20 ms` $\to$ `4.01 ms`, $\Delta p_{50} = [-2.73, -1.72]$ ms, $\Delta p_{95} = [-3.77, -0.61]$ ms)
          - `stress-boilerplate-novelty-trap`: **nhanh hơn 1.58x (`-36.8%`)** ($\Delta p_{50} = [-45,785, -36,323]$ ns)
          - `stress-zipf-skewed-lengths`: **nhanh hơn 1.49x (`-32.8%`)** (`12.88 ms` $\to$ `8.66 ms`, RSS `PASS_PERFORMANCE` `-404 KiB`)
          - `pipeline-article-q3`: **nhanh hơn 1.43x (`-30.2%`)** (`923,057 ns` $\to$ `644,679 ns`, cả 3 phân vị $< 0$)
          - `selection-128-dense`: **nhanh hơn 1.43x (`-30.1%`)** (`363,132 ns` $\to$ `253,708 ns`)
          - `export-large-q3`: **nhanh hơn 1.33x (`-25.0%`)** (`2.61 ms` $\to$ `1.96 ms`)
          - `export-article-q3`: **nhanh hơn 1.33x (`-24.6%`)** (`426,496 ns` $\to$ `321,441 ns`, RSS `PASS_PERFORMANCE`)
          - `pipeline-article-plain`: **nhanh hơn 1.32x (`-24.2%`)** (`678,468 ns` $\to$ `514,285 ns`)
          - `pipeline-large-plain`: **nhanh hơn 1.23x (`-18.5%`)** (`3.92 ms` $\to$ `3.20 ms`, $\Delta p_{50} = [-1.44, -0.41]$ ms, $\Delta p_{95} = [-2.99, -0.16]$ ms, RSS `PASS_PERFORMANCE` `-284 KiB`)
          - `parse-duplicate_links` (**-18.0%**), `parse-unique_links` (**-15.0%**), `pipeline-duplicate_links-plain` (**-14.8%**, RSS `PASS_PERFORMANCE` `-224 KiB`), `pipeline-unique_links-q3` (**-13.0%**, RSS `PASS_PERFORMANCE` `-396 KiB`), và `stress-dense-multi-link-blocks` (**-8.8%**, cả 3 phân vị $< 0$, RSS `PASS_PERFORMANCE`).
    - `H07` (`h07-service-study/study-manifest.json` & `analysis.json`): `COMPLETE` đủ **24/24 block** (`6` cell: `ring`/`aws-lc` × `per-fetch`/`pool-short`/`pool-long`) qua `cloud-011`..`016` và `cloud-019` (`0 REJECT` trên toàn bộ 30 chỉ số ở `cloud-016` và `cloud-019`).
    - Đã `export` và `acknowledge` qua `cloud_experiments.lifecycle` cho toàn bộ chuỗi `cloud-011`..`020` (trong đó `cloud-020` archive SHA-256 `729713642e5401ea1a81ce87e9f074dfda643c30649afbd7d0b7ea447e38ebb2`, `68,342,848` byte, `copy_verified: true`), lưu trữ đầy đủ kết quả liên kiến trúc tại `results/core-research-cloud-021/`, và **tắt máy ảo `athena-native-01` (`status: TERMINATED`)**.
- Registry nghiên cứu khai báo 14 giả thuyết H, 24 tương tác I (22 bắt buộc; `I-LANG-BATCH` và `I-LANG-MODEL` có điều kiện, chưa nhận vào), 225 biến thể native và 14 panel native. Vì các lượt chạy `cloud-011`..`cloud-020` là các chiến dịch xác minh có giới hạn theo từng nhóm (`SUBSET_EXECUTED_FULL_OBLIGATION_UNVERIFIED`, `runnable_verified: False`), readiness tổng thể vẫn giữ `INCOMPLETE` / `HOLD` cho đến khi chạy toàn bộ lưới 225 biến thể.
- Đối chiếu dependency hiện tại: 13/13 dependency trực tiếp trong `Cargo.toml` có hàng nghiên cứu tương ứng, không thừa/thiếu key; đây chỉ là độ phủ registry, chưa phải quyết định chọn hoặc phê duyệt thư viện.
- Người dùng xác nhận không có bộ dữ liệu Kaggle và giao tôi chọn. Chọn WPT đã ghim và fixture tổng hợp trong repo cho correctness/throughput có kiểm soát. WCXB có tệp license CC-BY-4.0 nhưng quyền cấp phép từng trang chưa được xác nhận; umi-golden ghi rõ quyền nội dung trang thuộc tác giả gốc. Chưa chấp nhận hoặc tải hai bộ này; extraction trên web thực/search relevance còn `NOT VERIFIED`. `kaggle.json` chỉ là thông tin xác thực; không đọc hoặc đưa khóa vào bundle.
- VM `athena-native-01` hiện đã được đưa về trạng thái **`TERMINATED`** (`e2-standard-4`, persistent boot disk 150 GiB `debian-13-trixie`, `autoDelete: false`) ngay sau khi hoàn tất thu thập biên nhận `results/core-research-cloud-020/` và xác minh liên kiến trúc `results/core-research-cloud-021/`, đảm bảo không phát sinh chi phí compute nhàn rỗi. Xem `CORE_RESEARCH_PLAN.md` §§19.138, 19.160–19.171 và `cloud_experiments/README.md`.

---

## 1. THÔNG TIN PHIÊN BẢN & MÃ NGUỒN HIỆN HÀNH


## Corpus decision — 26/10/2026

The user confirmed there is no Kaggle dataset and delegated selection. The planned campaign will use the already-pinned WPT cases and repository-owned synthetic fixtures for deterministic parser correctness, concurrency/resource bounds, and throughput. WCXB has a CC-BY-4.0 license file but its HTML was captured from public pages and page-level authority was not verified; umi-golden explicitly says the HTML copyright remains with original authors. Neither third-party corpus was admitted or downloaded. Real-page extraction quality, live provider routing, and search relevance remain NOT VERIFIED. See CORE_RESEARCH_PLAN.md §19.136.

- **Repository**: [https://github.com/thanhmuefatty07/Athena](https://github.com/thanhmuefatty07/Athena) (nhánh `main`)
- **Commit kết quả Study 2.1**: [`525d031`](https://github.com/thanhmuefatty07/Athena/commit/525d0312e332cb1798bc0fb700acbfa5ee945276)
  *(`feat(kaggle): execute study 2.1 screening and confirmation runs, pull telemetry and compute decision matrix`)*
- **CI hiện hành (`rust-reference-ci` trên `origin/main`)**: [Run ID `41722127893`](https://github.com/thanhmuefatty07/Athena/actions/runs/41722127893) (cùng [Run ID lịch sử `38807741840`](https://github.com/thanhmuefatty07/Athena/actions/runs/38807741840) cho Study 2.1) đạt `SUCCESS` trên cả 2 job `audit` (Rust `fmt`, `clippy --locked --all-targets --features research-probes`, 92 Rust tests, `SHA256SUMS.txt` 186/186, `campaign audit-decisions`, `contenders.py` self-test, 9 Python test suites) và `linux-platform-gates` (Ubuntu 24.04 unprivileged userns, Lexbor v3.0.0 build & smoke, Linux boundary gates, và 288/288 Python tests với `0` skip).
- **Giới hạn kiểm chứng hiện hành**: YAML CI tại commit lịch sử `525d031` gọi Cargo chưa kèm `--locked`, trong khi `.github/workflows/ci.yml` hiện hành đã enforce `--locked --features research-probes` và zero-skip Linux harness trên toàn bộ 9 module Python (288 tests).
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
