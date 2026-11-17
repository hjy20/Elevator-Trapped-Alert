"""T-B-005 · 派生信号层。

运行：python tests/test_signals.py -v
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import IMUEvent, VisionOut  # noqa: E402
from model.config import PRESET_DEMO  # noqa: E402
from model.signals import SignalEstimator  # noqa: E402

TS = 1763300000000


def vision(ts, person=0, fall=0.0, door=0.02, crowded=0.10, ok=True):
    return VisionOut(
        ts=ts,
        person_count=person,
        fall_prob=fall,
        door_approach=door,
        crowded=crowded,
        keypoints_ok=ok,
    )


def imu(ts, idle=0.99, estop=0.05, vibration=0.0):
    return IMUEvent(
        ts=ts, estop=estop, vibration=vibration, idle=idle, window_sec=10.0
    )


class TestSignals(unittest.TestCase):
    def test_motion_from_idle(self):
        est = SignalEstimator(PRESET_DEMO)
        ev = est.update(vision(TS), imu(TS, idle=0.9))
        self.assertAlmostEqual(ev.motion_level, 0.1, places=6)
        self.assertLess(ev.motion_level, PRESET_DEMO.thresholds.move_thresh)

    def test_motion_visual_proxy_when_no_imu(self):
        est = SignalEstimator(PRESET_DEMO)
        est.update(vision(TS, person=2), None)
        ev = est.update(vision(TS + 1000, person=3), None)
        self.assertGreater(ev.motion_level, 0.0)
        self.assertIn("imu_unavailable", ev.flags)

    def test_person_count_capped(self):
        est = SignalEstimator(PRESET_DEMO)
        with self.assertLogs("model.signals", level="INFO") as logs:
            ev = est.update(vision(TS, person=12), imu(TS))
        self.assertEqual(ev.person_count, 8)
        self.assertTrue(any("raw_person_count=12" in line for line in logs.output))

    def test_door_proxy_on_person_change(self):
        est = SignalEstimator(PRESET_DEMO)
        est.update(vision(TS, person=2, door=0.60), imu(TS))
        first = est.update(vision(TS + 1000, person=1, door=0.20), imu(TS + 1000))
        self.assertTrue(first.door_open_signal)
        later = est.update(vision(TS + 2500, person=1, door=0.20), imu(TS + 2500))
        self.assertFalse(later.door_open_signal)

    def test_door_proxy_requires_two_evidence(self):
        est = SignalEstimator(PRESET_DEMO)
        est.update(vision(TS, person=2, door=0.20), imu(TS))
        only_person_changed = est.update(
            vision(TS + 1000, person=1, door=0.20), imu(TS + 1000)
        )
        self.assertFalse(only_person_changed.door_open_signal)

    def test_door_proxy_flags_technical_debt(self):
        est = SignalEstimator(PRESET_DEMO)
        est.update(vision(TS, person=2, door=0.60), imu(TS))
        ev = est.update(vision(TS + 1000, person=1, door=0.20), imu(TS + 1000))
        self.assertTrue(ev.door_open_signal)
        self.assertIn("technical_debt", ev.flags)

    def test_probability_clipped(self):
        est = SignalEstimator(PRESET_DEMO)
        with self.assertLogs("model.signals", level="WARNING") as logs:
            ev = est.update(vision(TS, fall=1.3, door=-0.2), imu(TS))
        self.assertEqual(ev.fall_prob, 1.0)
        self.assertEqual(ev.door_approach, 0.0)
        self.assertTrue(any("field_clipped" in line for line in logs.output))

    def test_keypoints_unreliable_zeroes_fall(self):
        est = SignalEstimator(PRESET_DEMO)
        ev = est.update(vision(TS, fall=0.95, ok=False), imu(TS))
        self.assertEqual(ev.fall_prob, 0.0)
        self.assertIn("keypoints_unreliable", ev.flags)

    def test_reset_clears_state(self):
        est = SignalEstimator(PRESET_DEMO)
        est.update(vision(TS, person=2), imu(TS))
        est.reset()
        ev = est.update(vision(TS + 1000, person=3), imu(TS + 1000))
        self.assertFalse(ev.door_open_signal)


if __name__ == "__main__":
    unittest.main(verbosity=2)