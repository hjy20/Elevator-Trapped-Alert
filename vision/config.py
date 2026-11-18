"""视觉模块的统一配置与路径解析。

三条硬规矩（来自任务卡）：
1. 任何路径都用 ``pathlib`` 拼，根目录由 ``ELEVATOR_RESCUE_HOME`` 环境变量决定，
   默认取本文件所在仓库的根 —— **代码里不出现任何绝对路径**。
2. 所有阈值集中在 ``vision/configs/default.json``，改参数不用改代码。
3. 门框 ROI 是"每台电梯一次"的人工标定结果，存在 ``vision/configs/rois/*.json``。

配置优先级：内置默认值 < 配置文件 < 命令行参数。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "PROJECT_ROOT",
    "DATA_DIR",
    "RAW_DIR",
    "PROCESSED_DIR",
    "LABEL_DIR",
    "DEMO_DIR",
    "RESULTS_DIR",
    "CONFIG_DIR",
    "ROI_DIR",
    "MODEL_DIR",
    "project_root",
    "resolve_path",
    "DoorRoi",
    "ModelConfig",
    "RuntimeConfig",
    "FallConfig",
    "DoorConfig",
    "CrowdConfig",
    "QualityConfig",
    "VisionConfig",
    "DEFAULT_CONFIG",
    "load_config",
    "list_rois",
]

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """仓库根目录；优先环境变量，其次按本文件位置上溯。"""
    env = os.environ.get("ELEVATOR_RESCUE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT: Path = project_root()
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
LABEL_DIR: Path = DATA_DIR / "labels"
DEMO_DIR: Path = PROJECT_ROOT / "common" / "demo_data"
RESULTS_DIR: Path = PROJECT_ROOT / "results"
CONFIG_DIR: Path = PROJECT_ROOT / "vision" / "configs"
ROI_DIR: Path = CONFIG_DIR / "rois"
MODEL_DIR: Path = PROJECT_ROOT / "models"


def resolve_path(path: "str | Path", *, create_parent: bool = False) -> Path:
    """把相对路径解释成相对仓库根，绝对路径原样返回。"""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()
    if create_parent:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


# ---------------------------------------------------------------------------
# 门框 ROI
# ---------------------------------------------------------------------------


@dataclass
class DoorRoi:
    """门区域的人工标定结果（像素坐标，基于 frame_size 的原始分辨率）。

    roi     : (x, y, w, h) 门板/门框矩形
    seam_x  : 门缝中线的 x 坐标（双开门就是两扇门的接缝）
    frame_size: (w, h) 标定时用的画面尺寸；推理时会自动按比例缩放
    """

    roi: Tuple[int, int, int, int] = (980, 120, 300, 540)
    seam_x: int = 1130
    frame_size: Tuple[int, int] = (1280, 720)
    name: str = "default"

    def __post_init__(self) -> None:
        self.roi = tuple(int(v) for v in self.roi)  # type: ignore[assignment]
        self.frame_size = tuple(int(v) for v in self.frame_size)  # type: ignore[assignment]
        self.seam_x = int(self.seam_x)
        if len(self.roi) != 4 or len(self.frame_size) != 2:
            raise ValueError("DoorRoi.roi 必须是 4 元组，frame_size 必须是 2 元组")

    def scaled(self, width: int, height: int) -> "DoorRoi":
        """按目标画面尺寸等比换算。"""
        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            return self
        sx, sy = width / fw, height / fh
        x, y, w, h = self.roi
        return DoorRoi(
            roi=(int(round(x * sx)), int(round(y * sy)), int(round(w * sx)), int(round(h * sy))),
            seam_x=int(round(self.seam_x * sx)),
            frame_size=(int(width), int(height)),
            name=self.name,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["roi"] = list(self.roi)
        payload["frame_size"] = list(self.frame_size)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DoorRoi":
        return cls(
            roi=tuple(payload.get("roi", (980, 120, 300, 540))),  # type: ignore[arg-type]
            seam_x=payload.get("seam_x", 1130),
            frame_size=tuple(payload.get("frame_size", (1280, 720))),  # type: ignore[arg-type]
            name=str(payload.get("name", "default")),
        )


def roi_path(name: str) -> Path:
    return ROI_DIR / f"{name}.json"


def load_roi(name_or_path: "str | Path | None", default: DoorRoi | None = None) -> DoorRoi:
    """按名字或路径读取 ROI；找不到就退回默认矩形（并允许后续交互标定）。"""
    if not name_or_path:
        return default or DoorRoi()
    candidate = Path(name_or_path)
    if candidate.suffix != ".json":
        candidate = roi_path(str(name_or_path))
    if not candidate.is_absolute():
        candidate = resolve_path(candidate)
    if not candidate.exists():
        return default or DoorRoi()
    with candidate.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if "roi" not in payload and isinstance(payload.get("door_roi"), (list, tuple)):
        payload["roi"] = payload["door_roi"]
    return DoorRoi.from_dict(payload)


def save_roi(roi: DoorRoi, name: Optional[str] = None) -> Path:
    target = roi_path(name or roi.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(roi.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


def list_rois() -> list[str]:
    if not ROI_DIR.exists():
        return []
    return sorted(p.stem for p in ROI_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# 配置分段
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """姿态模型与检测超参。"""

    backend: str = "auto"                      # auto | yolo | onnx | synthetic
    weights: str = "models/yolov8n-pose.pt"
    onnx: str = "models/yolov8n-pose.onnx"
    imgsz: int = 480                           # 480 是 CPU 的目标；640 作基线对比
    conf: float = 0.30
    iou: float = 0.45
    max_det: int = 12
    device: str = "cpu"
    kpt_conf: float = 0.30


@dataclass
class RuntimeConfig:
    """推理工程参数：跳帧、时间基准。"""

    frame_skip: int = 2                        # 每 N 帧跑一次模型，中间帧复用
    base_ts: Optional[int] = None              # 毫秒；None 表示用视频元数据/当前时间
    warmup: int = 3                            # 预热帧数，不计入 FPS
    video_backend: str = "opencv"


@dataclass
class FallConfig:
    """倒地判定：几何阈值 + 3 秒时序投票。"""

    angle_deg: float = 60.0                    # 躯干与竖直方向夹角阈值
    window_sec: float = 3.0                    # 时序窗口
    vote_ratio: float = 0.70                   # 窗口内疑似倒地帧占比阈值
    hold_ratio: float = 0.30                   # 迟滞：低于该比例才解除
    high_prob: float = 0.90                    # 触发后的置信度
    low_scale: float = 0.35                    # 未触发时的按比例分值
    hip_drop_ratio: float = 0.50               # 髋部高度低于站立参考的多少 → 视为蹲/躺
    min_visible_kpts: int = 6                  # 至少多少个可信关键点才做判定
    kpt_conf: float = 0.30
    head_below_hip: bool = True                # 头部 y > 髋部 y 也算疑似
    sustain_sec: float = 0.60                  # 单帧判定需连续保持的最短时长（防止抖动）


@dataclass
class DoorConfig:
    """贴门判定：腕部落进门缝附近 + 持续时长。"""

    roi: Optional[str] = None                  # ROI 名字或 json 路径；None 用内置默认
    near_ratio: float = 0.12                   # 距门缝 <= near_ratio*门宽 视为"贴着门缝"
    persist_sec: float = 1.00                  # 手在门区持续多久才给高分
    decay_sec: float = 1.50                    # 手离开后多久衰减到 0
    wrist_conf: float = 0.30


@dataclass
class CrowdConfig:
    """拥挤度：人体框面积占画面比例的饱和映射。"""

    saturation: float = 0.55                   # 面积占比达到该值即视为 1.0


@dataclass
class QualityConfig:
    """关键点可信度（决定 keypoints_ok）。"""

    kpt_conf: float = 0.30
    min_visible_kpts: int = 8
    min_mean_conf: float = 0.35


@dataclass
class VisionConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    fall: FallConfig = field(default_factory=FallConfig)
    door: DoorConfig = field(default_factory=DoorConfig)
    crowd: CrowdConfig = field(default_factory=CrowdConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    source: Optional[str] = None               # 记录配置来源，方便复现

    # -- 序列化 ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": asdict(self.model),
            "runtime": asdict(self.runtime),
            "fall": asdict(self.fall),
            "door": asdict(self.door),
            "crowd": asdict(self.crowd),
            "quality": asdict(self.quality),
        }

    # -- 应用覆盖 ----------------------------------------------------------
    def apply_overrides(self, overrides: Optional[Dict[str, Any]]) -> "VisionConfig":
        """``{"model.imgsz": 640, "runtime.frame_skip": 1}`` 形式的分段覆盖。"""
        for dotted, value in (overrides or {}).items():
            if value is None:
                continue
            section, _, attr = dotted.partition(".")
            if not attr:
                raise KeyError(f"覆盖项 {dotted!r} 必须是 'section.field' 形式")
            target = getattr(self, section, None)
            if target is None or not hasattr(target, attr):
                raise KeyError(f"未知配置项 {dotted!r}")
            setattr(target, attr, value)
        return self

    def merge_dict(self, payload: Dict[str, Any]) -> "VisionConfig":
        for section_name, section_payload in (payload or {}).items():
            section = getattr(self, section_name, None)
            if section is None or not is_dataclass(section):
                continue
            allowed = {f.name for f in fields(section)}
            for key, value in (section_payload or {}).items():
                if key in allowed:
                    setattr(section, key, value)
        return self

    def cli_overrides(self, **kwargs: Any) -> "VisionConfig":
        """把命令行里非 None 的参数按 'section.field' 塞进来。"""
        flat: Dict[str, Any] = {}
        for key, value in kwargs.items():
            if value is None:
                continue
            section, _, attr = key.partition("__")
            if attr:
                flat[f"{section}.{attr}"] = value
        return self.apply_overrides(flat)


DEFAULT_CONFIG = VisionConfig()


def load_config(path: "str | Path | None" = None, *, overrides: Optional[Dict[str, Any]] = None) -> VisionConfig:
    """读取配置；文件不存在就用内置默认值（不抛异常，离线可跑）。"""
    config = VisionConfig()
    candidate: Optional[Path] = None
    if path:
        candidate = resolve_path(path)
    else:
        default_json = CONFIG_DIR / "default.json"
        candidate = default_json if default_json.exists() else None
    if candidate and candidate.exists():
        # utf-8-sig：队友在 Windows 上用记事本/某些编辑器保存会带 BOM，这里直接吃掉
        with candidate.open("r", encoding="utf-8-sig") as handle:
            config.merge_dict(json.load(handle))
        config.source = str(candidate.relative_to(PROJECT_ROOT)) if candidate.is_relative_to(PROJECT_ROOT) else str(candidate)
    if overrides:
        config.apply_overrides(overrides)
    return config