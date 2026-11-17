"""T-B-021 / T-B-022 / T-B-023 · 评测骨架、指标手算、混淆矩阵与口径固化。

运行：python tests/test_eval.py -v
"""

import csv
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import PRESET_DEMO  # noqa: E402
from model.eval import (  # noqa: E402
    EVENT_TYPES,
    SCOPE_STATEMENT,
    build_record,
    compute_metrics,
    plot_confusion,
    render_metrics_md,
    segment_level,
    write_claims,
)

TS = 1763300000000


def record(case, expected, predicted, trapped_frames=0, total=10, first_alarm=None, stop=TS):
    return {
        "case": case,
        "expected": expected,
        "predicted": predicted,
        "expected_urgency": 0,
        "max_urgency": 0,
        "total_frames": total,
        "trapped_frames": trapped_frames,
        "first_alarm_ts": first_alarm,
        "stop_enter_ts": stop,
        "observed_ms": total * 1000,
    }


class TestSegmentLevel(unittest.TestCase):
    def test_priority_order(self):
        class Fake:
            def __init__(self, event_type, urgency):
                self.event_type = event_type
                self.urgency = urgency

        decisions = [Fake("normal", 0), Fake("trapped", 2), Fake("pry", 2)]
        self.assertEqual(segment_level(decisions), "pry")
        decisions = [Fake("faint", 3), Fake("pry", 2)]
        self.assertEqual(segment_level(decisions), "faint")
        self.assertEqual(segment_level([]), "normal")


class TestMetricsHandComputed(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("a", "trapped", "trapped", trapped_frames=5),
            record("b", "normal", "normal", trapped_frames=0),
            record("c", "pry", "normal", trapped_frames=0),
        ]
        self.metrics = compute_metrics(self.records, PRESET_DEMO)

    def test_class_metrics(self):
        trapped = self.metrics["per_class"]["trapped"]
        self.assertEqual((trapped["tp"], trapped["fp"], trapped["fn"]), (1, 0, 0))
        self.assertAlmostEqual(trapped["precision"], 1.0, places=9)
        self.assertAlmostEqual(trapped["recall"], 1.0, places=9)

        normal = self.metrics["per_class"]["normal"]
        self.assertEqual((normal["tp"], normal["fp"], normal["fn"]), (1, 1, 0))
        self.assertAlmostEqual(normal["precision"], 0.5, places=9)
        self.assertAlmostEqual(normal["recall"], 1.0, places=9)

        pry = self.metrics["per_class"]["pry"]
        self.assertEqual((pry["tp"], pry["fp"], pry["fn"]), (0, 0, 1))
        self.assertAlmostEqual(pry["precision"], 0.0, places=9)
        self.assertAlmostEqual(pry["recall"], 0.0, places=9)

    def test_rates(self):
        self.assertAlmostEqual(self.metrics["miss_rate"], 0.5, places=9)
        self.assertAlmostEqual(self.metrics["segment_fp_rate"], 0.0, places=9)
        self.assertAlmostEqual(self.metrics["frame_fp_rate"], 0.0, places=9)

    def test_no_empty_cells(self):
        for event_type in EVENT_TYPES:
            row = self.metrics["per_class"][event_type]
            self.assertIsInstance(row["precision"], float)
            self.assertIsInstance(row["recall"], float)
            self.assertIsInstance(row["f1"], float)

    def test_observed_hours_and_fp_per_day(self):
        self.assertAlmostEqual(self.metrics["observed_hours"], 0.008333, places=5)
        self.assertEqual(self.metrics["fp_per_day"], 0.0)

    def test_response_time(self):
        self.assertGreaterEqual(self.metrics["mean_response_sec"], 0.0)


class TestArtifacts(unittest.TestCase):
    def test_scope_statement_present(self):
        records = [record("a", "normal", "normal")]
        metrics = compute_metrics(records, PRESET_DEMO)
        text = render_metrics_md(metrics, PRESET_DEMO, "python model/eval.py --cases all")
        self.assertIn(SCOPE_STATEMENT, text)
        self.assertIn("生成命令", text)
        self.assertIn("config_snapshot.json", text)
        self.assertIn("数据来源：合成场景库（mock，非真实采集）", text)

    def test_fp_per_day_label(self):
        metrics = compute_metrics([record("a", "normal", "normal")], PRESET_DEMO)
        text = render_metrics_md(metrics, PRESET_DEMO, "cmd")
        self.assertIn("仅示意", text)
        self.assertIn("不可外推", text)
        self.assertIn("秒观测换算", text)

    def test_false_alarm_section_before_recall(self):
        metrics = compute_metrics([record("a", "normal", "normal")], PRESET_DEMO)
        text = render_metrics_md(metrics, PRESET_DEMO, "cmd")
        self.assertLess(text.index("正常场景误报"), text.index("检出能力"))

    def test_claims_traceable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            metrics = compute_metrics([record("a", "normal", "normal")], PRESET_DEMO)
            write_claims(metrics, out, "results/metrics.csv")
            claims = json.loads((out / "claims.json").read_text(encoding="utf-8"))
        self.assertGreater(len(claims), 0)
        for entry in claims:
            self.assertTrue(entry["source_file"])
            self.assertTrue(entry["key_path"])
            self.assertEqual(entry["scope"], SCOPE_STATEMENT)
            if entry["metric"] == "fp_per_day":
                self.assertTrue(entry["extrapolated"])
                self.assertIn("observed_hours", entry)

    def test_confusion_matrix_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "confusion_matrix.png"
            plot_confusion(
                [record("a", "trapped", "trapped"), record("b", "normal", "normal")], out
            )
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 1024)


