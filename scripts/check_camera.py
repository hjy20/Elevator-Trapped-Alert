"""现场画面验收脚本（只读，不修改 service/、common/、results/）。

用法（项目根执行）：
  python scripts/check_camera.py --sources   # 源清单契约（T-CAM-002）
  python scripts/check_camera.py --range     # 视频 Range 判定（T-CAM-002）
  python scripts/check_camera.py --kpts      # 关键点窗口 + bbox 同口径（T-CAM-003）
  python scripts/check_camera.py --frame     # 静态帧降级 + /camera/health（T-CAM-004）
  python scripts/check_camera.py --ui        # 单文件/零外链/体积/面板锚点（T-CAM-005）
  python scripts/check_camera.py --align     # 唯一时间基准/会话契约（T-CAM-006）
  python scripts/check_camera.py --link      # 选源规则/事件隔离/中文提示（T-CAM-006）
  python scripts/check_camera.py --status    # 状态机/退避/轮询隔离（T-CAM-007）
  python scripts/check_camera.py --privacy   # 存储/导出/水印/日志（T-CAM-008）
  python scripts/check_camera.py --frozen    # 冻结产物集合相等（T-CAM-008）
  python scripts/check_camera.py --strict    # 总闸门（T-CAM-008）

约束：只调用 service.camera 的真实端点服务函数/纯函数，**不复制服务端公式**；
静态断言只读 dashboard/index.html 与 service/main.py，绝不改文件。
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import service.camera as cam  # noqa: E402
import service.config as cfg  # noqa: E402

RESULTS = []
PAGE = BASE_DIR / "dashboard" / "index.html"
MAIN_PY = BASE_DIR / "service" / "main.py"
CAM_PY = BASE_DIR / "service" / "camera.py"
BASELINE = BASE_DIR / "docs" / "spec" / "camera_baseline.txt"
CAM_BEGIN = "// === CAM-BEGIN ==="
CAM_END = "// === CAM-END ==="
MAX_BYTES = 48 * 1024


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    suffix = "  (%s)" % detail if detail else ""
    print("  %s %s%s" % ("✓" if ok else "✗", name, suffix))


def _expect_error(fn, status, code):
    try:
        fn()
    except cam.CameraError as exc:
        return exc.status_code == status and exc.code == code, "%s/%s" % (exc.status_code, exc.code)
    except Exception as exc:  # noqa: BLE001 - 脚本需要暴露非预期异常
        return False, "%s: %s" % (type(exc).__name__, exc)
    return False, "未抛异常"


def _temp_source(missing_mp4):
    return cam.CameraSource(
        source_id="demo_temp",
        device_id=cam.DEFAULT_DEVICE_ID,
        label="临时源",
        event_type="normal",
        fps=25.0,
        frames=10,
        width=640,
        height=360,
        anchor_ts_ms=1700000000000,
        duration_ms=400,
        video=False,
        keypoints=False,
        vision=False,
        transport=["video"],
        demo=True,
        video_path=missing_mp4,
        kpts_path=missing_mp4.with_suffix(".jsonl"),
        vision_path=missing_mp4.with_suffix(".json"),
    )


# ---------------------------------------------------------------- --sources


def run_sources():
    print("[--sources] 画面源清单契约")
    cam.reset_state()
    payload = cam.sources_payload()
    items = payload["items"]

    metas = {}
    for meta_path in sorted((BASE_DIR / "common" / "demo_data").glob("*_meta.json")):
        if meta_path.name.endswith("_imu_meta.json"):
            continue
        metas[meta_path.name[: -len("_meta.json")]] = json.loads(
            meta_path.read_text(encoding="utf-8")
        )

    check("items 恰 5 条", len(items) == 5, "len=%d" % len(items))
    by_id = dict((item["source_id"], item) for item in items)
    check("source_id 集合与 meta 一致", set(by_id) == set(metas), str(sorted(by_id)))
    check("items 按 source_id 字典序", [i["source_id"] for i in items] == sorted(by_id))

    mismatch = []
    for source_id, meta in metas.items():
        item = by_id.get(source_id, {})
        pairs = (
            ("fps", "fps"),
            ("frames", "frames"),
            ("width", "width"),
            ("height", "height"),
            ("anchor_ts_ms", "start_ts_ms"),
            ("event_type", "event_type"),
        )
        for item_key, meta_key in pairs:
            if item.get(item_key) != meta[meta_key]:
                mismatch.append("%s.%s" % (source_id, item_key))
        expect_duration = int(round(float(meta["duration_sec"]) * 1000.0))
        if abs(int(item.get("duration_ms", -1)) - expect_duration) > 40:
            mismatch.append("%s.duration_ms" % source_id)
    check("items 与 meta 逐项相等", not mismatch, ",".join(mismatch))

    again = cam.sources_payload()["items"]
    check(
        "items 两次调用逐字节一致",
        json.dumps(items, sort_keys=True, ensure_ascii=False)
        == json.dumps(again, sort_keys=True, ensure_ascii=False),
    )
    check(
        "正常响应键集合 = 4",
        set(payload) == {"items", "server_time_ms", "mode", "watermark"},
        str(sorted(payload)),
    )
    check("水印文案", payload.get("watermark") == "演示数据 · 非取证")

    original = cfg.CAMERA_SOURCE_MODE
    cfg.CAMERA_SOURCE_MODE = "rtsp"
    cam.reset_state()
    try:
        empty = cam.sources_payload()
        check(
            "rtsp → items=[] + degraded=true（200 语义）",
            empty["items"] == [] and empty.get("degraded") is True,
            "mode=%s" % empty.get("mode"),
        )
    finally:
        cfg.CAMERA_SOURCE_MODE = original
        cam.reset_state()

    ok, detail = _expect_error(
        lambda: cam.video_payload("demo_missing", None), 404, "CAM_SOURCE_NOT_FOUND"
    )
    check("未知源 → 404 CAM_SOURCE_NOT_FOUND", ok, detail)

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing.mp4"
        source = _temp_source(missing)
        ok, detail = _expect_error(
            lambda: cam.resolve_video(source, None), 404, "CAM_VIDEO_NOT_FOUND"
        )
        check("登记有源但 mp4 缺失 → 404 CAM_VIDEO_NOT_FOUND", ok, detail)
        check("未改动 common/demo_data/*.mp4", not (BASE_DIR / "common" / "demo_data" / "demo_temp.mp4").exists())


# ---------------------------------------------------------------- --range


def run_range():
    print("[--range] 视频分发与 Range")
    cam.reset_state()
    source = cam.require_source("demo_faint")
    size = source.video_path.stat().st_size
    with source.video_path.open("rb") as handle:
        head = handle.read(100)

    status, headers, start, length = cam.resolve_video(source, None)
    check("无 Range → 200 全量", status == 200 and start == 0 and length == size, "size=%d" % size)
    check("Content-Length = size", headers.get("Content-Length") == str(size))
    check("Content-Type = video/mp4", headers.get("Content-Type") == "video/mp4")
    check("Accept-Ranges = bytes", headers.get("Accept-Ranges") == "bytes")
    check("Cache-Control = no-store", headers.get("Cache-Control") == "no-store")

    status, headers, start, length = cam.resolve_video(source, "bytes=0-99")
    body = b"".join(cam.iter_video_chunks(source.video_path, start, length))
    check(
        "bytes=0-99 → 206 + Content-Range",
        status == 206 and length == 100 and headers.get("Content-Range") == "bytes 0-99/%d" % size,
        headers.get("Content-Range", ""),
    )
    check("首 100 字节与文件一致", body == head)

    status, headers, start, length = cam.resolve_video(source, "bytes=1000-")
    check(
        "bytes=1000- → 206 长度 size-1000",
        status == 206 and start == 1000 and length == size - 1000,
        "length=%d" % length,
    )

    status, headers, start, length = cam.resolve_video(source, "bytes=-100")
    body = b"".join(cam.iter_video_chunks(source.video_path, start, length))
    with source.video_path.open("rb") as handle:
        handle.seek(size - 100)
        tail = handle.read(100)
    check(
        "bytes=-100 → 后缀型 206 长度 100",
        status == 206 and length == 100 and start == size - 100 and body == tail,
        "start=%d" % start,
    )

    for header, label in (
        ("bytes=%d-" % size, "start 越界"),
        ("bytes=50-10", "start > end"),
        ("bytes=0-99,200-299", "多段 Range"),
    ):
        status, headers, start, length = cam.resolve_video(source, header)
        check(
            "%s → 416 + bytes */size" % label,
            status == 416 and headers.get("Content-Range") == "bytes */%d" % size,
            headers.get("Content-Range", ""),
        )

    status, headers, start, length = cam.resolve_video(source, "bytes=0-99999999")
    check(
        "end 超长 → 钳到 size-1 不报错",
        status == 206 and length == size,
        "length=%d" % length,
    )

    ok, detail = _expect_error(
        lambda: cam.video_payload("demo_missing", None), 404, "CAM_SOURCE_NOT_FOUND"
    )
    check("未知源 → 404 CAM_SOURCE_NOT_FOUND", ok, detail)

    with tempfile.TemporaryDirectory() as tmp:
        source_missing = _temp_source(Path(tmp) / "missing.mp4")
        ok, detail = _expect_error(
            lambda: cam.video_payload("demo_temp", None), 404, "CAM_SOURCE_NOT_FOUND"
        )
        check("未知源（fixture 不入清单）仍 404", ok, detail)
        ok, detail = _expect_error(
            lambda: cam.resolve_video(source_missing, None), 404, "CAM_VIDEO_NOT_FOUND"
        )
        check("文件缺失 → 404 CAM_VIDEO_NOT_FOUND", ok, detail)


# ---------------------------------------------------------------- --kpts


def run_kpts():
    print("[--kpts] 关键点窗口与视觉合并")
    cam.reset_state()
    window = cam.kpts_payload("demo_faint", from_frame=0, count=120)
    items = window["items"]
    check(
        "from_frame=0&count=120 → 120 条且首帧 0",
        len(items) == 120 and items[0]["frame"] == 0 and window["from_frame"] == 0,
        "len=%d" % len(items),
    )
    persons = items[0]["persons"]
    check("每人 17 点", bool(persons) and all(len(p["keypoints"]) == 17 for p in persons), "人=%d" % len(persons))
    check(
        "bbox 4 元且 x1<x2,y1<y2",
        all(
            len(p["bbox"]) == 4 and p["bbox"][0] < p["bbox"][2] and p["bbox"][1] < p["bbox"][3]
            for p in persons
        ),
    )

    tail = cam.kpts_payload("demo_faint", from_frame=490, count=120)
    check(
        "from_frame=490&count=120 → clamped + 末帧 499",
        tail["clamped"] is True and tail["items"][-1]["frame"] == 499,
        "count=%d" % tail["count"],
    )

    ok, detail = _expect_error(
        lambda: cam.kpts_payload("demo_faint", count=121), 400, "CAM_WINDOW_TOO_LARGE"
    )
    check("count=121 → 400 CAM_WINDOW_TOO_LARGE（非 422）", ok, detail)
    ok, detail = _expect_error(
        lambda: cam.kpts_payload("demo_faint", count=0), 400, "CAM_BAD_REQUEST"
    )
    check("count=0 → 400 CAM_BAD_REQUEST", ok, detail)
    ok, detail = _expect_error(
        lambda: cam.kpts_payload("demo_faint", count="abc"), 400, "CAM_BAD_REQUEST"
    )
    check("count=abc → 400 CAM_BAD_REQUEST", ok, detail)

    cam.reset_state()
    ok, detail = _expect_error(
        lambda: cam.kpts_payload("demo_faint", ts=1794916201000), 409, "CAM_NOT_ALIGNED"
    )
    check("无会话带 ts → 409 CAM_NOT_ALIGNED", ok, detail)

    small = cam.kpts_payload("demo_faint", from_frame=0, count=5)
    six = {"ts", "person_count", "fall_prob", "door_approach", "crowded", "keypoints_ok"}
    visions = [item["vision"] for item in small["items"]]
    check(
        "vision 六字段直通",
        all(row is None or set(row) == six for row in visions),
        str(sorted(set().union(*[set(r) for r in visions if r]))),
    )
    check(
        "vision.ts == items.ts",
        all(item["vision"]["ts"] == item["ts"] for item in small["items"] if item["vision"]),
    )

    numpy_diff = None
    try:
        import numpy as np

        from vision.geometry import bbox_from_keypoints

        worst = 0.0
        for item in items[:40]:
            for person in item["persons"]:
                reference = bbox_from_keypoints(np.asarray(person["keypoints"], dtype=np.float32))
                got = cam.bbox_from_points(person["keypoints"])
                for left, right in zip(got, reference.tolist()):
                    worst = max(worst, abs(left - float(right)))
        numpy_diff = worst
        check("bbox 与 numpy 版比对 ≤1e-4", worst <= 1e-4, "max=%.6f" % worst)
    except ImportError as exc:  # noqa: F841
        check("bbox 与 numpy 版比对 ≤1e-4", False, "numpy 不可用")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        demo = root / "common" / "demo_data"
        demo.mkdir(parents=True, exist_ok=True)
        meta = {
            "case": "demo_no_kpts",
            "fps": 25.0,
            "frames": 10,
            "width": 640,
            "height": 360,
            "duration_sec": 0.4,
            "start_ts_ms": 1794916200000,
            "event_type": "normal",
            "keypoint_sidecar": "demo_no_kpts_kpts.jsonl",
        }
        (demo / "demo_no_kpts_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        cam.reset_state()
        cam.bootstrap(base_dir=root)
        fallback = cam.kpts_payload("demo_no_kpts", from_frame=0, count=3)
        check(
            "缺侧车 → 200 + persons=[] + vision=null",
            len(fallback["items"]) == 3
            and all(item["persons"] == [] for item in fallback["items"])
            and all(item["vision"] is None for item in fallback["items"]),
        )
        cam.reset_state()
        cam.bootstrap()

    check("items 内 persons 允许空数组", isinstance(items[0]["persons"], list))


# ---------------------------------------------------------------- --frame


def run_frame():
    print("[--frame] 静态帧降级与自检")
    cam.reset_state()
    cv2_ok = cam.cv2_available()
    check("cv2 探测结果可读", cv2_ok in (True, False), "cv2=%s" % cv2_ok)

    if cv2_ok:
        before = cam.frame_decode_count()
        data, index, ts_ms = cam.frame_payload("demo_faint", frame=0)
        check("JPEG 头 FFD8", data[:2] == b"\xff\xd8", "len=%d" % len(data))
        check("X-Camera-Frame 与请求一致", index == 0 and ts_ms == 1794916200000, "ts=%d" % ts_ms)
        mid = cam.frame_decode_count()
        check("首次解码计数 +1", mid == before + 1, "%d→%d" % (before, mid))
        again, _, _ = cam.frame_payload("demo_faint", frame=0)
        check(
            "同帧 1 s 内第二次命中缓存（不重复解码）",
            cam.frame_decode_count() == mid and again == data,
        )

        by_ts, index_ts, _ = cam.frame_payload("demo_faint", ts=1794916200120)
        check("ts 反算帧号（素材时间）", index_ts == 3, "frame=%d" % index_ts)

        cam.reset_state()
        cached_ts, cached_index, _ = cam.frame_payload("demo_faint", frame=10)
        check("缓存上限 8 帧生效", cam.frame_cache_size() <= 8, "cache=%d" % cam.frame_cache_size())

    saved = sys.modules.get("cv2")
    sys.modules["cv2"] = None
    try:
        cam.reset_state()
        ok, detail = _expect_error(
            lambda: cam.frame_payload("demo_faint", frame=0), 501, "CAM_TRANSPORT_UNSUPPORTED"
        )
        check("屏蔽 cv2 → 501 CAM_TRANSPORT_UNSUPPORTED", ok, detail)
        payload = cam.sources_payload()
        check("屏蔽 cv2 后其余调用不崩", isinstance(payload.get("items"), list), "items=%d" % len(payload["items"]))
    finally:
        if saved is None:
            sys.modules.pop("cv2", None)
        else:
            sys.modules["cv2"] = saved
        cam.reset_state()

    for frame_value, label in ((500, "frame=500"), (-1, "frame=-1")):
        ok, detail = _expect_error(
            lambda value=frame_value: cam.frame_payload("demo_faint", frame=value),
            400,
            "CAM_BAD_FRAME",
        )
        check("%s → 400 CAM_BAD_FRAME（不钳制）" % label, ok, detail)
    ok, detail = _expect_error(lambda: cam.frame_payload("demo_faint"), 400, "CAM_BAD_REQUEST")
    check("缺 frame/ts → 400 CAM_BAD_REQUEST", ok, detail)

    with tempfile.TemporaryDirectory() as tmp:
        source_missing = _temp_source(Path(tmp) / "missing.mp4")
        ok, detail = _expect_error(
            lambda: cam.resolve_video(source_missing, None), 404, "CAM_VIDEO_NOT_FOUND"
        )
        check("缺文件 → 404 CAM_VIDEO_NOT_FOUND", ok, detail)

    health_a = cam.health_payload()
    check(
        "health 键集合 = 6",
        set(health_a) == {"sources", "mode", "cv2", "frame_cache", "session", "degraded"},
        str(sorted(health_a)),
    )
    check(
        "health.session 结构",
        set(health_a["session"]) == {"active", "source_id"},
    )
    cam.reset_state()
    first = cam.health_payload()
    decode_before = cam.frame_decode_count()
    second = cam.health_payload()
    check(
        "health 连调两次无副作用",
        first["session"] == second["session"] and cam.frame_decode_count() == decode_before,
    )


def _page_text():
    return PAGE.read_text(encoding="utf-8")


def _cam_segment(text):
    if text.count(CAM_BEGIN) != 1 or text.count(CAM_END) != 1:
        return None
    start = text.index(CAM_BEGIN) + len(CAM_BEGIN)
    end = text.index(CAM_END)
    return text[start:end] if end > start else None


def _cam_msg_keys(text):
    block = re.search(r"const CAM_MSG = \{(.*?)\};", text, re.S)
    return set(re.findall(r"(CAM_[A-Z_]+)\s*:", block.group(1))) if block else set()


def _git(*args):
    proc = subprocess.run(
        ["git"] + list(args), cwd=str(BASE_DIR), capture_output=True, text=True
    )
    return proc.stdout


# ---------------------------------------------------------------- --ui


def run_ui():
    print("[--ui] 单文件 / 零外链 / 面板锚点 / 坐标映射 / 体积")
    files = [p for p in (BASE_DIR / "dashboard").iterdir() if p.is_file()]
    check(
        "dashboard 单文件（仅 index.html）",
        len(files) == 1 and files[0].name == "index.html",
        ",".join(sorted(p.name for p in files)),
    )
    text = _page_text()
    check(
        "零外链（无 http(s)/link/img）",
        not re.search(r"https?://|<link|<script[^>]*src|<img", text),
    )
    check("<video> 恰 1 个", len(re.findall(r"<video\b", text)) == 1)
    check("<canvas> ≥1 个", len(re.findall(r"<canvas\b", text)) >= 1)
    seg = _cam_segment(text)
    check("CAM_BEGIN/CAM_END 各 1 处", seg is not None)
    seg = seg or ""
    check(
        "画面段无外链/图片",
        not re.search(r"https?://|<img|<link", seg),
    )
    check(
        "画面段不引用 setConn/okCount/failCount",
        seg.count("setConn(") == 0 and seg.count("okCount") == 0
        and seg.count("failCount") == 0,
    )
    check("canvas 带 role=\"img\"", 'role="img"' in text)
    check("aria-label=\"现场画面", 'aria-label="现场画面' in text)
    check("水印文案常驻", "演示数据 · 非取证" in text and 'class="cam-wm"' in text)
    check("复用 --u3 颜色令牌", "--u3" in seg)
    check("媒体前缀同源 /api/v1/camera/", "/api/v1/camera/video?source_id=" in seg)
    check(
        "16:9 + padding-top 兜底",
        "aspect-ratio: 16 / 9" in text and "padding-top: 56.25%" in text,
    )
    check("DPR 上限 2", "Math.min(window.devicePixelRatio || 1, 2)" in seg)
    check(
        "内容区等比映射（同源 sx/sy）",
        "CAM_SRC_W / CAM_SRC_H" in seg and "sx: contentW / CAM_SRC_W" in seg,
    )
    check("叠加上限 25 fps", "CAM_OVERLAY_FPS = 25" in seg)
    check(
        "体积 ≤ 48 KB",
        len(text.encode("utf-8")) <= MAX_BYTES,
        "%d B" % len(text.encode("utf-8")),
    )


# ---------------------------------------------------------------- --align


def run_align():
    print("[--align] 唯一时间基准 / 会话契约")
    cam.reset_state()
    cam.bootstrap()
    source = cam.require_source("demo_faint")
    step = cam.frame_ms_of(source)

    for delta, label in ((0, "offset=0"), (-31616020000, "负偏移"), (86400000, "正偏移")):
        cam.reset_state()
        cam.bootstrap()
        offset = delta
        cam.session_start(source.source_id, int(source.anchor_ts_ms) + offset)
        state = cam.alignment(source.source_id)
        alert_ts = int(source.anchor_ts_ms) + offset + 300 * step
        frame, clamped = cam.frame_for_alert(source, alert_ts, state.offset_ms)
        error = abs(cam.wall_ms_of(source, frame, state.offset_ms) - alert_ts)
        check(
            "%s 对齐误差 ≤200ms" % label,
            error <= 200 and not clamped and frame == 300,
            "err=%dms frame=%d" % (error, frame),
        )

    cam.session_start(source.source_id, int(source.anchor_ts_ms))
    frame, clamped = cam.frame_for_alert(source, int(source.anchor_ts_ms) - 1000, 0)
    check("早于锚点 → 钳到 0 + clamped", clamped and frame == 0, "frame=%d" % frame)
    frame, clamped = cam.frame_for_alert(source, int(source.anchor_ts_ms) + 999 * step, 0)
    check(
        "晚于末帧 → 钳到 frames-1 + clamped",
        clamped and frame == int(source.frames) - 1,
        "frame=%d" % frame,
    )

    cam.reset_state()
    cam.bootstrap()
    ok, detail = _expect_error(
        lambda: cam.kpts_payload(
            "demo_faint", ts=int(source.anchor_ts_ms) + 300 * step
        ),
        409,
        "CAM_NOT_ALIGNED",
    )
    check("无会话按告警时间取帧 → 409 CAM_NOT_ALIGNED", ok, detail)

    wall_a = int(source.anchor_ts_ms) + 1000
    wall_b = int(source.anchor_ts_ms) + 5000
    cam.session_start(source.source_id, wall_a)
    cam.session_start(source.source_id, wall_b)
    check(
        "两次 start 只剩 1 条会话",
        cam.session_count() == 1,
        "count=%d" % cam.session_count(),
    )
    check(
        "offset 取自后一次 start",
        cam.alignment(source.source_id).offset_ms == wall_b - int(source.anchor_ts_ms),
    )

    cam.reset_state()
    cam.bootstrap()
    ok, detail = _expect_error(
        lambda: cam.session_action({"source_id": "demo_faint", "event": "start"}),
        400,
        "CAM_BAD_TIMESTAMP",
    )
    check("start 缺 start_wall_ms → 400 CAM_BAD_TIMESTAMP", ok, detail)
    ok, detail = _expect_error(
        lambda: cam.session_action(
            {"source_id": "demo_faint", "event": "start", "start_wall_ms": 1794916200}
        ),
        400,
        "CAM_BAD_TIMESTAMP",
    )
    check("10 位秒级时间戳 → 400 CAM_BAD_TIMESTAMP", ok, detail)
    ok, detail = _expect_error(
        lambda: cam.session_action({"source_id": "demo_faint", "event": "bogus"}),
        400,
        "CAM_BAD_REQUEST",
    )
    check("event 非法 → 400 CAM_BAD_REQUEST", ok, detail)
    ok, detail = _expect_error(
        lambda: cam.session_action(
            {"source_id": "demo_x", "event": "start", "start_wall_ms": 1794916200000}
        ),
        404,
        "CAM_SOURCE_NOT_FOUND",
    )
    check("未知 source_id → 404 CAM_SOURCE_NOT_FOUND", ok, detail)

    now = 1794916300000
    started = cam.session_action(
        {"source_id": "demo_faint", "event": "start", "start_wall_ms": now}, now
    )
    check(
        "start 返回 session 5 字段",
        set(started.get("session") or {})
        == {"session_id", "source_id", "start_wall_ms", "offset_ms", "updated_ms"},
        str(sorted(started.get("session") or {})),
    )
    beat = cam.session_action({"source_id": "demo_faint", "event": "heartbeat"}, now + 1000)
    check(
        "heartbeat 只刷 updated_ms、offset 不变",
        beat["session"]["updated_ms"] == now + 1000
        and beat["session"]["offset_ms"] == started["session"]["offset_ms"],
    )
    payload = cam.session_payload("demo_faint", now + 1000)
    check(
        "GET 键集合 = 7 且 expire_sec=600",
        set(payload) == {
            "source_id", "session", "aligned", "offset_ms", "drift_ms",
            "server_time_ms", "expire_sec",
        }
        and payload["expire_sec"] == 600,
        str(sorted(payload)),
    )
    check("GET aligned=true（会话有效）", payload["aligned"] is True)
    stopped = cam.session_action({"source_id": "demo_faint", "event": "stop"}, now + 2000)
    check("stop → session=null", stopped.get("session") is None and stopped.get("ok") is True)
    check(
        "stop 后 GET aligned=false",
        cam.session_payload("demo_faint", now + 2000)["aligned"] is False,
    )

    try:
        import service.main as svc
        from fastapi import HTTPException as HttpError

        def _run_coro(coro):
            """无事件循环驱动（沙箱 socketpair 不可用），仅用于单次 await 的路由函数。"""
            try:
                coro.send(None)
            except StopIteration as stop:
                return stop.value
            raise RuntimeError("协程未在单次驱动内完成")

        svc._load_db()
        svc._load_cache()
        cam.reset_state()
        cam.bootstrap()

        class _Req(object):
            def __init__(self, body):
                self._body = body

            async def json(self):
                return self._body

        routed = _run_coro(
            svc.camera_session_post(
                _Req({"source_id": "demo_faint", "event": "start", "start_wall_ms": cam.now_ms()})
            )
        )
        check(
            "main.POST /camera/session 直通",
            routed.get("ok") is True and bool(routed.get("session")),
            str(sorted(routed.get("session") or {})),
        )
        fetched = svc.camera_session_get("demo_faint")
        check(
            "main.GET /camera/session 直通",
            fetched.get("aligned") is True and fetched.get("expire_sec") == 600,
            "aligned=%s" % fetched.get("aligned"),
        )
        try:
            _run_coro(
                svc.camera_session_post(_Req({"source_id": "demo_x", "event": "stop"}))
            )
            check("未知源路由层 404", False, "未抛 HTTPException")
        except HttpError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            check(
                "未知源路由层 404 + CAM_SOURCE_NOT_FOUND",
                exc.status_code == 404 and detail.get("code") == "CAM_SOURCE_NOT_FOUND",
                "%s/%s" % (exc.status_code, detail.get("code")),
            )
    except Exception as exc:  # noqa: BLE001 - 路由直调失败必须显式红
        check("main 会话路由直通", False, "%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------- --link


def run_link():
    print("[--link] 选源规则 / 事件隔离 / 中文提示")
    text = _page_text()
    seg = _cam_segment(text) or ""
    check(
        "按 event_type 唯一匹配选源",
        "s.event_type === item.event_type" in seg and "matches.length === 1" in seg,
    )
    check("选源不按 device_id 匹配", not re.search(r"device_id\s*===", seg))
    check(
        "查看画面按钮可键盘激活",
        '"查看画面 "' in text and "camBtn" in text,
    )
    check(
        "ack 按钮隔离冒泡",
        'ackBtn.addEventListener("click", (ev) => ev.stopPropagation());' in text,
    )
    check("展开按钮隔离冒泡", 'ev.stopPropagation();\n    const open = reasons.hidden;' in text)
    for sentence in (
        "画面未对齐（尚未开始播放）",
        "该告警时刻不在当前画面范围内",
        "已手动选择",
    ):
        check("文案「%s」" % sentence, sentence in seg)
    check(
        "prefers-reduced-motion 分支",
        "prefers-reduced-motion" in seg and "CAM_REDUCE" in seg,
    )
    check("楼层取告警 location 非源 label", "item.location || \"位置未知\"" in seg)
    check("联动不改筛选/滚动（无 scrollTop/currentStatus 赋值）",
          "scrollTop" not in seg and "currentStatus =" not in seg)


# ---------------------------------------------------------------- --status


def run_status():
    print("[--status] 状态机 / 退避 / 轮询隔离 / 可见性单入口")
    text = _page_text()
    seg = _cam_segment(text) or ""
    check("冻结阈值 CAM_STALL_MS = 5000", "CAM_STALL_MS = 5000" in seg)
    check(
        "退避 CAM_RETRY_MS = [1000, 2000, 4000]",
        re.search(r"CAM_RETRY_MS = \[1000, 2000, 4000\]", seg) is not None,
    )
    check("重试上限 CAM_RETRY_MAX = 3", "CAM_RETRY_MAX = 3" in seg)
    check("visibilitychange 唯一入口", text.count("visibilitychange") == 1)
    check(
        "画面段 setConn/okCount/failCount = 0",
        seg.count("setConn(") == 0 and seg.count("okCount") == 0 and seg.count("failCount") == 0,
    )
    check("画面段不引用 POLL_OK_MS", "POLL_OK_MS" not in seg)
    check(
        "camPause 内 video.pause()",
        re.search(r"function camPause\(\) \{[^}]*camVideo\.pause\(\);", seg) is not None,
    )
    check(
        "切源释放顺序 pause → removeAttribute(src) → load",
        re.search(
            r"camVideo\.pause\(\);\s*camVideo\.removeAttribute\(\"src\"\);\s*camVideo\.load\(\);",
            seg,
        )
        is not None,
    )
    allsettled = re.search(r"Promise\.allSettled\(\[(.*?)\]\)", text, re.S)
    check(
        "refresh 的 allSettled 数组内无 camera",
        allsettled is not None and "camera" not in allsettled.group(1),
    )
    check(
        "画面独立节流常量",
        "CAM_KPTS_MS = 1000" in seg and "CAM_SESSION_POLL_MS = 5000" in seg,
    )
    states = ("IDLE", "LOADING", "PLAYING", "STALLED", "PAUSED", "OFFLINE")
    check("状态机六态齐全", all(('"%s"' % s) in seg for s in states))
    check("画面段仅 1 个 setInterval（无第二个轮询调度器）", seg.count("setInterval") == 1)
    check("关闭开关 ?camera=0", "camera=0" in seg and "CAM_ON" in seg)
    check("单 <video> 运行时约束（切源复用同一元素）", seg.count("createElement(\"video\")") == 0)


# ---------------------------------------------------------------- --privacy


def run_privacy():
    print("[--privacy] 存储 / 导出 / 水印 / 日志")
    text = _page_text()
    seg = _cam_segment(text) or ""
    check("无 localStorage", text.count("localStorage") == 0)
    check("无 indexedDB", text.count("indexedDB") == 0)
    stores = [line for line in text.splitlines() if "sessionStorage" in line]
    check(
        "sessionStorage 仅存 UI 偏好 camSource",
        bool(stores) and all("camSource" in line for line in stores),
        str(len(stores)),
    )
    check(
        "无 MediaRecorder / toDataURL / canvas 导出",
        text.count("MediaRecorder") == 0 and text.count("toDataURL") == 0,
    )
    check("前端无 .jpg/.png 落盘引用", not re.search(r"\.(jpg|png)", text))
    check(
        "水印常驻于画面面板",
        "演示数据 · 非取证" in text and 'class="cam-wm"' in text,
    )
    cam_text = CAM_PY.read_text(encoding="utf-8")
    check(
        "服务端无 imwrite/写盘落图",
        "imwrite" not in cam_text and re.search(r'open\([^)]*"[wa]b"\)', cam_text) is None,
    )
    check(
        "服务端日志不含关键点/bbox 全文",
        not re.search(r"log\.(info|warning)\([^)]*(keypoints|bbox)", cam_text),
    )
    check("代码中无人脸/身份字段", "face" not in text.lower() and "face" not in seg.lower())


# ---------------------------------------------------------------- --frozen


def run_frozen():
    print("[--frozen] 冻结产物集合相等（common/results/results_integration）")
    if not BASELINE.exists():
        check("基线文件存在", False, str(BASELINE))
        return
    expected = sorted(
        line for line in BASELINE.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    actual = sorted(
        line
        for line in _git("status", "--short", "common", "results", "results_integration").splitlines()
        if line.strip()
    )
    check(
        "冻结集合与基线逐行相等",
        actual == expected,
        "now=%d base=%d" % (len(actual), len(expected)),
    )


# ---------------------------------------------------------------- --strict


def run_strict():
    print("[--strict] 总闸门（源清单/错误码/路由位置/体积/依赖/PEP604/推送）")
    cam.reset_state()
    sources = cam.sources_payload()["items"]
    metas = {}
    for meta_path in sorted((BASE_DIR / "common" / "demo_data").glob("*_meta.json")):
        if meta_path.name.endswith("_imu_meta.json"):
            continue
        metas[meta_path.name[: -len("_meta.json")]] = json.loads(
            meta_path.read_text(encoding="utf-8")
        )
    check("源清单 5 条且与 meta 一致", len(sources) == 5 and
          set(i["source_id"] for i in sources) == set(metas))
    mismatch = []
    for item in sources:
        meta = metas.get(item["source_id"], {})
        if (
            item["anchor_ts_ms"] != meta.get("start_ts_ms")
            or item["fps"] != meta.get("fps")
            or item["frames"] != meta.get("frames")
        ):
            mismatch.append(item["source_id"])
    check("锚点/帧数/帧率逐项相等", not mismatch, ",".join(mismatch))
    check("ERROR_CODES 11 条", len(cam.ERROR_CODES) == 11 and
          len(set(cam.ERROR_CODES)) == 11, str(len(cam.ERROR_CODES)))

    text = _page_text()
    keys = _cam_msg_keys(text)
    check(
        "前后端错误码键集合相等",
        keys == set(cam.ERROR_CODES),
        "front=%d server=%d" % (len(keys), len(cam.ERROR_CODES)),
    )
    main_text = MAIN_PY.read_text(encoding="utf-8")
    camera_routes = [
        m.start() for m in re.finditer(r'@app\.(get|post)\("/api/v1/camera/', main_text)
    ]
    mount_match = re.search(r'^\s*app\.mount\("/"', main_text, re.M)
    mount_at = mount_match.start() if mount_match else -1
    check(
        "/camera/* 路由 7 条且全在 app.mount 之前",
        len(camera_routes) == 7 and mount_at > 0
        and all(pos < mount_at for pos in camera_routes),
        "routes=%d mount=%d" % (len(camera_routes), mount_at),
    )
    check(
        "ack 404 仍为字符串 detail",
        'detail=f"告警 {alert_id} 不存在或已处置"' in main_text,
    )
    check("体积 ≤ 48 KB", len(text.encode("utf-8")) <= MAX_BYTES,
          "%d B" % len(text.encode("utf-8")))
    check("单文件且零外链", len([p for p in (BASE_DIR / "dashboard").iterdir() if p.is_file()]) == 1
          and not re.search(r"https?://|<link|<script[^>]*src|<img", text))

    numstat = _git("diff", "--numstat", "--", "service/main.py")
    cached = _git("diff", "--cached", "--numstat", "--", "service/main.py")
    deletions = 0
    for blob in (numstat, cached):
        for line in blob.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].isdigit():
                deletions += int(parts[1])
    check("main.py 删除行 = 0", deletions == 0, "deleted=%d" % deletions)

    req = _git("diff", "--name-only", "requirements.txt", "requirements.lock.txt",
               "requirements-c39.txt")
    req_cached = _git("diff", "--cached", "--name-only", "requirements.txt",
                      "requirements.lock.txt", "requirements-c39.txt")
    check(
        "requirements* 工作树未改且暂存区仅 c39",
        not req.strip() and set(req_cached.split()) <= {"requirements-c39.txt"},
        req_cached.strip().replace("\n", ","),
    )
    cam_text = CAM_PY.read_text(encoding="utf-8")
    check(
        "camera.py 无 PEP 604",
        not re.search(r": *[A-Za-z0-9_.\[\]]+ *\|", cam_text),
    )
    check("无 WebSocket/SSE 推送", "WebSocket" not in cam_text and
          "WebSocket" not in main_text and "EventSource" not in text)
    check("无 numpy/onnxruntime/ultralytics 导入",
          not re.search(r"^\s*(import|from)\s+(numpy|onnxruntime|ultralytics)", cam_text, re.M))
    check(
        "媒体请求只走 /api/v1/camera/",
        '"/api/v1/camera/video?source_id="' in text,
    )


def main():
    parser = argparse.ArgumentParser(description="现场画面验收（只读）")
    parser.add_argument("--sources", action="store_true", help="源清单契约")
    parser.add_argument("--range", action="store_true", help="视频 Range 判定")
    parser.add_argument("--kpts", action="store_true", help="关键点窗口")
    parser.add_argument("--frame", action="store_true", help="静态帧降级 + health")
    parser.add_argument("--ui", action="store_true", help="单文件/零外链/体积/面板锚点")
    parser.add_argument("--align", action="store_true", help="唯一时间基准/会话契约")
    parser.add_argument("--link", action="store_true", help="选源规则/事件隔离/中文提示")
    parser.add_argument("--status", action="store_true", help="状态机/退避/轮询隔离")
    parser.add_argument("--privacy", action="store_true", help="存储/导出/水印/日志")
    parser.add_argument("--frozen", action="store_true", help="冻结产物集合相等")
    parser.add_argument("--strict", action="store_true", help="总闸门（全量汇总）")
    args = parser.parse_args()

    runners = []
    for name, runner in (
        ("sources", run_sources),
        ("range", run_range),
        ("kpts", run_kpts),
        ("frame", run_frame),
        ("ui", run_ui),
        ("align", run_align),
        ("link", run_link),
        ("status", run_status),
        ("privacy", run_privacy),
        ("frozen", run_frozen),
        ("strict", run_strict),
    ):
        if getattr(args, name):
            runners.append(runner)

    if not runners:
        parser.print_help()
        return 1

    print("=" * 56)
    print("  现场画面验收（T-CAM-002~008）")
    print("=" * 56)
    for runner in runners:
        runner()

    passed, total = sum(RESULTS), len(RESULTS)
    print("-" * 56)
    print("结果：%d/%d 通过" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())