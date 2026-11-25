"""T-UP-008 · 上传源可见性、水印与对齐。

运行：python -m unittest tests.test_upload_sources -v
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
import service.upload as up  # noqa: E402
from tests.upload_helpers import fake_record, write_source_dir  # noqa: E402

START = 1763300180000


def _vision_rows(count, start=START):
    return [
        {
            "ts": up.ts_of_frame(start, 25.0, index),
            "person_count": 1,
            "fall_prob": 0.0,
            "door_approach": 0.0,
            "crowded": 0.2,
            "keypoints_ok": False,
        }
        for index in range(count)
    ]


class TestUploadSources(unittest.TestCase):
    def setUp(self):
        cam.reset_state()
        self.temp = tempfile.TemporaryDirectory(prefix="up_src_")
        self.base = pathlib.Path(self.temp.name)

    def tearDown(self):
        cam.reset_state()
        self.temp.cleanup()

    def test_demo_sources_unchanged(self):
        payload = cam.sources_payload()
        self.assertEqual(len(payload["items"]), 5)
        self.assertTrue(all(item["demo"] is True for item in payload["items"]))
        self.assertEqual(payload["watermark"], cam.WATERMARK)

    def test_upload_source_fields(self):
        record = fake_record(
            self.base,
            upload_id="8" * 32,
            device="ELEV-6F-001",
            location="6F",
            state="reported",
            frames=10,
            duration_ms=400,
            start_ts_ms=START,
        )
        directory = write_source_dir(self.base, record, vision=False)
        up.write_manifest(
            directory / up.MANIFEST_NAME,
            record,
            final_event_type="trapped",
        )
        sources = cam.scan_uploads(self.base)
        self.assertEqual(len(sources), 1)
        item = sources[0].as_public()
        self.assertEqual(item["source_id"], record.source_id)
        self.assertEqual(item["event_type"], "trapped")
        self.assertFalse(item["demo"])
        self.assertTrue(item["video"])
        self.assertFalse(item["keypoints"])
        self.assertFalse(item["vision"])
        self.assertEqual(item["anchor_ts_ms"], START)
        self.assertEqual(item["watermark"], cam.UPLOAD_WATERMARK)
        self.assertIn("6F", item["label"])

    def test_expired_upload_hidden(self):
        record = fake_record(
            self.base, upload_id="9" * 32, expire_ms=up.now_ms() - 1, frames=10, duration_ms=400
        )
        write_source_dir(self.base, record)
        self.assertEqual(cam.scan_uploads(self.base), [])

    def test_deleted_source_hidden(self):
        record = fake_record(self.base, upload_id="a1" * 16, frames=10, duration_ms=400)
        directory = write_source_dir(self.base, record)
        self.assertEqual(len(cam.scan_uploads(self.base)), 1)
        (directory / up.SOURCE_NAME).unlink()
        self.assertEqual(cam.scan_uploads(self.base), [])

    def test_upload_alignment_implicit(self):
        record = fake_record(self.base, upload_id="b2" * 16, frames=10, duration_ms=400)
        directory = write_source_dir(self.base, record, vision=False)
        (directory / up.VISION_NAME).write_text(
            json.dumps(_vision_rows(10)), encoding="utf-8"
        )
        source = cam.scan_uploads(self.base)[0]

        cam._SOURCES = [source]
        cam._BOOTSTRAPPED = True
        cam._UPLOAD_CACHE = []
        state = cam.alignment(source.source_id)
        self.assertTrue(state.aligned)
        self.assertEqual(state.offset_ms, 0)
        self.assertEqual(state.drift_ms, 0)

    def test_kpts_upload_has_empty_persons_and_vision(self):
        record = fake_record(self.base, upload_id="c3" * 16, frames=10, duration_ms=400)
        directory = write_source_dir(self.base, record, vision=False)
        (directory / up.VISION_NAME).write_text(
            json.dumps(_vision_rows(10)), encoding="utf-8"
        )
        source = cam.scan_uploads(self.base)[0]
        cam._SOURCES = [source]
        cam._BOOTSTRAPPED = True
        cam._UPLOAD_CACHE = []
        cam._KPT_STORE.clear()

        payload = cam.kpts_payload(source.source_id, count=5)
        self.assertEqual(payload["count"], 5)
        self.assertEqual(payload["items"][0]["persons"], [])
        self.assertEqual(payload["items"][0]["vision"]["person_count"], 1)
        self.assertTrue(payload["aligned"])
        self.assertEqual(payload["offset_ms"], 0)


if __name__ == "__main__":
    unittest.main()