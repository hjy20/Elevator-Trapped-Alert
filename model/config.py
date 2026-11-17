"""B 侧唯一配置源：生产/演示双预设 + CLI 覆盖 + 校验 + 快照。

依赖图叶子模块：只允许标准库，不导入 `common.schema`，不导入任何 `model/` 内模块。
"""

import argparse
import dataclasses
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_PRODUCTION = "production"
MODE_DEMO = "demo"

DEFAULT_IMU_EPOCH_BASE_MS = 1763300000000
SIDECAR_SUFFIX = "_imu.meta.json"
MILLISECOND_SCALE_MIN = 10 ** 12
DWELL_THRESHOLD_MAX_SEC = 500.0


class ConfigError(ValueError):
    """非法配置。CLI 层捕获后打印字段名并以退出码 2 终止。"""


@dataclass(frozen=True)
class ThresholdConfig:
    dwell_threshold: float = 180.0
    fall_thresh: float = 0.70
    door_thresh: float = 0.60
    estop_thresh: float = 0.80
    idle_thresh: float = 0.80
    move_thresh: float = 0.15
    stop_thresh: float = 0.05
    dwell_accel_factor: float = 2.0
    fall_persist_sec: float = 3.0
    door_open_timeout: float = 120.0


@dataclass(frozen=True)
class ImuConfig:
    start_ts_ms: Optional[int] = None
    imu_epoch_base_ms: int = DEFAULT_IMU_EPOCH_BASE_MS
    estop_rate_center: float = 3.0
    estop_rate_gain: float = 6.0
    estop_win_steps: int = 10
    vib_band_low_hz: float = 2.0
    vib_band_high_hz: float = 8.0
    vib_ratio_low: float = 0.25
    vib_ratio_high: float = 0.60
    idle_var_center: float = 0.020
    idle_var_gain: float = 200.0


@dataclass(frozen=True)
class ReasonConfig:
    crowded_reason_thresh: float = 0.60
    vib_reason_thresh: float = 0.60
    conf_w_dwell: float = 0.40
    conf_w_person: float = 0.25
    conf_w_imu: float = 0.20
    conf_w_fall: float = 0.10
    conf_w_door: float = 0.05


@dataclass(frozen=True)
class AblationSwitch:
    no_fall: bool = False
    no_imu: bool = False
    no_temporal: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    imu: ImuConfig = field(default_factory=ImuConfig)
    reason: ReasonConfig = field(default_factory=ReasonConfig)
    ablation: AblationSwitch = field(default_factory=AblationSwitch)
    device_id: str = "ELEV-DEMO-001"
    location: str = "3F"
    mode: str = MODE_PRODUCTION
    demo_threshold: bool = False
    seed: int = 0
    align_tolerance_ms: int = 500
    align_hold_ms: int = 10000


PRESET_PRODUCTION = RuntimeConfig()

PRESET_DEMO = RuntimeConfig(
    thresholds=ThresholdConfig(dwell_threshold=8.0),
    mode=MODE_DEMO,
    demo_threshold=True,
)


_THRESHOLD_FLAGS = (
    ("--dwell-threshold", "dwell_threshold"),
    ("--fall-thresh", "fall_thresh"),
    ("--door-thresh", "door_thresh"),
    ("--estop-thresh", "estop_thresh"),
    ("--idle-thresh", "idle_thresh"),
    ("--accel-factor", "dwell_accel_factor"),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="b-config", add_help=False, allow_abbrev=False)
    parser.add_argument("--demo", action="store_true")
    for flag, dest in _THRESHOLD_FLAGS:
        parser.add_argument(flag, dest=dest, type=float)
    parser.add_argument("--device-id", dest="device_id", type=str)
    parser.add_argument("--location", dest="location", type=str)
    parser.add_argument("--start-ts-ms", dest="start_ts_ms", type=int)
    parser.add_argument("--seed", dest="seed", type=int)
    return parser


def build_runtime_config(argv: Optional[List[str]] = None) -> RuntimeConfig:
    """按 `PRESET_*` -> CLI 显式参数 -> 校验 -> 冻结 的顺序构造配置。"""
    ns, _unknown = _build_parser().parse_known_args(argv)
    base = PRESET_DEMO if ns.demo else PRESET_PRODUCTION

    thresholds = base.thresholds
    overrides: Dict[str, Any] = {}
    for _flag, dest in _THRESHOLD_FLAGS:
        value = getattr(ns, dest)
        if value is not None:
            overrides[dest] = value
    if overrides:
        thresholds = dataclasses.replace(thresholds, **overrides)

    imu = base.imu
    if ns.start_ts_ms is not None:
        imu = dataclasses.replace(imu, start_ts_ms=ns.start_ts_ms)

    cfg = dataclasses.replace(
        base,
        thresholds=thresholds,
        imu=imu,
        device_id=base.device_id if ns.device_id is None else ns.device_id,
        location=base.location if ns.location is None else ns.location,
        seed=base.seed if ns.seed is None else ns.seed,
    )

    validate_config(cfg)
    return cfg


