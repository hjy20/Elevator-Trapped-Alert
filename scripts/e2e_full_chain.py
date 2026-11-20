"""三人代码的全链路联调：A 视觉 -> B 融合 -> C 服务端 -> 看板数据。

    # 1. 先起服务（另一个窗口）
    python -m service.main

    # 2. 再跑联调
    python scripts/e2e_full_chain.py

它做四件事，每一步都打勾/打叉：
  1. 用 A 的 `vision.infer` 把 5 段演示视频变成 `VisionOut`；
  2. 用 B 的 `model.imu_detect` 读同一段的 IMU，再用 `model.fuse` 合成 `Decision`；
  3. 把每个场景"最严重的那一条" `Decision` POST 到 C 的 `/api/v1/edge/report`；
  4. 查 `/api/v1/alerts`，确认告警真的落库了，并且 `reason` 被还原成数组。

时间戳会平移到当前时刻（和 `scripts/play_case.py` 同样的做法），
否则 2026-11 的历史数据不会出现在看板的"今日告警"里。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.schema import Decision  # noqa: E402
from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.fuse import fuse_with_config  # noqa: E402
from model.imu_detect import detect_imu  # noqa: E402
from vision.config import DEMO_DIR  # noqa: E402
from vision.infer import infer_case  # noqa: E402

CASES = ["demo_normal", "demo_estop", "demo_trapped", "demo_pry", "demo_faint"]
EXPECT = {
    "demo_normal": "normal",
    "demo_estop": "estop",
    "demo_trapped": "trapped",
    "demo_pry": "pry",
    "demo_faint": "faint",
}
BASE = "http://127.0.0.1:18080"


def call(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 8.0):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_worst(decisions: List[Decision]) -> Decision:
    """取紧急度最高的那条；同分取更晚的（判定更成熟）。"""
    return max(decisions, key=lambda d: (d.urgency, d.ts))


def shift_to_now(decision: Decision, anchor_ms: int, now_ms: int) -> Decision:
    payload = decision.__dict__.copy()
    payload["ts"] = now_ms - (anchor_ms - decision.ts)
    return Decision(**payload)


def main(argv: Optional[List[str]] = None) -> int:
    global BASE
    parser = argparse.ArgumentParser(description="A->B->C 全链路联调")
    parser.add_argument("--base", default=BASE)
    parser.add_argument(
        "--mode",
        choices=["demo", "production"],
        default="demo",
        help="demo=演示阈值(dwell 8s，20 秒片段能出告警)；production=生产阈值(dwell 180s)",
    )
    parser.add_argument("--cases", nargs="*", default=CASES)
    args = parser.parse_args(argv)

    BASE = args.base
    cfg = PRESET_DEMO if args.mode == "demo" else PRESET_PRODUCTION

    print("=" * 78)
    print("  全链路联调 · A 视觉 -> B 融合 -> C 服务端")
    print(f"  服务端 {BASE}")
    print("=" * 78)

    try:
        health = call("GET", "/api/v1/health")
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 服务端连不上（{type(exc).__name__}）。先在另一个窗口跑 python -m service.main")
        return 1
    print(f"[1/4] 服务端健康：{health.get('status')}  db={health.get('db_mode')} "
          f"cache={health.get('cache_mode')} degraded={health.get('degraded')}")
    print(f"      融合阈值：{args.mode}（dwell={cfg.thresholds.dwell_threshold:.0f}s）")

    rows: List[Dict[str, Any]] = []
    now_ms = int(time.time() * 1000)

    print(f"\n[2/4] A 的视觉 + B 的 IMU/融合（{len(args.cases)} 个场景）")
    for case in args.cases:
        video = DEMO_DIR / f"{case}.mp4"
        imu_path = DEMO_DIR / f"{case}_imu.npy"
        if not video.exists() or not imu_path.exists():
            print(f"  [x] {case}: 缺 {video.name} 或 {imu_path.name}")
            continue

        vision = infer_case(case)
        imu = detect_imu(str(imu_path))
        decisions = fuse_with_config(vision, imu, cfg)
        worst = pick_worst(decisions) if decisions else None
        anchor = max(vision[-1].ts, imu[-1].ts) if vision and imu else now_ms

        print(f"  {case:<14} VisionOut={len(vision):>4}  IMUEvent={len(imu):>3}  "
              f"Decision={len(decisions):>4}  "
              + (f"-> {worst.event_type}(urgency={worst.urgency})" if worst else "-> 无判决"))
        rows.append(
            {
                "case": case,
                "vision": len(vision),
                "imu": len(imu),
                "decision": len(decisions),
                "worst": worst,
                "anchor": anchor,
                "expect": EXPECT.get(case),
            }
        )

    print(f"\n[3/4] POST 到 C 的 /api/v1/edge/report（每场景一条最严重判决）")
    posted = 0
    for row in rows:
        worst: Optional[Decision] = row["worst"]
        if worst is None:
            continue
        payload = shift_to_now(worst, row["anchor"], now_ms)
        payload.device_id = f"ELEV-E2E-{row['case'].split('_')[-1].upper()}"
        body = {
            "ts": payload.ts,
            "device_id": payload.device_id,
            "trapped": payload.trapped,
            "urgency": payload.urgency,
            "location": payload.location,
            "people_count": payload.people_count,
            "event_type": payload.event_type,
            "confidence": payload.confidence,
            "reason": list(payload.reason),
        }
        try:
            resp = call("POST", "/api/v1/edge/report", body)
            row["post"] = resp
            posted += 1
            status = "ok" if resp.get("ok", True) else resp
            print(f"  ✓ {row['case']:<14} {body['event_type']:<8} urgency={body['urgency']} "
                  f"people={body['people_count']} -> {status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            row["post"] = {"error": detail}
            print(f"  ✗ {row['case']:<14} HTTP {exc.code}: {detail}")

    print(f"\n[4/4] 查询 /api/v1/alerts 确认落库")
    try:
        alerts = call("GET", "/api/v1/alerts?status=all&limit=50")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 查询失败：{type(exc).__name__} {exc}")
        return 1
    items = alerts.get("items") or alerts.get("alerts") or []
    by_device = {a.get("device_id"): a for a in items}
    hits = 0
    for row in rows:
        device = f"ELEV-E2E-{row['case'].split('_')[-1].upper()}"
        alert = by_device.get(device)
        if alert is None:
            print(f"  ✗ {row['case']:<14} 看板里查不到 {device}")
            continue
        reason = alert.get("reason")
        reason_ok = isinstance(reason, list) and len(reason) > 0
        hits += 1
        print(f"  ✓ {row['case']:<14} id={alert.get('id')} event={alert.get('event_type')} "
              f"urgency={alert.get('urgency')} reason={'数组 OK' if reason_ok else reason}")
        if reason_ok:
            print(f"      reason = {reason}")

    print("\n" + "=" * 78)
    print(f"  结果：{hits}/{len(rows)} 个场景的告警在看板上可查，POST 成功 {posted} 条")
    print("=" * 78)
    ok = hits == len(rows) and posted == len(rows)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())