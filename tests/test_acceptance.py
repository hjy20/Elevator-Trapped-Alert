"""T-B-010 · D2–D3 验收闸门：假数据跑到 OCCUPIED 并报警。

运行：python tests/test_acceptance.py -v
"""

import dataclasses
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import Decision  # noqa: E402
from model.config import PRESET_DEMO  # noqa: E402
from model.mock_scenario import generate_scenario  # noqa: E402
from model.signals import SignalEstimator  # noqa: E402
from model.state_machine import StateMachine  # noqa: E402

try:
    from model.imu_detect import detect_imu  # type: ignore

    HAS_IMU_DETECTOR = True
except ImportError:
    HAS_IMU_DETECTOR = False

RESULTS = ROOT / "results"


def run_scenario(kind, cfg=PRESET_DEMO, fps=1):
    bundle = generate_scenario(kind, cfg, fps=fps)
    estimator = SignalEstimator(cfg)
    machine = StateMachine(cfg)
    decisions = []
    for frame in bundle.vision:
        decisions.append(machine.step_evidence(estimator.update(frame, None)))
    return bundle, machine, decisions


class TestD3Gate(unittest.TestCase):
    def test_acceptance_state_machine_reaches_occupied(self):
        bundle, machine, decisions = run_scenario("trapped", fps=1)

        states = []
        estimator = SignalEstimator(PRESET_DEMO)
        replay = StateMachine(PRESET_DEMO)
        for frame in bundle.vision:
            replay.step_evidence(estimator.update(frame, None))
            states.append(replay.ctx.state.name)

        for required in ("IDLE", "MOVING", "STOPPED", "DOOR_CLOSED", "OCCUPIED"):
            self.assertIn(required, states, f"state trace missing {required}: {states}")

        trapped_frames = [d for d in decisions if d.trapped]
        self.assertTrue(
            trapped_frames,
            f"no trapped frame; states={states[-5:]}, last={decisions[-1]}",
        )
        first = trapped_frames[0]
        self.assertEqual(first.urgency, 2)
        self.assertEqual(first.event_type, "trapped")
        self.assertGreater(self._dwell_sec(first), 8.0)

        for decision in decisions:
            self.assertIsInstance(decision, Decision)
            self.assertGreaterEqual(decision.urgency, 0)
            self.assertLessEqual(decision.urgency, 3)
            self.assertGreaterEqual(decision.people_count, 0)
            self.assertLessEqual(decision.people_count, 8)
            self.assertGreaterEqual(decision.confidence, 0.0)
            self.assertLessEqual(decision.confidence, 1.0)
            self.assertTrue(decision.reason)

        RESULTS.mkdir(parents=True, exist_ok=True)
        payload = {
            "case": bundle.case,
            "preset": "demo",
            "frames": len(decisions),
            "state_trace": states,
            "first_alarm": dataclasses.asdict(first),
            "trapped_frames": len(trapped_frames),
        }
        (RESULTS / "d2_acceptance.log").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _dwell_sec(decision):
        for item in decision.reason:
            if item.startswith("dwell="):
                return float(item.split("=", 1)[1].split("s")[0])
        raise AssertionError(f"reason without dwell: {decision.reason}")

    @unittest.skipUnless(HAS_IMU_DETECTOR, "SKIP: pending T-B-014")
    def test_acceptance_imu_five_cases_nonempty(self):
        for kind in ("normal", "estop", "trapped", "pry", "faint"):
            with self.subTest(kind=kind):
                bundle = generate_scenario(kind, PRESET_DEMO)
                import tempfile

                with tempfile.TemporaryDirectory() as tmp:
                    path = pathlib.Path(tmp) / f"{bundle.case}_imu.npy"
                    import numpy as np

                    np.save(path, bundle.imu_data)
                    events = detect_imu(str(path))
                self.assertGreaterEqual(len(events), 3)
                self.assertTrue(all(e.window_sec == 10.0 for e in events))
                gaps = [b.ts - a.ts for a, b in zip(events, events[1:])]
                self.assertTrue(all(gap == 10000 for gap in gaps))


if __name__ == "__main__":
    unittest.main(verbosity=2)