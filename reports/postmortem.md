# Postmortem — DR Drill Lab 23 (TEMPLATE)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-08-25T12:30:29` | Chaos script ghi nhận yêu cầu outage Region A | `chaos/chaos-events.jsonl:1` |
| `+0.29s` | Request tiếp tục thành công từ Region A; không có user impact thực tế | `reports/drill-2-withdr.jsonl:54` |
| Không xảy ra | Health checker không đánh dấu Region A `UNHEALTHY` | `reports/health-events.jsonl` |
| `2026-08-25T12:30:38` | Runbook probe Region A 3 lần, cả 3 đều ready; `outage_confirmed:false` | `reports/runbook-run.jsonl:1` |
| Không xảy ra | Snapshot restore / GPU scale / DNS cutover không được thực thi | `reports/runbook-run.jsonl:1` |
| Trong toàn bộ drill | 200/200 request thành công và tiếp tục được Region A phục vụ | `reports/drill-2-withdr.jsonl:1-200` |

Replication vẫn hoạt động trong thời gian drill. Snapshot mới được tạo định kỳ khoảng 30 giây và sử dụng:
```text
embed-model=vi-e5-base@v3
```

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `Không đo được` · gap: `Không`
- RPO mục tiêu: 300s · đo được: `Không đo được` (`Không đo được` doc bị mất) · gap: `Không`
- **Bước tốn nhiều giây nhất:** `Tạo outage bằng bare chaos backend` — vì sao?
Drill không tạo được chuỗi sự kiện bắt buộc:

outage
  ↓
user failure
  ↓
health detection
  ↓
snapshot restore
  ↓
Region B ready
  ↓
DNS cutover
  ↓
recovery

Thay vào đó hệ thống quan sát được:

kill event
  ↓
Region A vẫn ready
  ↓
runbook abort

Do đó không thể tính RTO/RPO 

## 3. Root cause (5 whys)

Why 1 — Tại sao failover không xảy ra?

Vì runbook xác nhận Region A vẫn ready trong cả ba probe và trả về:

primary_outage_not_confirmed

Evidence:

reports/runbook-run.jsonl:1
Why 2 — Tại sao Region A vẫn ready sau chaos event?

Vì chaos script không thực sự dừng process Region A.

Sau khi ghi event kill, script báo:

khong tim thay PID cua region-a trong run

Traffic tiếp tục được Region A phục vụ bình thường.

Why 3 — Tại sao script không nhận được PID dù run/region-a.pid tồn tại?

up_bare.sh chạy Uvicorn trong background của Git Bash và lưu $!, trong khi kill_region.py dùng Python os.kill(pid, 0) và POSIX signal để xác minh/điều khiển process.

Trên môi trường Git Bash + Python native Windows, PID/process semantics giữa hai lớp không hoạt động giống môi trường POSIX thuần.

Why 4 — Tại sao vấn đề môi trường không được phát hiện trước drill?

Setup chỉ kiểm tra /healthz và edge state.

Các kiểm tra này chứng minh service đang chạy nhưng không xác minh rằng chaos backend có thể pause/resume chính PID đã ghi.

Không có pre-flight check:

PID file
  ↓
Python pid_of()
  ↓
signal capability

trước khi bắt đầu graded drill.

Why 5 — Tại sao điều này làm mất toàn bộ evidence?

RTO/RPO phải được suy ra từ một outage thực tế.

Khi chaos không có tác dụng:

không có failed request;
không có health transition của Region A;
không có failover;
không có DNS cutover;
không có recovery từ Region B.

Vì vậy mọi số RTO/RPO được suy ra trong tình trạng này đều không có ý nghĩa.

Root Cause

Bare-mode chaos process control không tương thích với môi trường Git Bash + Python native Windows đang sử dụng, và không có pre-flight validation để phát hiện vấn đề trước khi chạy drill.

## 4. Action Items

| # | Action | Owner | Deadline | Tác động đến RTO/RPO |
|---|---|---|---|---|
| 1 | Thêm pre-flight check xác nhận PID trong `run/region-a.pid` có thể được chaos backend điều khiển | Platform Engineer | 2026-08-26 | Ngăn sinh drill/evidence `INVALID` |
| 2 | Chạy lại graded drill trong môi trường có process/signal semantics tương thích với bare backend hoặc phương án Windows-compatible được cho phép | On-call / Platform Engineer | 2026-08-26 | Cho phép đo RTO/RPO thật |
| 3 | Chạy lại toàn bộ `ingest → replicate → loadgen → health checker → kill → runbook → measure_rto` từ trạng thái sạch | On-call | 2026-08-26 | Thu được RTO/RPO có thể kiểm chứng |
| 4 | Không sử dụng các số reference làm kết quả nếu chúng không xuất hiện trong log thực tế | Incident Commander | Trước submission | Đảm bảo evidence chính xác |

---

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
Cấu hình hiện tại:

```text
interval = 5s
threshold = 3
```

Do đó:

```text
Detection floor = 5 × 3 = 15 giây
```

Detection floor lý thuyết là **15 giây**.

Không thể tính phần trăm của RTO trong drill hiện tại vì **RTO không đo được**. Việc ghi một tỷ lệ phần trăm lúc này sẽ là số liệu không có evidence.

2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
Với threshold giữ nguyên bằng 3:

```text
Hiện tại:     5 × 3 = 15s
Sau thay đổi: 1 × 3 = 3s
```

Detection floor lý thuyết giảm:

```text
15 - 3 = 12 giây
```

Đổi lại:

- Số lượng health probe tăng khoảng 5 lần.
- Tăng tải lên serving API và network.
- Nhạy hơn với transient failure.
- Nguy cơ failover không cần thiết tăng nếu anti-flapping không đủ tốt.

Threshold 3 lần liên tiếp vẫn là lớp bảo vệ quan trọng chống flapping.

3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?
`docs_lost` là số document đã tồn tại ở primary nhưng chưa có trong snapshot/replica được restore.

Ở góc nhìn khách hàng, đó có thể là những dữ liệu đã được hệ thống chấp nhận trước sự cố nhưng không xuất hiện sau disaster recovery, ví dụ:

- Yêu cầu/ticket mới.
- Cập nhật dữ liệu.
- Record vừa ingest.
- Dữ liệu cần cho retrieval/inference.

Vì vậy RPO không chỉ là số giây. `docs_lost` biểu diễn trực tiếp **mức độ mất dữ liệu mà người dùng có thể quan sát được**.

Trong drill hiện tại chưa có restore nên `docs_lost` **chưa đo được**.