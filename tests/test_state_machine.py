"""T-B-006 / T-B-007 · 六态状态机、dwell 双量、时序保护与 estop 加速。

运行：python tests/test_state_machine.py -v
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
from model.signals import SignalEstimator  # noqa: E402
from model.state_machine import ElevatorState as S  # noqa: E402
from model.state_machine import StateMachine  # noqa: E402

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
    return IMUEvent(ts=ts, estop=estop, vibration=vibration, idle=idle, window_sec=10.0)


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg
        self.estimator = SignalEstimator(cfg)
        self.machine = StateMachine(cfg)

    def run(self, v, e=None):
        return self.machine.step_evidence(self.estimator.update(v, e), e)

    def advance_to_occupied(self, ts=TS, person=1):
        for _ in range(3):
            self.run(vision(ts), imu(ts, idle=0.50))
            ts += 1000
        for _ in range(2):
            self.run(vision(ts), imu(ts, idle=0.99))
            ts += 1000
        self.run(vision(ts, person=0, door=0.60), imu(ts, idle=0.99))
        ts += 1000
        self.run(vision(ts, person=person, door=0.22), imu(ts, idle=0.99))
        ts += 1000
        self.run(vision(ts, person=person, door=0.22), imu(ts, idle=0.99))
        ts += 1000
        self.run(vision(ts, person=person, door=0.22), imu(ts, idle=0.99))
        ts += 1000
        self.run(vision(ts, person=person, door=0.22), imu(ts, idle=0.99))
        ts += 1000
        return ts


class TestTransitions(unittest.TestCase):
    def setUp(self):
        self.h = Harness(PRESET_DEMO)

    def test_initial_state_idle(self):
        self.assertIs(self.h.machine.ctx.state, S.IDLE)
        self.assertEqual(len(list(S)), 6)

    def test_idle_to_moving(self):
        ts = TS
        for _ in range(26):
            self.h.run(vision(ts), imu(ts, idle=0.50))
            ts += 40
        self.assertIs(self.h.machine.ctx.state, S.MOVING)

    def test_moving_to_stopped_records_ts(self):
        ts = TS
        for _ in range(3):
            self.h.run(vision(ts), imu(ts, idle=0.50))
            ts += 1000
        self.assertIs(self.h.machine.ctx.state, S.MOVING)
        for _ in range(2):
            self.h.run(vision(ts), imu(ts, idle=0.99))
            ts += 1000
        self.assertIs(self.h.machine.ctx.state, S.STOPPED)
        self.assertEqual(self.h.machine.ctx.stop_enter_ts, ts - 1000)
        self.assertEqual(self.h.machine.ctx.raw_dwell_ms, 0)

    def test_estop_forces_stopped(self):
        ts = TS
        for _ in range(3):
            self.h.run(vision(ts), imu(ts, idle=0.50))
            ts += 1000
        self.assertIs(self.h.machine.ctx.state, S.MOVING)
        self.h.run(vision(ts), imu(ts, idle=0.50, estop=0.95))
        self.assertIs(self.h.machine.ctx.state, S.STOPPED)
        self.assertEqual(self.h.machine.ctx.stop_enter_ts, ts)

    def test_door_open_to_closed(self):
        ts = TS
        for _ in range(3):
            self.h.run(vision(ts), imu(ts, idle=0.50))
            ts += 1000
        for _ in range(2):
            self.h.run(vision(ts), imu(ts, idle=0.99))
            ts += 1000
        self.h.run(vision(ts, person=0, door=0.60), imu(ts, idle=0.99))
        ts += 1000
        self.h.run(vision(ts, person=2, door=0.20), imu(ts, idle=0.99))
        ts += 1000
        self.assertIs(self.h.machine.ctx.state, S.DOOR_OPEN)
        for _ in range(2):
            self.h.run(vision(ts, person=2, door=0.20), imu(ts, idle=0.99))
            ts += 1000
        self.assertIs(self.h.machine.ctx.state, S.DOOR_CLOSED)

    def test_closed_to_occupied(self):
        ts = self.h.advance_to_occupied()
        self.assertIs(self.h.machine.ctx.state, S.OCCUPIED)
        self.assertIsNotNone(self.h.machine.ctx.stop_enter_ts)

    def test_six_states_reachable(self):
        ts = self.h.advance_to_occupied()
        for _ in range(3):
            self.h.run(vision(ts, person=0), imu(ts))
            ts += 1000
        reached = {self.h.machine.ctx.state}
        for (old, new) in self.h.machine.ctx.transition_count:
            reached.add(old)
            reached.add(new)
        for state in S:
            self.assertIn(state, reached)


class TestDwellAndJudgement(unittest.TestCase):
    def setUp(self):
        self.h = Harness(PRESET_DEMO)

    def test_door_open_resets_dwell(self):
        ts = self.h.advance_to_occupied()
        for _ in range(5):
            self.h.run(vision(ts, person=1), imu(ts))
            ts += 1000
        self.assertGreater(self.h.machine.ctx.raw_dwell_ms, 0)
        self.h.run(vision(ts, person=1, door=0.60), imu(ts))
        ts += 1000
        self.h.run(vision(ts, person=2, door=0.20), imu(ts))
        self.assertIs(self.h.machine.ctx.state, S.DOOR_OPEN)
        self.assertEqual(self.h.machine.ctx.raw_dwell_ms, 0)
        self.assertEqual(self.h.machine.ctx.effective_dwell_ms, 0)

    def test_door_open_long_zero_dwell(self):
        ts = self.h.advance_to_occupied()
        for i in range(130):
            person = 2 if i % 2 == 0 else 3
            door = max(0.0, 0.9 - (i % 4) * 0.3)
            self.h.run(vision(ts, person=person, door=door), imu(ts, idle=0.99))
            ts += 1000
            if self.h.machine.ctx.state is S.DOOR_OPEN:
                self.assertEqual(self.h.machine.ctx.raw_dwell_ms, 0)
        self.assertTrue(self.h.machine.ctx.long_open)

    def test_trapped_judgement(self):
        ts = self.h.advance_to_occupied()
        first = None
        for _ in range(12):
            decision = self.h.run(vision(ts, person=1), imu(ts, idle=0.99))
            ts += 1000
            if decision.trapped:
                first = decision
                break
        self.assertIsNotNone(first)
        self.assertEqual(first.urgency, 2)
        self.assertEqual(first.event_type, "trapped")
        self.assertGreater(first.reason and len(first.reason), 0)

    def test_trapped_boundary_equal(self):
        cfg = dataclasses.replace(
            PRESET_DEMO,
            thresholds=dataclasses.replace(PRESET_DEMO.thresholds, dwell_threshold=8.0),
        )
        h = Harness(cfg)
        ts = h.advance_to_occupied()
        decision = None
        hit_boundary = False
        for _ in range(12):
            decision = h.run(vision(ts, person=1), imu(ts, idle=0.99))
            ts += 1000
            if h.machine.ctx.effective_dwell_ms == 8000:
                hit_boundary = True
                self.assertFalse(decision.trapped)
                break
        self.assertTrue(hit_boundary)

    def test_non_occupied_never_trapped(self):
        cfg = dataclasses.replace(
            PRESET_DEMO,
            thresholds=dataclasses.replace(PRESET_DEMO.thresholds, dwell_threshold=8.0),
        )
        h = Harness(cfg)
        ts = TS
        for _ in range(3):
            h.run(vision(ts, person=1), imu(ts, idle=0.50))
            ts += 1000
        for _ in range(30):
            decision = h.run(vision(ts, person=1), imu(ts, idle=0.99))
            ts += 1000
            if h.machine.ctx.state is S.STOPPED:
                self.assertFalse(decision.trapped)

    def test_empty_car_no_alarm(self):
        ts = self.h.advance_to_occupied()
        self.h.run(vision(ts, person=0), imu(ts))
        ts += 1000
        decision = self.h.run(vision(ts, person=0), imu(ts))
        self.assertFalse(decision.trapped)
        self.assertEqual(decision.event_type, "normal")


class TestTimeProtectionAndLatch(unittest.TestCase):
    def test_ts_regression_dropped(self):
        h = Harness(PRESET_DEMO)
        seq = [TS, TS, TS - 1000, TS + 1000]
        for ts in seq:
            h.run(vision(ts), imu(ts))
        self.assertEqual(h.machine.ctx.dropped_frames, 2)
        self.assertEqual(h.machine.ctx.last_ts, TS + 1000)

    def test_imu_none_still_advances(self):
        h = Harness(PRESET_DEMO)
        ts = TS
        h.run(vision(ts, person=0))
        ts += 1000
        h.run(vision(ts, person=1))
        ts += 1000
        h.run(vision(ts, person=1))
        ts += 1000
        for _ in range(4):
            h.run(vision(ts, person=1))
            ts += 1000
        decision = None
        for _ in range(12):
            decision = h.run(vision(ts, person=1))
            ts += 1000
            if decision.trapped:
                break
        self.assertTrue(decision.trapped)
        self.assertIn("imu_unavailable=true", decision.reason)
        self.assertLessEqual(decision.urgency, 2)

    def test_decision_fields_complete(self):
        h = Harness(PRESET_DEMO)
        decision = h.run(vision(TS), imu(TS))
        keys = set(dataclasses.asdict(decision))
        self.assertEqual(
            keys,
            {
                "ts",
                "device_id",
                "trapped",
                "urgency",
                "location",
                "people_count",
                "event_type",
                "confidence",
                "reason",
            },
        )
        self.assertGreaterEqual(len(decision.reason), 1)

    def test_estop_latch_accelerates(self):
        h = Harness(PRESET_PRODUCTION)
        ts = h.advance_to_occupied()
        for i in range(10):
            h.run(vision(ts, person=1), imu(ts, idle=0.99, estop=0.92))
            ts += 1000
            if (i + 1) % 10 == 0:
                pass
        for i in range(10):
            h.run(vision(ts, person=1), imu(ts, idle=0.99, estop=0.92))
            ts += 1000
        ctx = h.machine.ctx
        self.assertGreater(ctx.raw_dwell_ms, 15000)
        self.assertGreater(ctx.effective_dwell_ms, ctx.raw_dwell_ms)

    def test_estop_latch_expires(self):
        h = Harness(PRESET_DEMO)
        ts = h.advance_to_occupied()
        h.run(vision(ts, person=1), imu(ts, idle=0.99, estop=0.95))
        ts += 1000
        for _ in range(12):
            h.run(vision(ts, person=1), imu(ts, idle=0.99, estop=0.10))
            ts += 1000
        ctx = h.machine.ctx
        self.assertFalse(ctx.estop_latch)
        self.assertLessEqual(ctx.effective_dwell_ms - ctx.raw_dwell_ms, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)