"""T-UP-004 · L2 启发式特征后端。

运行：python -m unittest tests.test_upload_heuristic -v
"""
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402
import service.upload_features as feat  # noqa: E402
from model.eval import load_vision_sequence  # noqa: E402
from tests.upload_helpers import make_video  # noqa: E402


class TestUploadHeuristic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="up_heur_")
        cls.base = pathlib.Path(cls.temp.name)
        cls.color = make_video(cls.base / "color.mp4", seconds=4)
        cls.black = make_video(cls.base / "black.mp4", seconds=4, black=True)
        cls.info = up.probe_video(cls.color, 1763300180000)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def _extract(self, video, start=1763300180000):
        return feat.extract_rows(
            mode="heuristic",
            video_path=video,
            start_ts_ms=start,
            door_roi=None,
            fps=self.info.fps,
            frames_total=self.info.frames,
            deadline=time.monotonic() + 120,
            progress=lambda done, total: None,
        )

    def test_rows_count_and_fields(self):
        result = self._extract(self.color)
        self.assertEqual(result.mode, "heuristic")
        self.assertEqual(len(result.rows), self.info.frames)
        for row in result.rows:
            self.assertEqual(set(row.__dict__), set(feat.VISION_FIELDS))

    def test_l2_red_lines(self):
        result = self._extract(self.color)
        self.assertTrue(all(row.fall_prob == 0.0 for row in result.rows))
        self.assertTrue(all(row.keypoints_ok is False for row in result.rows))
        self.assertGreaterEqual(max(row.person_count for row in result.rows), 1)

    def test_ts_strictly_increasing(self):
        result = self._extract(self.color)
        self.assertEqual(result.rows[0].ts, 1763300180000)
        for previous, current in zip(result.rows, result.rows[1:]):
            self.assertGreater(current.ts, previous.ts)

    def test_black_video_no_person(self):
        result = self._extract(self.black)
        self.assertTrue(all(row.person_count == 0 for row in result.rows))
        step = feat.frame_step()
        self.assertEqual(result.low_conf_count, (len(result.rows) + step - 1) // step)

    def test_validate_heuristic_rejects_fall(self):
        rows = self._extract(self.color).rows
        broken = list(rows)
        broken[0] = type(rows[0])(rows[0].ts, 1, 0.5, 0.0, 0.0, False)
        with self.assertRaises(ValueError):
            feat.validate_heuristic_vision(broken)

    def test_validate_shape(self):
        rows = self._extract(self.color).rows
        feat.validate_vision_shape(rows, len(rows), rows[0].ts)
        with self.assertRaises(ValueError):
            feat.validate_vision_shape(rows[:-1], len(rows), rows[0].ts)

    def test_write_and_reload_vision(self):
        rows = self._extract(self.color).rows
        path = pathlib.Path(self.temp.name) / "vision.json"
        feat.write_vision_json(path, rows)
        loaded = load_vision_sequence(path)
        self.assertEqual(len(loaded), len(rows))
        self.assertEqual(loaded[0].ts, rows[0].ts)

    def test_detect_feature_mode_heuristic(self):
        mode, ok = feat.detect_feature_mode(self.base)
        self.assertEqual(mode, "heuristic")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()