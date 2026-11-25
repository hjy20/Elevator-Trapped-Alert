"""T-UP-007 · 上传件判决、reason 扩展与边沿。

运行：python -m unittest tests.test_upload_judge -v
"""
import dataclasses
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402
import service.upload_worker as worker  # noqa: E402
from common.schema import Decision, VisionOut  # noqa: E402
from model.config import PRESET_DEMO  # noqa: E402
from model.fuse import FusionRunner  # noqa: E402
from tests.upload_helpers import fake_record  # noqa: E402

START = 1763300180000


def _rows(count=300):
    return [
        VisionOut(
            ts=up.ts_of_frame(START, 25.0, index),
            person_count=1,
            fall_prob=0.0,
            door_approach=0.0,
            crowded=0.2,
            keypoints_ok=False,
        )
        for index in range(count)
    ]


class TestUploadJudge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="up_judge_")
        self.base = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_trapped_after_dwell(self):
        record = fake_record(self.base, start_ts_ms=START)
        cfg = worker.build_runtime_config(record)
        self.assertEqual(cfg.thresholds.dwell_threshold, 8.0)
        decisions = FusionRunner(cfg).run(_rows(), [])
        trapped = [item for item in decisions if item.trapped]
        self.assertTrue(trapped)
        self.assertTrue(all(item.urgency <= 2 for item in decisions))
        self.assertTrue(all(item.event_type in ("trapped", "pry") for item in trapped))

    def test_continuous_trapped_single_edge(self):
        edges = worker.edge_alerts(FusionRunner(PRESET_DEMO).run(_rows(), []))
        self.assertEqual(len(edges), 1)

    def test_decorate_reason_adds_upload_keys(self):
        decision = Decision(START, "ELEV-3F-001", True, 2, "3F", 1, "trapped", 0.8, ["dwell=9s > 8s"])
        decorated = worker.decorate_reason(decision, "heuristic", "default")
        self.assertIn("feature_mode=heuristic", decorated.reason)
        self.assertIn("roi=default", decorated.reason)
        self.assertNotIn("count_low_conf=true", decorated.reason)
        low = dataclasses.replace(decision, people_count=0)
        self.assertIn(
            "count_low_conf=true", worker.decorate_reason(low, "heuristic", "default").reason
        )
        normal = dataclasses.replace(decision, trapped=False, urgency=0)
        self.assertEqual(
            worker.decorate_reason(normal, "heuristic", "default").reason, normal.reason
        )

    def test_time_window_blocks(self):
        bad = [Decision(START - 2000, "ELEV-3F-001", True, 2, "3F", 1, "trapped", 0.8, [])]
        with self.assertRaises(up.UploadError) as ctx:
            up.validate_time_window(bad, START, 12000)
        self.assertEqual(ctx.exception.code, "UP_TIME_BASE_INVALID")

    def test_decisions_roundtrip(self):
        decisions = FusionRunner(PRESET_DEMO).run(_rows(100), [])
        path = pathlib.Path(self.temp.name) / "decisions.json"
        worker.write_decisions(path, decisions)
        loaded = worker.read_decisions(path)
        self.assertEqual(len(loaded), len(decisions))
        self.assertEqual(loaded[-1].ts, decisions[-1].ts)
        self.assertEqual(loaded[-1].trapped, decisions[-1].trapped)

    def test_build_runtime_config_injects_identity(self):
        record = fake_record(self.base, device="ELEV-9F-009", location="9F")
        cfg = worker.build_runtime_config(record)
        self.assertEqual(cfg.device_id, "ELEV-9F-009")
        self.assertEqual(cfg.location, "9F")
        self.assertTrue(cfg.demo_threshold)


if __name__ == "__main__":
    unittest.main()