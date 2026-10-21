# Athena: nghiên cứu core trên Kaggle — study 2.1

Hướng dẫn ngày 16/10/2026. Người dùng tự khởi chạy Kaggle. Study 2.1 giữ wire format `athena-research/2`, bổ sung phép đo và khóa policy `paired-rounds/2.1` trong bundle trước lượt chạy mới. Không nâng dependency hoặc sửa production core.

Lượt `kaggle_results/v2-run-001` được giữ nguyên: c1 hoàn chỉnh theo policy 2.0, c2 thiếu GNU time, c3 bị guard nhầm đường dẫn root package. Không đổi policy rồi xét lại dữ liệu đó thành PASS. Các bộ `kaggle_results/c1`, `c2`, `c3` trước protocol v2 vẫn là legacy. Study 2.1 cần ba cụm mới cùng bundle; không ghép c1 cũ với c2/c3 mới.

## 1. Phạm vi có thể quyết định sau khi chạy

| Cụm | Phép so sánh và oracle | Số đo confirmation |
|---|---|---|
| c1 | Source baseline `8de5709e6c47fd57713ca821131579b2e8c25a36` và source hiện tại; cùng lockfile, compiler, profile và harness. BM25 đối chiếu công thức Python; bảng đối chiếu expected slots/error; AAE đối chiếu nội dung/hash/link và byte stream. | 7 công đoạn: rank, table, parse, export có/không query, pipeline có/không query. 20 cặp round × 250 mẫu/nhánh = **70.000 dòng**, p50/p95/p99. |
| c2 | Cùng hai source; 7 mức 100–20.000 links, kiểm parity và đủ link; DOM sâu kiểm đúng nội dung. | **70.000 mẫu** CPU; **280 mẫu** peak RSS từ process CLI mới bằng GNU time `%M`. RSS chỉ suy luận median và báo maximum quan sát, không công bố p99 từ 20 process/nhánh. |
| c3 | `rand 0.9.5 / rand_distr 0.5.1` và `0.10.3 / 0.6.0` trong hai bản sao scratch của source hiện tại. Compile consumer Bandit thật, chạy Rust tests/Clippy, kiểm 8 dạng Beta bằng moment/CDF và central variance có bound định trước. | 20 cặp/shape, 100.000 draws/process: **320 phép đo block**, 32 triệu draws. Metric là median ns/draw của block, không phải p99 từng draw. SingleFlight/cancellation và 17 điểm IP là kiểm tra chức năng riêng. |

Baseline đã được thay **có khai báo** Cargo.toml, Cargo.lock, toolchain và harness bằng bản chung để tách tác động source khỏi tác động dependency/security patch. `snapshots_meta.json` ghi hash trước/sau của các control; đây không phải executable nguyên trạng của commit cũ. `candidate_commit` kèm hash từng file vì workspace có thể chưa commit.

`export` có query thực hiện cả ranking/highlight; không dùng mức giảm của nó làm bằng chứng riêng cho serializer/hash. `export_no_query` bỏ query và parse sẵn ngoài khoảng đo. Đối chiếu hai export với `parse`, `pipeline_no_query` để khoanh vùng hồi quy; chênh lệch median giữa các stage không tự trở thành phân rã chi phí chính xác.

Mỗi bản nguồn dùng thư mục Cargo target riêng được chỉ định tường minh. c1/c2 còn có control khác biệt preformatted đã biết: baseline cũ và candidate phải thể hiện hai hành vi khác nhau ở control này; chỉ candidate phải giữ nguyên indentation. Control nằm ngoài corpus yêu cầu byte parity và ngoài số đo so sánh. Thiếu khác biệt thì dừng để tránh dùng nhầm binary/cache.

Các nhánh **chưa có comparator**: TreeSink/lol_html, pooling DNS/TLS, HTML table header integration, index dùng lại/top-K, neural rerank. Kết quả này có thể hỗ trợ quyết định nâng từng phần đã đo; không đủ tự chọn những nhánh đó. IP classifier không chứng minh SSRF containment, SingleFlight probe không đo HTTP throughput, quota logic không chứng minh hard cap RSS.

## 2. Chuẩn bị local, chưa chạy Kaggle

Mở PowerShell tại `C:\Users\ADMIN\Athena`:

```powershell
python -B -m unittest kaggle_experiments.test_protocol -v
python -B -m kaggle_experiments.pack_snapshots
python -B -m kaggle_experiments.generate_clusters --owner vnthanhphm
```

Thay `vnthanhphm` nếu tài khoản Kaggle của bạn khác. Hai lệnh cuối chỉ đóng gói source và tạo script, không upload/run. File nguồn cần sửa là `protocol.py`, `runner.py`, `probe.rs`, `src/bin/bench_core.rs`; ba `main.py` là file sinh tự động. Mọi thay đổi source/spec/harness phải đóng gói và sinh lại **cả ba cụm**; `bundle.json` định danh chung. Giữ bộ local này đến khi tải và phân tích xong; không sinh bộ khác giữa chừng.

