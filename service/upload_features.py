"""用户上传真实视频 · 特征后端（C · T-UP-004/005）。

默认档 L2：`HeuristicBackend` —— 只用 cv2 稳定子集做前景统计，
  `person_count / door_approach / crowded` 来自连通域与门区占比；
  `fall_prob` 恒 0.0、`keypoints_ok` 恒 False（三层红线之一，见设计 6.5）。

可选档 L1：`PoseFeatureBackend` —— 复用 `vision.infer.infer_video`
  （侧车关键点或已安装的 yolo/onnx）；不可用时由 worker 降级 L2。

顶层只 import 标准库 + `service.upload` 纯函数 + `service.config`；
cv2/numpy 仅在 `extract()` 内懒加载。
"""
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from common.schema import PERSON_COUNT_MAX, VisionOut

from .upload import (
    UploadTimeout,
    _atomic_write_text,
    frame_step,
    resolve_door_roi,
    ts_of_frame,
)

log = logging.getLogger("elevator.upload.features")

MIN_FG_RATIO = float(os.getenv("UPLOAD_MIN_FG_RATIO", "0.005"))
MIN_BLOB_AREA_RATIO = float(os.getenv("UPLOAD_MIN_BLOB_AREA_RATIO", "0.002"))
DOOR_FG_FULL = float(os.getenv("UPLOAD_DOOR_FG_FULL", "0.35"))
CROWDED_FG_FULL = float(os.getenv("UPLOAD_CROWDED_FG_FULL", "0.45"))
PART_FLUSH_FRAMES = 256
PROGRESS_EVERY = 32

VISION_FIELDS: Tuple[str, ...] = (
    "ts",
    "person_count",
    "fall_prob",
    "door_approach",
    "crowded",
    "keypoints_ok",
)


class FeatureDecodeError(Exception):
    """视频无法解码（上层转 422 UP_NOT_DECODABLE）。"""


class PoseUnavailable(Exception):
    """L1 依赖/权重/侧车不可用（worker 降级 L2）。"""


@dataclass(frozen=True)
class FeatureResult:
    rows: List[VisionOut]
    roi_source: str
    low_conf_count: int
    mode: str


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return float(low if value < low else high if value > high else value)


def _default_roi(width: int, height: int) -> List[int]:
    return [0, int(round(height * 2.0 / 3.0)), int(width), int(height)]


def write_vision_json(path: Path, rows: List[VisionOut]) -> None:
    """原子写逐帧六字段数组（`vision.json`），UTF-8 无 BOM。"""
    payload = [asdict(row) for row in rows]
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


def append_vision_part(path: Path, rows: List[VisionOut]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False))
            handle.write("\n")


def validate_vision_shape(rows: List[VisionOut], frames: int, start_ts_ms: int) -> None:
    """条数 = frames、首帧 ts = 录制起始时间、ts 严格递增。"""
    if len(rows) != int(frames):
        raise ValueError("vision rows %d != frames %d" % (len(rows), int(frames)))
    if not rows:
        raise ValueError("empty vision sequence")
    if int(rows[0].ts) != int(start_ts_ms):
        raise ValueError("vision first ts %s != start_ts_ms %s" % (rows[0].ts, start_ts_ms))
    for previous, current in zip(rows, rows[1:]):
        if int(current.ts) <= int(previous.ts):
            raise ValueError("vision ts not strictly increasing: %s -> %s" % (previous.ts, current.ts))


def validate_heuristic_vision(rows: List[VisionOut]) -> None:
    """L2 红线：fall_prob 全 0，keypoints_ok 全 False。"""
    for row in rows:
        if float(row.fall_prob) != 0.0:
            raise ValueError("heuristic fall_prob must be 0.0")
        if bool(row.keypoints_ok):
            raise ValueError("heuristic keypoints_ok must be False")


def detect_feature_mode(base_dir: Optional[Path] = None) -> Tuple[str, bool]:
    """运行时探测：只有依赖 + 非空权重同时成立才判 `pose`，否则 `heuristic`。"""
    from importlib.util import find_spec

    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
    weights = base / "models" / "yolov8n-pose.pt"
    onnx = base / "models" / "yolov8n-pose.onnx"
    if find_spec("ultralytics") is not None and weights.is_file() and weights.stat().st_size > 0:
        return "pose", True
    if find_spec("onnxruntime") is not None and onnx.is_file() and onnx.stat().st_size > 0:
        return "pose", True
    return "heuristic", False


