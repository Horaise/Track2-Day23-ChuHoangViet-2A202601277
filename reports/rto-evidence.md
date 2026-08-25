# RTO/RPO Evidence — Lab 23 (TEMPLATE — sinh viên điền bằng SỐ CỦA MÌNH)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | Không hợp lệ | Chaos command được gọi nhưng Region A không bị dừng thật | `chaos/chaos-events.jsonl` |
| Request fail đầu tiên | Không có | Không quan sát được request `ok:false` sau chaos | `reports/drill-1-nodr.jsonl` |
| Request thành công sau đó | Có | Region A tiếp tục phục vụ request | `reports/drill-1-nodr.jsonl` |
| RTO | Không đo được | Outage không xảy ra thực tế | `reports/drill-1-nodr.jsonl` |

**Kết luận Drill 1:** `INVALID` — chaos không tác dụng trên Region A, vì vậy không thể dùng lần chạy này làm evidence cho `NO_RECOVERY`.

## 2. Drill 2 — có DR

Chaos event gần nhất được phát tại:

- `t_outage = 1787661029.264704`
- ISO: `2026-08-25T12:30:29`

Tuy nhiên Region A không thực sự bị dừng. Request đầu tiên sau timestamp này xuất hiện khoảng **+0.29s**, vẫn trả HTTP 200 và vẫn được phục vụ bởi Region A.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:1` |
| Request đầu tiên sau t_outage | +0.29s | Request đầu tiên có `ts >= t_outage`; vẫn `ok:true`, phục vụ bởi A | `reports/drill-2-withdr.jsonl:54` |
| User thấy lỗi đầu tiên | Không có | Không có dòng `ok:false` trong toàn bộ drill | `reports/drill-2-withdr.jsonl:1-200` |
| Health check phát hiện Region A | Không có | Không có `to:UNHEALTHY, region:a` | `reports/health-events.jsonl` |
| Health check phát hiện Region B | Trước t_outage | B bị đánh dấu `UNHEALTHY` vì chưa ready | `reports/health-events.jsonl:1` |
| Snapshot restore xong | Không xảy ra | Runbook abort trước khi gọi failover | `reports/runbook-run.jsonl:1` |
| Region phụ ready | Không xảy ra | Không chạy đến failover Step 4 | `reports/runbook-run.jsonl:1` |
| DNS cutover | Không xảy ra | `outage_confirmed:false` | `reports/runbook-run.jsonl:1` |
| **RTO đo được** | **Không đo được** | Không tồn tại outage/recovery sequence hợp lệ | `reports/drill-2-withdr.jsonl:1-200` |

Runbook xác nhận Region A vẫn ready trong cả 3 probe:

- `ready:true`
- `ready:true`
- `ready:true`
- `outage_confirmed:false`

Evidence: `reports/runbook-run.jsonl:1`.

Trong khi đó Region B chưa sẵn sàng và trả HTTP 503 trong cả ba probe.

Evidence: `reports/runbook-run.jsonl:1`.

### Kết quả tổng hợp

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO — Inference API | Không đo được | 300s | **INVALID** |
| RPO — Vector DB | Không đo được tại restore | 300s | **INVALID** |
| Requests failed | 0 / 200 | Phải có failure sau outage | **INVALID** |
| Region phục vụ toàn bộ drill | A | Recovery phải từ B | **INVALID** |

Replication đã hoạt động bình thường.

Ví dụ snapshot đầu tiên của lần chạy mới:

- `snapshot_at = 1787660979.8209476`
- `latest_doc_ts = 1787660979.0388439`
- `embed_model_version = embed-model=vi-e5-base@v3`

Evidence: `reports/replication.jsonl:6`.

Các snapshot tiếp tục được tạo khoảng mỗi 30 giây, nên cơ chế snapshot/replication đã hoạt động. Phần thất bại nằm ở việc tạo outage, không phải replication.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

Do outage không xảy ra nên chưa thể phân rã **RTO đo được** thành bốn thành phần. Tuy nhiên cấu hình và các thành phần lý thuyết đã xác định được:

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | **15.0s** | `interval_s=5.0 × threshold=3` | Giảm interval hoặc threshold, đổi lại tăng probe traffic và nguy cơ phản ứng với failure ngắn |
| Snapshot restore | Chưa đo được | Không chạy đến `2_restore_snapshot` | Snapshot thường xuyên hơn và storage/restore nhanh hơn |
| GPU pool warm-up | Chưa đo được | Không chạy đến `4_wait_ready` | Giữ warm standby hoặc giảm thời gian khởi tạo compute |
| DNS/LB TTL cache | Chưa đo được | Không có `5_dns_cutover` | Giảm TTL nếu phù hợp với tải DNS/LB |

Cấu hình health checker:

```text
interval_s = 5.0
threshold = 3
detection_floor = 5 × 3 = 15 giây
```

## 4. Kết luận Evidence

Lần chạy hiện tại không được dùng để tuyên bố RTO ≤ 300s hoặc RPO ≤ 300s.

Nguyên nhân trực tiếp:

- Chaos script phát event kill.
- Bare backend sau đó không tìm thấy PID Region A.
- Region A tiếp tục phục vụ toàn bộ 200 request.
- Health checker không phát hiện A unhealthy.
- Runbook đúng thiết kế đã từ chối failover vì outage không được xác nhận.
- Vì không có restore/cutover/recovery thật nên RTO/RPO không tồn tại trong drill này.

Cần chạy lại drill trong môi trường mà netblock --mock thực sự dừng Region A trước khi dùng rto-evidence.md cho submission cuối.