def validate_config(cfg: RuntimeConfig) -> None:
    """8 类非法配置一律拒绝，错误文本必须含字段名。"""
    th = cfg.thresholds

    if th.dwell_threshold <= 0:
        raise ConfigError(f"dwell_threshold={th.dwell_threshold} must be > 0")
    if th.dwell_threshold > DWELL_THRESHOLD_MAX_SEC:
        raise ConfigError(
            f"dwell_threshold={th.dwell_threshold} is implausible "
            f"(> {DWELL_THRESHOLD_MAX_SEC:g}s)"
        )
    for name in ("fall_thresh", "door_thresh", "estop_thresh", "idle_thresh"):
        value = getattr(th, name)
        if not (0.0 <= value <= 1.0):
            raise ConfigError(f"{name}={value} not in [0,1]")
    if th.dwell_accel_factor < 1.0:
        raise ConfigError(
            f"dwell_accel_factor={th.dwell_accel_factor} must be >= 1.0"
        )
    if th.move_thresh <= th.stop_thresh:
        raise ConfigError(
            f"move_thresh({th.move_thresh}) must be > stop_thresh({th.stop_thresh})"
        )
    if not cfg.device_id or not cfg.device_id.strip():
        raise ConfigError("device_id must be non-empty")

    for name in ("align_tolerance_ms", "align_hold_ms"):
        if getattr(cfg, name) < 0:
            raise ConfigError(f"{name}={getattr(cfg, name)} must be >= 0")
    if cfg.imu.imu_epoch_base_ms < MILLISECOND_SCALE_MIN:
        raise ConfigError(
            f"imu_epoch_base_ms={cfg.imu.imu_epoch_base_ms} must be a millisecond "
            f"epoch (>= {MILLISECOND_SCALE_MIN})"
        )


def config_snapshot(cfg: RuntimeConfig) -> Dict[str, Any]:
    """完整配置留痕。不得包含任何 `Decision` 字段。"""
    return {
        "mode": cfg.mode,
        "demo_threshold": bool(cfg.demo_threshold),
        "device_id": cfg.device_id,
        "location": cfg.location,
        "seed": cfg.seed,
        "align_tolerance_ms": cfg.align_tolerance_ms,
        "align_hold_ms": cfg.align_hold_ms,
        "thresholds": dataclasses.asdict(cfg.thresholds),
        "imu": dataclasses.asdict(cfg.imu),
        "reason": dataclasses.asdict(cfg.reason),
        "ablation": dataclasses.asdict(cfg.ablation),
    }


def sidecar_path(npy_path: str) -> Path:
    """`<case>_imu.npy` -> `<case>_imu.meta.json`。"""
    path = Path(npy_path)
    name = path.name
    if name.endswith("_imu.npy"):
        name = name[: -len(".npy")] + ".meta.json"
    else:
        name = path.stem + ".meta.json"
    return path.with_name(name)


def _read_sidecar(sidecar: Path) -> Tuple[Optional[int], Optional[str]]:
    if not sidecar.exists():
        return None, "missing"

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, "json_decode"
    except Exception:
        return None, "unreadable"

    if not isinstance(payload, dict) or "start_ts_ms" not in payload:
        return None, "missing_field"

    value = payload["start_ts_ms"]
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "not_int"
    if value < MILLISECOND_SCALE_MIN:
        return None, "not_millisecond_scale"
    return value, None


def resolve_start_ts_ms(npy_path: str, cfg: RuntimeConfig) -> Tuple[int, str]:
    """解析 IMU 段起点毫秒时间戳，返回 `(t0_ms, source)`。

    优先级：sidecar -> `cfg.imu.start_ts_ms`（CLI）-> `cfg.imu.imu_epoch_base_ms`。
    本函数不读取 `.npy`。
    """
    sidecar = sidecar_path(npy_path)
    value, reason = _read_sidecar(sidecar)
    if value is not None:
        logger.info("ts_base_source=sidecar path=%s", sidecar.name)
        return value, "sidecar"
    if reason is not None and sidecar.exists():
        logger.warning("sidecar_invalid:%s path=%s", reason, sidecar.name)
    elif reason == "missing":
        logger.info("sidecar_absent path=%s", sidecar.name)

    if cfg.imu.start_ts_ms is not None:
        logger.info("ts_base_source=cli")
        return int(cfg.imu.start_ts_ms), "cli"

    logger.warning("ts_base_fallback base=%d", cfg.imu.imu_epoch_base_ms)
    return int(cfg.imu.imu_epoch_base_ms), "default"