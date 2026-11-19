"""六态困人状态机：IDLE / MOVING / STOPPED / DOOR_OPEN / DOOR_CLOSED / OCCUPIED。

时间语义一律毫秒整数：`raw_dwell_ms = now_ts - stop_enter_ts`（DEC-03）。
"""

import dataclasses
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, Set, Tuple

from common.schema import Decision, IMUEvent, VisionOut
from model.config import PRESET_PRODUCTION, RuntimeConfig
from model.reason import ReasonBuilder, build_context, derive_verdict
from model.signals import FrameEvidence, SignalEstimator

logger = logging.getLogger(__name__)

MAX_SANE_DWELL_MS = 86_400_000


class ElevatorState(Enum):
    IDLE = "IDLE"
    MOVING = "MOVING"
    STOPPED = "STOPPED"
    DOOR_OPEN = "DOOR_OPEN"
    DOOR_CLOSED = "DOOR_CLOSED"
    OCCUPIED = "OCCUPIED"


@dataclass
class RunClock:
    since_ts: Optional[int] = None

    def update(self, cond: bool, now_ts: int) -> int:
        if not cond:
            self.since_ts = None
            return 0
        if self.since_ts is None:
            self.since_ts = int(now_ts)
        return int(now_ts) - self.since_ts

    def reset(self) -> None:
        self.since_ts = None


@dataclass
class MachineContext:
    state: ElevatorState = ElevatorState.IDLE
    last_ts: Optional[int] = None
    stop_enter_ts: Optional[int] = None
    raw_dwell_ms: int = 0
    accel_extra_ms: int = 0
    estop_latch: bool = False
    estop_latch_ts: Optional[int] = None
    door_open_enter_ts: Optional[int] = None
    door_open_hold_ts: Optional[int] = None
    motion_run: RunClock = field(default_factory=RunClock)
    stop_run: RunClock = field(default_factory=RunClock)
    door_closed_run: RunClock = field(default_factory=RunClock)
    empty_run: RunClock = field(default_factory=RunClock)
    fall_run: RunClock = field(default_factory=RunClock)
    move_recover_run: RunClock = field(default_factory=RunClock)
    last_person_count: Optional[int] = None
    alarmed: bool = False
    first_alarm_ts: Optional[int] = None
    long_open: bool = False
    flags: Set[str] = field(default_factory=set)
    dropped_frames: int = 0
    transition_count: Dict[Tuple[ElevatorState, ElevatorState], int] = field(
        default_factory=dict
    )

    @property
    def effective_dwell_ms(self) -> int:
        return int(self.raw_dwell_ms + self.accel_extra_ms)

    @property
    def dwell_sec(self) -> float:
        return self.effective_dwell_ms / 1000.0


@dataclass(frozen=True)
class _Signals:
    move_ms: int
    stop_ms: int
    not_open_ms: int
    empty_ms: int
    fall_ms: int


@dataclass(frozen=True)
class TransitionRule:
    name: str
    from_states: Tuple[ElevatorState, ...]
    to_state: ElevatorState
    match: Callable[[FrameEvidence, _Signals, MachineContext, RuntimeConfig], bool]


S = ElevatorState


def _min_ms(cfg: RuntimeConfig, base_ms: int) -> int:
    """消融 `no_temporal` 时最短持续置 0（单帧即转移），否则用业务阈值。"""
    return 0 if cfg.ablation.no_temporal else base_ms


