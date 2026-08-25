"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import statistics
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)

    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }

    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")

    print(json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True

    answer = input(f"{msg} [y/N]: ").strip().lower()
    return answer == "y"


def latest_outage_ts(primary: str):
    """Lấy timestamp kill gần nhất của primary từ chaos log."""
    if not CHAOS_LOG.exists():
        return None

    latest = None

    for line in CHAOS_LOG.read_text().splitlines():
        if not line.strip():
            continue

        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if rec.get("action") == "kill" and rec.get("region") == primary:
            latest = rec.get("ts")

    return latest


def percentile95(values):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    idx = int(0.95 * (len(values) - 1))
    return round(values[idx], 1)


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực hiện đúng 7 bước của runbook."""
    started = time.time()

    # 1. Xác nhận outage
    primary_results = []
    target_results = []

    for _ in range(3):
        p_ready, p_reason = health_checker.probe(primary, 2.0)
        t_ready, t_reason = health_checker.probe(target, 2.0)

        primary_results.append(
            {
                "ready": p_ready,
                "reason": p_reason,
            }
        )

        target_results.append(
            {
                "ready": t_ready,
                "reason": t_reason,
            }
        )

        time.sleep(0.2)

    primary_failed = all(not r["ready"] for r in primary_results)

    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        target=target,
        primary_results=primary_results,
        target_results=target_results,
        outage_confirmed=primary_failed,
    )

    if not primary_failed:
        return {
            "ok": False,
            "reason": "primary_outage_not_confirmed",
        }

    # Semi-automatic confirmation
    if not confirm(
        auto,
        f"Region {primary} đã được xác nhận không ready. Failover sang region {target}?",
    ):
        return {
            "ok": False,
            "reason": "operator_cancelled",
        }

    # 2. Thông báo incident
    t_outage = latest_outage_ts(primary)
    t_operator = time.time()

    notification_delay = (
        None
        if t_outage is None
        else round(t_operator - t_outage, 2)
    )

    step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        t_outage=t_outage,
        t_operator=t_operator,
        notification_delay_s=notification_delay,
    )

    # 3. Gọi failover đúng MỘT lần
    result = fo.failover(target, backend, wait=60.0)

    step(
        3,
        "scale_gpu_pool",
        target=target,
        failover_ok=result.get("ok", False),
        failover_result=result,
    )

    # 4. Verify state replica
    target_state = result.get("state") or {}

    vectors = target_state.get("vectors") or {}

    step(
        4,
        "verify_state_replica",
        target=target,
        vectors_count=vectors.get("count"),
        weights_ok=(
            "model_weights_missing"
            not in (target_state.get("reasons") or [])
        ),
        target_state=target_state,
        rpo_seconds=result.get("rpo_seconds"),
        docs_lost=result.get("docs_lost"),
        embed_model_version=result.get("embed_model_version"),
    )

    # 5. Record cutover result
    step(
        5,
        "dns_cutover",
        target=target,
        ok=result.get("ok", False),
    )

    if not result.get("ok"):
        elapsed = round(time.time() - started, 2)

        step(
            7,
            "post_incident",
            ok=False,
            elapsed_s=elapsed,
            measure_command=(
                "python3 tools/measure_rto.py "
                "--loadgen reports/drill-2-withdr.jsonl "
                "--target-rto 300"
            ),
        )

        return {
            "ok": False,
            "target": target,
            "reason": result.get("reason"),
            "elapsed_s": elapsed,
        }

    # 6. Golden signals: 10 request thật vào target region
    latencies = []
    failures = 0
    responses = []

    for i in range(10):
        t0 = time.time()

        try:
            r = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"golden signal request {i}"},
                timeout=3.0,
            )

            latency_ms = round((time.time() - t0) * 1000, 1)
            latencies.append(latency_ms)

            ok = r.status_code == 200

            if not ok:
                failures += 1

            responses.append(
                {
                    "status": r.status_code,
                    "ok": ok,
                    "latency_ms": latency_ms,
                }
            )

        except Exception as exc:
            latency_ms = round((time.time() - t0) * 1000, 1)
            latencies.append(latency_ms)
            failures += 1

            responses.append(
                {
                    "status": None,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "error": type(exc).__name__,
                }
            )

    error_rate = failures / 10.0
    p95 = percentile95(latencies)

    step(
        6,
        "verify_golden_signals",
        target=target,
        requests=10,
        failures=failures,
        error_rate=error_rate,
        p95_latency_ms=p95,
        responses=responses,
    )

    # 7. Post incident
    elapsed = round(time.time() - started, 2)

    step(
        7,
        "post_incident",
        ok=True,
        elapsed_s=elapsed,
        measure_command=(
            "python3 tools/measure_rto.py "
            "--loadgen reports/drill-2-withdr.jsonl "
            "--target-rto 300"
        ),
    )

    return {
        "ok": True,
        "target": target,
        "elapsed_s": elapsed,
        "error_rate": error_rate,
        "p95_latency_ms": p95,
        "rpo_seconds": result.get("rpo_seconds"),
        "docs_lost": result.get("docs_lost"),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))