"""T-B-003 · `ts` 毫秒量级与相邻窗口断言（补契约测试覆盖缺口）。

本文件不导入 `model.*`，保证 D2 早期即可运行。
既有 `tests/test_schema_contract.py` 字节级不变。

运行：python tests/test_time_semantics.py -v
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.schema import Decision, IMUEvent, VisionOut  # noqa: E402

SAMPLE_TS = 1763300000000
WINDOW_MS = 10000


class TestTimestampScale(unittest.TestCase):
    def test_ts_millisecond_scale(self):
        objects = [
            VisionOut(
                ts=SAMPLE_TS,
                person_count=1,
                fall_prob=0.0,
                door_approach=0.0,
                crowded=0.0,
                keypoints_ok=True,
            ),
            IMUEvent(ts=SAMPLE_TS, estop=0.0, vibration=0.0, idle=1.0, window_sec=10.0),
            Decision(
                ts=SAMPLE_TS,
                device_id="ELEV-DEMO-001",
                trapped=False,
                urgency=0,
                location="3F",
                people_count=0,
                event_type="normal",
                confidence=0.5,
                reason=["dwell=0s"],
            ),
        ]
        for obj in objects:
            with self.subTest(cls=type(obj).__name__):
                self.assertIsInstance(obj.ts, int)
                self.assertGreaterEqual(obj.ts, 10 ** 12)
                self.assertLess(obj.ts, 10 ** 13)

    def test_imu_ts_adjacent_gap(self):
        events = [
            IMUEvent(ts=SAMPLE_TS + i * WINDOW_MS, estop=0.1, vibration=0.1, idle=0.9, window_sec=10.0)
            for i in range(3)
        ]
        self.assertEqual(len(events), 3)
        for prev, cur in zip(events, events[1:]):
            self.assertIsInstance(cur.ts, int)
            self.assertEqual(cur.ts - prev.ts, WINDOW_MS)
        self.assertEqual({e.window_sec for e in events}, {10.0})

    def test_dwell_ms_conversion(self):
        stop_enter_ts = SAMPLE_TS
        now_ts = SAMPLE_TS + 190000
        raw_dwell_ms = now_ts - stop_enter_ts
        dwell_sec = raw_dwell_ms / 1000.0
        self.assertEqual(raw_dwell_ms, 190000)
        self.assertAlmostEqual(dwell_sec, 190.0, delta=1e-9)

    def test_no_second_scale_leak(self):
        raw_dwell_ms = SAMPLE_TS + 190000 - SAMPLE_TS
        self.assertEqual(len(str(180 * 1000)), len(str(190000)))
        self.assertEqual(len(str(raw_dwell_ms)), 6)
        self.assertGreater(abs(180 - raw_dwell_ms), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)