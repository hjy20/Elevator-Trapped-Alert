"""T-B-024 · 【阻断式守门】正常场景零误报。

本用例失败 → 禁止进入阈值标定（T-B-026）。
运行：python tests/test_no_false_alarm_gate.py -v
"""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.fuse import FusionRunner  # noqa: E402
from model.imu_detect import detect_imu  # noqa: E402
from model.mock_scenario import generate_scenario, write_bundle  # noqa: E402

RESULTS = ROOT / "results"


def run_normal(cfg) -> dict:
    bundle = generate_scenario("normal", cfg, fps=25)
    paths = write_bundle(bundle)
    imu = detect_imu(paths["imu_npy"])
    runner = FusionRunner(cfg)
    decisions = runner.run(bundle.vision, imu)

    trapped_true = sum(1 for item in decisions if item.trapped)
    first_fp = next((item for item in decisions if item.trapped), None)
    gate = {
        "preset": cfg.mode,
        "dwell_threshold": cfg.thresholds.dwell_threshold,
        "normal_frames": len(decisions),
        "trapped_true": trapped_true,
        "max_urgency": max((item.urgency for item in decisions), default=0),
        "frame_fp_rate": round(trapped_true / len(decisions), 6) if decisions else 0.0,
        "first_fp_ts": first_fp.ts if first_fp else None,
        "first_fp_reason": first_fp.reason if first_fp else None,
        "door_open_seen": any(row["state"] == "DOOR_OPEN" for row in runner.trace),
        "raw_dwell_at_door_open": [
            row["raw_dwell_ms"]
            for row in runner.trace
            if row["state"] == "DOOR_OPEN"
        ][:5],
    }
    return gate, decisions


class TestZeroFalseAlarmGate(unittest.TestCase):
    def _assert_gate(self, cfg):
        gate, decisions = run_normal(cfg)
        self.assertEqual(
            gate["trapped_true"],
            0,
            f"false alarm at ts={gate['first_fp_ts']} reason={gate['first_fp_reason']}",
        )
        self.assertLessEqual(gate["max_urgency"], 1)
        self.assertEqual(gate["frame_fp_rate"], 0.0)
        return gate

    def test_gate_production_preset(self):
        gate = self._assert_gate(PRESET_PRODUCTION)
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "gate_normal_zero_fp.json").write_text(
            json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_gate_demo_preset(self):
        self._assert_gate(PRESET_DEMO)

    def test_door_open_resets_dwell_in_normal(self):
        gate, _ = run_normal(PRESET_PRODUCTION)
        self.assertTrue(gate["door_open_seen"], "normal 场景必须出现 DOOR_OPEN 转移")
        self.assertTrue(all(value == 0 for value in gate["raw_dwell_at_door_open"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)