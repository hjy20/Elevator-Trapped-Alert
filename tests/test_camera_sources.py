"""T-CAM-001 · 画面源登记与时间基准（契约单一来源）。

运行：python -m unittest tests.test_camera_sources -v

覆盖（≥10 项）：5 源与锚点逐字相等、排序稳定、IMU 侧车排除、
缺字段/ID 不一致 fixture 被跳过、frame_ms、三种偏移对齐误差、
越界钳制、无会话 aligned=False、重复 start 覆盖、TTL 过期、
对外字段不含内部路径、错误码 11 条、usb/rtsp 模式不崩。
"""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.camera as cam  # noqa: E402
import service.config as cfg  # noqa: E402

DEMO_DIR = ROOT / "common" / "demo_data"
ANCHORS = {
    "demo_normal": 1794915000000,
    "demo_estop": 1794915300000,
    "demo_trapped": 1794915600000,
    "demo_pry": 1794915900000,
    "demo_faint": 1794916200000,
}


def _meta_payload(case):
    return {
        "case": case,
        "fps": 25.0,
        "frames": 500,
        "width": 1280,
        "height": 720,
        "duration_sec": 20.0,
        "start_ts_ms": 1794916200000,
        "event_type": "faint",
        "keypoint_sidecar": "%s_kpts.jsonl" % case,
    }


