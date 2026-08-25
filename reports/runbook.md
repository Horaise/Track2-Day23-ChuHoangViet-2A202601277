# Runbook 1 trang — Region chính down

Runbook xử lý sự cố khi Region A (primary) không còn khả năng phục vụ inference.
Mục tiêu: xác nhận outage, chuyển traffic an toàn sang Region B, kiểm tra dịch vụ,
đo RTO/RPO và chỉ rollback khi Region A đã thực sự ổn định.

> **Nguyên tắc:** không cutover traffic sang Region B trước khi `/readyz` của B trả HTTP 200.
> Failover mặc định là bán tự động để tránh flapping giữa hai region.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A không phục vụ được qua readiness check; xác nhận lỗi liên tiếp thay vì kết luận từ một probe duy nhất | On-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | Runbook ghi bước `thong_bao_incident` cùng timestamp vào `reports/runbook-run.jsonl`; operator xác nhận `y` để tiếp tục | On-call |
| 3 | Restore state ở Region B | Runbook tự gọi `failover.failover(...)`; kiểm tra bằng `cat reports/failover-events.jsonl` | Có event `2_restore_snapshot` với `rpo_seconds`, `docs_lost`, `embed_model_version` | On-call / Platform Engineer |
| 4 | Scale pool warm → full | Runbook tự ghi `full` vào `state/region-b/pool_state`; kiểm tra bằng `curl localhost:8002/readyz` | `/readyz` của B trả HTTP 200, vector DB có dữ liệu, model weights tồn tại và pool đã `full` | Platform Engineer |
| 5 | DNS/LB cutover | Runbook chỉ cutover sau khi B ready; kiểm tra bằng `curl localhost:8080/edge/state` | `active_region` là `b` sau khi hết edge TTL; `reports/failover-events.jsonl` có `5_dns_cutover` | Incident Commander |
| 6 | Verify golden signals | Runbook tự gửi 10 request thật; xem `reports/runbook-run.jsonl` | Event `verify_golden_signals` có 10 requests; mục tiêu error rate = 0 và p95 không vượt baseline/SLO đã thống nhất | On-call / SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `rto_verdict="PASS"` và có số đo `rpo_at_restore_s` / `docs_lost` để đưa vào postmortem | Incident Commander |

## Rollback — failover ngược về Region A

**Không tự động rollback chỉ vì Region A vừa sống lại.**

Chỉ trả traffic từ Region B về Region A khi:

1. Region A đã được restore và `/readyz` trả HTTP 200 ổn định qua nhiều lần kiểm tra.
2. Vector DB và model weights của Region A đã được xác nhận đầy đủ/tương thích.
3. Pool của Region A ở trạng thái `full` và warm-up đã hoàn tất.
4. Golden signals của Region A đạt yêu cầu: không có lỗi trong request kiểm tra và latency nằm trong baseline/SLO đã thống nhất.
5. Không còn dấu hiệu Region A tiếp tục flap hoặc tái diễn nguyên nhân outage.

**Quyền quyết định rollback:** Incident Commander.

On-call/Platform Engineer có trách nhiệm cung cấp bằng chứng readiness, state và golden
signals. Không tự ý đổi `edge/active_region` khi chưa được Incident Commander phê duyệt.

Khi rollback được phê duyệt:

```bash
printf a > edge/active_region
sleep 6
curl localhost:8080/edge/state
curl localhost:8080/v1/infer
```

Rollback hoàn tất khi edge báo active_region=a và inference qua edge thành công từ Region A.

Nếu Region A lại mất readiness, error rate tăng, hoặc inference thất bại sau rollback,
ngay lập tức trả traffic về Region B:

printf b > edge/active_region
sleep 6
curl localhost:8080/edge/state

Sau sự cố, ghi timeline và số đo thực tế vào reports/postmortem.md.