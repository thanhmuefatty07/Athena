# ATHENA ENGINEERING CONSTITUTION (HIẾN CHƯƠNG KỸ THUẬT ATHENA)

**Phiên bản:** 1.0.0-audited  
**Ngày ban hành:** 16/10/2026  
**Dự án:** Athena V2 — High-Throughput, Freshness-Aware Autonomous Retrieval Engine  
**Cơ sở thẩm quyền:** [MASTER_BLUEPRINT.md](MASTER_BLUEPRINT.md), [CORE_RESEARCH_PLAN.md](CORE_RESEARCH_PLAN.md), tiêu chuẩn **Proof-Driven Engineering**

---

## ĐIỀU 1: NGUYÊN TẮC CỐT LÕI (CORE INVARIANTS)

1. **Bằng chứng trên Tuyên bố (Evidence Over Assertions):**
   Mọi khẳng định về tốc độ, tối ưu hóa, an toàn bộ nhớ hoặc tính năng mới bắt buộc phải đi kèm với bằng chứng kiểm tra thực nghiệm độc lập (unit test, integration test, hoặc dữ liệu benchmark đám mây được lưu vết). Nghiêm cấm mọi hình thức tự chứng nhận chủ quan hoặc báo cáo "cảm tính".

2. **Chống Overclaims & Chống Thiên lệch (Anti-Overclaim & Anti-Bias):**
   - Không tuyên bố một thành phần là "nhanh hơn" hoặc "tối ưu tuyệt đối" nếu khoảng tin cậy thống kê còn cắt giá trị 0 hoặc thoái hóa ở các quantile đuôi (tail latency $p_{95}, p_{99}$).
   - Mọi thực nghiệm phải thiết kế theo khối ghép cặp luân phiên cân bằng (*Balanced Paired Alternating Blocks*), loại bỏ vòng khởi động (*warm-up discard*), và gắn luồng CPU cố định (*core pinning*) để triệt tiêu nhiễu môi trường.

3. **Bảo toàn An toàn Mạng & Ranh giới Thất bại (Safety & Security Boundaries):**
   - Nghiêm cấm truy cập các dải địa chỉ IP nội bộ/đặc thù theo bảng phân bổ của IANA (chống SSRF tuyệt đối).
   - Mọi tiến trình mạng bị hủy (cancellation) phải giữ nguyên permit tài nguyên đến khi luồng OS kết thúc hẳn, ngăn ngừa rò rỉ permit gây sập server.
   - Dữ liệu thu thập từ bên ngoài là untrusted: nghiêm cấm để nội dung trang can thiệp vào tầng điều khiển của AI Agent (chống Prompt Injection).

---

## ĐIỀU 2: PHÂN CẤP BẰNG CHỨNG (EVIDENCE HIERARCHY)

Mọi dòng tài liệu, đặc tả và báo cáo trong Athena phải được gắn nhãn minh bạch theo 5 cấp độ:

- **`[STD]` (Standard Specification):** Ràng buộc xuất phát từ các tiêu chuẩn Internet chính thức đã đối chiếu (RFC 9110, RFC 9111, RFC 9309, WHATWG HTML, Rust Reference).
- **`[DERIVED]` (Mathematical Derivation):** Công thức toán học suy diễn giải tích chính xác từ giả định đầu vào đã xác định rõ (Erlang-C, Decimal COGS, Hoeffding/Chebyshev bounds).
- **`[IMPLEMENTED]` (Code in Workspace):** Mã nguồn đã có trong kho lưu trữ, đã biên dịch và vượt qua bộ kiểm thử tự động (`cargo test`, `python unittest`).
- **`[MEASURE]` (Empirical Telemetry):** Số liệu thu thập từ môi trường đo lường thực tế (Kaggle Linux VM, GitHub Actions runner), lưu vết đầy đủ trong các tệp telemetry thô (`samples.jsonl`, `manifest.json`).
- **`[PROPOSED]` (Forward Architecture):** Thiết kế hoặc thuật toán đang trong giai đoạn nghiên cứu, chưa được triển khai hoặc chưa có benchmark đối chứng.

---

## ĐIỀU 3: QUY CHUẨN ĐO LƯỜNG & KHÓA AN TOÀN (BENCHMARKING & SAFETY GATES)

1. **Bộ Phân Tích Thống Kê Chuẩn Hóa:**
   - Sử dụng phương pháp **Paired-Cluster Bootstrap** với tối thiểu **30.000 lượt bốc thăm** (resamples), seed cố định để đảm bảo khả năng tái lập 100%.
   - Kiểm soát sai số tích lũy bằng hiệu chỉnh **Bonferroni đa biến** ($\alpha = 0.05 / M$) trên toàn bộ các khoảng tin cậy được khai báo đồng thời.

