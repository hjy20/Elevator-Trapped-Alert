"""IMU 事件检测：急停 / 振动 / 静止三分数。

10 秒窗口只做事件检测，困人由状态机 + 长时间累积判定（REQ-FUNC-007）。
本模块禁止导入 state_machine / fuse / reason / eval / vision。
"""

import argparse
import json
import logging
import pathlib
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import schema
from common.schema import IMUEvent
from model.config import (
    ConfigError,
    PRESET_PRODUCTION,
    RuntimeConfig,
    build_runtime_config,
    resolve_start_ts_ms,
)

logger = logging.getLogger(__name__)

WINDOW_MS = int(schema.IMU_WINDOW_SEC * 1000)
SAMPLE_DT_SEC = 1.0 / schema.IMU_HZ


def _clip01(value: float) -> float:
    return float(min(1.0, max(0.0, value)))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + np.exp(-z))
    exp_z = float(np.exp(z))
    return exp_z / (1.0 + exp_z)


def _estop_score(win: np.ndarray, cfg) -> float:
    magnitude = np.sqrt((win.astype(np.float64) ** 2).sum(axis=1))
    if magnitude.size < 2:
        return 0.0
    rate = np.abs(np.diff(magnitude)) / SAMPLE_DT_SEC
    peak = float(np.max(rate))
    typical = float(np.percentile(rate, 90))
    spike = peak - typical
    # suppress: 轻微晃动（持续振荡的 P90 与峰值接近，突增量小）-> REQ-FUNC-048
    return _clip01(_sigmoid(cfg.estop_rate_gain * (spike - cfg.estop_rate_center)))


def _vibration_score(win: np.ndarray, cfg) -> float:
    steps = win.shape[0]
    freq_step = schema.IMU_HZ / float(steps)
    low_bin = max(1, int(round(cfg.vib_band_low_hz / freq_step)))
    high_bin = min(steps // 2, int(round(cfg.vib_band_high_hz / freq_step)))
    band_bins = high_bin - low_bin + 1
    best = 0.0
    for channel in range(win.shape[1]):
        signal = win[:, channel].astype(np.float64)
        signal = signal - signal.mean()
        power = np.abs(np.fft.rfft(signal)) ** 2
        tail = power[1:]
        total = float(tail.sum())
        if total <= 0.0:
            continue
        band = float(power[low_bin : high_bin + 1].sum())
        ratio = band / total
        # 白噪声在 2–8Hz 的期望占比 = 频带 bin 数 / 总 bin 数，先扣掉该基线再看超额
        baseline = band_bins / max(float(tail.size), 1.0)
        excess = (ratio - baseline) / max(1.0 - baseline, 1e-9)
        best = max(best, excess)
    span = max(cfg.vib_ratio_high - cfg.vib_ratio_low, 1e-9)
    return _clip01((best - cfg.vib_ratio_low) / span)


def _idle_score(win: np.ndarray, cfg) -> float:
    var = float(win.astype(np.float64).var(axis=0).mean())
    return _clip01(_sigmoid(-cfg.idle_var_gain * (var - cfg.idle_var_center)))


def score_window(win: np.ndarray, cfg) -> tuple:
    return (
        _estop_score(win, cfg),
        _vibration_score(win, cfg),
        _idle_score(win, cfg),
    )


def _load_npy(npy_path: str) -> np.ndarray:
    name = Path(npy_path).name
    try:
        arr = np.load(npy_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{name}: cannot load npy ({exc})")

    expected = (schema.IMU_STEPS, schema.IMU_CHANNELS)
    if arr.ndim not in (2, 3):
        raise ValueError(f"{name}: expected 2-D or 3-D array, got ndim={arr.ndim} shape={arr.shape}")
    if arr.ndim == 3 and arr.shape[1:] != expected:
        raise ValueError(f"{name}: expected (N,{expected[0]},{expected[1]}), got {arr.shape}")
    if arr.ndim == 2 and arr.shape[1] != schema.IMU_CHANNELS:
        raise ValueError(
            f"{name}: expected (T,{schema.IMU_CHANNELS}) steps, got shape={arr.shape}"
        )
    if arr.dtype != np.float32:
        raise ValueError(f"{name}: expected float32, got {arr.dtype}")
    if not np.isfinite(arr).all():
        raise ValueError(f"{name}: contains NaN or Inf")
    return arr


def _prepare_windows(arr: np.ndarray) -> tuple:
    """返回 (windows, tail_steps)。

    - `(N,200,3)`：契约形状，N 个窗口，无 tail；
    - `(T,3)`：扁平步数，按 200 步整窗切分，余数记 tail。
    """
    if arr.ndim == 3:
        return arr, 0
    total_steps = arr.shape[0]
    n_win = total_steps // schema.IMU_STEPS
    tail = total_steps - n_win * schema.IMU_STEPS
    windows = arr[: n_win * schema.IMU_STEPS].reshape(
        n_win, schema.IMU_STEPS, schema.IMU_CHANNELS
    )
    return windows, tail


def detect_imu_with_config(npy_path: str, cfg: RuntimeConfig) -> List[IMUEvent]:
    arr = _load_npy(npy_path)
    windows, tail = _prepare_windows(arr)
    n_win = windows.shape[0]

    if n_win == 0:
        logger.warning(
            "insufficient window: N=%d < %d", arr.shape[0], schema.IMU_STEPS
        )
        return []
    if tail > 0:
        logger.warning("tail_dropped: %d steps", tail)

    t0_ms, source = resolve_start_ts_ms(str(npy_path), cfg)
    logger.info("ts_base_source=%s t0_ms=%d windows=%d", source, t0_ms, n_win)

    events: List[IMUEvent] = []
    for index in range(n_win):
        estop, vibration, idle = score_window(windows[index], cfg.imu)
        events.append(
            IMUEvent(
                ts=int(t0_ms + (index + 1) * WINDOW_MS),
                estop=round(estop, 6),
                vibration=round(vibration, 6),
                idle=round(idle, 6),
                window_sec=schema.IMU_WINDOW_SEC,
            )
        )
    return events


def detect_imu(npy_path: str) -> List[IMUEvent]:
    return detect_imu_with_config(npy_path, PRESET_PRODUCTION)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imu_detect",
        description="IMU 事件检测：每个 10 秒窗口输出 estop / vibration / idle。",
    )
    parser.add_argument("--npy", required=True, help="输入 .npy，形状 (N,200,3) float32")
    parser.add_argument("--start-ts-ms", type=int, default=None, help="段起点毫秒时间戳")
    parser.add_argument("--out", default=None, help="输出 JSON 路径")
    parser.add_argument("--verbose", action="store_true", help="打印摘要表")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    config_argv: List[str] = []
    if args.start_ts_ms is not None:
        config_argv += ["--start-ts-ms", str(args.start_ts_ms)]

    try:
        cfg = build_runtime_config(config_argv)
        events = detect_imu_with_config(args.npy, cfg)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2

    payload = [
        {
            "ts": e.ts,
            "estop": e.estop,
            "vibration": e.vibration,
            "idle": e.idle,
            "window_sec": e.window_sec,
        }
        for e in events
    ]

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.verbose:
        print(f"{'ts':>14} {'estop':>7} {'vib':>7} {'idle':>7}")
        for event in events:
            print(
                f"{event.ts:>14} {event.estop:>7.3f} "
                f"{event.vibration:>7.3f} {event.idle:>7.3f}"
            )
        print(f"windows={len(events)}")

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())