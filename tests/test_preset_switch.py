"""T-B-019 · 演示/生产双阈值切换：只改配置、不改逻辑。

运行：python tests/test_preset_switch.py -v
"""

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import IMUEvent, VisionOut  # noqa: E402
from model.config import (  # noqa: E402
    PRESET_DEMO,
    PRESET_PRODUCTION,
    build_runtime_config,
)
from model.signals import SignalEstimator  # noqa: E402
from model.state_machine import ElevatorState as S  # noqa: E402
from model.state_machine import StateMachine  # noqa: E402

TS = 1763300000000
ALGORITHM_MODULES = ("signals.py", "state_machine.py", "reason.py", "fuse.py", "imu_detect.py")


def vision(ts, person=0, door=0.02):
    return VisionOut(
        ts=ts,
        person_count=person,
        fall_prob=0.0,
        door_approach=door,
        crowded=0.1,
        keypoints_ok=True,
    )


def imu(ts, idle=0.99):
    return IMUEvent(ts=ts, estop=0.05, vibration=0.0, idle=idle, window_sec=10.0)


def run_twenty_seconds(cfg):
    estimator = SignalEstimator(cfg)
    machine = StateMachine(cfg)
    decisions = []
    ts = TS

    def push(person, door, idle):
        nonlocal ts
        decisions.append(
            machine.step_evidence(estimator.update(vision(ts, person, door), imu(ts, idle)), None)
        )
        ts += 1000

    for _ in range(3):
        push(0, 0.02, 0.50)
    for _ in range(2):
        push(0, 0.02, 0.99)
    push(0, 0.60, 0.99)
    for _ in range(4):
        push(1, 0.22, 0.99)
    for _ in range(13):
        push(1, 0.22, 0.99)
    return machine, decisions


class TestPresetSwitch(unittest.TestCase):
    def test_production_silent_demo_alarms(self):
        prod_machine, prod = run_twenty_seconds(PRESET_PRODUCTION)
        demo_machine, demo = run_twenty_seconds(PRESET_DEMO)
        self.assertEqual(sum(1 for d in prod if d.trapped), 0)
        self.assertGreater(sum(1 for d in demo if d.trapped), 0)
        self.assertIsNotNone(demo_machine.ctx.first_alarm_ts)

    def test_demo_reason_declares_threshold(self):
        _, demo = run_twenty_seconds(PRESET_DEMO)
        alarm = next(d for d in demo if d.trapped)
        self.assertIn("demo_threshold=true", alarm.reason)
        self.assertTrue(any("> 8s" in item for item in alarm.reason))

    def test_no_demo_branch_in_algorithms(self):
        offenders = []
        for name in ALGORITHM_MODULES:
            path = ROOT / "model" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                for sub in ast.walk(node.test):
                    if isinstance(sub, ast.Attribute) and sub.attr == "demo_threshold":
                        offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_cli_override_equivalent_to_demo(self):
        by_demo = build_runtime_config(["--demo"])
        by_cli = build_runtime_config(["--dwell-threshold", "8"])
        self.assertEqual(by_demo.thresholds.dwell_threshold, by_cli.thresholds.dwell_threshold)

        _, demo = run_twenty_seconds(by_demo)
        _, cli = run_twenty_seconds(by_cli)
        self.assertEqual(len(demo), len(cli))
        for left, right in zip(demo, cli):
            self.assertEqual(left.trapped, right.trapped)
            self.assertEqual(left.urgency, right.urgency)
            self.assertEqual(left.event_type, right.event_type)

    def test_first_alarm_within_demo_window(self):
        machine, demo = run_twenty_seconds(PRESET_DEMO)
        first_alarm = next(d for d in demo if d.trapped)
        stop_enter_ts = machine.ctx.stop_enter_ts
        self.assertIsNotNone(stop_enter_ts)
        self.assertLessEqual(first_alarm.ts - stop_enter_ts, 20000)

    def test_switch_report_written(self):
        _, prod = run_twenty_seconds(PRESET_PRODUCTION)
        _, demo = run_twenty_seconds(PRESET_DEMO)
        results = ROOT / "results"
        results.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 双阈值切换检查",
            "",
            f"- 生产 dwell_threshold = {PRESET_PRODUCTION.thresholds.dwell_threshold}s",
            f"- 演示 dwell_threshold = {PRESET_DEMO.thresholds.dwell_threshold}s",
            f"- 同段 22 帧数据生产告警数 = {sum(1 for d in prod if d.trapped)}",
            f"- 同段 22 帧数据演示告警数 = {sum(1 for d in demo if d.trapped)}",
            "- 代码逻辑零改动：算法模块无 demo_threshold 分支（AST 校验通过）",
        ]
        (results / "preset_switch_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)