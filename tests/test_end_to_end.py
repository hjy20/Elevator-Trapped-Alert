"""T-B-025 · 端到端与确定性（T8）：5 类场景 → 5 份 JSON。

运行：python tests/test_end_to_end.py -v
"""

import dataclasses
import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import PRESET_DEMO  # noqa: E402
from model.eval import materialize, run_case, segment_level  # noqa: E402
from model.mock_scenario import KINDS  # noqa: E402

EVENT_FIELDS = {
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


def _run(kind, seed=0):
    return run_case(kind, PRESET_DEMO, seed=seed)


class TestEndToEnd(unittest.TestCase):
    def test_mock_five_cases(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                result = _run(kind)
                decisions = result["decisions"]
                self.assertTrue(decisions)
                for item in decisions:
                    self.assertEqual(set(dataclasses.asdict(item)), EVENT_FIELDS)
                    self.assertTrue(item.reason)
                    self.assertGreaterEqual(item.urgency, 0)
                    self.assertLessEqual(item.urgency, 3)
                    self.assertGreaterEqual(item.people_count, 0)
                    self.assertLessEqual(item.people_count, 8)
                    self.assertGreaterEqual(item.confidence, 0.0)
                    self.assertLessEqual(item.confidence, 1.0)

    def test_five_event_types_covered(self):
        found = {segment_level(_run(kind)["decisions"]) for kind in KINDS}
        self.assertEqual(found, {"normal", "estop", "trapped", "pry", "faint"})

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            for kind in KINDS:
                materialize(_run(kind), out)
            files = sorted(out.glob("*_decision.json"))
            self.assertEqual(len(files), 5)
            for path in files:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertGreater(len(payload), 0)
                for item in payload:
                    self.assertEqual(set(item), EVENT_FIELDS)
                raw = path.read_text(encoding="utf-8")
                for token in ("NaN", "Infinity", "-Infinity"):
                    self.assertNotIn(token, raw)

    def test_determinism_three_runs(self):
        first = [dataclasses.asdict(item) for item in _run("trapped")["decisions"]]
        for _ in range(2):
            again = [dataclasses.asdict(item) for item in _run("trapped")["decisions"]]
            self.assertEqual(first, again)

    def test_file_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            for kind in KINDS:
                materialize(_run(kind), out)
            for path in out.glob("*_decision.json"):
                self.assertRegex(path.name, r"^[a-z0-9_]+_decision\.json$")
                self.assertFalse(re.search(r"[\u4e00-\u9fff]", path.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)