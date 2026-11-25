"""T-UP-002 · 记录、设备归属、幂等与删除。

运行：python -m unittest tests.test_upload_record -v
"""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402
from tests.upload_helpers import fake_record, write_source_dir  # noqa: E402


class TestUploadRecord(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="up_record_")
        self.base = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_upload_id_stable_and_device_scoped(self):
        digest = "a" * 64
        first = up.build_upload_id("ELEV-3F-001", digest)
        again = up.build_upload_id("ELEV-3F-001", digest)
        other = up.build_upload_id("ELEV-4F-001", digest)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 32)

    def test_source_id_length_and_charset(self):
        source_id = up.source_id_of("b" * 32)
        self.assertEqual(source_id, "up_" + "b" * 16)
        self.assertEqual(len(source_id), 19)
        self.assertTrue(all(ch.islower() or ch.isdigit() or ch == "_" for ch in source_id))

    def test_parse_task_id_roundtrip(self):
        record = fake_record(self.base)
        task_id = up.task_id_of(record)
        upload_id, received = up.parse_task_id(task_id)
        self.assertEqual(upload_id, record.upload_id)
        self.assertEqual(received, record.received_ms)
        with self.assertRaises(up.UploadError) as ctx:
            up.parse_task_id("bogus")
        self.assertEqual(ctx.exception.code, "UP_TASK_NOT_FOUND")

    def test_duration_ms(self):
        self.assertEqual(up.duration_ms_of(300, 25.0), 12000)
        self.assertEqual(up.duration_ms_of(500, 25.0), 20000)

    def test_manifest_roundtrip(self):
        record = fake_record(self.base)
        directory = write_source_dir(self.base, record)
        data = json.loads((directory / up.MANIFEST_NAME).read_text(encoding="utf-8"))
        loaded = up.record_from_manifest(data)
        self.assertEqual(loaded, record)
        self.assertEqual(data["vision_file"], up.VISION_NAME)
        self.assertEqual(data["decisions_file"], up.DECISIONS_NAME)
        self.assertEqual(data["final_event_type"], "normal")

    def test_items_sorted_by_received_desc(self):
        first = fake_record(self.base, upload_id="1" * 32, device="ELEV-1F-001", received_ms=1000)
        second = fake_record(self.base, upload_id="2" * 32, device="ELEV-2F-001", received_ms=2000)
        write_source_dir(self.base, first)
        write_source_dir(self.base, second)
        payload = up.items_payload(base_dir=self.base)
        self.assertEqual(payload["total"], 2)
        self.assertEqual([item["upload_id"] for item in payload["items"]], ["2" * 32, "1" * 32])
        self.assertEqual(payload["quota"]["items_used"], 2)
        self.assertEqual(payload["watermark"], up.WATERMARK)

    def test_expired_items_hidden(self):
        record = fake_record(self.base, upload_id="3" * 32, expire_ms=up.now_ms() - 1)
        write_source_dir(self.base, record)
        self.assertEqual(up.items_payload(base_dir=self.base)["total"], 0)
        with self.assertRaises(up.UploadError) as ctx:
            up.item_payload("3" * 32, base_dir=self.base)
        self.assertEqual(ctx.exception.code, "UP_NOT_FOUND")

    def test_delete_default_keeps_derived(self):
        record = fake_record(self.base, upload_id="4" * 32)
        directory = write_source_dir(self.base, record)
        result = up.delete_item("4" * 32, base_dir=self.base)
        self.assertTrue(result["ok"])
        self.assertTrue(result["deleted"][up.SOURCE_NAME])
        self.assertFalse((directory / up.SOURCE_NAME).exists())
        self.assertTrue((directory / up.MANIFEST_NAME).exists())
        self.assertTrue((directory / up.VISION_NAME).exists())

    def test_delete_purge_removes_dir(self):
        record = fake_record(self.base, upload_id="5" * 32)
        directory = write_source_dir(self.base, record)
        result = up.delete_item("5" * 32, purge=True, base_dir=self.base)
        self.assertTrue(result["deleted"][up.MANIFEST_NAME])
        self.assertFalse(directory.exists())

    def test_delete_unknown_404(self):
        with self.assertRaises(up.UploadError) as ctx:
            up.delete_item("6" * 32, base_dir=self.base)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()