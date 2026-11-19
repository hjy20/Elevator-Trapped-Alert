"""时序滤波：把逐帧的抖动变成可以告警的证据。

为什么必须有这一层（技术文档要用）：
YOLO-Pose 在轿厢里会遇到逆光、鱼眼畸变、局部遮挡，腕/踝关键点会**逐帧乱跳**，
单个帧的"倒地"或"贴门"结论几乎没有可信度。工程上的解法不是换模型，
而是**让结论必须在一段时间里持续成立**：

* 倒地：3 秒滑动窗口内 >= 70% 的"可评估帧"都疑似倒地，才给 0.9 的置信度；
        解除用更低的阈值（0.30）做迟滞，避免在阈值附近来回跳。
* 贴门：手要在门缝附近**持续 >= 1 秒**才计高分；手一离开按 1.5 秒衰减。
* 计数：3 帧中值滤波，消掉单帧的漏检/重检。

这一层是"用时间换准确率"，代价是几百毫秒的延迟——对困人这种分钟级事件完全可接受。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from .config import DoorConfig, FallConfig
from .geometry import FallFeatures

__all__ = ["SlidingWindow", "FallFilter", "DoorFilter", "MedianCounter", "EmaSmoother"]


# ---------------------------------------------------------------------------
# 通用滑动窗口
# ---------------------------------------------------------------------------


class SlidingWindow:
    """按时间戳裁剪的滑动窗口，元素是 (ts_ms, value)。"""

    def __init__(self, window_sec: float) -> None:
        self.window_ms = int(round(window_sec * 1000))
        self._items: Deque[Tuple[int, float]] = deque()

    def push(self, ts: int, value: float) -> None:
        self._items.append((int(ts), float(value)))
        self._prune(int(ts))

    def _prune(self, now: int) -> None:
        cutoff = now - self.window_ms
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()

    def values(self, now: Optional[int] = None) -> List[float]:
        if now is not None:
            self._prune(int(now))
        return [v for _, v in self._items]

    def __len__(self) -> int:
        return len(self._items)

    @property
    def span_sec(self) -> float:
        if len(self._items) < 2:
            return 0.0
        return (self._items[-1][0] - self._items[0][0]) / 1000.0

    def clear(self) -> None:
        self._items.clear()


# ---------------------------------------------------------------------------
# 倒地：3 秒投票 + 迟滞
# ---------------------------------------------------------------------------


@dataclass
class FallState:
    prob: float = 0.0
    ratio: float = 0.0
    evaluable: int = 0
    latched: bool = False
    suspect_frame: bool = False
    crouch_like: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prob": round(self.prob, 3),
            "ratio": round(self.ratio, 3),
            "evaluable": self.evaluable,
            "latched": self.latched,
            "suspect_frame": self.suspect_frame,
            "crouch_like": self.crouch_like,
        }


class FallFilter:
    """把逐帧 fall_features 融成 0-1 的 fall_prob。"""

    MIN_EVALUABLE = 4          # 窗口里至少要有这么多"可评估帧"才敢给结论
    MIN_SPAN_SEC = 0.8         # 且窗口跨度至少 0.8s，避免开头几帧就下结论

    def __init__(self, cfg: FallConfig) -> None:
        self.cfg = cfg
        self._window = SlidingWindow(cfg.window_sec)
        self._evaluable_window = SlidingWindow(cfg.window_sec)
        self._last_evaluable_ts: Optional[int] = None
        self.state = FallState()

    def reset(self) -> None:
        self._window.clear()
        self._evaluable_window.clear()
        self._last_evaluable_ts = None
        self.state = FallState()

    def update(self, ts: int, features: FallFeatures) -> float:
        ts = int(ts)
        self.state.suspect_frame = bool(features.suspect and features.evaluable)
        self.state.crouch_like = bool(features.crouch_like)

        if features.evaluable:
            self._last_evaluable_ts = ts
            self._window.push(ts, 1.0 if features.suspect else 0.0)
            self._evaluable_window.push(ts, 1.0)

        suspects = self._window.values(ts)
        evaluable = len(self._evaluable_window.values(ts))
        self.state.evaluable = evaluable

        if evaluable < self.MIN_EVALUABLE or self._window.span_sec < self.MIN_SPAN_SEC:
            self.state.ratio = 0.0
            self.state.prob = min(self.state.prob, self.cfg.low_scale * 0.5)
            if self.state.latched and evaluable == 0:
                self.state.latched = False
                self.state.prob = 0.0
            return self.state.prob

        ratio = float(sum(suspects) / max(1, len(suspects)))
        self.state.ratio = ratio

        if ratio >= self.cfg.vote_ratio:
            self.state.latched = True
        elif ratio <= self.cfg.hold_ratio:
            self.state.latched = False

        if self.state.latched:
            self.state.prob = self.cfg.high_prob
        else:
            self.state.prob = min(self.cfg.high_prob * 0.6, self.cfg.low_scale * ratio)

        # 关键点长时间丢失：证据过期，概率按秒衰减
        if self._last_evaluable_ts is not None:
            gap_sec = (ts - self._last_evaluable_ts) / 1000.0
            if gap_sec > self.cfg.window_sec:
                decay = max(0.0, 1.0 - (gap_sec - self.cfg.window_sec) / max(1e-3, self.cfg.window_sec))
                self.state.prob *= decay
                if self.state.prob < 0.05:
                    self.state.latched = False
        return self.state.prob


# ---------------------------------------------------------------------------
# 贴门：持续 1 秒才计分
# ---------------------------------------------------------------------------


class DoorFilter:
    """腕部贴门的"持续性"过滤。

    输入逐帧几何分（0-1），输出经过"必须在门缝附近待够 persist_sec"的门控分。
    """

    PRESENCE_SCORE = 0.40      # 单帧到这个分才算"手在门区"

    def __init__(self, cfg: DoorConfig) -> None:
        self.cfg = cfg
        horizon = max(cfg.persist_sec, cfg.decay_sec) * 2.0
        self._window = SlidingWindow(horizon)
        self._last_high: float = 0.0
        self._last_high_ts: Optional[int] = None
        self.last_raw: float = 0.0
        self.last_ratio: float = 0.0

    def reset(self) -> None:
        self._window.clear()
        self._last_high = 0.0
        self._last_high_ts = None
        self.last_raw = 0.0
        self.last_ratio = 0.0

    def update(self, ts: int, raw_score: float) -> float:
        ts = int(ts)
        self.last_raw = float(raw_score)
        self._window.push(ts, raw_score)

        persist_ms = int(self.cfg.persist_sec * 1000)
        recent = [
            (t, v) for t, v in self._window._items if t >= ts - persist_ms  # noqa: SLF001
        ]
        if recent:
            present = sum(1 for _, v in recent if v >= self.PRESENCE_SCORE)
            self.last_ratio = present / len(recent)
        else:
            self.last_ratio = 0.0

        span_ok = self._window.span_sec >= self.cfg.persist_sec * 0.8
        if self.last_ratio >= 0.70 and span_ok and raw_score >= self.PRESENCE_SCORE:
            peak = max(v for _, v in recent)
            self._last_high = peak
            self._last_high_ts = ts
            return float(peak)

        # 未满足持续性：只能给低分，不制造假告警
        if self._last_high_ts is not None:
            elapsed = (ts - self._last_high_ts) / 1000.0
            if elapsed < self.cfg.decay_sec:
                decayed = self._last_high * (1.0 - elapsed / self.cfg.decay_sec)
                return float(max(0.0, decayed))
        return float(min(self.PRESENCE_SCORE - 0.05, raw_score * 0.5))


# ---------------------------------------------------------------------------
# 计数与拥挤度平滑
# ---------------------------------------------------------------------------


class MedianCounter:
    """3 帧中值滤波：单帧漏检不再让 number 跳一下。"""

    def __init__(self, size: int = 3) -> None:
        self.size = max(1, int(size))
        self._items: Deque[int] = deque(maxlen=self.size)

    def push(self, value: int) -> int:
        self._items.append(int(value))
        ordered = sorted(self._items)
        return int(ordered[len(ordered) // 2])

    def reset(self) -> None:
        self._items.clear()


class EmaSmoother:
    """指数滑动平均，用于拥挤度这种连续量。"""

    def __init__(self, alpha: float = 0.35) -> None:
        self.alpha = float(alpha)
        self.value: Optional[float] = None

    def push(self, value: float) -> float:
        if self.value is None:
            self.value = float(value)
        else:
            self.value = self.alpha * float(value) + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None