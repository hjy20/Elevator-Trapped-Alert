"""T-B-004 · 五类场景假数据生成器。

运行：python tests/test_mock_scenario.py -v
"""

import pathlib
import sys
import tempfile
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import schema  # noqa: E402
from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.mock_scenario import (  # noqa: E402
    KINDS,
    ScenarioBundle,
    generate_scenario,
    make_frames,
    write_bundle,
)


class TestScenarioGeneration(unittest.TestCase):
    def test_five_kinds_non_empty(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                bundle = generate_scenario(kind, PRESET_DEMO, seed=0)
                self.assertIsInstance(bundle, ScenarioBundle)
                self.assertTrue(bundle.vision)
                self.assertIsInstance(bundle.expected, object)

    def test_expected_matches_appendix_a(self):
        expected = {
            "normal": ("normal", 0, False),
            "estop": ("estop", 1, False),
            "trapped": ("trapped", 2, True),
            "pry": ("pry", 2, True),
            "faint": ("faint", 3, True),
        }
        for kind, triple in expected.items():
            with self.subTest(kind=kind):
                exp = generate_scenario(kind, PRESET_DEMO).expected
                self.assertEqual((exp.event_type, exp.urgency, exp.trapped), triple)

    def test_timestamps_millisecond_and_increasing(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                bundle = generate_scenario(kind, PRESET_DEMO, fps=25)
                ts = [v.ts for v in bundle.vision]
                self.assertTrue(all(isinstance(t, int) for t in ts))
                self.assertTrue(all(10 ** 12 <= t < 10 ** 13 for t in ts))
                self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])))

    def test_fps_modes(self):
        slow = generate_scenario("trapped", PRESET_DEMO, fps=1)
        fast = generate_scenario("trapped", PRESET_DEMO, fps=25)
        self.assertLess(len(slow.vision), len(fast.vision))
        self.assertEqual(slow.vision[1].ts - slow.vision[0].ts, 1000)
        self.assertEqual(fast.vision[1].ts - fast.vision[0].ts, 40)

    def test_make_frames_normal_sequence(self):
        frames = make_frames("normal", PRESET_DEMO, fps=1)
        counts = [f.person_count for f in frames]
        self.assertEqual(counts[0], 0)
        self.assertIn(2, counts)
        self.assertEqual(counts[-1], 0)

    def test_imu_shape_and_dtype(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                data = generate_scenario(kind, PRESET_DEMO).imu_data
                self.assertEqual(data.ndim, 3)
                self.assertEqual(data.shape[1:], (schema.IMU_STEPS, schema.IMU_CHANNELS))
                self.assertEqual(data.dtype, np.float32)
                self.assertTrue(np.isfinite(data).all())
                self.assertGreaterEqual(data.shape[0], 3)
                self.assertLessEqual(float(np.abs(data).max()), 1.0)

    def test_imu_duration_matches_vision_length(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                bundle = generate_scenario(kind, PRESET_DEMO, fps=1)
                vision_sec = len(bundle.vision)
                imu_sec = bundle.imu_data.shape[0] * schema.IMU_STEPS / schema.IMU_HZ
                self.assertGreaterEqual(imu_sec + 10, vision_sec)


class TestDeterminism(unittest.TestCase):
    def test_same_seed_identical(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                a = generate_scenario(kind, PRESET_DEMO, seed=11)
                b = generate_scenario(kind, PRESET_DEMO, seed=11)
                self.assertEqual([vars(v) for v in a.vision], [vars(v) for v in b.vision])
                self.assertTrue(np.array_equal(a.imu_data, b.imu_data))

    def test_different_seed_changes_imu_only(self):
        a = generate_scenario("normal", PRESET_DEMO, seed=1)
        b = generate_scenario("normal", PRESET_DEMO, seed=2)
        self.assertEqual([vars(v) for v in a.vision], [vars(v) for v in b.vision])
        self.assertFalse(np.array_equal(a.imu_data, b.imu_data))


class TestImuSignalSeparability(unittest.TestCase):
    def test_estop_scenario_has_pulse(self):
        data = generate_scenario("estop", PRESET_DEMO, seed=3).imu_data
        magnitudes = np.sqrt((data ** 2).sum(axis=2))
        first_window = magnitudes[0]
        self.assertGreater(float(first_window.max()), 0.5)

    def test_quiet_scenario_stays_quiet(self):
        data = generate_scenario("trapped", PRESET_DEMO, seed=3).imu_data
        last_window = data[-1]
        self.assertLess(float(np.abs(last_window).max()), 0.2)


class TestWriteBundle(unittest.TestCase):
    def test_writes_npy_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = generate_scenario("trapped", PRESET_DEMO)
            paths = write_bundle(bundle, pathlib.Path(tmp))
            self.assertTrue(pathlib.Path(paths["imu_npy"]).exists())
            self.assertTrue(pathlib.Path(paths["sidecar"]).exists())
            loaded = np.load(paths["imu_npy"])
            self.assertEqual(loaded.shape, bundle.imu_data.shape)

    def test_refuses_demo_data_dir(self):
        bundle = generate_scenario("normal", PRESET_DEMO)
        with self.assertRaises(ValueError):
            write_bundle(bundle, ROOT / "common" / "demo_data")

    def test_default_dir_is_results_mock(self):
        from model.mock_scenario import MOCK_DIR

        self.assertEqual(MOCK_DIR.as_posix(), "results/_mock")


if __name__ == "__main__":
    unittest.main(verbosity=2)