Không lấy Python bytecode, `target`, `.git`, credentials hoặc kết quả cũ vào snapshot. Runner kiểm archive SHA-256, từng file, inventory và đường dẫn trước khi build. Đây là kiểm tra toàn vẹn/tái lập, không phải chữ ký chứng minh nguồn đáng tin.

Để kiểm chức năng local, `bench_core export_no_query 10 1 --verify-only` xuất fingerprint và chạy chức năng một lần, bỏ warmup/timer. Đây không phải mẫu benchmark và runner sẽ từ chối packet thiếu raw timing. Giữ regression trong `test_protocol.py`, không tạo nhiều file test tạm trong repo.

Hash `protocol.py`/`runner.py` trong bundle tính trên UTF-8 đã chuẩn hóa newline thành LF để nhúng; hash source trong archive, probe và binary tính trên bytes thực tế. Chuẩn hóa Python được ghi ở đây để tránh nhầm hash LF/CRLF khi chuyển máy.

Kaggle CLI phải được cài và xác thực trên máy của bạn; xem [hướng dẫn chính thức](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md). Bộ nghiên cứu không cài dependency Python mới. Phân tích có thể dùng NumPy sẵn có để nhanh hơn; nếu không có sẽ dùng stdlib cùng thuật toán và ghi backend, có thể lâu hơn.

Nếu CLI đã xác thực thì bỏ qua bước credential sau. Nếu chưa, dùng config riêng, không ghi đè khóa khác:

```powershell
$athenaKaggleConfig = 'C:\Users\ADMIN\.kaggle-athena-research'
if (Test-Path -LiteralPath $athenaKaggleConfig) { throw 'Thư mục đã tồn tại; chọn thư mục mới hoặc dùng cấu hình sẵn có.' }
New-Item -ItemType Directory -Path $athenaKaggleConfig
Copy-Item -LiteralPath 'C:\Users\ADMIN\Downloads\kaggle (2).json' -Destination (Join-Path $athenaKaggleConfig 'kaggle.json')
$env:KAGGLE_CONFIG_DIR = $athenaKaggleConfig
```

Không đưa khóa vào source, Notebook, dataset hoặc log. Việc tên tệp đã được tìm thấy không xác nhận khóa còn hợp lệ.

## 3. Bạn tự chạy Kaggle

Các metadata tạo **kernel private mới có hậu tố `-v21`**, CPU, internet bật để tải công cụ/crates trước khoảng đo; giữ kernel `-v2` cũ. Rust phải đúng **1.99.1**, không fallback sang stable. c2 chuẩn bị GNU `/usr/bin/time` bằng `apt-get` nếu thiếu, yêu cầu Linux/root; lưu lệnh/exit, version, hash executable và self-check RSS >0. Thiếu quyền, lỗi cài hoặc RSS không hợp lệ đều dừng; không thay bằng số 0 hay polling. CPU/image/quota thực tế được ghi sau khi chạy.

**Nên chạy screening trước** để kiểm hai lỗi môi trường/guard đã sửa với chi phí đo nhỏ:

```powershell
python -B -m kaggle_experiments.generate_clusters --owner vnthanhphm --screen
```

Lệnh chỉ sinh script, chưa chạy. Screening dùng kernel `-v21-screen`, 2 cặp × 5 iterations, RNG 1.000 draws. Không được dùng screening để chốt hiệu năng. Sau khi cả ba cụm screening kết thúc, tải giữ riêng bằng `python -B -m kaggle_results.pull_and_analyze --destination kaggle_results/v21-screen-001` (thêm `--diagnostics` nếu có lỗi). Sửa nguyên nhân trước confirmation, giữ cả lần lỗi.

**`kernels push` dưới đây sẽ upload và khởi chạy**, chỉ bạn thực hiện. Chạy từng cụm, kiểm status; đợi cụm trước kết thúc rồi chuyển cụm kế tiếp:

```powershell
kaggle kernels push -p kaggle_experiments/athena-c1-core-stages-20261015
kaggle kernels status ((Get-Content kaggle_experiments/athena-c1-core-stages-20261015/kernel-metadata.json -Raw | ConvertFrom-Json).id)
```

```powershell
kaggle kernels push -p kaggle_experiments/athena-c2-dom-rss-20261015
kaggle kernels status ((Get-Content kaggle_experiments/athena-c2-dom-rss-20261015/kernel-metadata.json -Raw | ConvertFrom-Json).id)
```

