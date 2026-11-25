"""生成 5 段符合 L1 契约的演示数据（视频 + IMU + 关键点轨迹 + 标签 + 真值）。

用法：
    python data/scripts/build_demo_data.py --all
    python data/scripts/build_demo_data.py --case demo_faint
    python data/scripts/build_demo_data.py --all --seconds 20 --fps 25

产物（全部落在 common/demo_data/ 与 data/）：
    common/demo_data/demo_<case>.mp4          1280x720 / 25fps / H.264
    common/demo_data/demo_<case>_kpts.jsonl   逐帧 17 关键点（合成轨迹，供离线兜底）
    common/demo_data/demo_<case>_meta.json    视频元数据（起始时间戳、ROI、场景说明）
    common/demo_data/demo_<case>_imu.npy      (N,200,3) float32，每通道 [-1,1]
    common/demo_data/demo_<case>_imu_raw.npy  (M,3)   float32，原始 m/s²
    common/demo_data/demo_<case>_imu_meta.json 采样率/窗口/归一化常数
    data/labels/demo_<case>.json              场景级弱标签（B 做评测用）
    data/processed/demo_<case>_gt.json        逐帧真值（人数/倒地/贴门），用于量化视觉误差
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.scripts.scene import (  # noqa: E402
    POSE_LIBRARY,
    CabinRenderer,
    SceneGeometry,
    Track,
    TrackKey,
    person_visible_px,
)
from data.scripts.video_io import H264Writer  # noqa: E402

DEMO_DIR = PROJECT_ROOT / "common" / "demo_data"
LABEL_DIR = PROJECT_ROOT / "data" / "labels"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

# 演示数据统一锚在 2026-11-17 19:30（+08:00），每段间隔 5 分钟
ANCHOR = datetime(2026, 11, 17, 19, 30, 0, tzinfo=timezone(timedelta(hours=8)))

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 场景脚本
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """一段演示数据的完整剧本。"""

    case: str
    event_type: str
    urgency: int
    trapped: bool
    summary: str
    tracks: List[Track]
    door_keys: List[Tuple[float, float]]          # (t, 开门比例 0-1)
    shake_keys: List[Tuple[float, float]] = None  # type: ignore[assignment]
    fallen_labels: Tuple[str, ...] = ()
    imu_kind: str = "normal"

    def __post_init__(self) -> None:
        if self.shake_keys is None:
            self.shake_keys = [(0.0, 0.0)]

    # -- 门开度 ------------------------------------------------------------
    def door_open(self, t: float) -> float:
        keys = self.door_keys
        if t <= keys[0][0]:
            return keys[0][1]
        if t >= keys[-1][0]:
            return keys[-1][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                span = max(1e-6, t1 - t0)
                w = (t - t0) / span
                w = w * w * (3 - 2 * w)
                return float(v0 + (v1 - v0) * w)
        return float(keys[-1][1])

    def shake(self, t: float) -> Tuple[float, float]:
        keys = self.shake_keys
        if len(keys) == 1:
            return keys[0][1], keys[0][1]
        if t <= keys[0][0]:
            return 0.0, 0.0
        if t >= keys[-1][0]:
            return 0.0, 0.0
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                span = max(1e-6, t1 - t0)
                w = 1.0 - abs(2.0 * ((t - t0) / span) - 1.0)   # 三角包络
                return float(v0 + (v1 - v0) * w), 0.0
        return 0.0, 0.0


def build_normal() -> Scenario:
    """正常乘梯：两人先出画、再回场，中途一人蹲下捡东西（考"不误报"）。

    时间轴（对齐 supplement_ac.md C1/C2/C3）：
      0–6.6 s 两人在场（「有人 + 门未开」6.6 s < 8 s dwell）；
      6.6–12.5 s 两人真出画（人数归零 ≥3 s，触发 B 的 empty_steady 复位）；
      12.5–14.2 s 从右侧回场；14.5–17.0 s 蹲下防误报用例。
    """
    p1 = Track(
        label="p1",
        depth=0.93,
        x_base=-1.05,
        palette=("navy", "dark", "skin"),
        seed=11,
        keys=[
            TrackKey(0.0, "stand", note="等电梯/站立"),
            TrackKey(1.2, "walk_a", x=0.0, note="走动"),
            TrackKey(2.4, "walk_b", x=0.35),
            TrackKey(3.6, "walk_a", x=0.55),
            TrackKey(4.6, "stand", x=0.55, note="停下"),
            TrackKey(5.0, "walk_b", x=0.55),
            TrackKey(6.4, "walk_a", x=4.70, note="向右真出画（depth 0.93 阈值 3.44 m）"),
            TrackKey(12.5, "walk_a", x=4.70, note="画外等待"),
            TrackKey(13.8, "walk_b", x=0.00, note="从右侧回场"),
            TrackKey(14.5, "crouch", x=0.00, note="蹲下捡东西（防误报用例）"),
            TrackKey(17.0, "stand", x=0.00),
            TrackKey(19.9, "stand", x=0.00),
        ],
    )
    p2 = Track(
        label="p2",
        depth=0.82,
        x_base=0.62,
        palette=("grey", "denim", "skin"),
        seed=23,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(5.4, "stand_shift", x=-0.10),
            TrackKey(6.6, "walk_a", x=3.50, note="向右真出画（depth 0.82 阈值 3.28 m）"),
            TrackKey(12.8, "walk_a", x=3.50, note="画外等待"),
            TrackKey(14.2, "walk_b", x=-0.10, note="跟随回场"),
            TrackKey(19.9, "stand", x=-0.10),
        ],
    )
    return Scenario(
        case="demo_normal",
        event_type="normal",
        urgency=0,
        trapped=False,
        summary="正常乘梯：2 人先出画再回场，中途一人蹲下捡东西，不触发任何告警",
        tracks=[p1, p2],
        door_keys=[
            (0.0, 0.0),
            (6.2, 0.0),
            (7.0, 1.0),
            (8.6, 0.0),
            (12.8, 0.0),
            (13.6, 1.0),
            (15.0, 0.0),
            (19.9, 0.0),
        ],
        imu_kind="normal",
    )


def build_estop() -> Scenario:
    """急停：轿厢猛停并轻微抖动，1 人被晃后自行脱困离场。

    时间轴（对齐 supplement_ac.md C1/C2）：
      0–5.5 s 1 人在场（含 4.05 s 急停冲击）；
      5.5–7.4 s 向右真出画（depth 0.90 阈值 3.56 m）；
      7.4–20.0 s 画面无人，覆盖 IMU 窗口 1（结束 9.95 s）的对齐起点 9.45 s。
    """
    p1 = Track(
        label="p1",
        depth=0.90,
        x_base=-0.15,
        palette=("red", "dark", "skin"),
        seed=31,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(4.05, "stand", note="急停瞬间"),
            TrackKey(4.6, "bend", note="被晃得前倾，但很快站直"),
            TrackKey(5.4, "stand"),
            TrackKey(5.5, "walk_b", x=0.00, note="转身离开"),
            TrackKey(7.4, "walk_a", x=3.95, note="向右真出画（急停后自行脱困）"),
            TrackKey(19.9, "walk_a", x=3.95, note="画外，保持无人"),
        ],
    )
    return Scenario(
        case="demo_estop",
        event_type="estop",
        urgency=1,
        trapped=False,
        summary="电梯急停：轿厢剧烈抖动，1 人站立未倒地并自行脱困离开轿厢，紧急度 1（关注）",
        tracks=[p1],
        door_keys=[(0.0, 0.0), (19.9, 0.0)],
        shake_keys=[(0.0, 0.0), (4.1, 7.0), (4.9, 0.0)],
        imu_kind="estop",
    )


def build_trapped() -> Scenario:
    """困人：停梯、门不开、2 人在轿厢里滞留并来回踱步。"""
    p1 = Track(
        label="p1",
        depth=0.92,
        x_base=-0.55,
        palette=("beige", "grey", "skin"),
        seed=41,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(3.0, "walk_b", x=0.25),
            TrackKey(5.0, "stand", x=0.30),
            TrackKey(8.0, "stand_shift", x=0.34),
            TrackKey(11.0, "walk_a", x=-0.10),
            TrackKey(13.5, "stand", x=-0.15),
            TrackKey(16.0, "stand_shift", x=-0.20),
            TrackKey(19.9, "stand", x=-0.20),
        ],
    )
    p2 = Track(
        label="p2",
        depth=0.80,
        x_base=0.95,
        palette=("green", "dark", "skin"),
        seed=53,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(4.5, "stand_shift", x=-0.12),
            TrackKey(6.0, "crouch", note="蹲下看电梯地板缝，不是倒地"),
            TrackKey(8.2, "crouch"),
            TrackKey(9.2, "stand"),
            TrackKey(12.0, "stand", x=-0.01),
            TrackKey(15.0, "walk_b", x=-0.35),
            TrackKey(17.0, "stand", x=-0.38),
            TrackKey(19.9, "stand", x=-0.38),
        ],
    )
    return Scenario(
        case="demo_trapped",
        event_type="trapped",
        urgency=2,
        trapped=True,
        summary="停梯困人：门始终未开，轿厢内 2 人滞留 20 秒（演示阈值 8s 会触发）",
        tracks=[p1, p2],
        door_keys=[(0.0, 0.0), (19.9, 0.0)],
        imu_kind="trapped",
    )


def build_pry() -> Scenario:
    """扒门：两人被困，一人从 10 s 起反复拍门、扒门缝。

    时间轴（对齐 supplement_ac.md 2.4）：
      0–10.0 s 两人在场且不贴门（「有人 + 门未开」10 s 内 dwell 尚未达阈值）；
      10.0–18.0 s 贴门（door>0.6 与 trapped 窗口重叠 ≥2 s）；
      18.8 s 起 p2 蹲下（贴门段外）。
    """
    p1 = Track(
        label="p1",
        depth=0.86,
        x_base=2.02,
        palette=("navy", "denim", "skin"),
        seed=61,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(9.0, "walk_a", x=-0.60, note="走向门"),
            TrackKey(10.0, "reach_door", x=-0.60, note="伸手扒门缝"),
            TrackKey(11.2, "push_door"),
            TrackKey(12.8, "reach_door"),
            TrackKey(14.4, "push_door"),
            TrackKey(15.6, "reach_door"),
            TrackKey(16.8, "push_door"),
            TrackKey(18.0, "reach_door"),
            TrackKey(19.0, "walk_b", x=-0.30),
            TrackKey(19.9, "stand", x=-0.30),
        ],
    )
    p2 = Track(
        label="p2",
        depth=0.78,
        x_base=0.35,
        palette=("black", "grey", "skin"),
        seed=71,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(9.0, "stand_shift", x=0.05),
            TrackKey(18.8, "crouch", note="蹲下抱头（贴门段外）"),
            TrackKey(19.9, "crouch"),
        ],
    )
    return Scenario(
        case="demo_pry",
        event_type="pry",
        urgency=2,
        trapped=True,
        summary="扒门求助：门未开，一人反复扒门缝/拍门，一只手在门缝持续 >1s",
        tracks=[p1, p2],
        door_keys=[(0.0, 0.0), (19.9, 0.0)],
        imu_kind="pry",
    )


def build_faint() -> Scenario:
    """倒地：一人突然晕倒躺地，另一人蹲下查看。"""
    p1 = Track(
        label="p1",
        depth=0.93,
        x_base=-0.30,
        palette=("grey", "dark", "skin"),
        seed=81,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(3.0, "stand_shift", x=-0.05),
            TrackKey(6.4, "stand", note="站着说话"),
            TrackKey(6.9, "sit", note="腿一软先坐下"),
            TrackKey(7.6, "lie", y=0.0, note="倒在地上"),
            TrackKey(19.9, "lie", occlude=(15, 16), note="躺地上不动（下肢被身体遮挡）"),
        ],
    )
    p2 = Track(
        label="p2",
        depth=0.80,
        x_base=1.05,
        palette=("beige", "denim", "skin"),
        seed=93,
        keys=[
            TrackKey(0.0, "stand"),
            TrackKey(7.4, "stand", note="愣住"),
            TrackKey(8.6, "walk_a", x=-0.55),
            TrackKey(9.8, "crouch", x=-0.60, note="蹲下查看同伴（不是倒地）"),
            TrackKey(19.9, "crouch", x=-0.60),
        ],
    )
    return Scenario(
        case="demo_faint",
        event_type="faint",
        urgency=3,
        trapped=True,
        summary="轿厢内晕倒：一人 6.9s 起倒地不起，另一人蹲下查看，紧急度 3",
        tracks=[p1, p2],
        door_keys=[(0.0, 0.0), (19.9, 0.0)],
        fallen_labels=("p1",),
        imu_kind="faint",
    )


SCENARIOS = {
    "demo_normal": build_normal,
    "demo_estop": build_estop,
    "demo_trapped": build_trapped,
    "demo_pry": build_pry,
    "demo_faint": build_faint,
}
CASE_ORDER = ["demo_normal", "demo_estop", "demo_trapped", "demo_pry", "demo_faint"]


# ---------------------------------------------------------------------------
# IMU 仿真
# ---------------------------------------------------------------------------


def synth_imu(kind: str, seconds: float, fs: float = 20.0, seed: int = 5) -> np.ndarray:
    """生成 (M,3) 的 [ax, ay, az]（m/s²）。

    这是一个**简化物理模型**，只求"事件在信号上的形态是对的"：
      * 匀速/停止：az ≈ 9.81，三轴只有传感器噪声；
      * 启动/到站：az 有 ±0.35 m/s² 的平滑加减速包络；
      * 急停：az 在 0.4s 内跌到 ~6.2、回弹到 ~15.5，随后 3-8Hz 余振；
      * 拍门：4-8Hz 的冲击串，幅度 ~0.8 m/s²；
      * 倒地：一次性 ~1.5 m/s² 的脉冲（低频冲击）。
    """
    rng = np.random.default_rng(seed)
    n = int(round(seconds * fs))
    t = np.arange(n) / fs
    ax = rng.normal(0.0, 0.035, n)
    ay = rng.normal(0.0, 0.035, n)
    az = 9.81 + rng.normal(0.0, 0.045, n)

    def bump(center: float, width: float, amp: float) -> np.ndarray:
        env = np.exp(-0.5 * ((t - center) / max(1e-3, width)) ** 2)
        return amp * env

    if kind == "normal":
        # 两段运行：起步加速 + 到站减速
        for center, amp in ((1.6, 0.35), (3.4, -0.35), (23.0, 0.30), (25.0, -0.30)):
            az += bump(center, 0.55, amp)
        az += 0.02 * np.sin(2 * np.pi * 0.7 * t)
    elif kind == "estop":
        az += bump(1.2, 0.6, 0.30)
        az += bump(4.05, 0.16, -3.4)                       # 急停冲击
        az += bump(4.42, 0.22, 5.4)                        # 回弹
        ring = np.exp(-(t - 4.3).clip(min=0) * 0.75) * (t >= 4.3)
        az += 1.1 * ring * np.sin(2 * np.pi * 5.2 * (t - 4.3))
        ax += 0.30 * ring * np.sin(2 * np.pi * 4.1 * (t - 4.3) + 0.6)
        ax += 0.18 * ring * np.sin(2 * np.pi * 7.3 * (t - 4.3))
    elif kind == "trapped":
        az += 0.015 * np.sin(2 * np.pi * 0.25 * t)
        ax += 0.010 * np.sin(2 * np.pi * 0.31 * t + 1.0)
    elif kind == "pry":
        for center in (10.6, 12.2, 13.8, 15.4):
            burst = (np.abs(t - center) < 0.45).astype(float) * np.exp(-np.abs(t - center) * 6)
            ax += 0.85 * burst * np.sin(2 * np.pi * 6.4 * (t - center))
            ay += 0.55 * burst * np.sin(2 * np.pi * 5.1 * (t - center) + 1.2)
            az += 0.35 * burst * np.sin(2 * np.pi * 7.5 * (t - center))
        # 人拍门时轿厢有轻微晃
        az += 0.03 * np.sin(2 * np.pi * 0.9 * t)
    elif kind == "faint":
        az += bump(7.55, 0.10, 1.6)                        # 身体落地冲击
        az += 0.25 * np.exp(-(t - 7.6).clip(min=0) * 2.2) * (t >= 7.6) * np.sin(2 * np.pi * 3.4 * (t - 7.6))
    return np.stack([ax, ay, az], axis=1).astype(np.float32)


IMU_SCALES = {"ax": 3.0, "ay": 3.0, "az_offset": 9.81, "az_scale": 6.0}


def normalize_imu(raw: np.ndarray, scales: Dict[str, float] = IMU_SCALES) -> np.ndarray:
    """按固定常数归一化到 [-1,1]：ax/3、ay/3、(az-9.81)/6。

    用固定常数而不是按样本最大最小值缩放，是为了让**不同段之间、云端与边缘之间可比**：
    同一段急停放到别的视频里，数值还是一样的含义（这一点写进 DATA_DICT）。
    """
    out = np.empty_like(raw, dtype=np.float32)
    out[:, 0] = np.clip(raw[:, 0] / float(scales["ax"]), -1.0, 1.0)
    out[:, 1] = np.clip(raw[:, 1] / float(scales["ay"]), -1.0, 1.0)
    out[:, 2] = np.clip((raw[:, 2] - float(scales["az_offset"])) / float(scales["az_scale"]), -1.0, 1.0)
    return out


def make_windows(samples: np.ndarray, window: int = 200, stride: int = 100) -> np.ndarray:
    """把连续采样切成 (N, window, 3) 的窗口张量。"""
    chunks = []
    for start in range(0, max(1, len(samples) - window + 1), stride):
        chunk = samples[start:start + window]
        if len(chunk) == window:
            chunks.append(chunk)
    if not chunks:
        padded = np.zeros((window, samples.shape[1]), dtype=samples.dtype)
        padded[: min(window, len(samples))] = samples[: min(window, len(samples))]
        chunks = [padded]
    return np.stack(chunks, axis=0).astype(np.float32)


# ---------------------------------------------------------------------------
# 单段生成
# ---------------------------------------------------------------------------


def ground_truth(scenario: Scenario, seconds: float, fps: float) -> List[dict]:
    """逐帧真值：人数、是否有倒地、是否有手贴门（用来量化视觉模块误差）。

    判定口径与渲染器完全一致：**用同一套投影**判断人是否在画面内，
    这样"视频里看得见的人"和"真值里算的人"永远是同一批。
    """
    geom = SceneGeometry(width=1280, height=720)
    rows: List[dict] = []
    total = int(round(seconds * fps))
    near_limit = 0.12 * 288.0          # 与 door.near_ratio × 门宽 对齐
    for index in range(total):
        t = index / fps
        present: List[str] = []
        fallen = 0
        door_contact = 0
        for track in scenario.tracks:
            kpts_m, x_m, y_m, _occ = track.sample(t)
            kpts_px = geom.project(kpts_m, x_m, y_m, track.depth)
            if not person_visible_px(kpts_px, geom.width, geom.height):
                continue
            present.append(track.label)
            if track.label in scenario.fallen_labels and t >= 7.0:
                fallen += 1
            wrist_px = float(kpts_px[[9, 10], 0].max())
            if abs(wrist_px - 1084.0) <= near_limit:
                door_contact += 1
        rows.append(
            {
                "frame": index,
                "t": round(t, 3),
                "person_count": len(present),
                "fallen": fallen,
                "door_contact": 1 if door_contact else 0,
                "present": present,
            }
        )
    return rows


def build_case(
    scenario: Scenario,
    *,
    seconds: float = 20.0,
    fps: float = 25.0,
    start_ts_ms: int,
    seed: int = 7,
) -> Dict[str, object]:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    geom = SceneGeometry(width=1280, height=720)
    renderer = CabinRenderer(geom, seed=seed)
    video_path = DEMO_DIR / f"{scenario.case}.mp4"
    kpts_path = DEMO_DIR / f"{scenario.case}_kpts.jsonl"
    total = int(round(seconds * fps))
    gt_rows = ground_truth(scenario, seconds, fps)

    print(f"[{scenario.case}] 渲染 {total} 帧 -> {video_path.relative_to(PROJECT_ROOT)}")
    with H264Writer(video_path, geom.width, geom.height, fps=fps) as writer, kpts_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as sidecar:
        for index in range(total):
            t = index / fps
            ts = start_ts_ms + int(round(index * 1000.0 / fps))
            shake = scenario.shake(t)
            frame, keypoints, labels = renderer.render(
                t,
                scenario.tracks,
                door_open=scenario.door_open(t),
                shake=shake,
                fallen_labels=scenario.fallen_labels,
                clock_text=f"CAM-01 ELEV-3F  {ANCHOR.strftime('%Y-%m-%d')} {_clock(start_ts_ms, index, fps)}",
            )
            writer.write(frame)
            sidecar.write(
                json.dumps(
                    {
                        "frame": index,
                        "ts": ts,
                        "persons": [np.round(k, 2).tolist() for k in keypoints],
                        "scores": [0.93 for _ in keypoints],
                        "labels": labels,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # ---- IMU ----
    raw = synth_imu(scenario.imu_kind, seconds=seconds + 10.0, fs=20.0, seed=seed + 1000)
    normalized = normalize_imu(raw)
    windows = make_windows(normalized, window=200, stride=100)
    np.save(DEMO_DIR / f"{scenario.case}_imu.npy", windows)
    np.save(DEMO_DIR / f"{scenario.case}_imu_raw.npy", raw)
    fs_imu = 20.0
    window_end_ts = [
        start_ts_ms + int(round((start + 199) / fs_imu * 1000))
        for start in range(0, len(raw) - 199, 100)
    ]
    (DEMO_DIR / f"{scenario.case}_imu_meta.json").write_text(
        json.dumps(
            {
                "case": scenario.case,
                "fs": fs_imu,
                "window": 200,
                "window_sec": 10.0,
                "stride": 100,
                "stride_sec": 5.0,
                "shape": list(windows.shape),
                "channels": ["ax", "ay", "az"],
                "unit": "m/s^2 (raw) / [-1,1] (normalized)",
                "normalize": IMU_SCALES,
                "raw_samples": int(len(raw)),
                "window_end_ts_ms": window_end_ts,
                "generator": "data/scripts/build_demo_data.py:synth_imu(kind=%s)" % scenario.imu_kind,
                "disclaimer": "简化物理模型合成信号，不是真机采集；形态与真实事件一致，数值不可当真机标定依据。",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # ---- 视频元数据（推理时读它拿时间基准与门框 ROI）----
    (DEMO_DIR / f"{scenario.case}_meta.json").write_text(
        json.dumps(
            {
                "case": scenario.case,
                "scene": "synthetic",
                "fps": fps,
                "frames": total,
                "width": geom.width,
                "height": geom.height,
                "duration_sec": round(total / fps, 2),
                "start_ts_ms": start_ts_ms,
                "door_roi": "demo_cabin",
                "event_type": scenario.event_type,
                "expected_urgency": scenario.urgency,
                "summary": scenario.summary,
                "keypoint_sidecar": kpts_path.name,
                "generator": "data/scripts/build_demo_data.py",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # ---- 场景级弱标签（B 评测用）----
    (LABEL_DIR / f"{scenario.case}.json").write_text(
        json.dumps(
            {
                "case": scenario.case,
                "event_type": scenario.event_type,
                "urgency": scenario.urgency,
                "trapped": scenario.trapped,
                "people_max": max(row["person_count"] for row in gt_rows),
                "duration_sec": round(total / fps, 2),
                "label_level": "scene",
                "summary": scenario.summary,
                "notes": [
                    "场景级弱标签：只标这一段属于哪一类，不逐帧标",
                    "逐帧真值见 data/processed/%s_gt.json" % scenario.case,
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (PROCESSED_DIR / f"{scenario.case}_gt.json").write_text(
        json.dumps(
            {
                "case": scenario.case,
                "fps": fps,
                "frames": total,
                "source": "synthetic-ground-truth",
                "rows": gt_rows,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "case": scenario.case,
        "video": str(video_path),
        "frames": total,
        "imu_shape": list(windows.shape),
        "people_max": max(row["person_count"] for row in gt_rows),
    }


def _clock(start_ts_ms: int, index: int, fps: float) -> str:
    moment = datetime.fromtimestamp(start_ts_ms / 1000.0 + index / fps, tz=CST)
    return moment.strftime("%H:%M:%S") + f".{int(index % fps):02d}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 5 段 L1 合规演示数据")
    parser.add_argument("--case", choices=CASE_ORDER, help="只生成某一段")
    parser.add_argument("--all", action="store_true", help="生成全部 5 段")
    parser.add_argument("--seconds", type=float, default=20.0, help="每段时长（秒），默认 20")
    parser.add_argument("--fps", type=float, default=25.0, help="帧率，契约要求 25")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    if not args.all and not args.case:
        parser.error("要么 --all，要么 --case <name>")

    cases = CASE_ORDER if args.all else [args.case]
    summary = []
    for offset, case in enumerate(cases):
        start_ts_ms = int((ANCHOR + timedelta(minutes=5 * CASE_ORDER.index(case))).timestamp() * 1000)
        summary.append(
            build_case(
                SCENARIOS[case](),
                seconds=args.seconds,
                fps=args.fps,
                start_ts_ms=start_ts_ms,
                seed=args.seed + offset,
            )
        )

    print("\n=== 生成完成 ===")
    for row in summary:
        print(f"  {row['case']:<14} frames={row['frames']:<4} imu={row['imu_shape']} people_max={row['people_max']}")
    print(f"输出目录：{DEMO_DIR.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())