def load_kpts_sidecar(path: Path, frames: int) -> int:
    """校验 `source_kpts.jsonl`：17 点、ts 单调、帧号合法；返回行数。"""
    if not path.is_file():
        raise PoseUnavailable("缺少关键点侧车")
    count = 0
    previous_ts = -1
    with open(str(path), "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("kpts row must be object")
            frame = int(payload.get("frame"))
            ts = int(payload.get("ts"))
            if frame < 0 or frame >= int(frames):
                raise ValueError("kpts frame out of range")
            if ts <= previous_ts:
                raise ValueError("kpts ts not strictly increasing")
            previous_ts = ts
            persons = payload.get("persons") or []
            for points in persons:
                if not isinstance(points, list) or len(points) != 17:
                    raise ValueError("kpts person must be 17 points")
                for point in points:
                    if not isinstance(point, (list, tuple)) or len(point) < 3:
                        raise ValueError("kpts point must be [x,y,conf]")
            count += 1
    if count == 0:
        raise ValueError("empty kpts sidecar")
    return count


class HeuristicBackend:
    """L2：前景减除 + 连通域 + 门区占比（`fall_prob` 唯一赋值点恒 0）。"""

    mode = "heuristic"

    def extract(
        self,
        video_path: Path,
        start_ts_ms: int,
        door_roi: Optional[List[int]],
        step: int,
        progress: Callable[[int, int], None],
        deadline: float,
        fps: Optional[float] = None,
        frames_total: int = 0,
        part_path: Optional[Path] = None,
    ) -> FeatureResult:
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            raise FeatureDecodeError("cv2 不可用：%s" % type(exc).__name__)

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise FeatureDecodeError("视频无法解码")
        rows: List[VisionOut] = []
        low_conf_count = 0
        index = 0
        step_value = max(1, int(step))
        try:
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps_value = float(fps if fps else (capture.get(cv2.CAP_PROP_FPS) or 0.0))
            if fps_value <= 0:
                fps_value = 25.0
            total = int(frames_total or capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if width <= 0 or height <= 0:
                raise FeatureDecodeError("视频尺寸无法读取")

            roi, roi_source = resolve_door_roi(door_roi, width, height)
            x1, y1, x2, y2 = roi
            door_area = max(1, (x2 - x1) * (y2 - y1))
            min_area = max(1.0, MIN_BLOB_AREA_RATIO * width * height)
            background = None
            try:
                background = cv2.createBackgroundSubtractorMOG2(
                    history=300, varThreshold=16, detectShadows=False
                )
            except Exception:  # noqa: BLE001 - 5.x 差异 → absdiff 回退
                background = None
            previous_gray = None
            last_count = 0
            empty_run = 0
            last_door = 0.0
            last_crowded = 0.0

            while True:
                if time.monotonic() > float(deadline):
                    raise UploadTimeout(rows, index)
                ok, image = capture.read()
                if not ok or image is None:
                    break
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                if index % step_value == 0:
                    mask = background.apply(gray) if background is not None else None
                    if previous_gray is None:
                        # 首帧只用于初始化背景模型，不产生前景（否则全黑视频也报“有人”）
                        mask = gray * 0
                    else:
                        diff = cv2.absdiff(previous_gray, gray)
                        _code, diff_mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                        mask = diff_mask if mask is None else cv2.bitwise_or(mask, diff_mask)
                    previous_gray = gray
                    try:
                        mask = cv2.medianBlur(mask, 5)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        _code, binary = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
                    except Exception:  # noqa: BLE001
                        binary = mask
                    fg_pixels = int(cv2.countNonZero(binary))
                    fg_ratio = fg_pixels / float(width * height)
                    contours = cv2.findContours(
                        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                    )[0]
                    valid = [item for item in contours if cv2.contourArea(item) >= min_area]
                    if fg_ratio < MIN_FG_RATIO or not valid:
                        raw_count = 0
                        low_conf_count += 1
                    else:
                        raw_count = min(PERSON_COUNT_MAX, len(valid))
                    # 人数只增不减：残影造成的 1↔2 抖动会让 B 的状态机误判“有人走动”，
                    # 这里保持峰值，只有连续 25 个计算帧为空才视为空梯（约 2 s）。
                    if raw_count > last_count:
                        last_count = int(raw_count)
                        empty_run = 0
                    elif raw_count == 0:
                        empty_run += 1
                        if empty_run >= 25:
                            last_count = 0
                            empty_run = 0
                    else:
                        empty_run = 0
                    door_fg = int(cv2.countNonZero(binary[y1:y2, x1:x2]))
                    door_ratio = door_fg / float(door_area)
                    in_door = 0
                    for item in valid:
                        bx, by, bw, bh = cv2.boundingRect(item)
                        if by + bh >= y1 and by <= y2 and bx < x2 and bx + bw > x1:
                            in_door += 1
                    person_in_door = (in_door / float(last_count)) if last_count else 0.0
                    last_door = _clamp(
                        0.6 * (door_ratio / DOOR_FG_FULL) + 0.4 * person_in_door
                    )
                    last_crowded = _clamp(fg_ratio / CROWDED_FG_FULL)

                ts = ts_of_frame(start_ts_ms, fps_value, index)
                rows.append(
                    VisionOut(
                        ts=ts,
                        person_count=int(last_count),
                        fall_prob=0.0,
                        door_approach=float(last_door),
                        crowded=float(last_crowded),
                        keypoints_ok=False,
                    )
                )
                index += 1
                if part_path is not None and len(rows) % PART_FLUSH_FRAMES == 0:
                    append_vision_part(part_path, rows[-PART_FLUSH_FRAMES:])
                if index % PROGRESS_EVERY == 0:
                    progress(index, total or index)
            if part_path is not None:
                remainder = len(rows) % PART_FLUSH_FRAMES
                if remainder:
                    append_vision_part(part_path, rows[-remainder:])
        finally:
            capture.release()

        if index == 0:
            raise FeatureDecodeError("视频无法解码")
        return FeatureResult(
            rows=rows,
            roi_source=roi_source,
            low_conf_count=int(low_conf_count),
            mode=self.mode,
        )


class PoseFeatureBackend:
    """L1：复用 A 侧 `vision.infer.infer_video`（侧车关键点 / yolo / onnx）。"""

    mode = "pose"

    def extract(
        self,
        video_path: Path,
        start_ts_ms: int,
        door_roi: Optional[List[int]],
        step: int,
        progress: Callable[[int, int], None],
        deadline: float,
        fps: Optional[float] = None,
        frames_total: int = 0,
        part_path: Optional[Path] = None,
    ) -> FeatureResult:
        try:
            from vision.config import load_config
            from vision.infer import infer_video
        except Exception as exc:  # noqa: BLE001
            raise PoseUnavailable("视觉依赖不可用：%s" % type(exc).__name__)
        cfg = load_config()
        cfg.runtime.base_ts = int(start_ts_ms)
        cfg.runtime.frame_skip = max(1, int(step))
        try:
            rows = infer_video(str(video_path), cfg, backend="synthetic")
        except Exception as exc:  # noqa: BLE001
            raise PoseUnavailable("L1 推理失败：%s" % type(exc).__name__)
        if not rows:
            raise PoseUnavailable("L1 无输出")
        progress(len(rows), frames_total or len(rows))
        if time.monotonic() > float(deadline):
            raise UploadTimeout(rows, len(rows))
        return FeatureResult(
            rows=rows,
            roi_source="client" if door_roi else "default",
            low_conf_count=0,
            mode=self.mode,
        )


def make_backend(mode: str) -> Any:
    return PoseFeatureBackend() if str(mode).lower() == "pose" else HeuristicBackend()


def extract_rows(
    mode: str,
    video_path: Path,
    start_ts_ms: int,
    door_roi: Optional[List[int]],
    fps: float,
    frames_total: int,
    deadline: float,
    progress: Callable[[int, int], None],
    part_path: Optional[Path] = None,
) -> FeatureResult:
    """统一入口：L1 不可用自动降级 L2（只影响本件）。"""
    backend = make_backend(mode)
    try:
        return backend.extract(
            video_path=video_path,
            start_ts_ms=start_ts_ms,
            door_roi=door_roi,
            step=frame_step(),
            progress=progress,
            deadline=deadline,
            fps=fps,
            frames_total=frames_total,
            part_path=part_path,
        )
    except (PoseUnavailable, ValueError) as exc:
        if backend.mode != "pose":
            raise
        log.warning("upload_pose_degraded reason=%s", type(exc).__name__)
        return HeuristicBackend().extract(
            video_path=video_path,
            start_ts_ms=start_ts_ms,
            door_roi=door_roi,
            step=frame_step(),
            progress=progress,
            deadline=deadline,
            fps=fps,
            frames_total=frames_total,
            part_path=part_path,
        )