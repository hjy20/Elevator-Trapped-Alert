"""L1 契约快照测试。

目的：任何人改动 common/schema.py 的字段名、顺序或类型，都会在这里直接失败。
修改契约时必须"改 schema + 改本快照"同一次提交，并同步 A/C。

运行：
    python tests/test_schema_contract.py -v
"""

import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import schema  # noqa: E402


def field_signature(cls):
    return [(f.name, str(f.type)) for f in dataclasses.fields(cls)]


EXPECTED_FIELDS = {
    "VisionOut": [
        ("ts", "<class 'int'>"),
        ("person_count", "<class 'int'>"),
        ("fall_prob", "<class 'float'>"),
        ("door_approach", "<class 'float'>"),
        ("crowded", "<class 'float'>"),
        ("keypoints_ok", "<class 'bool'>"),
    ],
    "IMUEvent": [
        ("ts", "<class 'int'>"),
        ("estop", "<class 'float'>"),
        ("vibration", "<class 'float'>"),
        ("idle", "<class 'float'>"),
        ("window_sec", "<class 'float'>"),
    ],
    "Decision": [
        ("ts", "<class 'int'>"),
        ("device_id", "<class 'str'>"),
        ("trapped", "<class 'bool'>"),
        ("urgency", "<class 'int'>"),
        ("location", "<class 'str'>"),
        ("people_count", "<class 'int'>"),
        ("event_type", "<class 'str'>"),
        ("confidence", "<class 'float'>"),
        ("reason", "typing.List[str]"),
    ],
}

EXPECTED_SHAPE = {
    "IMU_STEPS": 200,
    "IMU_HZ": 20,
    "IMU_CHANNELS": 3,
    "IMU_WINDOW_SEC": 10.0,
    "PERSON_COUNT_MAX": 8,
}


class TestContractFields(unittest.TestCase):
    def test_contract_version(self):
        self.assertEqual(schema.CONTRACT_VERSION, "L1-1.0")

    def test_vision_out_fields(self):
        self.assertEqual(field_signature(schema.VisionOut), EXPECTED_FIELDS["VisionOut"])

    def test_imu_event_fields(self):
        self.assertEqual(field_signature(schema.IMUEvent), EXPECTED_FIELDS["IMUEvent"])

    def test_decision_fields(self):
        self.assertEqual(field_signature(schema.Decision), EXPECTED_FIELDS["Decision"])

    def test_no_extra_dataclasses(self):
        found = {
            name
            for name, obj in vars(schema).items()
            if dataclasses.is_dataclass(obj) and isinstance(obj, type)
        }
        self.assertEqual(found, {"VisionOut", "IMUEvent", "Decision"})


class TestContractValues(unittest.TestCase):
    def test_shape_constants(self):
        for name, expected in EXPECTED_SHAPE.items():
            self.assertEqual(getattr(schema, name), expected, name)

    def test_event_types(self):
        self.assertEqual(
            tuple(schema.EVENT_TYPES),
            ("normal", "estop", "trapped", "pry", "faint"),
        )

    def test_urgency_pairs_are_legal(self):
        flat = [etype for types in schema.URGENCY_EVENT_MAP.values() for etype in types]
        self.assertEqual(sorted(flat), sorted(schema.EVENT_TYPES))

    def test_trapped_never_urgency_1(self):
        for urgency, types in schema.URGENCY_EVENT_MAP.items():
            if urgency == 1:
                self.assertNotIn("trapped", types)


class TestConstructible(unittest.TestCase):
    def test_decision_roundtrip(self):
        decision = schema.Decision(
            ts=1763300000000,
            device_id="ELEV-DEMO-001",
            trapped=True,
            urgency=3,
            location="3F",
            people_count=2,
            event_type="faint",
            confidence=0.91,
            reason=["dwell=412s > 180s", "fall_prob=0.87 > 0.70"],
        )
        self.assertEqual(dataclasses.asdict(decision)["urgency"], 3)
        self.assertIn(decision.event_type, schema.URGENCY_EVENT_MAP[decision.urgency])

    def test_vision_out_and_imu_event(self):
        vision = schema.VisionOut(
            ts=0, person_count=2, fall_prob=0.1, door_approach=0.2, crowded=0.3, keypoints_ok=True
        )
        imu = schema.IMUEvent(ts=10000, estop=0.1, vibration=0.1, idle=0.9, window_sec=10.0)
        self.assertTrue(vision.keypoints_ok)
        self.assertEqual(imu.window_sec, schema.IMU_WINDOW_SEC)


if __name__ == "__main__":
    unittest.main(verbosity=2)