2. **Quy Tắc Phán Quyết Kỹ Thuật (Decision Rules):**
   - **`PASS_PERFORMANCE`**: Tất cả các cận trên (upper bounds) của khoảng tin cậy $\Delta = \text{Candidate} - \text{Baseline} \le 0$ và ít nhất một cận $< 0$; đồng thời vượt qua 100% các cổng kiểm tra tính đúng đắn (*correctness oracles*), không vi phạm tự tương quan hoặc rò rỉ bộ nhớ.
   - **`REJECT`**: Bất kỳ quantile nào ($p_{50}, p_{95}, p_{99}$) có cận dưới (lower bound) $> 0$ (chứng minh hồi quy hiệu năng rõ rệt). **Mã nguồn bị REJECT tuyệt đối không được merge vào core sản xuất.**
   - **`HOLD`**: Khoảng tin cậy delta cắt số 0 (không có bằng chứng vượt trội), hoặc vi phạm ngưỡng trôi dạt tự tương quan ($|r_{\text{lag-1}}| \ge 0.5$), hoặc quan sát thấy Peak RSS cực đại nhỉnh hơn baseline.
   - **`NOT_RUN`**: Các nhánh kỹ thuật chưa có mã nguồn so sánh trực tiếp (*scratch comparator*). Cấm dùng suy luận lý thuyết để tự động chuyển thành ACCEPT.

3. **Quy Tắc Khóa Cốt Lõi (Core Freeze Gate):**
   Quyết định tổng thể (`overall_decision`) phải được xác lập qua tệp `research-decision.json`. Nếu có bất kỳ thành phần con nào bị `REJECT`, toàn bộ quá trình tích hợp cốt lõi tự động chuyển sang trạng thái **`HOLD`**.

---

## ĐIỀU 4: GIAO THỨC LƯU TRỮ VÀ PHỤC HỒI CHECKPOINT (PERSISTENCE & RECOVERY)

1. **Độc lập khỏi Trạng thái Phiên (Session-Independent Persistence):**
   Mọi tiến trình nghiên cứu, kết quả thực nghiệm, mã băm tarball, snapshot và phán quyết kỹ thuật **không bao giờ được phép lưu trữ độc quyền trong bộ nhớ tạm (RAM) của mô hình AI hay phiên hội thoại**.
   Tất cả phải được lưu trữ vĩnh viễn dưới dạng tệp văn bản có quản lý phiên bản trên Git:
   - Thư mục lưu vết thực nghiệm: `kaggle_results/`
   - Siêu dữ liệu snapshot: `kaggle_experiments/snapshots_meta.json`
   - Mã định danh bundle: `kaggle_experiments/bundle.json`
   - Trạng thái checkpoint hiện hành: `CHECKPOINT.md`

2. **Quy Trình Phục Hồi sau Khởi Động Lại Hệ Thống (Server Restart Recovery):**
   Khi có thông báo hệ thống về việc khởi động lại máy chủ (*server restart notice*), tác vụ phải:
   - Đọc trực tiếp tệp `CHECKPOINT.md` và `git log` để xác định chính xác giai đoạn đang thực hiện.
   - Tuyệt đối không chạy lại từ đầu các cụm thực nghiệm đã có tệp kết quả và biên nhận (`pull_receipt.json`) nguyên vẹn.
   - Tiếp tục công việc từ mục "Kế hoạch Tiếp theo (Next Action)" đã được ghi nhận trong checkpoint.

3. **Toàn Vẹn Mã Băm (Cryptographic Integrity):**
   Mọi tập tin trong bộ nghiên cứu phải được ghi nhận mã băm trong `SHA256SUMS.txt`. Bất kỳ sự thay đổi mã nguồn nào không cập nhật bảng băm đều bị coi là vi phạm tính toàn vẹn và bị từ chối ở bước kiểm tra `source_integrity`.

---

## ĐIỀU 5: NGUYÊN TẮC QUẢN TRỊ MÃ NGUỒN & CI/CD

1. **Không Cảnh Báo (Zero-Warning Policy):**
   - Mọi bản build phải vượt qua `cargo clippy --all-targets -- -D warnings`.
   - Toàn bộ mã nguồn phải tuân thủ nghiêm ngặt định dạng `cargo fmt --all -- --check`.
   - 100% test suites (Rust unit tests, integration tests, Python mathematical tests, protocol tests) phải PASS trước khi tạo commit.

2. **Tính Tái Lập Khóa Phụ Thuộc (Deterministic Lockfile):**
   Mọi lượt chạy CI/CD và thực nghiệm đám mây phải sử dụng `cargo --locked` với tệp `Cargo.lock` đã được kiểm duyệt. Không tự ý nâng cấp dependency nếu không có sự ủy quyền rõ ràng và bài kiểm tra hồi quy độc lập.

---
*Hiến chương này có hiệu lực ngay lập tức và áp dụng bắt buộc cho toàn bộ các phiên làm việc, subagents và nhà phát triển tham gia vào dự án Athena.*
