"""T-B-016 / T-B-017 / T-B-020 · 毫秒对齐、融合编排、降级与健康度。

运行：python tests/test_fuse.py -v
"""

import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import IMUEvent, VisionOut  # noqa: E402
from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.fuse import Aligner, FusionRunner, fuse_with_config  # noqa: E402
from model.imu_detect import detect_imu  # noqa: E402
from model.mock_scenario import generate_scenario  # noqa: E402

TS = 1763300000000
CONTRACT_KEYS = {
    "ts",
    "device_id",
    "trapped",
    "urgency",
    "location",
    "people_count",
    "event_type",
    "confidence",
    "reason",
}


def event(ts, estop=0.1, idle=0.9, vibration=0.1):
    return IMUEvent(ts=ts, estop=estop, vibration=vibration, idle=idle, window_sec=10.0)


def vision(ts, person=0, fall=0.0, door=0.02, crowded=0.1, ok=True):
    return VisionOut(
        ts=ts,
        person_count=person,
        fall_prob=fall,
        door_approach=door,
        crowded=crowded,
        keypoints_ok=ok,
    )


class TestAligner(unittest.TestCase):
    def test_align_nearest_500(self):
        aligner = Aligner([event(TS + 10000)], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS + 10400)
        self.assertIsNotNone(found)
        self.assertEqual(info.match, "nearest")
        self.assertEqual(info.delta_ms, 400)

    def test_align_nearest_over_500(self):
        aligner = Aligner([event(TS + 10000)], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS + 10600)
        self.assertIsNotNone(found)
        self.assertEqual(info.match, "hold")
        self.assertEqual(info.delta_ms, 600)

    def test_align_hold_10000(self):
        aligner = Aligner([event(TS + 30000)], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS + 40000)
        self.assertIsNotNone(found)
        self.assertEqual(info.match, "hold")
        self.assertEqual(info.delta_ms, 10000)

    def test_align_hold_over_10000(self):
        aligner = Aligner([event(TS + 30000)], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS + 41000)
        self.assertIsNone(found)
        self.assertEqual(info.match, "none")

    def test_align_equal_distance_prefers_earlier(self):
        aligner = Aligner([event(TS + 20000), event(TS + 21000)], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS + 20500)
        self.assertIsNotNone(found)
        self.assertEqual(found.ts, TS + 20000)
        self.assertEqual(info.delta_ms, 500)

    def test_align_empty_events(self):
        aligner = Aligner([], PRESET_PRODUCTION)
        found, info = aligner.nearest(TS)
        self.assertIsNone(found)
        self.assertEqual(info.match, "none")


class TestFusion(unittest.TestCase):
    def test_count_equals_vision(self):
        frames = [vision(TS + i * 40, person=1) for i in range(750)]
        out = fuse_with_config(frames, [event(TS + 10000)], PRESET_DEMO)
        self.assertEqual(len(out), 750)

    def test_empty_vision(self):
        out = fuse_with_config([], [event(TS + 10000, estop=0.99)], PRESET_DEMO)
        self.assertEqual(out, [])

    def test_missing_imu_degrade(self):
        scenario = generate_scenario("trapped", PRESET_DEMO, fps=1)
        out = fuse_with_config(scenario.vision, [], PRESET_DEMO)
        for decision in out:
            self.assertIn("imu_unavailable=true", decision.reason)
            self.assertNotEqual(decision.urgency, 3)

    def test_person_cap(self):
        out = fuse_with_config(
            [vision(TS + 10000, person=12)], [event(TS + 10000)], PRESET_DEMO
        )
        self.assertEqual(out[0].people_count, 8)

    def test_degrade_keeps_schema(self):
        out = fuse_with_config(
            [vision(TS + 10000, person=1)], [], PRESET_DEMO
        )
        self.assertEqual(set(dataclasses.asdict(out[0])), CONTRACT_KEYS)

    def test_probability_clipped(self):
        out = fuse_with_config(
            [vision(TS + 10000, fall=1.3, door=-0.2)], [event(TS + 10000)], PRESET_DEMO
        )
        self.assertGreaterEqual(out[0].confidence, 0.0)
        self.assertLessEqual(out[0].confidence, 1.0)

    def test_frame_error_isolated(self):
        bad = vision(TS + 20000)
        object.__setattr__(bad, "person_count", "not-a-number")
        frames = [vision(TS + 10000, person=1), bad, vision(TS + 30000, person=1)]
        out = fuse_with_config(frames, [event(TS + 10000)], PRESET_DEMO)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(d.reason for d in out))

    def test_unsorted_input_is_stabilised(self):
        frames = [vision(TS + 30000), vision(TS + 10000), vision(TS + 20000)]
        out = fuse_with_config(frames, [event(TS + 20000)], PRESET_DEMO)
        self.assertEqual([d.ts for d in out], [TS + 10000, TS + 20000, TS + 30000])


class TestFusionHealth(unittest.TestCase):
    def test_estop_scenario_alignment_ratio(self):
        scenario = generate_scenario("estop", PRESET_DEMO, fps=1)
        import tempfile
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "demo_estop_imu.npy"
            np.save(path, scenario.imu_data)
            imu = detect_imu(str(path))
        runner = FusionRunner(PRESET_DEMO)
        runner.run(scenario.vision, imu)
        health = runner.health.as_dict()
        self.assertGreater(health["nearest_ratio"] + health["hold_ratio"], 0.35)
        self.assertGreater(health["nearest_ratio"], 0.0)

    def test_none_ratio_warns_when_degraded(self):
        frames = [vision(TS + i * 1000) for i in range(40)]
        runner = FusionRunner(PRESET_DEMO)
        with self.assertLogs("model.fuse", level="WARNING") as logs:
            runner.run(frames, [event(TS + 10000)])
        self.assertGreater(runner.health.as_dict()["none_ratio"], 0.5)
        self.assertTrue(any("imu_alignment_degraded" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)