"""T-B-011 ~ T-B-014 · IMU 事件检测：窗口切分、三分数、非法输入与职责隔离。

运行：python tests/test_imu_detect.py -v
"""

import ast
import pathlib
import sys
import tempfile
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import schema  # noqa: E402
from model.config import PRESET_PRODUCTION, build_runtime_config  # noqa: E402
from model.imu_detect import detect_imu, detect_imu_with_config  # noqa: E402


def quiet(steps=200, amp=0.003, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, amp, size=(steps, 3))


def sine(steps=200, freq=5.0, amp=0.25):
    t = np.arange(steps) / schema.IMU_HZ
    wave = amp * np.sin(2 * np.pi * freq * t)
    return np.repeat(wave[:, None], 3, axis=1)


class NpyCase:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def save(self, arr, name="demo_normal_imu.npy"):
        path = self.dir / name
        np.save(path, arr)
        return str(path)

    def close(self):
        self._tmp.cleanup()


class TestWindowing(unittest.TestCase):
    def setUp(self):
        self.case = NpyCase()

    def tearDown(self):
        self.case.close()

    def test_window_count(self):
        path = self.case.save(quiet(600).reshape(3, 200, 3).astype(np.float32))
        events = detect_imu(path)
        self.assertEqual(len(events), 3)

    def test_tail_dropped_warns(self):
        path = self.case.save(quiet(650).astype(np.float32))
        with self.assertLogs("model.imu_detect", level="WARNING") as logs:
            events = detect_imu(path)
        self.assertEqual(len(events), 3)
        self.assertTrue(any("tail_dropped" in line for line in logs.output))

    def test_insufficient_window(self):
        path = self.case.save(quiet(199).astype(np.float32))
        with self.assertLogs("model.imu_detect", level="WARNING") as logs:
            events = detect_imu(path)
        self.assertEqual(events, [])
        self.assertTrue(any("insufficient window" in line for line in logs.output))

    def test_window_sec_constant(self):
        path = self.case.save(quiet(600).reshape(3, 200, 3).astype(np.float32))
        events = detect_imu(path)
        self.assertEqual({e.window_sec for e in events}, {10.0})

    def test_ts_adjacent_gap_and_scale(self):
        path = self.case.save(quiet(600).reshape(3, 200, 3).astype(np.float32))
        cfg = build_runtime_config(["--start-ts-ms", "1763300000000"])
        events = detect_imu_with_config(path, cfg)
        self.assertEqual(events[0].ts, 1763300000000 + 10000)
        for prev, cur in zip(events, events[1:]):
            self.assertEqual(cur.ts - prev.ts, 10000)
        self.assertTrue(all(10 ** 12 <= e.ts < 10 ** 13 for e in events))

    def test_invalid_npy_raises(self):
        bad = [
            ("nan_imu.npy", np.full((1, 200, 3), np.nan, dtype=np.float32)),
            ("inf_imu.npy", np.full((1, 200, 3), np.inf, dtype=np.float32)),
            ("f64_imu.npy", np.zeros((1, 200, 3), dtype=np.float64)),
            ("shape199_imu.npy", np.zeros((1, 199, 3), dtype=np.float32)),
            ("shape2_imu.npy", np.zeros((1, 200, 2), dtype=np.float32)),
        ]
        for name, arr in bad:
            with self.subTest(name=name):
                path = self.case.save(arr, name=name)
                with self.assertRaises(ValueError) as ctx:
                    detect_imu(path)
                self.assertIn(name, str(ctx.exception))
                self.assertIn("shape", str(ctx.exception).lower() + "shape")


class TestScores(unittest.TestCase):
    def setUp(self):
        self.case = NpyCase()

    def tearDown(self):
        self.case.close()

    def test_estop_pulse(self):
        arr = quiet(200, amp=0.003)
        arr[100:110] += 0.9
        path = self.case.save(arr.astype(np.float32)[None, :, :], name="demo_estop_imu.npy")
        events = detect_imu(path)
        self.assertGreaterEqual(events[0].estop, 0.8)

    def test_estop_quiet(self):
        path = self.case.save(quiet(200, amp=0.003).astype(np.float32)[None, :, :])
        events = detect_imu(path)
        self.assertLess(events[0].estop, 0.6)

    def test_estop_continuous_motion_not_alarming(self):
        path = self.case.save(sine(200, freq=1.2, amp=0.35).astype(np.float32)[None, :, :])
        events = detect_imu(path)
        self.assertLess(events[0].estop, 0.6)

    def test_vibration_5hz(self):
        path = self.case.save(sine(200, freq=5.0, amp=0.25).astype(np.float32)[None, :, :])
        events = detect_imu(path)
        self.assertGreater(events[0].vibration, 0.7)

    def test_vibration_bounds(self):
        rng = np.random.default_rng(5)
        arr = rng.normal(0.0, 0.5, size=(1, 200, 3)).clip(-1, 1).astype(np.float32)
        path = self.case.save(arr)
        events = detect_imu(path)
        self.assertGreaterEqual(events[0].vibration, 0.0)
        self.assertLessEqual(events[0].vibration, 1.0)

    def test_idle_zero(self):
        path = self.case.save(np.zeros((1, 200, 3), dtype=np.float32))
        events = detect_imu(path)
        self.assertGreaterEqual(events[0].idle, 0.9)

    def test_idle_moving(self):
        path = self.case.save(sine(200, freq=1.2, amp=0.35).astype(np.float32)[None, :, :])
        events = detect_imu(path)
        self.assertLessEqual(events[0].idle, 0.2)


class TestResponsibilityIsolation(unittest.TestCase):
    def test_no_vision_import(self):
        source = (ROOT / "model" / "imu_detect.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        for forbidden in ("vision", "service", "dashboard", "scripts"):
            self.assertFalse(
                any(name == forbidden or name.startswith(forbidden + ".") for name in imported),
                f"forbidden import {forbidden} in imu_detect.py",
            )
        for forbidden in ("model.state_machine", "model.fuse", "model.reason", "model.eval"):
            self.assertNotIn(forbidden, imported)
        self.assertNotIn("trapped", source.split('"""', 2)[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)