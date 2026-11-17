"""判决树、置信度与可解释理由生成。

`reason` 九阶段固定顺序，键集合受 13 键白名单约束（REQ-EXPL-004）。
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from common.schema import EVENT_TYPES, URGENCY_EVENT_MAP
from model.config import RuntimeConfig

logger = logging.getLogger(__name__)

WHITELIST = frozenset(
    {
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
)

_ENTRY_RE = re.compile(r"^([a-z_]+(\.[a-z_]+)?)=(.+)$")
_NUMERIC_ENTRY_RE = re.compile(r"^([a-z_]+(\.[a-z_]+)?)=(.*)$")

BANNED_PHRASES = ("检测到异常", "疑似困人", "请及时处理")


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _fmt_sec(value: float) -> str:
    rounded = round(float(value), 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return f"{rounded:.0f}"
    return f"{rounded:.1f}"


@dataclass(frozen=True)
class Verdict:
    trapped: bool
    urgency: int
    event_type: str
    confidence: float


@dataclass(frozen=True)
class ReasonContext:
    dwell_sec: float
    dwell_threshold: float
    person_count: int
    fall_prob: float
    door_approach: float
    crowded: float
    motion_level: float
    imu_estop: Optional[float]
    imu_idle: Optional[float]
    imu_vibration: Optional[float]
    keypoints_ok: bool
    imu_present: bool
    demo_mode: bool
    door_proxy_used: bool
    fall_persist_ms: int = 0
    idle_thresh: float = 0.80
    estop_thresh: float = 0.80
    door_thresh: float = 0.60
    fall_thresh: float = 0.70
    crowded_thresh: float = 0.60
    vib_thresh: float = 0.60


def assert_pair_legal(urgency: int, event_type: str) -> None:
    allowed = URGENCY_EVENT_MAP.get(int(urgency))
    if allowed is None:
        raise ValueError(f"urgency={urgency} is not in {sorted(URGENCY_EVENT_MAP)}")
    if event_type not in allowed:
        raise ValueError(
            f"illegal pair: urgency={urgency} + event_type={event_type!r}, "
            f"allowed={allowed}"
        )


def _confidence(
    event_type: str,
    cfg: RuntimeConfig,
    dwell_sec: float,
    person_count: int,
    crowded: float,
    fall_prob: float,
    keypoints_ok: bool,
    door_approach: float,
    imu_estop: Optional[float],
    imu_idle: Optional[float],
    imu_present: bool,
) -> float:
    rc = cfg.reason
    th = cfg.thresholds
    s_dwell = _clip(dwell_sec / max(th.dwell_threshold, 1e-6) - 1.0)
    # suppress: 人多拥挤（只降置信度，不改变 trapped 判定）-> REQ-FUNC-047
    s_person = (1.0 if person_count >= 1 else 0.0) * (1.0 - 0.5 * crowded)
    s_imu = max(imu_estop or 0.0, imu_idle or 0.0)
    s_fall = fall_prob if keypoints_ok else 0.0
    s_door = door_approach

    if event_type == "estop":
        return round(_clip(0.6 * (imu_estop or 0.0) + 0.4 * (1.0 - s_dwell)), 6)
    if event_type == "normal":
        return round(_clip(1.0 - max(s_dwell, imu_estop or 0.0), 0.05, 1.0), 6)

    channels = [
        (rc.conf_w_dwell, s_dwell),
        (rc.conf_w_person, s_person),
        (rc.conf_w_fall, s_fall),
        (rc.conf_w_door, s_door),
    ]
    if imu_present:
        channels.append((rc.conf_w_imu, s_imu))

    total = sum(weight for weight, _strength in channels)
    if total <= 0:
        return 0.0
    return round(_clip(sum(w * s for w, s in channels) / total), 6)


def derive_verdict(
    trapped: bool,
    cfg: RuntimeConfig,
    fall_prob: float,
    keypoints_ok: bool,
    fall_persist_ms: int,
    door_approach: float,
    imu_estop: Optional[float],
    imu_idle: Optional[float] = None,
    imu_present: bool = True,
    dwell_sec: float = 0.0,
    person_count: int = 0,
    crowded: float = 0.0,
) -> Verdict:
    th = cfg.thresholds
    fall_persist_ms_needed = (
        0
        if cfg.ablation.no_temporal
        else int(round(th.fall_persist_sec * 1000))
    )

    if (
        trapped
        and keypoints_ok
        and imu_present
        and fall_persist_ms >= fall_persist_ms_needed
        and fall_prob > th.fall_thresh
    ):
        urgency, event_type = 3, "faint"
    elif trapped and door_approach > th.door_thresh:
        urgency, event_type = 2, "pry"
    elif trapped:
        urgency, event_type = 2, "trapped"
    elif imu_estop is not None and imu_estop > th.estop_thresh:
        urgency, event_type = 1, "estop"
    else:
        urgency, event_type = 0, "normal"

    assert_pair_legal(urgency, event_type)
    confidence = _confidence(
        event_type,
        cfg,
        dwell_sec,
        person_count,
        crowded,
        fall_prob,
        keypoints_ok,
        door_approach,
        imu_estop,
        imu_idle,
        imu_present,
    )
    return Verdict(
        trapped=bool(trapped),
        urgency=urgency,
        event_type=event_type,
        confidence=confidence,
    )


class ReasonBuilder:
    def build(self, verdict: Verdict, ctx: ReasonContext) -> List[str]:
        reason: List[str] = []
        triggered = ">" if ctx.dwell_sec > ctx.dwell_threshold else "<="
        reason.append(
            f"dwell={_fmt_sec(ctx.dwell_sec)}s {triggered} {_fmt_sec(ctx.dwell_threshold)}s"
        )

        if verdict.event_type == "normal":
            if not ctx.keypoints_ok:
                reason.append("keypoints_unreliable=true")
            elif not ctx.imu_present:
                reason.append("imu_unavailable=true")
            elif ctx.demo_mode:
                reason.append("demo_threshold=true")
            elif ctx.person_count >= 1:
                reason.append(f"person_count={ctx.person_count} >= 1")
            self._self_check(verdict, reason)
            return reason

        reason.append(f"person_count={ctx.person_count} >= 1")

        if not ctx.keypoints_ok:
            reason.append("keypoints_unreliable=true")
        elif verdict.event_type == "faint":
            reason.append(f"fall_prob={ctx.fall_prob:.2f} > {ctx.fall_thresh:.2f}")

        if ctx.door_approach > ctx.door_thresh:
            reason.append(
                f"door_approach={ctx.door_approach:.2f} > {ctx.door_thresh:.2f}"
            )

        if ctx.imu_present:
            if ctx.imu_estop is not None and ctx.imu_estop > ctx.estop_thresh:
                reason.append(f"imu.estop={ctx.imu_estop:.2f} > {ctx.estop_thresh:.2f}")
            if verdict.urgency >= 2 and ctx.imu_idle is not None and ctx.imu_idle > ctx.idle_thresh:
                reason.append(f"imu.idle={ctx.imu_idle:.2f} > {ctx.idle_thresh:.2f}")
            if ctx.imu_vibration is not None and ctx.imu_vibration >= ctx.vib_thresh:
                reason.append(f"imu.vibration={ctx.imu_vibration:.2f}")

        if ctx.crowded >= ctx.crowded_thresh:
            reason.append(f"crowded={ctx.crowded:.2f}")

        if not ctx.imu_present:
            reason.append("imu_unavailable=true")

        if ctx.door_proxy_used and verdict.urgency >= 2:
            reason.append("technical_debt=door_signal_proxy")

        if ctx.demo_mode:
            reason.append("demo_threshold=true")

        self._self_check(verdict, reason)
        return reason

    def _self_check(self, verdict: Verdict, reason: List[str]) -> None:
        if not reason:
            raise AssertionError(f"C1 violated: empty reason for {verdict}")
        if verdict.event_type == "normal" and len(reason) > 2:
            raise AssertionError(f"C2 violated: normal reason too long {reason}")
        for item in reason:
            match = _ENTRY_RE.match(item)
            if match is None:
                raise AssertionError(f"C3 violated: {item!r} in {reason}")
            if match.group(1) not in WHITELIST:
                raise AssertionError(f"C3 violated: unknown key in {item!r}")
            for phrase in BANNED_PHRASES:
                if phrase in item:
                    raise AssertionError(f"REQ-EXPL-007 violated: {item!r}")
            if re.search(r"\d{10,}", item):
                raise AssertionError(f"millisecond leak in {item!r}")
        assert_pair_legal(verdict.urgency, verdict.event_type)


def render_reason(verdict: Verdict, ctx: ReasonContext) -> List[str]:
    return ReasonBuilder().build(verdict, ctx)


def build_context(evidence, cfg: RuntimeConfig, effective_dwell_ms: int, fall_persist_ms: int) -> ReasonContext:
    th = cfg.thresholds
    return ReasonContext(
        dwell_sec=effective_dwell_ms / 1000.0,
        dwell_threshold=th.dwell_threshold,
        person_count=evidence.person_count,
        fall_prob=evidence.fall_prob,
        door_approach=evidence.door_approach,
        crowded=evidence.crowded,
        motion_level=evidence.motion_level,
        imu_estop=evidence.imu_estop,
        imu_idle=evidence.imu_idle,
        imu_vibration=evidence.imu_vibration,
        keypoints_ok=evidence.keypoints_ok,
        imu_present=evidence.imu_present,
        demo_mode=bool(cfg.demo_threshold),
        door_proxy_used=bool(evidence.door_open_signal),
        fall_persist_ms=fall_persist_ms,
        idle_thresh=th.idle_thresh,
        estop_thresh=th.estop_thresh,
        door_thresh=th.door_thresh,
        fall_thresh=th.fall_thresh,
        crowded_thresh=cfg.reason.crowded_reason_thresh,
        vib_thresh=cfg.reason.vib_reason_thresh,
    )