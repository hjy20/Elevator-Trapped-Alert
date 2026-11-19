"""
造数据脚本：按时间轴把 results/decision_*.json 逐条 POST 到服务端。
用途：A/B 还没产出真数据时，让看板先动起来（D2-D3 验收用）。

用法：
  python scripts/mock_edge.py --case demo_trapped
  python scripts/mock_edge.py --file results/decision_trapped.json --speed 4
  python scripts/mock_edge.py --case demo_faint --no-wait     # 不按时间轴，立刻全发
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def post(base_url: str, payload: dict, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/edge/report",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rebase_ts(items: list) -> list:
    """把演示数据的绝对时间戳平移到当前时刻。
    原因：json 里的 ts 是固定基准，若原样发送，「今日告警数」会一直是 0、
    平均响应时长会算出天文数字。平移后相对间隔不变，时间轴行为完全一致。"""
    if not items:
        return items
    now = int(time.time() * 1000)
    base = items[0]["ts"]
    for it in items:
        it["ts"] = now + (it["ts"] - base)
    return items


def load_items(args) -> list:
    if args.file:
        path = Path(args.file)
    else:
        # 优先 B 的真实产出，其次 C 的合成数据（见 play_case.py 同款逻辑）
        candidates = [
            BASE_DIR / "results" / f"{args.case}_decision.json",
            BASE_DIR / "results" / f"decision_{args.case}.json",
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])
    if not path.exists():
        print(f"[FAIL] 找不到 {path}")
        print("       B 的产出未到位时，先跑：python scripts/make_demo_results.py")
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="把 Decision 序列回放到服务端")
    ap.add_argument("--case", default="demo_trapped", help="演示案例名，如 demo_trapped")
    ap.add_argument("--file", default="", help="直接指定 json 文件（优先于 --case）")
    ap.add_argument("--speed", type=float, default=1.0, help="倍速，2 = 两倍快")
    ap.add_argument("--no-wait", action="store_true", help="忽略时间轴，立刻全部发送")
    ap.add_argument("--keep-ts", action="store_true", help="保留 json 原始时间戳（默认平移到当前时刻）")
    ap.add_argument("--base-url", default="http://127.0.0.1:18080")
    args = ap.parse_args()

    items = load_items(args)
    items.sort(key=lambda x: x["ts"])
    if not args.keep_ts:
        rebase_ts(items)
    if not items:
        print("[FAIL] 数据为空")
        return 1

    print(f"共 {len(items)} 条，目标 {args.base_url}，倍速 {args.speed}x")
    t0 = time.time()
    base_ts = items[0]["ts"]
    sent = created = deduped = 0
    peak = 0
    failed = 0

    for it in items:
        if not args.no_wait:
            target = (it["ts"] - base_ts) / 1000.0 / max(args.speed, 0.01)
            delay = target - (time.time() - t0)
            if delay > 0:
                time.sleep(delay)
        try:
            r = post(args.base_url, it)
            sent += 1
            peak = max(peak, it["urgency"])
            if r.get("dedup"):
                deduped += 1
            elif r.get("id"):
                created += 1
            flag = "去重" if r.get("dedup") else ("入库" if r.get("id") else "心跳")
            print(f"  {time.strftime('%H:%M:%S')} urgency={it['urgency']} {it['event_type']:<8s} {flag}")
        except urllib.error.URLError as exc:
            failed += 1
            print(f"[FAIL] 请求失败：{exc}（服务是否已启动？）")
            if failed >= 3:
                print("连续失败 3 次，停止。请确认 python -m service.main 正在运行。")
                return 1

    print("-" * 52)
    print(f"发送 {sent} 条 · 入库 {created} 条 · 去重 {deduped} 条 · 最高紧急度 {peak}")
    print("打开 http://127.0.0.1:18080/ 查看看板")
    return 0


if __name__ == "__main__":
    sys.exit(main())