def _door_open(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.door_open_signal


def _estop_hit(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.imu_estop is not None and ev.imu_estop > cfg.thresholds.estop_thresh


def _motion_start(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.motion_level >= cfg.thresholds.move_thresh and s.move_ms >= _min_ms(cfg, 1000)


def _stop_steady(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.motion_level < cfg.thresholds.stop_thresh and s.stop_ms >= _min_ms(cfg, 1000)


def _idle_stopped_person(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return (
        ev.person_count >= 1
        and ev.motion_level < cfg.thresholds.stop_thresh
        and s.stop_ms >= _min_ms(cfg, 1000)
    )


def _occupied_person(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.person_count >= 1 and not ev.door_open_signal and s.not_open_ms >= _min_ms(cfg, 1000)


def _door_closed_steady(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return not ev.door_open_signal and s.not_open_ms >= _min_ms(cfg, 1000)


def _person_present(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.person_count >= 1


def _empty_steady(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return ev.person_count == 0 and s.empty_ms >= _min_ms(cfg, 3000)


def _always(ev: FrameEvidence, s: _Signals, ctx: MachineContext, cfg: RuntimeConfig) -> bool:
    return True


_RULES: Tuple[TransitionRule, ...] = (
    TransitionRule("moving_door_open", (S.MOVING,), S.DOOR_OPEN, _door_open),
    TransitionRule("stopped_door_open", (S.STOPPED,), S.DOOR_OPEN, _door_open),
    TransitionRule("closed_door_open", (S.DOOR_CLOSED,), S.DOOR_OPEN, _door_open),
    TransitionRule("occupied_door_open", (S.OCCUPIED,), S.DOOR_OPEN, _door_open),
    TransitionRule("idle_estop_stop", (S.IDLE,), S.STOPPED, _estop_hit),
    TransitionRule("moving_estop_stop", (S.MOVING,), S.STOPPED, _estop_hit),
    TransitionRule("idle_motion_start", (S.IDLE,), S.MOVING, _motion_start),
    TransitionRule("idle_stopped_person", (S.IDLE,), S.STOPPED, _idle_stopped_person),
    TransitionRule("moving_stop_steady", (S.MOVING,), S.STOPPED, _stop_steady),
    TransitionRule("stopped_to_closed", (S.STOPPED,), S.DOOR_CLOSED, _occupied_person),
    TransitionRule("stopped_recover", (S.STOPPED,), S.MOVING, _motion_start),
    TransitionRule("door_open_to_closed", (S.DOOR_OPEN,), S.DOOR_CLOSED, _door_closed_steady),
    TransitionRule("closed_to_occupied", (S.DOOR_CLOSED,), S.OCCUPIED, _person_present),
    TransitionRule("closed_empty_idle", (S.DOOR_CLOSED,), S.IDLE, _empty_steady),
    TransitionRule("closed_recover", (S.DOOR_CLOSED,), S.MOVING, _motion_start),
    TransitionRule("occupied_empty_idle", (S.OCCUPIED,), S.IDLE, _empty_steady),
    TransitionRule("occupied_recover", (S.OCCUPIED,), S.MOVING, _motion_start),
    TransitionRule("door_open_long_open", (S.DOOR_OPEN,), S.DOOR_OPEN, _always),
    TransitionRule("idle_hold", (S.IDLE,), S.IDLE, _always),
    TransitionRule("moving_hold", (S.MOVING,), S.MOVING, _always),
    TransitionRule("stopped_hold", (S.STOPPED,), S.STOPPED, _always),
    TransitionRule("occupied_hold", (S.OCCUPIED,), S.OCCUPIED, _always),
)


class StateMachine:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.builder = ReasonBuilder()
        self.reset()

    def reset(self) -> None:
        self.ctx = MachineContext()
        self._last_decision: Optional[Decision] = None

    def step(
        self, vision: VisionOut, imu_event: Optional[IMUEvent] = None
    ) -> Decision:
        estimator = SignalEstimator(self.cfg)
        return self.step_evidence(estimator.update(vision, imu_event), imu_event)

    def step_evidence(
        self, evidence: FrameEvidence, imu_event: Optional[IMUEvent] = None
    ) -> Decision:
        ctx = self.ctx
        cfg = self.cfg
        th = cfg.thresholds
        now = int(evidence.ts)

        if ctx.last_ts is not None and now <= ctx.last_ts:
            ctx.dropped_frames += 1
            # suppress: 时序抖动造成重复告警 -> REQ-FUNC-021
            logger.warning(
                "event=ts_regression prev_ts=%s cur_ts=%s", ctx.last_ts, now
            )
            if self._last_decision is not None:
                return dataclasses.replace(self._last_decision, ts=ctx.last_ts)

        delta_ms = 0 if ctx.last_ts is None else max(0, now - ctx.last_ts)

        if ctx.stop_enter_ts is not None:
            raw = now - ctx.stop_enter_ts
            if raw < 0 or raw > MAX_SANE_DWELL_MS:
                logger.error("event=suspicious_time_unit raw_dwell_ms=%s", raw)
            ctx.raw_dwell_ms = max(0, raw)
        else:
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0

        estop = evidence.imu_estop
        if estop is not None:
            if estop > th.estop_thresh:
                ctx.estop_latch = True
                ctx.estop_latch_ts = now
            else:
                ctx.estop_latch = False
        elif (
            ctx.estop_latch_ts is not None
            and now - ctx.estop_latch_ts > cfg.align_hold_ms
        ):
            ctx.estop_latch = False

        accel_states = (S.STOPPED, S.DOOR_CLOSED, S.OCCUPIED)
        if ctx.estop_latch and delta_ms > 0 and ctx.state in accel_states:
            ctx.accel_extra_ms += int(
                round(delta_ms * (th.dwell_accel_factor - 1.0))
            )

        signals = _Signals(
            move_ms=ctx.motion_run.update(
                evidence.motion_level >= th.move_thresh, now
            ),
            stop_ms=ctx.stop_run.update(evidence.motion_level < th.stop_thresh, now),
            not_open_ms=ctx.door_closed_run.update(not evidence.door_open_signal, now),
            empty_ms=ctx.empty_run.update(evidence.person_count == 0, now),
            fall_ms=ctx.fall_run.update(
                # suppress: 蹲下捡东西（需持续 fall_persist_sec 才成立）-> REQ-FUNC-046
                evidence.keypoints_ok and evidence.fall_prob > th.fall_thresh,
                now,
            ),
        )

        matched = None
        for rule in _RULES:
            if ctx.state in rule.from_states and rule.match(evidence, signals, ctx, cfg):
                matched = rule
                break

        if matched is None:
            logger.warning("undefined_transition state=%s", ctx.state.name)
        else:
            self._apply(matched, now)

        ctx.flags = set(evidence.flags)
        ctx.last_person_count = evidence.person_count

        effective_ms = ctx.effective_dwell_ms
        dwell_sec = effective_ms / 1000.0
        trapped = (
            # suppress: 检修停梯 / 空梯（无人则无论 dwell 多长都不判）-> REQ-FUNC-045
            ctx.state is S.OCCUPIED
            and evidence.person_count >= 1
            and effective_ms > int(round(th.dwell_threshold * 1000.0))
        )

        verdict = derive_verdict(
            trapped=trapped,
            cfg=cfg,
            fall_prob=evidence.fall_prob,
            keypoints_ok=evidence.keypoints_ok,
            fall_persist_ms=signals.fall_ms,
            door_approach=evidence.door_approach,
            imu_estop=evidence.imu_estop,
            imu_idle=evidence.imu_idle,
            imu_present=evidence.imu_present,
            dwell_sec=dwell_sec,
            person_count=evidence.person_count,
            crowded=evidence.crowded,
        )
        reason = self.builder.build(
            verdict, build_context(evidence, cfg, effective_ms, signals.fall_ms)
        )

        decision = Decision(
            ts=now,
            device_id=cfg.device_id,
            trapped=verdict.trapped,
            urgency=verdict.urgency,
            location=cfg.location,
            people_count=evidence.person_count,
            event_type=verdict.event_type,
            confidence=verdict.confidence,
            reason=reason,
        )

        if verdict.trapped and not ctx.alarmed:
            ctx.alarmed = True
            ctx.first_alarm_ts = now

        ctx.last_ts = now
        self._last_decision = decision
        return decision

    def _apply(self, rule: TransitionRule, now: int) -> None:
        ctx = self.ctx
        cfg = self.cfg
        old = ctx.state
        new = rule.to_state
        ctx.state = new
        ctx.transition_count[(old, new)] = ctx.transition_count.get((old, new), 0) + 1

        if new is S.STOPPED:
            # suppress: 正常到站停车（门未开也不判困人，需 OCCUPIED）-> REQ-FUNC-044
            ctx.stop_enter_ts = now
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0
            ctx.alarmed = False
        elif new is S.DOOR_OPEN:
            if old is not S.DOOR_OPEN:
                ctx.door_open_enter_ts = now
                ctx.long_open = False
            elif (
                ctx.door_open_enter_ts is not None
                and now - ctx.door_open_enter_ts
                > int(round(cfg.thresholds.door_open_timeout * 1000.0))
            ):
                # suppress: 门长开被误判为困人（只记 long_open，dwell 恒 0）-> REQ-FUNC-014
                ctx.long_open = True
            ctx.stop_enter_ts = None
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0
            ctx.alarmed = False
        elif new is S.DOOR_CLOSED:
            ctx.stop_enter_ts = now
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0
        elif new is S.OCCUPIED:
            if ctx.stop_enter_ts is None:
                ctx.stop_enter_ts = now
        elif new in (S.IDLE, S.MOVING):
            ctx.stop_enter_ts = None
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0
            ctx.alarmed = False


_DEFAULT_ESTIMATOR: Optional[SignalEstimator] = None
_DEFAULT_MACHINE: Optional[StateMachine] = None


def step(vision: VisionOut, imu_event: Optional[IMUEvent] = None) -> Decision:
    """单帧调试入口。批量必须用 `StateMachine(cfg)` 实例。"""
    global _DEFAULT_ESTIMATOR, _DEFAULT_MACHINE
    if _DEFAULT_MACHINE is None:
        _DEFAULT_MACHINE = StateMachine(PRESET_PRODUCTION)
        _DEFAULT_ESTIMATOR = SignalEstimator(PRESET_PRODUCTION)
    assert _DEFAULT_ESTIMATOR is not None
    evidence = _DEFAULT_ESTIMATOR.update(vision, imu_event)
    return _DEFAULT_MACHINE.step_evidence(evidence, imu_event)