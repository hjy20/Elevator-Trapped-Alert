"""派生信号层：把 `VisionOut` 与（可选的）`IMUEvent` 归一成 `FrameEvidence`。

本层只做派生与封顶，不做状态判定、不做困人判定。
"""

import logging
from dataclasses import dataclass
from typing import FrozenSet, Optional

from common.schema import IMUEvent, PERSON_COUNT_MAX, VisionOut
from model.config import RuntimeConfig

logger = logging.getLogger(__name__)

DOOR_HOLD_MS = 1000
DOOR_DROP_DELTA = 0.25
PROXY_MOTION_HOLD_MS = 2000


@dataclass(frozen=True)
class FrameEvidence:
    ts: int
    person_count: int
    fall_prob: float
    door_approach: float
    crowded: float
    keypoints_ok: bool
    motion_level: float
    door_open_signal: bool
    imu_estop: Optional[float]
    imu_idle: Optional[float]
    imu_vibration: Optional[float]
    imu_present: bool
    flags: FrozenSet[str]


def _clip(value: float, name: str) -> float:
    if value < 0.0 or value > 1.0:
        logger.warning("field_clipped field=%s value=%s", name, value)
        return min(1.0, max(0.0, float(value)))
    return float(value)


class SignalEstimator:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self._last_person_count: Optional[int] = None
        self._last_door_approach: Optional[float] = None
        self._last_motion_level: Optional[float] = None
        self._door_hold_until: Optional[int] = None
        self._proxy_motion_until: Optional[int] = None

    def update(
        self, vision: VisionOut, imu_event: Optional[IMUEvent] = None
    ) -> FrameEvidence:
        flags = set()
        ts = int(vision.ts)

        raw_person = int(vision.person_count)
        # suppress: 人多拥挤导致人数虚高 -> REQ-FUNC-041
        person = max(0, min(raw_person, PERSON_COUNT_MAX))
        if raw_person != person:
            logger.info(
                "raw_person_count=%d capped_person_count=%d", raw_person, person
            )

        fall_prob = _clip(vision.fall_prob, "fall_prob")
        if self.cfg.ablation.no_fall:
            fall_prob = 0.0
            flags.add("ablation_no_fall")
        door_approach = _clip(vision.door_approach, "door_approach")
        crowded = _clip(vision.crowded, "crowded")

        if not vision.keypoints_ok:
            fall_prob = 0.0
            flags.add("keypoints_unreliable")

        if imu_event is None:
            person_delta = (
                0
                if self._last_person_count is None
                else abs(person - self._last_person_count)
            )
            door_delta = (
                0.0
                if self._last_door_approach is None
                else abs(door_approach - self._last_door_approach)
            )
            if person_delta >= 1 or door_delta >= DOOR_DROP_DELTA:
                self._proxy_motion_until = ts + PROXY_MOTION_HOLD_MS
            moving_proxy = (
                self._proxy_motion_until is not None and ts < self._proxy_motion_until
            )
            motion_level = 0.5 if moving_proxy else 0.0
            imu_estop = imu_idle = imu_vibration = None
            imu_present = False
            flags.add("imu_unavailable")
        else:
            motion_level = min(
                1.0,
                max(
                    0.0,
                    (1.0 - float(imu_event.idle)) * (1.0 - float(imu_event.vibration)),
                ),
            )
            imu_estop = float(imu_event.estop)
            imu_idle = float(imu_event.idle)
            imu_vibration = float(imu_event.vibration)
            imu_present = True

        th = self.cfg.thresholds
        hits = 0
        if self._last_person_count is not None and person != self._last_person_count:
            hits += 1
        if (
            self._last_door_approach is not None
            and (self._last_door_approach - door_approach) >= DOOR_DROP_DELTA
        ):
            hits += 1
        if (
            self._last_motion_level is not None
            and self._last_motion_level >= th.move_thresh
            and motion_level < th.stop_thresh
        ):
            hits += 1
        if hits >= 2:
            # suppress: 正常到站门开抖动（单人证据不足，需两证据联合）-> REQ-FUNC-030
            self._door_hold_until = ts + DOOR_HOLD_MS

        door_open_signal = (
            self._door_hold_until is not None and ts < self._door_hold_until
        )
        if door_open_signal:
            flags.add("technical_debt")

        self._last_person_count = person
        self._last_door_approach = door_approach
        self._last_motion_level = motion_level

        return FrameEvidence(
            ts=ts,
            person_count=person,
            fall_prob=fall_prob,
            door_approach=door_approach,
            crowded=crowded,
            keypoints_ok=bool(vision.keypoints_ok),
            motion_level=motion_level,
            door_open_signal=door_open_signal,
            imu_estop=imu_estop,
            imu_idle=imu_idle,
            imu_vibration=imu_vibration,
            imu_present=imu_present,
            flags=frozenset(flags),
        )