class TestBuildRecord(unittest.TestCase):
    def test_record_uses_label_and_trace(self):
        result = {
            "case": "demo_trapped",
            "observed_ms": 207000,
            "stop_enter_ts": TS + 5000,
            "decisions": [],
        }
        label = {
            "expected_event": "trapped",
            "expected_urgency": 2,
            "observed_ms": 207000,
            "stop_enter_ts": None,
        }
        row = build_record(result, label)
        self.assertEqual(row["expected"], "trapped")
        self.assertEqual(row["observed_ms"], 207000)
        self.assertEqual(row["stop_enter_ts"], TS + 5000)


class TestAblationAndCalibration(unittest.TestCase):
    def setUp(self):
        self.results = ROOT / "results"

    def test_ablation_four_rows(self):
        path = self.results / "ablation.csv"
        self.assertTrue(path.exists(), "run: python model/eval.py --cases all --mock --ablation")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [row["variant"] for row in rows],
            ["baseline", "no_fall", "no_imu", "no_temporal"],
        )
        for row in rows:
            for key, value in row.items():
                self.assertNotEqual(value, "", f"{row['variant']}.{key} is empty")

    def test_ablation_delta_consistency(self):
        path = self.results / "ablation.csv"
        if not path.exists():
            self.skipTest("ablation.csv not generated yet")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        baseline = rows[0]
        for key in ("precision", "recall", "f1", "fp_count"):
            self.assertAlmostEqual(float(baseline[f"delta_{key}"]), 0.0, places=6)
        for row in rows[1:]:
            for key in ("precision", "recall", "f1", "fp_count"):
                expected = float(row[key]) - float(baseline[key])
                self.assertAlmostEqual(
                    float(row[f"delta_{key}"]), expected, places=6, msg=f"{row['variant']}.{key}"
                )

    def test_no_fall_degrades_faint(self):
        path = self.results / "ablation.csv"
        if not path.exists():
            self.skipTest("ablation.csv not generated yet")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = {row["variant"]: row for row in csv.DictReader(handle)}
        self.assertLess(float(rows["no_fall"]["faint_recall"]), 1.0)
        self.assertLess(float(rows["no_imu"]["estop_recall"]), 1.0)

    def test_calibration_order(self):
        path = self.results / "threshold_calibration.md"
        if not path.exists():
            self.skipTest("threshold_calibration.md not generated yet")
        text = path.read_text(encoding="utf-8")
        self.assertIn("正常场景误报", text)
        self.assertLess(text.index("正常场景误报"), text.index("选定阈值"))

    def test_calibration_keeps_baseline(self):
        path = self.results / "threshold_calibration.md"
        if not path.exists():
            self.skipTest("threshold_calibration.md not generated yet")
        text = path.read_text(encoding="utf-8")
        self.assertIn("dwell_threshold = 180.0", text)
        self.assertIn("fall_thresh = 0.7", text)


class TestModelCardAndWording(unittest.TestCase):
    BANNED = (
        r"准确率\s*\d+\s*%",
        "商用级",
        "SOTA",
        "state-of-the-art",
        r"95\s*%",
        r"99\s*%",
        "高精度",
    )

    def test_model_card_sections(self):
        path = ROOT / "model" / "MODEL_CARD.md"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        for section in (
            "任务定义与输入输出",
            "规则组成",
            "数据",
            "指标与消融",
            "已知局限与技术债",
            "与现有方案的差异",
            "演示参数声明",
        ):
            self.assertIn(section, text)
        self.assertIn("合成", text)
        self.assertIn("不可外推", text)

    def test_banned_words(self):
        import re

        files = [
            ROOT / "results" / "metrics.md",
            ROOT / "results" / "EVAL_REPORT.md",
            ROOT / "model" / "MODEL_CARD.md",
        ]
        files += sorted((ROOT / "results").glob("*.json"))
        for path in files:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in self.BANNED:
                self.assertIsNone(
                    re.search(pattern, text), f"banned wording {pattern!r} in {path.name}"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)