"""T-UP-003 · 探测与时间基准。

运行：python -m unittest tests.test_upload_time -v
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402
from common.schema import Decision  # noqa: E402

START = 1763300180000


def _decision(ts):
    return Decision(ts, "ELEV-3F-001", True, 2, "3F", 1, "trapped", 0.8, [])


class TestUploadTime(unittest.TestCase):
    def test_parse_ts13_three_states(self):
        self.assertEqual(up.parse_ts13("1763300180000"), START)
        self.assertIsNone(up.parse_ts13("1763300180"))
        self.assertIsNone(up.parse_ts13("1763300180000.0"))
        self.assertIsNone(up.parse_ts13("-1763300180000"))
        self.assertIsNone(up.parse_ts13(None))

    def test_ts_of_frame_first_and_last(self):
        self.assertEqual(up.ts_of_frame(START, 25.0, 0), START)
        self.assertEqual(up.ts_of_frame(START, 25.0, 299), START + 11960)

    def test_frame_of_ts_inverse(self):
        for fps in (25.0, 29.97):
            for index in (0, 1, 150, 299):
                ts = up.ts_of_frame(START, fps, index)
                self.assertEqual(up.frame_of_ts(START, fps, ts), index)

    def test_duration_and_window(self):
        self.assertEqual(up.duration_ms_of(300, 25.0), 12000)
        up.validate_time_window([_decision(START), _decision(START + 12000)], START, 12000)
        for bad in (START - 1001, START + 13001):
            with self.assertRaises(up.UploadError) as ctx:
                up.validate_time_window([_decision(bad)], START, 12000)
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(ctx.exception.code, "UP_TIME_BASE_INVALID")

    def test_history_not_shifted(self):
        old = START - 86400000 * 30
        self.assertIsNone(up.validate_time_window([_decision(old)], old, 12000))

    def test_resolve_door_roi(self):
        roi, source = up.resolve_door_roi([10, 20, 100, 200], 640, 360)
        self.assertEqual(roi, [10, 20, 100, 200])
        self.assertEqual(source, "client")
        default, source = up.resolve_door_roi(None, 640, 360)
        self.assertEqual(default, [0, 240, 640, 360])
        self.assertEqual(source, "default")
        fallback, source = up.resolve_door_roi([10, 20, 700, 400], 640, 360)
        self.assertEqual(fallback, [0, 240, 640, 360])
        self.assertEqual(source, "default")

    def test_parse_door_roi_rejects_shape(self):
        with self.assertRaises(up.UploadError) as ctx:
            up.parse_door_roi("1,2,3")
        self.assertEqual(ctx.exception.code, "UP_BAD_ROI")
        with self.assertRaises(up.UploadError):
            up.parse_door_roi("100,200,10,20")
        self.assertEqual(up.parse_door_roi("0,0,640,360"), [0, 0, 640, 360])


if __name__ == "__main__":
    unittest.main()