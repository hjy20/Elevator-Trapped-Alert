"""
一键回放演示（答辩现场唯一会用到的东西）。

用法：
  python scripts/play_case.py --case demo_trapped
  python scripts/play_case.py --case demo_faint --speed 1.5
  python scripts/play_case.py --case demo_trapped --no-video     # 只发数据，调试用

做的事：播放 common/demo_data/<case>.mp4，同时按视频时间轴把
results/decision_<case>.json 逐条 POST 到服务端，让看板与视频同步弹告警。
视频不存在或未装 opencv 时自动退化为"只发数据"，不会中断演示。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

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


def post_session(base_url: str, payload: dict, timeout: float = 5.0) -> Optional[dict]:
    """画面会话登记（T-CAM-006）：失败返回 None，不影响既有回放行为。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/camera/session",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError:
        return None


class Counter:
    def __init__(self) -> None:
        self.sent = self.created = self.deduped = self.failed = 0
        self.peak = 0

    def hit(self, item: dict, resp: Optional[dict]) -> None:
        if resp is None:
            self.failed += 1
            return
        self.sent += 1
        self.peak = max(self.peak, item.get("urgency", 0))
        if resp.get("dedup"):
            self.deduped += 1
        elif resp.get("id"):
            self.created += 1

    def summary(self, case: str, seconds: float) -> None:
        print("\n" + "=" * 52)
        print(f"演示摘要 · {case}")
        print(f"  耗时        {seconds:.1f} 秒")
        print(f"  发送        {self.sent} 条（失败 {self.failed} 条）")
        print(f"  入库告警    {self.created} 条")
        print(f"  去重丢弃    {self.deduped} 条")
        print(f"  最高紧急度  {self.peak}（0正常/1关注/2告警/3紧急）")
        print("=" * 52)


def rebase_ts(items: list) -> list:
    """把演示数据的绝对时间戳平移到当前时刻（相对间隔不变），
    否则「今日告警数」为 0、平均响应时长会算出天文数字。"""
    if not items:
        return items
    now = int(time.time() * 1000)
    base = items[0]["ts"]
    for it in items:
        it["ts"] = now + (it["ts"] - base)
    return items


def load_items(case: str) -> list:
    # 优先用 B 的真实产出（results/<case>_decision.json），
    # 其次回退到 C 的合成数据（results/decision_<case>.json），
    # 保证 A/B 还没交付时也能单独演示。
    candidates = [
        BASE_DIR / "results" / f"{case}_decision.json",
        BASE_DIR / "results" / f"decision_{case}.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        print(f"[FAIL] 找不到 {candidates[0].name} 或 {candidates[1].name}")
        print("       B 的产出未到位时，先跑：python scripts/make_demo_results.py")
        sys.exit(1)
    print(f"数据源：results/{path.name}")
    items = json.loads(path.read_text(encoding="utf-8"))
    items.sort(key=lambda x: x["ts"])
    return items


def play_video(video: Path, items: list, base_url: str, speed: float, counter: Counter) -> None:
    import cv2  # 局部导入：没装 opencv 时不影响 --no-video

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"[WARN] 打不开视频 {video.name}，改为只发数据")
        cap.release()
        play_data_only(items, base_url, speed, counter)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_delay = max(1, int(1000 / fps))
    base_ts = items[0]["ts"]
    idx, t0 = 0, time.time()
    window = f"Elevator Rescue · {video.stem}"

    print(f"播放 {video.name}（{fps:.0f} fps），按 q 可提前结束")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            elapsed_ms = (time.time() - t0) * 1000.0 * speed
            while idx < len(items) and (items[idx]["ts"] - base_ts) <= elapsed_ms:
                item = items[idx]
                try:
                    resp = post(base_url, item)
                except urllib.error.URLError:
                    resp = None
                counter.hit(item, resp)
                if resp and resp.get("id"):
                    print(f"  [告警] urgency={item['urgency']} {item['event_type']} @ {item['location']}")
                idx += 1
            cv2.imshow(window, frame)
            if (cv2.waitKey(frame_delay) & 0xFF) == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    # 视频短于数据时间轴时，把剩余条目发完
    for item in items[idx:]:
        try:
            resp = post(base_url, item)
        except urllib.error.URLError:
            resp = None
        counter.hit(item, resp)


def play_data_only(items: list, base_url: str, speed: float, counter: Counter) -> None:
    base_ts = items[0]["ts"]
    t0 = time.time()
    print(f"只发数据：{len(items)} 条，倍速 {speed}x")
    for item in items:
        target = (item["ts"] - base_ts) / 1000.0 / max(speed, 0.01)
        delay = target - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        try:
            resp = post(base_url, item)
        except urllib.error.URLError:
            resp = None
            if counter.failed == 0:
                print("[FAIL] 无法连接服务，请确认 python -m service.main 正在运行")
        counter.hit(item, resp)
        flag = "去重" if (resp and resp.get("dedup")) else ("入库" if (resp and resp.get("id")) else "心跳")
        print(f"  {time.strftime('%H:%M:%S')} urgency={item['urgency']} {item['event_type']:<8s} {flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键回放演示")
    ap.add_argument("--case", default="demo_trapped", help="案例名，对应 common/demo_data/<case>.mp4")
    ap.add_argument("--speed", type=float, default=1.0, help="数据时间轴倍速")
    ap.add_argument("--no-video", action="store_true", help="只发数据，不播视频")
    ap.add_argument("--base-url", default="http://127.0.0.1:18080")
    ap.add_argument("--keep-ts", action="store_true", help="保留原始时间戳（默认平移到当前时刻）")
    ap.add_argument("--camera-session", action="store_true",
                    help="起播时登记画面会话（看板进入跟随对齐，默认关）")
    args = ap.parse_args()

    items = load_items(args.case)
    if not args.keep_ts:
        rebase_ts(items)
    video = BASE_DIR / "common" / "demo_data" / f"{args.case}.mp4"
    counter = Counter()
    t0 = time.time()

    session_on = False
    if args.camera_session:
        started = post_session(
            args.base_url,
            {"source_id": args.case, "event": "start", "start_wall_ms": int(time.time() * 1000)},
        )
        if started and started.get("session"):
            session_on = True
            print("画面会话：%s offset=%s" % (
                started["session"]["session_id"], started["session"]["offset_ms"]))
        else:
            print("[WARN] 画面会话登记失败，看板将显示「画面未对齐」")

    if args.no_video or not video.exists():
        if not video.exists() and not args.no_video:
            print(f"[WARN] 没有视频 {video}（A 的产出，D7 前属正常），改为只发数据")
        play_data_only(items, args.base_url, args.speed, counter)
    else:
        try:
            play_video(video, items, args.base_url, args.speed, counter)
        except ImportError:
            print("[WARN] 未安装 opencv-python，改为只发数据")
            play_data_only(items, args.base_url, args.speed, counter)

    if session_on:
        post_session(args.base_url, {"source_id": args.case, "event": "stop"})

    counter.summary(args.case, time.time() - t0)
    print("看板：http://127.0.0.1:18080/")
    return 0 if counter.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
