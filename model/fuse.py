"""融合编排：毫秒最近邻对齐 + 逐帧状态机推进 + 后置降级钳制。"""

import dataclasses
import logging
import pathlib
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.schema import Decision, IMUEvent, VisionOut
from model.config import PRESET_PRODUCTION, RuntimeConfig
from model.signals import SignalEstimator
from model.state_machine import StateMachine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlignInfo:
    match: str
    delta_ms: Optional[int]


@dataclass
class FusionHealth:
    nearest: int = 0
    hold: int = 0
    none: int = 0
    warmup: int = 0
    delta_sum: int = 0
    delta_count: int = 0

    @property
    def total(self) -> int:
        return self.nearest + self.hold + self.none + self.warmup

    @property
    def effective_total(self) -> int:
        return self.nearest + self.hold + self.none

    def as_dict(self) -> dict:
        total = self.total or 1
        effective = self.effective_total or 1
        return {
            "nearest": self.nearest,
            "hold": self.hold,
            "none": self.none,
            "warmup": self.warmup,
            "nearest_ratio": round(self.nearest / total, 6),
            "hold_ratio": round(self.hold / total, 6),
            "none_ratio": round(self.none / effective, 6),
            "warmup_ratio": round(self.warmup / total, 6),
            "mean_abs_delta_ms": (
                round(self.delta_sum / self.delta_count, 3)
                if self.delta_count
                else 0.0
            ),
        }


class Aligner:
    def __init__(self, imu: List[IMUEvent], cfg: RuntimeConfig):
        self.events = sorted(imu, key=lambda event: event.ts)
        self.ts = np.array([event.ts for event in self.events], dtype=np.int64)
        self.tolerance = cfg.align_tolerance_ms
        self.hold_ms = cfg.align_hold_ms
        self.first_ts = int(self.ts[0]) if self.ts.size else None

    def nearest(self, frame_ts: int) -> Tuple[Optional[IMUEvent], AlignInfo]:
        if self.ts.size == 0:
            return None, AlignInfo("none", None)

        if (
            self.first_ts is not None
            and int(frame_ts) < self.first_ts - self.tolerance
        ):
            return None, AlignInfo("warmup", None)

        idx = int(np.searchsorted(self.ts, frame_ts, side="left"))
        best_index = None
        best_delta = None
        for candidate in (idx, idx - 1):
            if candidate < 0 or candidate >= self.ts.size:
                continue
            delta = abs(int(self.ts[candidate]) - int(frame_ts))
            if delta > self.tolerance:
                continue
            if (
                best_delta is None
                or delta < best_delta
                or (delta == best_delta and candidate < best_index)
            ):
                best_index = candidate
                best_delta = delta

        if best_index is not None:
            return self.events[best_index], AlignInfo("nearest", int(best_delta))

        prev_index = idx - 1
        if prev_index >= 0:
            hold_delta = int(frame_ts) - int(self.ts[prev_index])
            if hold_delta <= self.hold_ms:
                return self.events[prev_index], AlignInfo("hold", hold_delta)

        return None, AlignInfo("none", None)


