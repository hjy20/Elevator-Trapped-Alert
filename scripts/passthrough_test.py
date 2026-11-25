"""进程内直通验收（无需端口、无需 asyncio）。

受限网络/沙箱下 asyncio 的 socketpair 会失败（WinError 10051），uvicorn 与 TestClient
都无法使用。本脚本直接调用 service.main 里的**同步端点函数**（health/report/alerts/
ack/stats/devices），覆盖与 scripts/smoke_test.py 完全一致的 11 项断言；
另提供 --replay：用 B 的 results/demo_trapped_decision.json 验证「1 入库 / N 去重 / 0 失败」。

用法：
  python scripts/passthrough_test.py            # 11 项服务端验收（等价 smoke_test 11/11）
  python scripts/passthrough_test.py --replay   # 回放直通（等价 play_case.py --no-video）
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import service.main as svc  # noqa: E402
from service.models import DecisionIn  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))


def _prepare() -> None:
    svc._load_db()
    svc._load_cache()


def run_smoke() -> None:
    device = f"ELEV-PASS-{int(time.time())}"

    h = svc.health()
    check("健康检查返回 ok", h.get("status") == "ok", f"db={h.get('db_mode')} cache={h.get('cache_mode')}")
    check("未处于降级状态", h.get("degraded") is False)

    now = int(time.time() * 1000)
    payload = {
        "ts": now,
        "device_id": device,
        "trapped": True,
        "urgency": 2,
        "location": "5F",
        "people_count": 1,
        "event_type": "trapped",
        "confidence": 0.9,
        "reason": ["dwell=200s > 180s", "door_state=closed"],
    }

    r1 = svc.report(DecisionIn(**payload))
    check("首次上报成功入库", bool(r1.get("id")), f"id={r1.get('id')}")

    r2 = svc.report(DecisionIn(**payload))
    check("30 秒内重复上报被去重", r2.get("dedup") is True, "同设备同紧急度只留一条")

    items = svc.alerts(status="active", limit=100)["items"]
    hit = [x for x in items if x["device_id"] == device]
    check("告警可被查询到", len(hit) == 1, f"命中 {len(hit)} 条")
    if hit:
        check("reason 已还原为数组", isinstance(hit[0]["reason"], list), str(hit[0]["reason"]))
        check("楼层与人数正确", hit[0]["location"] == "5F" and hit[0]["people_count"] == 1)

    if r1.get("id"):
        a = svc.ack(r1["id"])
        check("确认处置成功", a.get("ok") is True)
        items2 = svc.alerts(status="active", limit=100)["items"]
        check("处置后退出活动列表", all(x["id"] != r1["id"] for x in items2))

    s = svc.stats()
    check("统计数据可读", isinstance(s.get("active_count"), int), f"今日 {s.get('today_total')} 条")
    d = svc.devices()["items"]
    check("设备心跳已记录", any(x["device_id"] == device for x in d))


def run_replay() -> None:
    path = BASE_DIR / "results" / "demo_trapped_decision.json"
    if not path.exists():
        check(f"找到回放数据 {path.name}", False, "先跑 model/eval.py --real --demo")
        return
    items = json.loads(path.read_text(encoding="utf-8"))
    items.sort(key=lambda x: x["ts"])
    now = int(time.time() * 1000)
    base = items[0]["ts"]

    sent = created = deduped = failed = 0
    peak = 0
    device = f"ELEV-REPLAY-{int(time.time())}"
    for raw in items:
        item = dict(raw)
        item["ts"] = now + (item["ts"] - base)
        item["device_id"] = device
        try:
            body = svc.report(DecisionIn(**item))
        except Exception:  # noqa: BLE001 - 单条失败不影响统计
            failed += 1
            continue
        sent += 1
        peak = max(peak, int(item.get("urgency", 0)))
        if body.get("dedup"):
            deduped += 1
        elif body.get("id"):
            created += 1

    print(
        f"  回放 {path.name}: 发送 {sent} 条（失败 {failed}）；"
        f"入库 {created} 条；去重 {deduped} 条；最高紧急度 {peak}"
    )
    check("回放无失败请求", failed == 0, f"failed={failed}")
    check("去重后有入库", created >= 1, f"created={created}")
    check("重复上报被去重", deduped >= 1, f"deduped={deduped}")


def main() -> int:
    parser = argparse.ArgumentParser(description="进程内直通验收（纯函数层）")
    parser.add_argument("--replay", action="store_true", help="回放 demo_trapped_decision.json")
    args = parser.parse_args()

    print("=" * 56)
    print("  进程内直通验收（同步端点）" + (" · 回放" if args.replay else " · 服务端 11 项"))
    print("=" * 56)
    _prepare()
    if args.replay:
        run_replay()
    else:
        run_smoke()

    passed = sum(results)
    total = len(results)
    print("-" * 56)
    print(f"结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