```powershell
kaggle kernels push -p kaggle_experiments/athena-c3-deps-concurrency-20261015
kaggle kernels status ((Get-Content kaggle_experiments/athena-c3-deps-concurrency-20261015/kernel-metadata.json -Raw | ConvertFrom-Json).id)
```

Một run có budget nội bộ 5.400 giây, mỗi subprocess có timeout riêng. Ngân sách Kaggle thực tế có thể ngắn hơn; timeout/OOM/compile lỗi được giữ là thất bại, không giảm số mẫu rồi PASS. c3 cố ý chỉ đổi hai dependency pin trong scratch: nếu API mới không compile cùng consumer thật, lưu lỗi để quyết định cần adapter/nghiên cứu tiếp; không sửa core để ép benchmark chạy.

c3 giữ bản Cargo.toml và Cargo.lock của lần thử ngay cả khi compiler thất bại sau resolve, cùng hash nguồn/probe và log exit. Nếu resolve thất bại thì lock có thể vẫn là bản cũ; thiếu dependency graph/binary không được hiểu là tuple mới đã build.

c3 so sánh metadata `(name, version, source)` thay vì coi Cargo package ID là đường dẫn có thể parse. Chỉ root đã xác minh cùng identity được phép đổi vị trí scratch; package/feature/dependency edge ngoài RNG closure vẫn phải giữ nguyên. Graph mới được lưu trước khi chạy guard để điều tra được nếu thất bại.

Sau khi kiểm screening và tải đủ artifact, sinh confirmation rồi lặp lại ba bước push/status ở trên:

```powershell
python -B -m kaggle_experiments.generate_clusters --owner vnthanhphm
```

Confirmation có bundle khác screening và kernel `-v21`. Không sinh lại script trong lúc đang chạy/tải một bundle.

## 4. Tải bằng chứng và phân tích

Khi cả ba kernel đều `COMPLETE`, tại đúng thư mục repo và đúng bundle đã upload:

```powershell
python -B -m kaggle_results.pull_and_analyze --destination kaggle_results/v21-run-001
```

Lệnh này **chỉ tải**. Thư mục đích phải mới; không trộn dữ liệu cũ. Đọc `pull_receipt.json` để lấy chính xác đường dẫn manifest từng cụm. Runner mới xuất vào `c1/athena-v21-c1`, `c2/athena-v21-c2`, `c3/athena-v21-c3`; dùng đường dẫn thực trong receipt nếu khác:

```powershell
python -B -m kaggle_results.verify_statistics --input kaggle_results/v21-run-001/c1/athena-v21-c1/samples.jsonl --manifest kaggle_results/v21-run-001/c1/athena-v21-c1/manifest.json --output kaggle_results/v21-run-001/c1-analysis.json
python -B -m kaggle_results.verify_statistics --input kaggle_results/v21-run-001/c2/athena-v21-c2/samples.jsonl --manifest kaggle_results/v21-run-001/c2/athena-v21-c2/manifest.json --output kaggle_results/v21-run-001/c2-analysis.json
python -B -m kaggle_results.verify_statistics --input kaggle_results/v21-run-001/c3/athena-v21-c3/samples.jsonl --manifest kaggle_results/v21-run-001/c3/athena-v21-c3/manifest.json --output kaggle_results/v21-run-001/c3-analysis.json
python -B -m kaggle_results.verify_statistics --combine kaggle_results/v21-run-001/c1-analysis.json kaggle_results/v21-run-001/c2-analysis.json kaggle_results/v21-run-001/c3-analysis.json --output kaggle_results/v21-run-001/research-decision.json
```

Các lệnh phân tích chỉ đọc dữ liệu, không chạy benchmark. Mỗi output phải là file mới. Báo cáo dùng đường dẫn tuyệt đối và kiểm lại hash raw/manifest khi combine; di chuyển dữ liệu thì phân tích lại từ vị trí mới. Giữ nguyên ba artifact `manifest.json`, `samples.jsonl`, `run.log`, lockfile RNG mới nếu resolve thành công và receipt. Không chỉ gửi bảng summary.

Nếu kernel `ERROR`/`CANCELLED`, dùng thư mục mới và thêm `--diagnostics` vào lệnh tải sau khi cả ba đều kết thúc. Lệnh lưu chẩn đoán rồi trả lỗi; đó không phải approval. Nếu các cụm khác chưa chạy/kết thúc, tải riêng kernel lỗi bằng `kaggle kernels output <kernel-id> -p <thư-mục-mới>`. Không rerun đến khi xanh rồi xóa lần lỗi; ghi nguyên nhân và giữ mọi run liên quan. Failed manifest không được phân tích như confirmation hoàn chỉnh; c1/c2 hoàn chỉnh vẫn có thể phân tích riêng nếu c3 thất bại.

## 5. Cách đọc kết quả và giao việc nâng core

