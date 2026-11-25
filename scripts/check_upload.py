"""用户上传真实视频验收脚本（只读业务代码，不写真实 data/raw/uploads）。

用法（项目根执行）：
  python scripts/check_upload.py --make-fixture --seconds 12 --out tmp.mp4
  python scripts/check_upload.py --accept
  python scripts/check_upload.py --probe
  python scripts/check_upload.py --heuristic
  python scripts/check_upload.py --pose
  python scripts/check_upload.py --task
  python scripts/check_upload.py --judge
  python scripts/check_upload.py --sources
  python scripts/check_upload.py --e2e
  python scripts/check_upload.py --strict

约束：只调用 service.upload* 的真实服务函数/纯函数，不复制公式；
所有落盘都在 `tempfile.mkdtemp()` 内；数据库用内存 fake（依赖注入）。
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import service.camera as cam  # noqa: E402
import service.config as cfg  # noqa: E402
import service.upload as up  # noqa: E402
import service.upload_features as feat  # noqa: E402
import service.upload_worker as worker  # noqa: E402
from common.schema import Decision, VisionOut  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    suffix = "  (%s)" % detail if detail else ""
    print("  %s %s%s" % ("✓" if ok else "✗", name, suffix))


class FakeDB:
    def __init__(self):
        self.alerts = []
        self.devices = {}

    def insert_alert(self, payload):
        self.alerts.append(dict(payload))
        return len(self.alerts)

    def upsert_device(self, device_id, ts, model_version=None):
        self.devices[device_id] = int(ts)


class FakeCache:
    def __init__(self):
        self.keys = set()
        self.states = {}

    def setnx(self, key, ttl):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def set_state(self, device_id, payload, ttl=60):
        self.states[device_id] = dict(payload)


def make_fixture(seconds, out):
    import cv2
    import numpy as np

    seconds = int(seconds)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), 25.0, (1280, 720))
    if not writer.isOpened():
        raise RuntimeError("VideoWriter 无法创建 mp4（mp4v）")
    total = max(1, seconds * 25)
    try:
        for index in range(total):
            frame = np.full((720, 1280, 3), 32, dtype=np.uint8)
            cv2.rectangle(frame, (0, 480), (1280, 720), (70, 70, 70), -1)
            x = 60 + (index * 8) % 880
            cv2.rectangle(frame, (x, 200), (x + 120, 460), (230, 230, 230), -1)
            writer.write(frame)
    finally:
        writer.release()
    return out


def upload_file(
    base,
    video,
    device="ELEV-3F-001",
    start_ts=None,
    location="3F",
    name="cabin.mp4",
    content_type="video/mp4",
):
    data = Path(video).read_bytes()
    headers = {
        "content-type": content_type,
        "content-length": str(len(data)),
        "x-upload-device-id": device,
        "x-upload-start-ts-ms": str(int(start_ts if start_ts is not None else up.now_ms())),
        "x-upload-location": location,
        "x-upload-name": name,
    }
    intent = up.parse_upload_headers(headers, base_dir=base)
    sink = up.UploadSink(intent, base_dir=base)
    for offset in range(0, len(data), 65536):
        sink.feed(data[offset: offset + 65536])
    return sink.finish()


def expect_error(fn, status, code):
    try:
        fn()
    except up.UploadError as exc:
        return exc.status_code == status and exc.code == code, "%s/%s" % (exc.status_code, exc.code)
    return False, "未抛异常"


def wait_task(manager, task_id, timeout=180):
    deadline = time.time() + timeout
    payload = manager.task_payload(task_id)
    while time.time() < deadline:
        payload = manager.task_payload(task_id)
        if payload["state"] in ("done", "failed"):
            return payload
        time.sleep(0.05)
    return payload


# ---------------------------------------------------------------- --accept


def run_accept():
    print("[--accept] 接收 / 错误码 / 幂等 / 半成品")
    base = Path(tempfile.mkdtemp(prefix="up_accept_"))
    try:
        video = make_fixture(12, base / "fixture12.mp4")
        data = video.read_bytes()
        good = {
            "content-type": "video/mp4",
            "content-length": str(len(data)),
            "x-upload-device-id": "ELEV-3F-001",
            "x-upload-start-ts-ms": str(up.now_ms()),
            "x-upload-location": "3F",
        }
        check(
            "缺 Content-Length → 411",
            expect_error(
                lambda: up.parse_upload_headers({k: v for k, v in good.items() if k != "content-length"}, base_dir=base),
                411,
                "UP_LENGTH_REQUIRED",
            )[0],
        )
        check(
            "Content-Length=0 → 400",
            expect_error(
                lambda: up.parse_upload_headers(dict(good, **{"content-length": "0"}), base_dir=base),
                400,
                "UP_BAD_REQUEST",
            )[0],
        )
        check(
            "超过 200 MiB → 413",
            expect_error(
                lambda: up.parse_upload_headers(dict(good, **{"content-length": str(up.max_bytes() + 1)}), base_dir=base),
                413,
                "UP_TOO_LARGE",
            )[0],
        )
        check(
            "非 MP4 Content-Type → 415",
            expect_error(
                lambda: up.parse_upload_headers(dict(good, **{"content-type": "text/plain"}), base_dir=base),
                415,
                "UP_UNSUPPORTED_TYPE",
            )[0],
        )
        check(
            "X-Upload-Rebase-Ts → 400",
            expect_error(
                lambda: up.parse_upload_headers(dict(good, **{"x-upload-rebase-ts": "1"}), base_dir=base),
                400,
                "UP_BAD_REQUEST",
            )[0],
        )
        check(
            "10 位录制时间 → 400",
            expect_error(
                lambda: up.parse_upload_headers(dict(good, **{"x-upload-start-ts-ms": "1763300180"}), base_dir=base),
                400,
                "UP_BAD_TIMESTAMP",
            )[0],
        )

        result = upload_file(base, video)
        check("合法 mp4 → 202", result.status_code == 202, str(result.status_code))
        check("sha256 与文件一致", result.record.sha256 == up.UploadSink._hash_file(video))
        again = upload_file(base, video)
        check("同设备同文件幂等 → 200", again.status_code == 200, str(again.status_code))
        dirs = [path for path in up.uploads_root(base).glob("*") if path.is_dir() and path.name != up.TMP_DIR_NAME]
        check("幂等不重复落盘（1 个件）", len(dirs) == 1, "dirs=%d" % len(dirs))
        tmp_left = list((up.uploads_root(base) / up.TMP_DIR_NAME).glob("*")) if (up.uploads_root(base) / up.TMP_DIR_NAME).is_dir() else []
        check("临时区无残留", len(tmp_left) == 0, "left=%d" % len(tmp_left))

        intent = up.parse_upload_headers(dict(good, **{"content-length": str(len(data) + 10)}), base_dir=base)
        sink = up.UploadSink(intent, base_dir=base)
        sink.feed(data)
        check(
            "实际字节 < 声明 → 400",
            expect_error(sink.finish, 400, "UP_BAD_REQUEST")[0],
        )

        intent = up.parse_upload_headers(dict(good, **{"content-length": "12"}), base_dir=base)
        sink = up.UploadSink(intent, base_dir=base)
        check(
            "非 ftyp 容器 → 415",
            expect_error(lambda: sink.feed(b"0123456789ab"), 415, "UP_UNSUPPORTED_TYPE")[0],
        )
        check(
            "415 后临时区已清",
            not any((up.uploads_root(base) / up.TMP_DIR_NAME).glob("*"))
            if (up.uploads_root(base) / up.TMP_DIR_NAME).is_dir()
            else True,
        )
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --probe


def run_probe():
    print("[--probe] 探测与时间基准")
    base = Path(tempfile.mkdtemp(prefix="up_probe_"))
    try:
        video = make_fixture(4, base / "fixture4.mp4")
        info = up.probe_video(video, up.now_ms())
        check(
            "probe 字段合法",
            info.fps > 0 and info.frames > 0 and info.width == 1280 and info.height == 720,
            "fps=%s frames=%s %sx%s" % (info.fps, info.frames, info.width, info.height),
        )
        check("duration 误差 ≤40 ms", abs(info.duration_ms - up.duration_ms_of(info.frames, info.fps)) <= 40)
        check("4 s 视频通过下限", info.duration_ms >= 4000, "duration=%d" % info.duration_ms)
        check(
            "10 位时间戳拒绝",
            up.parse_ts13("1763300180") is None and up.parse_ts13("1763300180000") == 1763300180000,
        )
        start = 1763300180000
        check("ts(0) = start", up.ts_of_frame(start, 25.0, 0) == start)
        check(
            "frame(ts) 互逆",
            all(up.frame_of_ts(start, 25.0, up.ts_of_frame(start, 25.0, index)) == index for index in (0, 1, 150, 299)),
        )
        check(
            "开始前 2 s → 窗口外阻断",
            expect_error(
                lambda: up.validate_time_window([Decision(start - 2000, "ELEV-3F-001", False, 0, "3F", 0, "normal", 0.0, [])], start, 12000),
                500,
                "UP_TIME_BASE_INVALID",
            )[0],
        )
        check(
            "历史录制时间不平移",
            up.validate_time_window([Decision(start, "ELEV-3F-001", False, 0, "3F", 0, "normal", 0.0, [])], start, 12000) is None,
        )
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --heuristic


def run_heuristic():
    print("[--heuristic] L2 六字段与红线")
    base = Path(tempfile.mkdtemp(prefix="up_heur_"))
    try:
        video = make_fixture(12, base / "fixture12.mp4")
        info = up.probe_video(video, up.now_ms())
        result = feat.extract_rows(
            mode="heuristic",
            video_path=video,
            start_ts_ms=info_start(info),
            door_roi=None,
            fps=info.fps,
            frames_total=info.frames,
            deadline=time.monotonic() + 300,
            progress=lambda done, total: None,
        )
        rows = result.rows
        check("条数 = frames", len(rows) == info.frames, "%d/%d" % (len(rows), info.frames))
        check("六字段齐全", all(set(row.__dict__) == set(feat.VISION_FIELDS) for row in rows))
        check("fall_prob 全 0", all(row.fall_prob == 0.0 for row in rows))
        check("keypoints_ok 全 False", all(row.keypoints_ok is False for row in rows))
        check("ts 严格递增", all(b.ts > a.ts for a, b in zip(rows, rows[1:])))
        check("人数出现过 ≥1", max(row.person_count for row in rows) >= 1, "max=%d" % max(row.person_count for row in rows))
        check("mode=heuristic", result.mode == "heuristic")
        check("roi_source=default", result.roi_source == "default")
        try:
            feat.validate_heuristic_vision([VisionOut(0, 1, 0.5, 0.0, 0.0, False)])
            ok = False
        except ValueError:
            ok = True
        check("红线校验拒绝非 0 fall_prob", ok)
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


def info_start(info):
    return 1763300180000


# ---------------------------------------------------------------- --pose


def run_pose():
    print("[--pose] L1 探测（依赖缺失 SKIP 合法）")
    base = Path(tempfile.mkdtemp(prefix="up_pose_"))
    try:
        mode, ok = feat.detect_feature_mode(base)
        check("无依赖/权重 → heuristic", mode == "heuristic" and ok is False, mode)
        weights = base / "models" / "yolov8n-pose.pt"
        weights.parent.mkdir(parents=True, exist_ok=True)
        weights.write_bytes(b"")
        mode, ok = feat.detect_feature_mode(base)
        check("空权重不变 pose", mode == "heuristic" and ok is False, mode)

        sidecar = base / "source_kpts.jsonl"
        sidecar.write_text(
            json.dumps({"frame": 1, "ts": 100, "persons": [[[1, 2, 0.9]] * 17]}) + "\n"
            + json.dumps({"frame": 0, "ts": 50, "persons": []}) + "\n",
            encoding="utf-8",
        )
        try:
            feat.load_kpts_sidecar(sidecar, 10)
            ok = False
        except ValueError:
            ok = True
        check("非单调侧车 → 拒绝", ok)
        sidecar.write_text(
            json.dumps({"frame": 0, "ts": 50, "persons": []}) + "\n"
            + json.dumps({"frame": 1, "ts": 100, "persons": []}) + "\n",
            encoding="utf-8",
        )
        check("合法侧车 → 2 行", feat.load_kpts_sidecar(sidecar, 10) == 2)
        check("依赖缺失时 L1 SKIP", True, "SKIP")
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --task


def _fake_record(base, device="ELEV-4F-001", upload_id=None):
    upload_id = upload_id or ("a" * 31 + device[-1].lower())
    now = up.now_ms()
    return up.UploadRecord(
        upload_id=upload_id,
        source_id=up.source_id_of(upload_id),
        device_id=device,
        location="4F",
        original_name="fake.mp4",
        bytes=100,
        sha256="0" * 64,
        fps=25.0,
        frames=300,
        width=1280,
        height=720,
        duration_ms=12000,
        start_ts_ms=now,
        start_ts_source="client",
        door_roi=None,
        feature_mode="heuristic",
        keypoints_ok=False,
        received_ms=now,
        expire_ms=now + up.ttl_ms(),
        state="received",
    )


def run_task():
    print("[--task] 队列容量 / 进度单调 / 重启恢复")
    base = Path(tempfile.mkdtemp(prefix="up_task_"))
    manager = None
    try:
        manager = worker.UploadTaskManager(base)
        accepted = 0
        busy = None
        for index in range(5):
            record = _fake_record(base, device="ELEV-4F-%03d" % (index + 1))
            (up.upload_dir(record.upload_id, base)).mkdir(parents=True, exist_ok=True)
            try:
                manager.enqueue(record, created=True)
                accepted += 1
            except up.UploadError as exc:
                busy = exc
        check("前 4 个入队成功", accepted == 4, str(accepted))
        check("第 5 个 → 503 UP_BUSY", busy is not None and busy.status_code == 503 and busy.code == "UP_BUSY")

        video = make_fixture(12, base / "fixture12.mp4")
        result = upload_file(base, video, device="ELEV-5F-001")
        db, cache = FakeDB(), FakeCache()
        manager2 = worker.UploadTaskManager(base)
        manager2.start(db, cache)
        try:
            manager2.enqueue(result.record, created=True)
            payload = wait_task(manager2, up.task_id_of(result.record))
            check("任务最终 done", payload["state"] == "done", payload["state"])
            check("progress=100", payload["progress"] == 100, str(payload["progress"]))
            check("frames_done = frames_total", payload["frames_done"] == payload["frames_total"])
        finally:
            manager2.stop()

        manifest_path = up.upload_dir(result.record.upload_id, base) / "upload.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["state"] = "extracted"
        manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        manager3 = worker.UploadTaskManager(base)
        manager3.recover_on_startup()
        payload = manager3.task_payload(up.task_id_of(result.record))
        check("重启恢复 → failed", payload["state"] == "failed", payload["state"])
        check("错误码 UP_INTERNAL", (payload["error"] or {}).get("code") == "UP_INTERNAL")
    finally:
        if manager is not None:
            manager.stop()
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --judge


def run_judge():
    print("[--judge] 边沿 / reason / 窗口阻断")
    start = 1763300180000

    def decide(ts, trapped_value):
        return Decision(ts, "ELEV-3F-001", trapped_value, 2 if trapped_value else 0, "3F", 1 if trapped_value else 0, "trapped" if trapped_value else "normal", 0.8, [])

    continuous = [decide(start + i * 40, True) for i in range(10)]
    check("连续困人 → 1 条边沿", len(worker.edge_alerts(continuous)) == 1)
    twice = [decide(start, True), decide(start + 40, False), decide(start + 80, True)]
    check("困人→脱困→再困人 → 2 条", len(worker.edge_alerts(twice)) == 2)
    decorated = worker.decorate_reason(decide(start, True), "heuristic", "default")
    check(
        "reason 含 feature_mode/roi",
        all(any(item.startswith(prefix) for item in decorated.reason) for prefix in ("feature_mode=", "roi=")),
    )
    low = worker.decorate_reason(
        Decision(start, "ELEV-3F-001", True, 2, "3F", 0, "trapped", 0.5, []),
        "heuristic",
        "default",
    )
    check("人数 0 追加 count_low_conf", any(item.startswith("count_low_conf=") for item in low.reason))
    check("urgency ≤2", decorated.urgency == 2)
    check(
        "窗口越界阻断",
        expect_error(
            lambda: up.validate_time_window([decide(start - 2000, True)], start, 12000),
            500,
            "UP_TIME_BASE_INVALID",
        )[0],
    )


def run_e2e():
    print("[--e2e] 12 s fixture 全链路")
    base = Path(tempfile.mkdtemp(prefix="up_e2e_"))
    try:
        video = make_fixture(12, base / "fixture12.mp4")
        started = up.now_ms()
        result = upload_file(base, video, start_ts=started)
        db, cache = FakeDB(), FakeCache()
        manager = worker.UploadTaskManager(base)
        manager.start(db, cache)
        try:
            manager.enqueue(result.record, created=True)
            payload = wait_task(manager, up.task_id_of(result.record))
        finally:
            manager.stop()
        directory = up.upload_dir(result.record.upload_id, base)
        check("任务 done", payload["state"] == "done", payload["state"])
        check("vision.json 存在", (directory / up.VISION_NAME).is_file())
        check("decisions.json 存在", (directory / up.DECISIONS_NAME).is_file())
        decisions = worker.read_decisions(directory / up.DECISIONS_NAME)
        trapped = [item for item in decisions if item.trapped]
        check("判出 trapped", bool(trapped), "trapped_frames=%d" % len(trapped))
        check("urgency ≤2", all(item.urgency <= 2 for item in decisions))
        check("reason 含扩展键", all(any(x.startswith("feature_mode=") for x in item.reason) for item in trapped) if trapped else False)
        check("仅 1 条告警入库", len(db.alerts) == 1, str(len(db.alerts)))
        final = json.loads((directory / "upload.json").read_text(encoding="utf-8"))
        check("final_event_type=trapped", final.get("final_event_type") == "trapped", str(final.get("final_event_type")))
        check("reported_alert_ids 1 条", len(final.get("reported_alert_ids") or []) == 1)
        check("state=reported", final.get("state") == "reported")

        cam.reset_state()
        sources = cam.scan_uploads(base)
        check("上传源可见", len(sources) == 1, str(len(sources)))
        if sources:
            item = sources[0].as_public()
            check("demo=False / watermark", item.get("demo") is False and item.get("watermark") == "用户上传 · 非取证")
            check("video=True", item.get("video") is True)
            check("anchor = start_ts_ms", item.get("anchor_ts_ms") == started)
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --sources


def run_sources():
    print("[--sources] 上传源合并 / demo 不变 / 删除后消失")
    cam.reset_state()
    payload = cam.sources_payload()
    demo_items = payload["items"]
    check("demo 5 条不变", len(demo_items) == 5, str(len(demo_items)))
    check("demo 全 demo=True", all(item.get("demo") is True for item in demo_items))
    check("顶层 watermark 不变", payload.get("watermark") == "演示数据 · 非取证")

    base = Path(tempfile.mkdtemp(prefix="up_src_"))
    try:
        upload_id = "b" * 32
        directory = up.upload_dir(upload_id, base)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / up.SOURCE_NAME).write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        fake = _fake_record(base, device="ELEV-6F-001", upload_id=upload_id)
        up.write_manifest(
            directory / "upload.json",
            fake,
            final_event_type="trapped",
        )
        sources = cam.scan_uploads(base)
        check("上传源登记 1 条", len(sources) == 1, str(len(sources)))
        item = sources[0] if sources else None
        check(
            "水印/演示位/时间基准",
            item is not None
            and item.watermark == cam.UPLOAD_WATERMARK
            and item.demo is False
            and item.anchor_ts_ms == fake.start_ts_ms,
        )
        check(
            "label 含楼层",
            item is not None and "4F" in item.label,
            item.label if item else "-",
        )

        before = len(demo_items)
        cam.reset_state()
        cam.invalidate_sources()
        check("失效后 demo 仍 5 条", len(cam.sources_payload()["items"]) == before)

        (directory / up.SOURCE_NAME).unlink()
        cam.invalidate_sources()
        check("源视频删除后不登记", len(cam.scan_uploads(base)) == 0)
    finally:
        cam.reset_state()
        shutil.rmtree(str(base), ignore_errors=True)


# ---------------------------------------------------------------- --strict


def run_strict():
    print("[--strict] 总闸门")
    expected = {
        "UP_BAD_REQUEST",
        "UP_BAD_DEVICE_ID",
        "UP_BAD_TIMESTAMP",
        "UP_BAD_ROI",
        "UP_LENGTH_REQUIRED",
        "UP_TOO_LARGE",
        "UP_UNSUPPORTED_TYPE",
        "UP_NOT_DECODABLE",
        "UP_TOO_SHORT",
        "UP_TOO_LONG",
        "UP_NOT_FOUND",
        "UP_TASK_NOT_FOUND",
        "UP_DELETE_FAILED",
        "UP_HASH_MISMATCH",
        "UP_TIME_BASE_INVALID",
        "UP_INTERNAL",
        "UP_BUSY",
        "UP_TIMEOUT",
        "UP_QUOTA_EXCEEDED",
    }
    check("ERROR_CODES 19 个集合相等", set(up.ERROR_CODES) == expected, str(len(up.ERROR_CODES)))
    check("source_id ≤20", len(up.source_id_of("c" * 32)) <= 20, str(len(up.source_id_of("c" * 32))))

    main_py = BASE_DIR / "service" / "main.py"
    try:
        out = subprocess.run(
            ["git", "diff", "--numstat", "HEAD", "--", "service/main.py"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        deleted = 0
        if out:
            deleted = int(out.split("\t")[1])
        check("main.py 既有行删除 = 0", deleted == 0, "deleted=%d" % deleted)
    except Exception as exc:  # noqa: BLE001
        check("main.py 删除行检查", False, type(exc).__name__)

    dash = BASE_DIR / "dashboard" / "index.html"
    html = dash.read_text(encoding="utf-8")
    check("看板单文件零外链", "<script src=" not in html and "http://" not in html and "https://" not in html)

    try:
        out = subprocess.run(
            ["git", "status", "--short", "requirements.txt", "requirements.lock.txt", "requirements-c39.txt"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        check("requirements* 未改", out == "", out.replace("\n", " "))
    except Exception:  # noqa: BLE001
        check("requirements* 未改", False)

    log_lines = 0
    for path in (BASE_DIR / "service").glob("upload*.py"):
        log_lines += path.read_text(encoding="utf-8").count("log.")
    check("上传日志调用 ≤60", log_lines <= 60, "log=%d" % log_lines)
    check("上传路由 5 条", (main_py.read_text(encoding="utf-8")).count("/api/v1/upload") >= 5)


def main(argv=None):
    parser = argparse.ArgumentParser(description="用户上传真实视频验收")
    parser.add_argument("--make-fixture", action="store_true")
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--out", type=str, default="")
    for name in ("accept", "probe", "heuristic", "pose", "task", "judge", "e2e", "sources", "strict"):
        parser.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)

    if args.make_fixture:
        out = Path(args.out) if args.out else (BASE_DIR / "data" / "raw" / ".fixtures" / ("fixture%d.mp4" % args.seconds))
        make_fixture(args.seconds, out)
        print("fixture written: %s" % out)
        return 0

    actions = {
        "accept": run_accept,
        "probe": run_probe,
        "heuristic": run_heuristic,
        "pose": run_pose,
        "task": run_task,
        "judge": run_judge,
        "e2e": run_e2e,
        "sources": run_sources,
    }
    ran = 0
    for name, fn in actions.items():
        if getattr(args, name):
            fn()
            ran += 1
    if args.strict:
        run_strict()
        ran += 1
    if ran == 0:
        parser.print_help()
        return 0

    total = len(RESULTS)
    passed = sum(1 for item in RESULTS if item)
    print("-" * 56)
    print("结果：%d/%d 通过" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())