"""T-UP-006 · 队列、任务状态机与重启恢复。

运行：python -m unittest tests.test_upload_task -v
"""
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402
import service.upload_worker as worker  # noqa: E402
from common.schema import Decision  # noqa: E402
from tests.upload_helpers import fake_record, make_video, upload_result, write_source_dir  # noqa: E402


class FakeDB:
    def __init__(self):
        self.alerts = []

    def insert_alert(self, payload):
        self.alerts.append(dict(payload))
        return len(self.alerts)

    def upsert_device(self, device_id, ts, model_version=None):
        return None


class FakeCache:
    def __init__(self):
        self.keys = set()

    def setnx(self, key, ttl):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def set_state(self, device_id, payload, ttl=60):
        return None


def _decide(ts, trapped):
    return Decision(ts, "ELEV-3F-001", trapped, 2 if trapped else 0, "3F", 1 if trapped else 0, "trapped" if trapped else "normal", 0.8, [])


class TestUploadTask(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="up_task_")
        self.base = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_capacity_four_then_busy(self):
        manager = worker.UploadTaskManager(self.base)
        accepted = 0
        busy = None
        for index in range(5):
            record = fake_record(
                self.base,
                upload_id=("%031d%d" % (index, index)),
                device="ELEV-%dF-001" % (index + 1),
            )
            up.upload_dir(record.upload_id, self.base).mkdir(parents=True, exist_ok=True)
            try:
                manager.enqueue(record, created=True)
                accepted += 1
            except up.UploadError as exc:
                busy = exc
        self.assertEqual(accepted, 4)
        self.assertIsNotNone(busy)
        self.assertEqual(busy.status_code, 503)
        self.assertEqual(busy.code, "UP_BUSY")

    def test_unknown_task_404(self):
        manager = worker.UploadTaskManager(self.base)
        with self.assertRaises(up.UploadError) as ctx:
            manager.task_payload("deadbeef-123")
        self.assertEqual(ctx.exception.code, "UP_TASK_NOT_FOUND")

    def test_recover_interrupted_task_failed(self):
        record = fake_record(self.base, upload_id="7" * 32, state="extracted")
        write_source_dir(self.base, record)
        manager = worker.UploadTaskManager(self.base)
        manager.recover_on_startup()
        payload = manager.task_payload(up.task_id_of(record))
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error"]["code"], "UP_INTERNAL")

    def test_task_runs_to_done(self):
        video = make_video(self.base / "fixture4.mp4", seconds=4)
        result = upload_result(self.base, video)
        manager = worker.UploadTaskManager(self.base)
        manager.start(FakeDB(), FakeCache())
        try:
            manager.enqueue(result.record, created=True)
            deadline = time.time() + 120
            payload = manager.task_payload(up.task_id_of(result.record))
            while time.time() < deadline:
                payload = manager.task_payload(up.task_id_of(result.record))
                if payload["state"] in ("done", "failed"):
                    break
                time.sleep(0.05)
        finally:
            manager.stop()
        self.assertEqual(payload["state"], "done", payload["error"])
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["frames_done"], payload["frames_total"])
        self.assertEqual(payload["alerts"]["produced"], 0)

    def test_edge_alerts(self):
        start = 1763300180000
        continuous = [_decide(start + index * 40, True) for index in range(10)]
        self.assertEqual(len(worker.edge_alerts(continuous)), 1)
        twice = [
            _decide(start, True),
            _decide(start + 40, False),
            _decide(start + 80, True),
        ]
        self.assertEqual(len(worker.edge_alerts(twice)), 2)


if __name__ == "__main__":
    unittest.main()