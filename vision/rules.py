"""规则层：把一帧的关键点变成一条 ``VisionOut``。

四个字段各对应一条可讲清楚的规则：

1. ``person_count`` —— 头部关键点聚类 + 肩膀交叉校验，取较大值（抗遮挡，不依赖 bbox 计数）
2. ``fall_prob``    —— 躯干与竖直方向夹角 / 头低于髋 的**几何证据**，再看 3 秒内的**持续比例**
3. ``door_approach``—— 腕部是否落进门框 ROI 且贴近门缝，且**持续 1 秒**
4. ``crowded``      —— 人体框面积和 / 画面面积，饱和映射到 0-1

``keypoints_ok`` 是给 B 的降级开关：关键点不可信时（逆光、遮挡、只看到背影），
B 必须只用人数 + 时长，不许用 fall_prob。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from common.schema import VisionOut

from .config import DoorRoi, VisionConfig, load_roi
from .geometry import (
    FallFeatures,
    Person,
    count_heads,
    count_shoulders,
    door_features,
    fall_features,
    mean_conf,
    visible_mask,
)
from .temporal import DoorFilter, EmaSmoother, FallFilter, MedianCounter

__all__ = ["VisionEngine", "FrameResult"]


@dataclass
class FrameResult:
    """一条 VisionOut 加上"为什么"（调试与文档用，不进契约）。"""

    vision: VisionOut
    persons: int = 0
    fall_state: Dict[str, Any] = field(default_factory=dict)
    door_raw: float = 0.0
    door_ratio: float = 0.0
    area_ratio: float = 0.0
    usable_persons: int = 0
    notes: List[str] = field(default_factory=list)


class VisionEngine:
    """有状态的推理引擎：同一段视频要按时间顺序喂帧。"""

    def __init__(
        self,
        cfg: Optional[VisionConfig] = None,
        roi: Optional[DoorRoi] = None,
        *,
        frame_size: Tuple[int, int] = (1280, 720),
    ) -> None:
        self.cfg = cfg or VisionConfig()
        base_roi = roi if roi is not None else load_roi(self.cfg.door.roi)
        self._base_roi = base_roi
        self.roi = base_roi
        self.frame_size = (int(frame_size[0]), int(frame_size[1]))
        self._roi_scaled_for: Optional[Tuple[int, int]] = None

        # 时序组件
        self.fall_filter = FallFilter(self.cfg.fall)
        self.door_filter = DoorFilter(self.cfg.door)
        self.counter = MedianCounter(3)
        self.crowd_smoother = EmaSmoother(0.35)

        self.last_result: Optional[FrameResult] = None
        self.frames_seen = 0

    # -- ROI 自适应 --------------------------------------------------------
    def _sync_roi(self, width: int, height: int) -> None:
        if self._roi_scaled_for == (width, height):
            return
        self.roi = self._base_roi.scaled(width, height)
        self._roi_scaled_for = (width, height)
        self.frame_size = (width, height)

    def reset(self) -> None:
        self.fall_filter.reset()
        self.door_filter.reset()
        self.counter.reset()
        self.crowd_smoother.reset()
        self.frames_seen = 0
        self.last_result = None

    # -- 主流程 ------------------------------------------------------------
    def process(self, frame: Any) -> VisionOut:
        """``frame`` 是 ``sources.Frame``（有 image/persons/ts/index）。"""
        width, height = self.frame_size
        if getattr(frame, "image", None) is not None:
            height, width = frame.image.shape[:2]
        self._sync_roi(int(width), int(height))
        self.frames_seen += 1

        persons: List[Person] = list(getattr(frame, "persons", []) or [])
        result = self._evaluate(frame.ts, persons, width, height)
        self.last_result = result
        return result.vision

    # -- 单帧评估 ----------------------------------------------------------
    def _evaluate(self, ts: int, persons: Sequence[Person], width: int, height: int) -> FrameResult:
        cfg = self.cfg
        notes: List[str] = []

        # ---- 人数 --------------------------------------------------------
        raw_count, count_note = self._raw_person_count(persons)
        person_count = self.counter.push(raw_count)
        if count_note:
            notes.append(count_note)

        # ---- 关键点质量 --------------------------------------------------
        usable = self._usable_persons(persons)
        keypoints_ok = self._keypoints_ok(persons, usable)
        if not keypoints_ok:
            notes.append("keypoints_unreliable")

        # ---- 倒地 --------------------------------------------------------
        fall_state = {}
        fall_prob = 0.0
        if persons:
            features = [fall_features(person, cfg.fall) for person in persons]
            suspect = any(f.suspect for f in features)
            evaluable = any(f.evaluable for f in features)
            lead = max(
                (f for f in features if f.evaluable),
                key=lambda f: (f.angle_deg or 0.0) + (25.0 if f.head_below_hip else 0.0),
                default=None,
            )
            merged = lead if lead is not None else FallFeatures()
            if lead is not None:
                merged.suspect = suspect
            fall_prob = self.fall_filter.update(ts, merged)
            fall_state = self.fall_filter.state.as_dict()
            fall_state["crouch_like"] = any(f.crouch_like for f in features)
        else:
            fall_prob = self.fall_filter.update(ts, FallFeatures())

        # ---- 贴门 --------------------------------------------------------
        door_raw = 0.0
        if persons:
            for person in persons:
                score, _hits = door_features(
                    person,
                    self.roi,
                    cfg_wrist_conf=cfg.door.wrist_conf,
                    near_ratio=cfg.door.near_ratio,
                )
                door_raw = max(door_raw, score)
        door_approach = self.door_filter.update(ts, door_raw)

        # ---- 拥挤度 ------------------------------------------------------
        frame_area = float(max(1, width * height))
        area_ratio = float(sum(p.area for p in persons) / frame_area) if persons else 0.0
        crowded = self.crowd_smoother.push(min(1.0, area_ratio / max(1e-3, cfg.crowd.saturation)))

        vision = VisionOut(
            ts=int(ts),
            person_count=int(person_count),
            fall_prob=float(fall_prob),
            door_approach=float(door_approach),
            crowded=float(crowded),
            keypoints_ok=bool(keypoints_ok),
        )
        return FrameResult(
            vision=vision,
            persons=len(persons),
            fall_state=fall_state,
            door_raw=float(door_raw),
            door_ratio=float(self.door_filter.last_ratio),
            area_ratio=area_ratio,
            usable_persons=len(usable),
            notes=notes,
        )

    # -- 子规则 ------------------------------------------------------------
    @staticmethod
    def _raw_person_count(persons: Sequence[Person]) -> Tuple[int, str]:
        """头部聚类 vs 肩膀聚类，取较大值；都没有就退回检测数。"""
        if not persons:
            return 0, ""
        thr = 0.30
        heads = count_heads(persons, thr)
        shoulders = count_shoulders(persons, thr)
        fallback = len(persons)
        count = max(heads, shoulders)
        note = ""
        if count == 0:
            count = fallback
            note = "no_head_or_shoulder: 回退到检测框计数"
        elif count > fallback:
            note = f"heads={heads}, shoulders={shoulders} -> {count}（bbox 合并，关键点救回）"
        return min(8, int(count)), note

    def _usable_persons(self, persons: Sequence[Person]) -> List[Person]:
        q = self.cfg.quality
        usable = []
        for person in persons:
            mask = visible_mask(person, q.kpt_conf)
            if int(mask.sum()) >= q.min_visible_kpts and mean_conf(person) >= q.min_mean_conf:
                usable.append(person)
        return usable

    @staticmethod
    def _keypoints_ok(persons: Sequence[Person], usable: Sequence[Person]) -> bool:
        """没人时算"可信"（人数 0 本身是可靠结论）；有人时要求一半以上的人关键点可用。"""
        if not persons:
            return True
        needed = max(1, math.ceil(0.5 * len(persons)))
        return len(usable) >= needed

    # -- 便于文档/调试 -----------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """当前实现用到的全部参数，写进 DATA_DICT 与交接说明。"""
        cfg = self.cfg
        return {
            "person_count": {
                "rule": "max(头部点聚类数, 肩膀点聚类数)，0 则回退检测框数，封顶 8",
                "kpt_conf": 0.30,
                "smooth": "3 帧中值滤波",
            },
            "fall_prob": {
                "angle_deg": cfg.fall.angle_deg,
                "window_sec": cfg.fall.window_sec,
                "vote_ratio": cfg.fall.vote_ratio,
                "hold_ratio": cfg.fall.hold_ratio,
                "high_prob": cfg.fall.high_prob,
                "hip_drop_ratio": cfg.fall.hip_drop_ratio,
                "head_below_hip": cfg.fall.head_below_hip,
                "kpt_conf": cfg.fall.kpt_conf,
                "min_visible_kpts": cfg.fall.min_visible_kpts,
            },
            "door_approach": {
                "roi": self.roi.to_dict(),
                "near_ratio": cfg.door.near_ratio,
                "persist_sec": cfg.door.persist_sec,
                "decay_sec": cfg.door.decay_sec,
                "wrist_conf": cfg.door.wrist_conf,
            },
            "crowded": {"saturation": cfg.crowd.saturation, "smooth": "EMA alpha=0.35"},
            "keypoints_ok": {
                "min_visible_kpts": cfg.quality.min_visible_kpts,
                "min_mean_conf": cfg.quality.min_mean_conf,
                "rule": "可用人数 >= ceil(0.5 * 检出人数)",
            },
        }