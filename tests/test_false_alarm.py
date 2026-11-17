"""T-B-018 · 五类误报抑制落点（M1–M7）。

运行：python tests/test_false_alarm.py -v
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import IMUEvent, VisionOut  # noqa: E402
from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.reason import derive_verdict  # noqa: E402
from model.signals import SignalEstimator  # noqa: E402
from model.state_machine import ElevatorState as S  # noqa: E402
from model.state_machine import StateMachine  # noqa: E402

TS = 1763300000000


def vision(ts, person=0, fall=0.0, door=0.02, crowded=0.1, ok=True):
    return VisionOut(
        ts=ts,
        person_count=person,
        fall_prob=fall,
        door_approach=door,
        crowded=crowded,
        keypoints_ok=ok,
    )


def imu(ts, idle=0.99, estop=0.05, vibration=0.0):
    return IMUEvent(ts=ts, estop=estop, vibration=vibration, idle=idle, window_sec=10.0)


class Harness:
    def __init__(self, cfg=PRESET_DEMO):
        self.cfg = cfg
        self.estimator = SignalEstimator(cfg)
        self.machine = StateMachine(cfg)

    def run(self, v, e=None):
        return self.machine.step_evidence(self.estimator.update(v, e), e)

    def to_occupied(self, ts=TS):
        for _ in range(3):
            self.run(vision(ts), imu(ts, idle=0.50))
            ts += 1000
        for _ in range(2):
            self.run(vision(ts), imu(ts, idle=0.99))
            ts += 1000
        self.run(vision(ts, person=0, door=0.60), imu(ts, idle=0.99))
        ts += 1000
        for _ in range(4):
            self.run(vision(ts, person=1, door=0.22), imu(ts, idle=0.99))
            ts += 1000
        return ts


class TestFalseAlarmSuppression(unittest.TestCase):
    def test_m1_normal_arrival_resets_dwell(self):
        h = Harness()
        ts = h.to_occupied()
        for _ in range(5):
            h.run(vision(ts, person=1, door=0.22), imu(ts))
            ts += 1000
        self.assertGreater(h.machine.ctx.raw_dwell_ms, 0)
        h.run(vision(ts, person=1, door=0.60), imu(ts))
        ts += 1000
        decision = h.run(vision(ts, person=2, door=0.10), imu(ts))
        self.assertFalse(decision.trapped)
        self.assertEqual(h.machine.ctx.raw_dwell_ms, 0)
        self.assertFalse(h.machine.ctx.alarmed)

    def test_m2_empty_car_never_trapped(self):
        h = Harness()
        ts = h.to_occupied()
        for _ in range(600):
            decision = h.run(vision(ts, person=0), imu(ts))
            ts += 1000
            self.assertFalse(decision.trapped)

    def test_m3_bending_down_short_not_faint(self):
        h = Harness()
        ts = h.to_occupied()
        faint = None
        for _ in range(2):
            decision = h.run(vision(ts, person=1, fall=0.95), imu(ts))
            ts += 1000
            faint = decision
        self.assertNotEqual(faint.event_type, "faint")

    def test_m4_crowded_keeps_trapped(self):
        kwargs = dict(
            trapped=True,
            fall_prob=0.1,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.22,
            imu_estop=0.1,
            imu_present=True,
            dwell_sec=412.0,
            person_count=4,
        )
        low = derive_verdict(cfg=PRESET_PRODUCTION, crowded=0.1, **kwargs)
        high = derive_verdict(cfg=PRESET_PRODUCTION, crowded=0.9, **kwargs)
        self.assertTrue(low.trapped)
        self.assertTrue(high.trapped)
        self.assertLess(high.confidence, low.confidence)

    def test_m5_slight_shake_not_estop(self):
        h = Harness()
        decision = h.run(vision(TS, person=1), imu(TS, idle=0.99, estop=0.60))
        self.assertNotEqual(decision.event_type, "estop")
        self.assertEqual(decision.urgency, 0)

    def test_m6_door_long_open_not_trapped(self):
        h = Harness()
        ts = h.to_occupied()
        for i in range(130):
            person = 2 if i % 2 == 0 else 3
            door = max(0.0, 0.9 - (i % 4) * 0.3)
            decision = h.run(vision(ts, person=person, door=door), imu(ts))
            ts += 1000
            self.assertFalse(decision.trapped)
            if h.machine.ctx.state is S.DOOR_OPEN:
                self.assertEqual(h.machine.ctx.raw_dwell_ms, 0)
        self.assertTrue(h.machine.ctx.long_open)

    def test_m7_ts_jitter_no_duplicate_alarm(self):
        h = Harness()
        ts = h.to_occupied()
        first = None
        for _ in range(10):
            decision = h.run(vision(ts, person=1), imu(ts))
            if decision.trapped:
                first = decision
                break
            ts += 1000
        self.assertIsNotNone(first)
        before = h.machine.ctx.dropped_frames
        repeated = h.run(vision(ts, person=1), imu(ts))
        self.assertEqual(h.machine.ctx.dropped_frames, before + 1)
        self.assertEqual(repeated.ts, first.ts)
        self.assertTrue(h.machine.ctx.alarmed)


class TestSuppressionComments(unittest.TestCase):
    def test_suppress_comments_present(self):
        count = 0
        for path in (ROOT / "model").glob("*.py"):
            count += path.read_text(encoding="utf-8").count("# suppress:")
        self.assertGreaterEqual(count, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)