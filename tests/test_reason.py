"""T-B-008 / T-B-009 · 判决树、置信度、reason 黄金用例与 impossible pair。

运行：python tests/test_reason.py -v
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.config import PRESET_DEMO, PRESET_PRODUCTION  # noqa: E402
from model.reason import (  # noqa: E402
    ReasonBuilder,
    ReasonContext,
    assert_pair_legal,
    derive_verdict,
)


def context(**overrides):
    base = dict(
        dwell_sec=412.0,
        dwell_threshold=180.0,
        person_count=1,
        fall_prob=0.31,
        door_approach=0.22,
        crowded=0.15,
        motion_level=0.01,
        imu_estop=0.12,
        imu_idle=0.91,
        imu_vibration=0.20,
        keypoints_ok=True,
        imu_present=True,
        demo_mode=False,
        door_proxy_used=True,
        fall_persist_ms=0,
    )
    base.update(overrides)
    return ReasonContext(**base)


class TestGoldenReasons(unittest.TestCase):
    def test_unique_trapped_reason(self):
        verdict = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.31,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.22,
            imu_estop=0.12,
            imu_idle=0.91,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
            crowded=0.15,
        )
        reason = ReasonBuilder().build(verdict, context())
        self.assertEqual(
            reason,
            [
                "dwell=412s > 180s",
                "person_count=1 >= 1",
                "imu.idle=0.91 > 0.80",
                "technical_debt=door_signal_proxy",
            ],
        )
        self.assertEqual(verdict.event_type, "trapped")
        self.assertEqual(verdict.urgency, 2)

    def test_pry_demo_reason(self):
        ctx = context(
            dwell_sec=95.0,
            dwell_threshold=8.0,
            person_count=2,
            door_approach=0.82,
            fall_prob=0.40,
            crowded=0.35,
            imu_estop=None,
            imu_idle=None,
            imu_vibration=None,
            imu_present=False,
            demo_mode=True,
        )
        verdict = derive_verdict(
            trapped=True,
            cfg=PRESET_DEMO,
            fall_prob=0.40,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.82,
            imu_estop=None,
            imu_present=False,
            dwell_sec=95.0,
            person_count=2,
            crowded=0.35,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertEqual(
            reason,
            [
                "dwell=95s > 8s",
                "person_count=2 >= 1",
                "door_approach=0.82 > 0.60",
                "imu_unavailable=true",
                "technical_debt=door_signal_proxy",
                "demo_threshold=true",
            ],
        )
        self.assertEqual(verdict.event_type, "pry")

    def test_faint_reason(self):
        ctx = context(
            dwell_sec=212.0,
            fall_prob=0.87,
            door_approach=0.35,
            crowded=0.0,
            imu_estop=0.92,
            imu_idle=0.88,
            imu_vibration=None,
            fall_persist_ms=4200,
        )
        verdict = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.87,
            keypoints_ok=True,
            fall_persist_ms=4200,
            door_approach=0.35,
            imu_estop=0.92,
            imu_idle=0.88,
            imu_present=True,
            dwell_sec=212.0,
            person_count=1,
            crowded=0.0,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertEqual(
            reason,
            [
                "dwell=212s > 180s",
                "person_count=1 >= 1",
                "fall_prob=0.87 > 0.70",
                "imu.estop=0.92 > 0.80",
                "imu.idle=0.88 > 0.80",
                "technical_debt=door_signal_proxy",
            ],
        )
        self.assertEqual(verdict.urgency, 3)
        self.assertEqual(verdict.event_type, "faint")

    def test_normal_reason_max_two(self):
        ctx = context(dwell_sec=3.0, person_count=2, imu_estop=None, imu_idle=None)
        verdict = derive_verdict(
            trapped=False,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.02,
            imu_estop=None,
            imu_present=True,
            dwell_sec=3.0,
            person_count=2,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertLessEqual(len(reason), 2)
        self.assertGreaterEqual(len(reason), 1)
        self.assertEqual(reason[0], "dwell=3s <= 180s")

    def test_estop_reason(self):
        ctx = context(dwell_sec=12.0, dwell_threshold=180.0, imu_estop=0.92, imu_idle=0.10)
        verdict = derive_verdict(
            trapped=False,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.02,
            imu_estop=0.92,
            imu_present=True,
            dwell_sec=12.0,
            person_count=1,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertEqual(verdict.event_type, "estop")
        self.assertEqual(verdict.urgency, 1)
        self.assertIn("imu.estop=0.92 > 0.80", reason)


class TestReasonRules(unittest.TestCase):
    def _all_reasons(self):
        builder = ReasonBuilder()
        cases = [
            (context(), True, 0.31, 0.22, 0.12, 0.91, 412.0),
            (context(door_approach=0.82, imu_estop=None, imu_idle=None, imu_present=False), True, 0.4, 0.82, None, None, 412.0),
            (context(fall_prob=0.87, fall_persist_ms=4200), True, 0.87, 0.35, 0.92, 0.88, 212.0),
        ]
        out = []
        for ctx, trapped, fall, door, estop, idle, dwell in cases:
            verdict = derive_verdict(
                trapped=trapped,
                cfg=PRESET_PRODUCTION,
                fall_prob=fall,
                keypoints_ok=ctx.keypoints_ok,
                fall_persist_ms=ctx.fall_persist_ms,
                door_approach=door,
                imu_estop=estop,
                imu_idle=idle,
                imu_present=ctx.imu_present,
                dwell_sec=dwell,
                person_count=ctx.person_count,
                crowded=ctx.crowded,
            )
            out.append(builder.build(verdict, ctx))
        return out

    def test_whitelist_only(self):
        allowed = {
            "dwell",
            "person_count",
            "fall_prob",
            "door_approach",
            "crowded",
            "motion_level",
            "imu.estop",
            "imu.idle",
            "imu.vibration",
            "keypoints_unreliable",
            "technical_debt",
            "demo_threshold",
            "imu_unavailable",
        }
        for reason in self._all_reasons():
            for item in reason:
                key = item.split("=", 1)[0]
                self.assertIn(key, allowed, item)

    def test_no_naked_words(self):
        banned = ("检测到异常", "疑似困人", "请及时处理")
        for reason in self._all_reasons():
            for item in reason:
                self.assertRegex(item, r"^[a-z_]+(\.[a-z_]+)?=.+$")
                for phrase in banned:
                    self.assertNotIn(phrase, item)

    def test_no_millisecond_leak(self):
        for reason in self._all_reasons():
            for item in reason:
                self.assertIsNone(re.search(r"\d{10,}", item), item)

    def test_keypoints_unreliable_written(self):
        ctx = context(keypoints_ok=False, fall_prob=0.99, fall_persist_ms=9000)
        verdict = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=False,
            fall_persist_ms=9000,
            door_approach=0.35,
            imu_estop=0.10,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertIn("keypoints_unreliable=true", reason)
        self.assertNotEqual(verdict.event_type, "faint")

    def test_imu_unavailable_written(self):
        ctx = context(imu_present=False, imu_estop=None, imu_idle=None)
        verdict = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.22,
            imu_estop=None,
            imu_present=False,
            dwell_sec=412.0,
            person_count=1,
        )
        reason = ReasonBuilder().build(verdict, ctx)
        self.assertIn("imu_unavailable=true", reason)
        self.assertLessEqual(verdict.urgency, 2)

    def test_pair_legal_raises(self):
        with self.assertRaises(ValueError):
            assert_pair_legal(1, "trapped")
        with self.assertRaises(ValueError):
            assert_pair_legal(3, "pry")
        with self.assertRaises(ValueError):
            assert_pair_legal(2, "faint")
        with self.assertRaises(ValueError):
            assert_pair_legal(9, "normal")

    def test_crowded_reduces_confidence(self):
        low = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.22,
            imu_estop=0.10,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
            crowded=0.1,
        )
        high = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.0,
            keypoints_ok=True,
            fall_persist_ms=0,
            door_approach=0.22,
            imu_estop=0.10,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
            crowded=0.9,
        )
        self.assertEqual(low.trapped, high.trapped)
        self.assertLess(high.confidence, low.confidence)

    def test_faint_requires_persistence(self):
        short = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.95,
            keypoints_ok=True,
            fall_persist_ms=2000,
            door_approach=0.2,
            imu_estop=0.1,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
        )
        long = derive_verdict(
            trapped=True,
            cfg=PRESET_PRODUCTION,
            fall_prob=0.95,
            keypoints_ok=True,
            fall_persist_ms=3000,
            door_approach=0.2,
            imu_estop=0.1,
            imu_present=True,
            dwell_sec=412.0,
            person_count=1,
        )
        self.assertNotEqual(short.event_type, "faint")
        self.assertEqual(long.event_type, "faint")


if __name__ == "__main__":
    unittest.main(verbosity=2)