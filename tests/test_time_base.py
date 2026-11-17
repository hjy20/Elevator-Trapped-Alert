"""T-B-001 · IMU 段起点 `t0_ms` 三级解析。

运行：python tests/test_time_base.py -v
"""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import (  # noqa: E402
    PRESET_PRODUCTION,
    build_runtime_config,
    resolve_start_ts_ms,
    sidecar_path,
)

SAMPLE_TS = 1763300000000


class TestResolveStartTs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.npy = self.tmp / "demo_trapped_imu.npy"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_sidecar(self, payload):
        path = sidecar_path(str(self.npy))
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_sidecar_path_rule(self):
        self.assertEqual(
            sidecar_path("common/demo_data/demo_trapped_imu.npy").name,
            "demo_trapped_imu.meta.json",
        )

    def test_sidecar_wins(self):
        self._write_sidecar({"start_ts_ms": SAMPLE_TS + 123000})
        cfg = build_runtime_config(["--start-ts-ms", str(SAMPLE_TS)])
        value, source = resolve_start_ts_ms(str(self.npy), cfg)
        self.assertEqual(source, "sidecar")
        self.assertEqual(value, SAMPLE_TS + 123000)

    def test_cli_when_no_sidecar(self):
        cfg = build_runtime_config(["--start-ts-ms", "1763300123000"])
        value, source = resolve_start_ts_ms(str(self.npy), cfg)
        self.assertEqual(source, "cli")
        self.assertEqual(value, 1763300123000)

    def test_default_emits_fallback_warning(self):
        with self.assertLogs("model.config", level="WARNING") as logs:
            value, source = resolve_start_ts_ms(str(self.npy), PRESET_PRODUCTION)
        self.assertEqual(source, "default")
        self.assertEqual(value, PRESET_PRODUCTION.imu.imu_epoch_base_ms)
        self.assertTrue(any("ts_base_fallback" in line for line in logs.output))

    def test_invalid_sidecars_fall_through(self):
        cases = [
            ({}, "missing_field"),
            ({"start_ts_ms": "1763300000000"}, "not_int"),
            ({"start_ts_ms": True}, "not_int"),
            ({"start_ts_ms": 1763300000}, "not_millisecond_scale"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                self._write_sidecar(payload)
                with self.assertLogs("model.config", level="WARNING") as logs:
                    value, source = resolve_start_ts_ms(
                        str(self.npy), build_runtime_config(["--start-ts-ms", str(SAMPLE_TS)])
                    )
                self.assertEqual(source, "cli")
                self.assertEqual(value, SAMPLE_TS)
                self.assertTrue(
                    any(f"sidecar_invalid:{reason}" in line for line in logs.output)
                )

    def test_broken_json_falls_through(self):
        sidecar_path(str(self.npy)).write_text("{not json", encoding="utf-8")
        with self.assertLogs("model.config", level="WARNING") as logs:
            value, source = resolve_start_ts_ms(str(self.npy), PRESET_PRODUCTION)
        self.assertEqual(source, "default")
        self.assertEqual(value, PRESET_PRODUCTION.imu.imu_epoch_base_ms)
        self.assertTrue(any("sidecar_invalid:json_decode" in line for line in logs.output))

    def test_never_raises_on_missing_everything(self):
        value, source = resolve_start_ts_ms(str(self.npy), PRESET_PRODUCTION)
        self.assertEqual(source, "default")
        self.assertIsInstance(value, int)
        self.assertGreaterEqual(value, 10 ** 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)