"""五类场景假数据生成器（D2–D3 单测与 D7 `--mock` 共用）。

落盘目录固定 `results/_mock/`，绝不写入 `common/demo_data/`（REQ-CONST-003）。
`ts` 一律 Unix epoch 毫秒（DEC-03）。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from common import schema
from model.config import RuntimeConfig

MOCK_DIR = Path("results") / "_mock"
FORBIDDEN_DIR = (Path("common") / "demo_data").resolve()

IMU_HZ = schema.IMU_HZ
STEPS_PER_WINDOW = schema.IMU_STEPS
WINDOW_SEC = schema.IMU_WINDOW_SEC
MIN_IMU_STEPS = 600

KINDS: Tuple[str, ...] = ("normal", "estop", "trapped", "pry", "faint")

EXPECTATIONS: Dict[str, Tuple[str, int, bool]] = {
    "normal": ("normal", 0, False),
    "estop": ("estop", 1, False),
    "trapped": ("trapped", 2, True),
    "pry": ("pry", 2, True),
    "faint": ("faint", 3, True),
}

# (持续秒, IMU 段类型, 人数, door_approach, fall_prob)
Phase = Tuple[float, str, int, float, float]


@dataclass(frozen=True)
class ScenarioExpectation:
    event_type: str
    urgency: int
    trapped: bool


@dataclass
class ScenarioBundle:
    case: str
    kind: str
    vision: List[schema.VisionOut]
    imu_data: np.ndarray
    start_ts_ms: int
    fps: int
    expected: ScenarioExpectation
    imu_path: Optional[str] = None


def _phases(kind: str) -> List[Phase]:
    if kind == "normal":
        return [
            (2.0, "idle", 0, 0.02, 0.0),
            (3.0, "moving", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.60, 0.0),
            (3.0, "idle", 2, 0.02, 0.0),
            (1.0, "idle", 2, 0.60, 0.0),
            (2.0, "idle", 0, 0.02, 0.0),
        ]
    if kind == "estop":
        return [
            (2.0, "idle", 0, 0.02, 0.0),
            (3.0, "moving", 1, 0.02, 0.0),
            (1.0, "estop_pulse", 1, 0.02, 0.10),
            (10.0, "idle", 1, 0.22, 0.10),
        ]
    if kind == "trapped":
        return [
            (2.0, "idle", 0, 0.02, 0.0),
            (2.0, "moving", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.60, 0.0),
            (1.0, "idle", 1, 0.22, 0.31),
            (200.0, "idle", 1, 0.22, 0.31),
        ]
    if kind == "pry":
        return [
            (2.0, "idle", 0, 0.02, 0.0),
            (2.0, "moving", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.02, 0.0),
            (1.0, "vibrate", 1, 0.82, 0.25),
            (200.0, "vibrate", 1, 0.82, 0.25),
        ]
    if kind == "faint":
        return [
            (2.0, "idle", 0, 0.02, 0.0),
            (2.0, "moving", 0, 0.02, 0.0),
            (1.0, "idle", 0, 0.02, 0.0),
            (1.0, "idle", 1, 0.30, 0.87),
            (200.0, "idle", 1, 0.30, 0.87),
        ]
    raise ValueError(f"unknown scenario kind: {kind!r}, expected one of {KINDS}")


def _segment(steps: int, motion: str, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(steps, dtype=np.float32) / IMU_HZ
    envelope = np.ones(steps, dtype=np.float32)
    fade = min(10, steps // 2)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        envelope[:fade] = ramp
        envelope[-fade:] = ramp[::-1]

    if motion == "idle":
        seg = rng.normal(0.0, 0.0025, size=(steps, 3))
    elif motion == "moving":
        wave = 0.35 * envelope * np.sin(2 * np.pi * 1.2 * t)
        seg = wave[:, None] + rng.normal(0.0, 0.02, size=(steps, 3))
    elif motion == "vibrate":
        wave = 0.25 * envelope * np.sin(2 * np.pi * 5.0 * t)
        seg = wave[:, None] + rng.normal(0.0, 0.02, size=(steps, 3))
    elif motion == "estop_pulse":
        seg = rng.normal(0.0, 0.003, size=(steps, 3))
        start = min(10, max(steps - 10, 0))
        seg[start : start + 10] += 0.9
    else:
        raise ValueError(f"unknown imu motion kind: {motion!r}")
    return np.clip(seg, -1.0, 1.0).astype(np.float32)


def _synth_imu(phases: List[Phase], rng: np.random.Generator) -> np.ndarray:
    total_sec = sum(p[0] for p in phases)
    raw_steps = max(MIN_IMU_STEPS, int(round(total_sec * IMU_HZ)))
    n_steps = -(-raw_steps // STEPS_PER_WINDOW) * STEPS_PER_WINDOW
    out = np.zeros((n_steps, 3), dtype=np.float32)
    cursor = 0
    for duration, motion, *_rest in phases:
        steps = int(round(duration * IMU_HZ))
        if steps <= 0:
            continue
        steps = min(steps, n_steps - cursor)
        if steps <= 0:
            break
        out[cursor : cursor + steps] = _segment(steps, motion, rng)
        cursor += steps
    if cursor < n_steps:
        out[cursor:] = _segment(n_steps - cursor, "idle", rng)
    return out.reshape(-1, STEPS_PER_WINDOW, schema.IMU_CHANNELS)


def make_frames(
    kind: str,
    cfg: RuntimeConfig,
    fps: int = 1,
    start_ts_ms: Optional[int] = None,
) -> List[schema.VisionOut]:
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    t0 = start_ts_ms
    if t0 is None:
        t0 = cfg.imu.start_ts_ms
    if t0 is None:
        t0 = cfg.imu.imu_epoch_base_ms

    frames: List[schema.VisionOut] = []
    index = 0
    for duration, _motion, person, door, fall in _phases(kind):
        count = max(1, int(round(duration * fps)))
        for _ in range(count):
            frames.append(
                schema.VisionOut(
                    ts=int(t0 + round(index * 1000.0 / fps)),
                    person_count=person,
                    fall_prob=fall,
                    door_approach=door,
                    crowded=0.10,
                    keypoints_ok=True,
                )
            )
            index += 1
    return frames


def generate_scenario(
    kind: str,
    cfg: RuntimeConfig,
    seed: int = 0,
    fps: int = 1,
    start_ts_ms: Optional[int] = None,
) -> ScenarioBundle:
    if kind not in KINDS:
        raise ValueError(f"unknown scenario kind: {kind!r}, expected one of {KINDS}")

    rng = np.random.default_rng(seed)
    phases = _phases(kind)
    vision = make_frames(kind, cfg, fps=fps, start_ts_ms=start_ts_ms)
    imu_data = _synth_imu(phases, rng)
    event_type, urgency, trapped = EXPECTATIONS[kind]

    return ScenarioBundle(
        case=f"demo_{kind}",
        kind=kind,
        vision=vision,
        imu_data=imu_data,
        start_ts_ms=vision[0].ts,
        fps=fps,
        expected=ScenarioExpectation(
            event_type=event_type, urgency=urgency, trapped=trapped
        ),
    )


def write_bundle(bundle: ScenarioBundle, out_dir: Optional[Path] = None) -> Dict[str, str]:
    target = Path(out_dir) if out_dir is not None else MOCK_DIR
    resolved = target.resolve()
    if resolved == FORBIDDEN_DIR or FORBIDDEN_DIR in resolved.parents:
        raise ValueError(
            "refusing to write mock data into common/demo_data/ (REQ-CONST-003)"
        )

    target.mkdir(parents=True, exist_ok=True)
    npy_path = target / f"{bundle.case}_imu.npy"
    np.save(npy_path, bundle.imu_data)

    meta_path = target / f"{bundle.case}_imu.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "case": bundle.case,
                "start_ts_ms": int(bundle.start_ts_ms),
                "fps": bundle.fps,
                "source": "mock",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bundle.imu_path = str(npy_path)
    return {"imu_npy": str(npy_path), "sidecar": str(meta_path)}


def write_all(kinds: Tuple[str, ...], cfg: RuntimeConfig, seed: int = 0, fps: int = 1) -> Dict[str, Dict[str, str]]:
    return {
        kind: write_bundle(generate_scenario(kind, cfg, seed=seed, fps=fps))
        for kind in kinds
    }