Statistical family 2.1 khóa trước gồm **57 intervals của cả ba cụm**: c1=21, c2=28, c3=8. Phân tích mặc định **30.000 paired-cluster bootstrap draws**, seed 42, hai phía với Bonferroni family α=0,05; resample nguyên cặp round, không giả định 5.000 iterations độc lập. Quantile từ raw dùng nearest index, Python round ties-to-even. Các summary tương thích cũ từ binary dùng ceil index và không được dùng thay bộ phân tích.

Bonferroni kiểm soát multiplicity có điều kiện các interval thành phần hợp lệ; bootstrap vẫn là xấp xỉ, không bảo đảm coverage chính xác trên VM/20 blocks. Policy 2.1 báo tương quan **trong block** dưới dạng diagnostic vì resample giữ nguyên block. Tương quan **giữa round** của median mỗi nhánh hoặc delta quantile từng cặp đạt `|r_lag1| ≥ 0,5` vẫn tạo HOLD. Ngưỡng này là screening, thấp hơn không chứng minh độc lập. Không bỏ outliers. RSS maximum quan sát cao hơn baseline cũng tạo HOLD; không suy ra hard memory bound.

Policy cũ 2.0 giữ nguyên gate cả trong và giữa block. Manifest thiếu revision mới được đọc theo 2.0; revision/policy không khớp bị từ chối. Study 2.1 yêu cầu thêm `export_no_query`, gate `rss_tool`/`rng_dependencies`, family 57; combine từ chối trộn policy/bundle hoặc thiếu case. Không được thay trường metadata của run cũ để đạt policy mới.

RNG dùng Hoeffding cho mean, raw second moment và CDF; riêng Beta tập trung mạnh cần central variance để tránh mean/CDF vẫn đẹp dù variance sụp. Chia 20 nhóm, dùng median của trung bình `(X−μ)²`, Chebyshev trên moment bậc bốn Beta và binomial bound cho median, error budget chung 0,001 dưới mô hình iid. Công thức moment có regression đối chiếu số hữu tỷ chính xác, không kiểm bằng một bản sao cùng công thức. Đây là diagnostic hữu hạn, không chứng minh toàn bộ distribution hay độ an toàn mật mã.

- **PASS_PERFORMANCE:** với case đó, mọi upper bound candidate−baseline của metric đã khai báo ≤0 và ít nhất một <0; correctness/provenance đầy đủ, đủ mẫu, không có cờ HOLD. Đây là bằng chứng có phạm vi trên corpus đã đo.
- **REJECT:** có interval với lower bound >0. Giữ kết quả xấu; không bù p99 hồi quy bằng p50 đẹp.
- **HOLD:** interval cắt 0, screening/legacy, thiếu precision, dependence/drift hoặc quan sát RSS bất lợi. Không suy “không significant” thành tương đương.
- **READY_FOR_SCOPED_REVIEW:** cả nhóm case liên quan đạt, đủ để review kế hoạch thay đổi hữu hạn. RNG vẫn cần kiểm advisory của hai lockfile và chi phí dependency trước adoption. Báo cáo tổng luôn `HOLD` cho deployment; không tự merge/nâng core.

Đầu ra cho agent triển khai: chọn từng nhóm trong `decision_matrix`, chỉ rõ symbols/consumers, lợi ích đo được, correctness contract, phần chưa biết và check khi thay đổi. `NOT_RUN` yêu cầu comparator/corpus mới; không được đổi thành ACCEPT bằng lập luận lý thuyết.

Muốn xem lại c1 legacy bằng phương pháp mới mà không ghi đè báo cáo cũ:

```powershell
python -B -m kaggle_results.verify_statistics --legacy --input kaggle_results/c1/samples.jsonl --output kaggle_results/legacy-c1-reanalysis-002.json
```

Legacy không có provenance/gate v2 nên không được PASS. Ba file nguồn chung và một file `test_protocol.py` là nơi duy trì regression; không sinh thêm file test tạm vào repo cho mỗi phép đo.

## 6. Căn cứ công cụ

[GNU time `%M`: maximum resident set](https://www.gnu.org/software/time/manual/html_node/Memory-Resources.html), [cách đặt format](https://www.gnu.org/software/time/manual/html_node/Setting-Format.html), [apt-get flags](https://manpages.debian.org/bookworm/apt/apt-get.8.en.html), [Cargo metadata và opaque IDs](https://doc.rust-lang.org/cargo/commands/cargo-metadata.html), [Kaggle kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md), [Python subprocess](https://docs.python.org/3.13/library/subprocess.html), [NIST: Beta density, mean và variance](https://www.itl.nist.gov/div898/handbook/eda/section3/eda366h.htm). Bootstrap công cụ RSS, build và benchmark tuple RNG nâng cấp trên Kaggle còn chờ lượt chạy mới; test local không thay bằng chứng đó.