def _write_meta(root, file_case, drop=(), **overrides):
    payload = _meta_payload(file_case)
    payload.update(overrides)
    for name in drop:
        payload.pop(name, None)
    demo = root / "common" / "demo_data"
    demo.mkdir(parents=True, exist_ok=True)
    path = demo / ("%s_meta.json" % file_case)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestCameraSources(unittest.TestCase):
    def setUp(self):
        self._mode = cfg.CAMERA_SOURCE_MODE
        self._enable = cfg.CAMERA_ENABLE
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        cam.reset_state()

    def tearDown(self):
        cfg.CAMERA_SOURCE_MODE = self._mode
        cfg.CAMERA_ENABLE = self._enable
        cam.reset_state()
        self._tmp.cleanup()

    # ---- 源清单 -------------------------------------------------------

    def test_five_sources_and_anchors(self):
        sources = cam.scan_sources()
        self.assertEqual(sorted(item.source_id for item in sources), sorted(ANCHORS))
        for source in sources:
            self.assertEqual(source.anchor_ts_ms, ANCHORS[source.source_id])
            meta = json.loads(
                (DEMO_DIR / ("%s_meta.json" % source.source_id)).read_text(encoding="utf-8")
            )
            self.assertEqual(source.fps, float(meta["fps"]))
            self.assertEqual(source.frames, int(meta["frames"]))
            self.assertEqual(source.width, int(meta["width"]))
            self.assertEqual(source.height, int(meta["height"]))
            self.assertEqual(source.event_type, meta["event_type"])
            self.assertEqual(
                source.duration_ms, int(round(int(meta["frames"]) * 1000.0 / float(meta["fps"])))
            )
            self.assertEqual(source.device_id, cam.DEFAULT_DEVICE_ID)

    def test_sources_sorted_and_stable(self):
        first = [item.source_id for item in cam.scan_sources()]
        second = [item.source_id for item in cam.scan_sources()]
        self.assertEqual(first, sorted(first))
        self.assertEqual(first, second)

    def test_imu_sidecar_excluded(self):
        ids = [item.source_id for item in cam.scan_sources()]
        self.assertFalse(any("imu" in source_id for source_id in ids))
        self.assertTrue((DEMO_DIR / "demo_faint_imu_meta.json").is_file())

    def test_capability_bits_follow_disk(self):
        faint = cam.require_source("demo_faint")
        self.assertTrue(faint.video)
        self.assertTrue(faint.keypoints)
        self.assertTrue(faint.vision)
        self.assertIn("video", faint.transport)
        self.assertIn("keypoints", faint.transport)

    def test_missing_field_fixture_skipped(self):
        _write_meta(self.tmp, "demo_broken", drop=("fps",))
        self.assertEqual(cam.scan_sources(self.tmp), [])

    def test_id_mismatch_fixture_skipped(self):
        _write_meta(self.tmp, "demo_other", case="demo_mismatch")
        self.assertEqual(cam.scan_sources(self.tmp), [])

    def test_duration_mismatch_fixture_skipped(self):
        _write_meta(self.tmp, "demo_bad_duration", duration_sec=35.0)
        self.assertEqual(cam.scan_sources(self.tmp), [])

    def test_imu_like_meta_never_scanned(self):
        demo = self.tmp / "common" / "demo_data"
        demo.mkdir(parents=True, exist_ok=True)
        (demo / "demo_x_imu_meta.json").write_text(
            json.dumps({"case": "demo_x_imu"}), encoding="utf-8"
        )
        self.assertEqual(cam.scan_sources(self.tmp), [])

    def test_bootstrap_is_idempotent(self):
        first = cam.bootstrap()
        second = cam.bootstrap()
        self.assertEqual(len(first), 5)
        self.assertEqual([item.source_id for item in first], [item.source_id for item in second])

    def test_mode_unsupported_returns_empty(self):
        cfg.CAMERA_SOURCE_MODE = "rtsp"
        cam.reset_state()
        self.assertEqual(cam.bootstrap(), [])
        payload = cam.sources_payload(now=1)
        self.assertEqual(payload["items"], [])
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["mode"], "rtsp")

    def test_camera_disabled_returns_empty(self):
        cfg.CAMERA_ENABLE = "0"
        cam.reset_state()
        self.assertEqual(cam.bootstrap(), [])
        self.assertEqual(cam.load_sources(), [])

    # ---- 时间基准 -----------------------------------------------------

    def test_frame_ms_is_forty(self):
        self.assertEqual(cam.frame_ms_of(cam.require_source("demo_faint")), 40)

    def test_alignment_roundtrip_three_offsets(self):
        source = cam.require_source("demo_faint")
        frame = 300
        for offset in (0, -31616020000, 86400000):
            alert_ts = cam.wall_ms_of(source, frame, offset)
            hit, clamped = cam.frame_for_alert(source, alert_ts, offset)
            self.assertEqual(hit, frame)
            self.assertFalse(clamped)
            self.assertLessEqual(abs(cam.wall_ms_of(source, hit, offset) - alert_ts), 200)

    def test_frame_for_alert_clamped_both_ends(self):
        source = cam.require_source("demo_faint")
        low, low_clamped = cam.frame_for_alert(source, source.anchor_ts_ms - 10 ** 9, 0)
        high, high_clamped = cam.frame_for_alert(source, source.anchor_ts_ms + 10 ** 9, 0)
        self.assertEqual((low, low_clamped), (0, True))
        self.assertEqual((high, high_clamped), (499, True))
        self.assertTrue(0 <= low <= 499 and 0 <= high <= 499)

    # ---- 会话 ---------------------------------------------------------

    def test_no_session_not_aligned(self):
        state = cam.alignment("demo_faint", now=1700000000000)
        self.assertFalse(state.aligned)
        self.assertEqual(state.offset_ms, 0)
        self.assertEqual(state.drift_ms, 0)
        self.assertIsNone(cam.session_get("demo_faint", now=1700000000000))

    def test_session_start_overrides_same_source(self):
        first = cam.session_start("demo_faint", 1763300180000, now=1000)
        second = cam.session_start("demo_faint", 1763300190000, now=2000)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(cam.session_count(), 1)
        current = cam.session_get("demo_faint", now=2000)
        self.assertIsNotNone(current)
        self.assertEqual(current.offset_ms, second.offset_ms)

    def test_session_ttl_expires(self):
        cam.session_start("demo_faint", 1763300180000, now=1000)
        alive = cam.alignment("demo_faint", now=1000 + cam.SESSION_TTL_MS - 1)
        self.assertTrue(alive.aligned)
        expired = cam.alignment("demo_faint", now=1000 + cam.SESSION_TTL_MS)
        self.assertFalse(expired.aligned)
        self.assertEqual(cam.session_count(), 0)

    def test_heartbeat_keeps_offset(self):
        started = cam.session_start("demo_faint", 1763300180000, now=1000)
        refreshed = cam.session_heartbeat("demo_faint", now=5000)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.offset_ms, started.offset_ms)
        self.assertGreater(refreshed.updated_ms, started.updated_ms)

    def test_stop_clears_session(self):
        cam.session_start("demo_faint", 1763300180000, now=1000)
        self.assertIsNotNone(cam.session_stop("demo_faint", now=2000))
        self.assertFalse(cam.alignment("demo_faint", now=2000).aligned)
        self.assertEqual(cam.session_count(), 0)

    # ---- 对外契约 -----------------------------------------------------

    def test_public_fields_hide_paths(self):
        public = cam.require_source("demo_faint").as_public()
        expected = {
            "source_id",
            "device_id",
            "label",
            "event_type",
            "fps",
            "frames",
            "width",
            "height",
            "duration_ms",
            "anchor_ts_ms",
            "video",
            "keypoints",
            "vision",
            "transport",
            "demo",
        }
        self.assertEqual(set(public), expected)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("video_path", blob)
        self.assertNotIn("kpts_path", blob)
        self.assertNotIn("\\\\", blob)

    def test_label_is_chinese_and_short(self):
        for source in cam.scan_sources():
            self.assertTrue(source.label)
            self.assertLessEqual(len(source.label), 24)

    def test_error_codes_match_contract(self):
        self.assertEqual(len(cam.ERROR_CODES), 11)
        self.assertEqual(len(set(cam.ERROR_CODES)), 11)
        for code in (
            "CAM_BAD_REQUEST",
            "CAM_BAD_TIMESTAMP",
            "CAM_BAD_FRAME",
            "CAM_WINDOW_TOO_LARGE",
            "CAM_SOURCE_NOT_FOUND",
            "CAM_VIDEO_NOT_FOUND",
            "CAM_NOT_ALIGNED",
            "CAM_RANGE_INVALID",
            "CAM_TRANSPORT_UNSUPPORTED",
            "CAM_NOT_READY",
            "CAM_INTERNAL",
        ):
            self.assertIn(code, cam.ERROR_CODES)

    def test_error_detail_shape(self):
        detail = cam.error_detail("CAM_SOURCE_NOT_FOUND", "未找到该画面源：demo_x", source_id="demo_x")
        self.assertEqual(detail["code"], "CAM_SOURCE_NOT_FOUND")
        self.assertEqual(detail["message"], "未找到该画面源：demo_x")
        self.assertEqual(detail["source_id"], "demo_x")

    def test_sources_payload_normal_has_no_degraded(self):
        payload = cam.sources_payload(now=1)
        self.assertEqual(set(payload), {"items", "server_time_ms", "mode", "watermark"})
        self.assertEqual(len(payload["items"]), 5)
        self.assertEqual(payload["watermark"], "演示数据 · 非取证")
        self.assertEqual(payload["server_time_ms"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)