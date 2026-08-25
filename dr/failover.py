"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)

    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }

    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")

    print(json.dumps(rec))
    return rec


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện failover theo đúng 5 bước bắt buộc."""

    # 1. Verify target
    try:
        response = httpx.get(f"{URL[target]}/v1/state", timeout=2.0)
        target_state = response.json()
    except Exception as exc:
        target_state = {
            "region": target,
            "error": type(exc).__name__,
        }

    emit(
        step="1_verify_target",
        target=target,
        state=target_state,
    )

    # 2. Restore snapshot
    meta = snapshot.get(target, backend)

    primary = "a" if target == "b" else "b"
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")

    rpo = snapshot.rpo(primary_db, restored_db)

    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        rpo_seconds=rpo["rpo_seconds"],
        docs_lost=rpo["docs_lost"],
        embed_model_version=meta["embed_model_version"],
    )

    # 3. Scale pool
    pool_state = pathlib.Path(f"state/region-{target}/pool_state")
    pool_state.write_text("full")

    emit(
        step="3_scale_pool",
        target=target,
        pool_state="full",
    )

    # 4. Wait until target is actually ready
    deadline = time.time() + wait
    last_reason = None

    while time.time() < deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=2.0)

            if response.status_code == 200:
                body = response.json()

                emit(
                    step="4_wait_ready",
                    target=target,
                    ready=True,
                    state=body,
                )
                break

            try:
                body = response.json()
                last_reason = body.get("reasons", f"http_{response.status_code}")
            except Exception:
                last_reason = f"http_{response.status_code}"

        except Exception as exc:
            last_reason = type(exc).__name__

        time.sleep(0.5)

    else:
        emit(
            step="4_wait_ready",
            target=target,
            ready=False,
            reason=last_reason or "timeout",
        )

        return {
            "ok": False,
            "target": target,
            "reason": "target_not_ready",
        }

    # 5. DNS cutover — ONLY after readiness succeeds
    active_region = pathlib.Path("edge/active_region")
    active_region.write_text(target)

    emit(
        step="5_dns_cutover",
        target=target,
        active_region=target,
    )

    return {
        "ok": True,
        "target": target,
        "state": body,
        "rpo_seconds": rpo["rpo_seconds"],
        "docs_lost": rpo["docs_lost"],
        "embed_model_version": meta["embed_model_version"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))