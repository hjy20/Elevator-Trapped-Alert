"""
一键验收：python scripts/smoke_test.py
（服务必须已在 18080 运行）

自动验证 8 项，用来替代手工 curl —— 答辩前跑一遍，全 ✓ 就能上场。
特别验证了「30 秒去重」，这是口头说不清、必须实测才可信的加分点。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18080"
DEVICE = "ELEV-SMOKE-001"
results = []


def call(method: str, path: str, body: dict | None = None, timeout: float = 5.0):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("=" * 56)
    print("  一键验收 · 电梯困人主动预警服务端")
    print(f"  目标 {BASE}")
    print("=" * 56)

    try:
        h = call("GET", "/api/v1/health")
    except (urllib.error.URLError, OSError) as exc:
        print(f"✗ 服务未启动：{exc}")
        print("  先运行：python -m service.main")
        return 1

    check("健康检查返回 ok", h.get("status") == "ok", f"db={h.get('db_mode')} cache={h.get('cache_mode')}")
    check("未处于降级状态", h.get("degraded") is False)

    now = int(time.time() * 1000)
    payload = {
        "ts": now,
        "device_id": DEVICE,
        "trapped": True,
        "urgency": 2,
        "location": "5F",
        "people_count": 1,
        "event_type": "trapped",
        "confidence": 0.9,
        "reason": ["dwell=200s > 180s", "door_state=closed"],
    }

    r1 = call("POST", "/api/v1/edge/report", payload)
    check("首次上报成功入库", bool(r1.get("id")), f"id={r1.get('id')}")

    r2 = call("POST", "/api/v1/edge/report", payload)
    check("30 秒内重复上报被去重", r2.get("dedup") is True, "同设备同紧急度只留一条")

    items = call("GET", "/api/v1/alerts?status=active")["items"]
    hit = [x for x in items if x["device_id"] == DEVICE]
    check("告警可被查询到", len(hit) == 1, f"命中 {len(hit)} 条")
    if hit:
        check("reason 已还原为数组", isinstance(hit[0]["reason"], list), str(hit[0]["reason"]))
        check("楼层与人数正确", hit[0]["location"] == "5F" and hit[0]["people_count"] == 1)

    if r1.get("id"):
        a = call("POST", f"/api/v1/alerts/{r1['id']}/ack")
        check("确认处置成功", a.get("ok") is True)
        items2 = call("GET", "/api/v1/alerts?status=active")["items"]
        check("处置后退出活动列表", all(x["id"] != r1["id"] for x in items2))

    s = call("GET", "/api/v1/stats")
    check("统计数据可读", isinstance(s.get("active_count"), int), f"今日 {s.get('today_total')} 条")
    d = call("GET", "/api/v1/devices")["items"]
    check("设备心跳已记录", any(x["device_id"] == DEVICE for x in d))

    total = len(results)
    passed = sum(results)
    print("-" * 56)
    print(f"结果：{passed}/{total} 通过")
    if passed == total:
        print("全部通过，可以截图发群 / 上台演示。")
        return 0
    print("有项目未通过，按上面 ✗ 的提示排查。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
