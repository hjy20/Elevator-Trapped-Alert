"""T-B-002 · 配置单一源、双预设、CLI 覆盖、校验与快照。

运行：python tests/test_config.py -v
"""

import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import (  # noqa: E402
    ConfigError,
    PRESET_DEMO,
    PRESET_PRODUCTION,
    build_runtime_config,
    config_snapshot,
    validate_config,
)

THRESHOLD_FIELDS = (
    "dwell_threshold",
    "fall_thresh",
    "door_thresh",
    "estop_thresh",
    "idle_thresh",
    "move_thresh",
    "stop_thresh",
    "dwell_accel_factor",
    "fall_persist_sec",
    "door_open_timeout",
)

DECISION_FIELDS = {
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


class TestPresets(unittest.TestCase):
    def test_production_defaults(self):
        cfg = build_runtime_config([])
        self.assertEqual(cfg.thresholds.dwell_threshold, 180.0)
        self.assertEqual(cfg.thresholds.fall_thresh, 0.70)
        self.assertEqual(cfg.thresholds.door_thresh, 0.60)
        self.assertEqual(cfg.thresholds.estop_thresh, 0.80)
        self.assertFalse(cfg.demo_threshold)
        self.assertEqual(cfg.mode, "production")
        self.assertEqual(len(dataclasses.fields(cfg.thresholds)), 10)

    def test_demo_preset(self):
        cfg = build_runtime_config(["--demo"])
        self.assertEqual(cfg.thresholds.dwell_threshold, 8.0)
        self.assertTrue(cfg.demo_threshold)
        self.assertEqual(cfg.mode, "demo")
        for name in THRESHOLD_FIELDS:
            if name == "dwell_threshold":
                continue
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(cfg.thresholds, name),
                    getattr(PRESET_PRODUCTION.thresholds, name),
                )

    def test_cli_override(self):
        cfg = build_runtime_config(["--dwell-threshold", "8"])
        self.assertEqual(cfg.thresholds.dwell_threshold, 8.0)
        self.assertFalse(cfg.demo_threshold)

        cfg = build_runtime_config(
            ["--demo", "--fall-thresh", "0.65", "--device-id", "demo_elev_02", "--location", "5F", "--seed", "7"]
        )
        self.assertEqual(cfg.thresholds.fall_thresh, 0.65)
        self.assertEqual(cfg.thresholds.dwell_threshold, 8.0)
        self.assertEqual(cfg.device_id, "demo_elev_02")
        self.assertEqual(cfg.location, "5F")
        self.assertEqual(cfg.seed, 7)

    def test_cli_start_ts(self):
        cfg = build_runtime_config(["--start-ts-ms", "1763300123000"])
        self.assertEqual(cfg.imu.start_ts_ms, 1763300123000)
        self.assertIsNone(PRESET_PRODUCTION.imu.start_ts_ms)


class TestValidation(unittest.TestCase):
    def test_invalid_configs_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--dwell-threshold", "0"])
        self.assertIn("dwell_threshold", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--dwell-threshold", "600"])
        self.assertIn("dwell_threshold", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--fall-thresh", "1.3"])
        self.assertIn("fall_thresh", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--door-thresh", "-0.1"])
        self.assertIn("door_thresh", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--estop-thresh", "2"])
        self.assertIn("estop_thresh", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--accel-factor", "0.5"])
        self.assertIn("dwell_accel_factor", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--device-id", ""])
        self.assertIn("device_id", str(ctx.exception))

        cfg = dataclasses.replace(
            PRESET_PRODUCTION,
            thresholds=dataclasses.replace(PRESET_PRODUCTION.thresholds, move_thresh=0.05, stop_thresh=0.15),
        )
        with self.assertRaises(ConfigError) as ctx:
            validate_config(cfg)
        self.assertIn("move_thresh", str(ctx.exception))

    def test_out_of_range_idle_thresh_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            build_runtime_config(["--idle-thresh", "1.5"])
        self.assertIn("idle_thresh", str(ctx.exception))


class TestSnapshotAndImmutability(unittest.TestCase):
    def test_snapshot_completeness(self):
        snapshot = config_snapshot(build_runtime_config(["--demo"]))
        self.assertEqual(len(snapshot["thresholds"]), 10)
        for name in THRESHOLD_FIELDS:
            self.assertIn(name, snapshot["thresholds"])
        for key in ("mode", "demo_threshold", "device_id", "location", "seed"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["mode"], "demo")
        self.assertTrue(snapshot["demo_threshold"])
        self.assertIn("imu_epoch_base_ms", snapshot["imu"])
        self.assertEqual(set(snapshot["ablation"]), {"no_fall", "no_imu", "no_temporal"})
        self.assertEqual(set(snapshot["thresholds"]) & DECISION_FIELDS, set())

    def test_config_immutable(self):
        cfg = build_runtime_config([])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.thresholds.dwell_threshold = 1.0
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.device_id = "other"

    def test_preset_demo_is_isolated(self):
        self.assertEqual(PRESET_PRODUCTION.thresholds.dwell_threshold, 180.0)
        build_runtime_config(["--demo", "--dwell-threshold", "12"])
        self.assertEqual(PRESET_DEMO.thresholds.dwell_threshold, 8.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)