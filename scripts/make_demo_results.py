"""
生成 5 段演示用 Decision 序列 → results/decision_<case>.json

作用：A/B 的真实产出到位前（D7 之前），让看板、回放器、联调都能跑起来。
格式严格遵循契约 §6.3 的 Decision，reason 必须可追到具体数值。

用法：python scripts/make_demo_results.py
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS = BASE_DIR / "results"
BASE_TS = 1763300000000          # 固定基准，便于复现
STEP_MS = 2000                   # 每 2 秒一条，20 秒左右一段
DEVICE = "ELEV-DEMO-001"


def decision(idx, urgency, location, people, event, conf, reasons, trapped=False):
    return {
        "ts": BASE_TS + idx * STEP_MS,
        "device_id": DEVICE,
        "trapped": trapped,
        "urgency": urgency,
        "location": location,
        "people_count": people,
        "event_type": event,
        "confidence": conf,
        "reason": reasons,
    }


def build_normal():
    floors = ["1F", "2F", "3F", "4F", "5F", "3F"]
    out = []
    for i, f in enumerate(floors):
        out.append(decision(i, 0, f, 2, "normal", 0.96, [
            f"dwell={12 + i * 3}s < 180s",
            "door_state=open",
            "fall_prob=0.02 < 0.70",
        ]))
    return out


def build_estop():
    out = [
        decision(0, 0, "6F", 1, "normal", 0.94, ["dwell=8s < 180s", "imu.estop=0.05"]),
        decision(1, 0, "6F", 1, "normal", 0.93, ["dwell=10s < 180s", "imu.estop=0.08"]),
    ]
    for i in range(2, 9):
        dwell = 45 + (i - 2) * 20
        out.append(decision(i, 1, "6F", 1, "estop", 0.88, [
            f"imu.estop=0.91 > 0.80",
            f"dwell={dwell}s < 180s",
            "door_state=closed",
            "fall_prob=0.05 < 0.70",
        ]))
    return out


def build_trapped():
    out = [
        decision(0, 0, "4F", 2, "normal", 0.95, ["dwell=9s < 180s", "door_state=open"]),
        decision(1, 1, "4F", 2, "estop", 0.86, ["imu.estop=0.84 > 0.80", "dwell=32s < 180s"]),
    ]
    for i in range(2, 10):
        dwell = 120 + (i - 2) * 40
        urgency = 2 if dwell > 180 else 1
        out.append(decision(i, urgency, "4F", 2, "trapped" if urgency == 2 else "estop", 0.90, [
            f"dwell={dwell}s {'>' if dwell > 180 else '<'} 180s",
            "door_state=closed",
            "imu.estop=0.90",
            f"fall_prob=0.12 < 0.70",
        ], trapped=urgency == 2))
    return out


def build_pry():
    out = [
        decision(0, 0, "2F", 1, "normal", 0.94, ["dwell=6s < 180s", "door_state=open"]),
        decision(1, 1, "2F", 1, "estop", 0.85, ["imu.estop=0.82 > 0.80", "dwell=40s < 180s"]),
    ]
    for i in range(2, 9):
        out.append(decision(i, 2, "2F", 1, "pry", 0.87, [
            f"door_approach={0.62 + i * 0.03:.2f} > 0.60",
            f"dwell={60 + (i - 2) * 25}s",
            "door_state=closed",
            "imu.vibration=0.71 > 0.60",
        ], trapped=True))
    return out


def build_faint():
    out = [
        decision(0, 0, "3F", 2, "normal", 0.95, ["dwell=10s < 180s", "door_state=open"]),
        decision(1, 1, "3F", 2, "estop", 0.86, ["imu.estop=0.88 > 0.80", "dwell=55s < 180s"]),
        decision(2, 2, "3F", 2, "trapped", 0.89, ["dwell=190s > 180s", "door_state=closed"], trapped=True),
    ]
    for i in range(3, 10):
        fall = 0.55 + (i - 3) * 0.08
        out.append(decision(i, 3, "3F", 2, "faint", 0.91, [
            f"dwell={190 + (i - 3) * 40}s > 180s",
            "door_state=closed",
            f"fall_prob={fall:.2f} > 0.70",
            "imu.estop=0.92",
        ], trapped=True))
    return out


CASES = {
    "demo_normal": build_normal,
    "demo_estop": build_estop,
    "demo_trapped": build_trapped,
    "demo_pry": build_pry,
    "demo_faint": build_faint,
}


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    for name, builder in CASES.items():
        items = builder()
        path = RESULTS / f"decision_{name}.json"
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        peak = max(it["urgency"] for it in items)
        print(f"[OK] {path.name:34s} {len(items):2d} 条  最高紧急度 {peak}")


if __name__ == "__main__":
    main()