class FusionRunner:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.health = FusionHealth()
        self.trace: List[dict] = []

    def run(
        self, vision: List[VisionOut], imu: List[IMUEvent]
    ) -> List[Decision]:
        ordered = sorted(vision, key=lambda frame: frame.ts)
        if not ordered:
            logger.info("empty vision input: no decisions produced")
            return []

        self.trace = []

        ordered_imu = sorted(imu, key=lambda event: event.ts)
        if self.cfg.ablation.no_imu:
            ordered_imu = []
        imu_present = bool(ordered_imu)
        aligner = Aligner(ordered_imu, self.cfg)
        estimator = SignalEstimator(self.cfg)
        machine = StateMachine(self.cfg)

        decisions: List[Decision] = []
        for frame in ordered:
            try:
                event, info = aligner.nearest(frame.ts)
                self._record(info)
                evidence = estimator.update(frame, event)
                decision = machine.step_evidence(evidence, event)
                if not imu_present and decision.urgency > 2:
                    decision = dataclasses.replace(
                        decision, urgency=2, event_type="trapped"
                    )
                decisions.append(decision)
                self.trace.append(
                    {
                        "ts": decision.ts,
                        "state": machine.ctx.state.name,
                        "trapped": decision.trapped,
                        "urgency": decision.urgency,
                        "event_type": decision.event_type,
                        "dwell_ms": machine.ctx.effective_dwell_ms,
                        "raw_dwell_ms": machine.ctx.raw_dwell_ms,
                        "flags": sorted(evidence.flags),
                        "match": info.match,
                        "delta_ms": info.delta_ms,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单帧异常不得中断整段
                logger.error("frame_failed ts=%s error=%s", frame.ts, exc)
                decisions.append(
                    Decision(
                        ts=int(frame.ts),
                        device_id=self.cfg.device_id,
                        trapped=False,
                        urgency=0,
                        location=self.cfg.location,
                        people_count=0,
                        event_type="normal",
                        confidence=0.0,
                        reason=["frame_error"],
                    )
                )

        if not imu_present:
            logger.warning("imu_unavailable: degraded to vision-only")
        else:
            health = self.health.as_dict()
            # 首个 10s 窗口闭合前的帧记为 warmup（物理必然），不计入退化判定
            if health["none_ratio"] > 0.5 and self.health.effective_total >= 20:
                logger.warning(
                    "imu_alignment_degraded none_ratio=%.3f effective=%d warmup=%d",
                    health["none_ratio"],
                    self.health.effective_total,
                    self.health.warmup,
                )
        return decisions

    def _record(self, info: AlignInfo) -> None:
        if info.delta_ms is not None:
            self.health.delta_sum += abs(int(info.delta_ms))
            self.health.delta_count += 1
        if info.match == "nearest":
            self.health.nearest += 1
        elif info.match == "hold":
            self.health.hold += 1
        elif info.match == "warmup":
            self.health.warmup += 1
        else:
            self.health.none += 1


def fuse(vision: List[VisionOut], imu: List[IMUEvent]) -> List[Decision]:
    return FusionRunner(PRESET_PRODUCTION).run(vision, imu)


def fuse_with_config(
    vision: List[VisionOut], imu: List[IMUEvent], cfg: RuntimeConfig
) -> List[Decision]:
    return FusionRunner(cfg).run(vision, imu)


def main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path

    from model.config import build_runtime_config
    from model.imu_detect import detect_imu
    from model.mock_scenario import KINDS, generate_scenario, write_bundle

    parser = argparse.ArgumentParser(prog="fuse", description="融合与对齐健康度报告")
    parser.add_argument("--mock", action="store_true", help="使用合成场景库")
    parser.add_argument("--demo", action="store_true", help="使用演示阈值预设")
    parser.add_argument("--report-health", action="store_true", help="输出对齐健康度表")
    args = parser.parse_args(argv)

    cfg = build_runtime_config(["--demo"] if args.demo else [])
    report = {}
    for kind in KINDS:
        bundle = generate_scenario(kind, cfg)
        paths = write_bundle(bundle)
        imu = detect_imu(paths["imu_npy"])
        runner = FusionRunner(cfg)
        runner.run(bundle.vision, imu)
        report[kind] = {
            "frames": len(bundle.vision),
            "imu_events": len(imu),
            **runner.health.as_dict(),
        }

    results = Path("results")
    results.mkdir(parents=True, exist_ok=True)
    (results / "fusion_health.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.report_health:
        print(f"{'case':>8} {'nearest':>8} {'hold':>6} {'none':>6} {'none_ratio':>11}")
        for kind, row in report.items():
            print(
                f"{kind:>8} {row['nearest']:>8} {row['hold']:>6} "
                f"{row['none']:>6} {row['none_ratio']:>11.3f}"
            )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())