"""视频读入 + 姿态来源（YOLO-Pose / ONNX / 合成关键点）。

一个视频永远走同一条流水线：

    视频文件 -> VideoReader（解码、缩放、算时间戳）
             -> PoseBackend（出一帧的 17 关键点）
             -> iter_frames()（带跳帧复用）
             -> rules.VisionEngine（出 VisionOut）

三种后端只在"关键点从哪来"这一点上不同：

* ``yolo``      —— 生产路径。ultralytics 加载 yolov8n-pose.pt，CPU 推理。
* ``onnx``      —— 同一权重的 ONNX 版本，onnxruntime 推理（昇腾/无 torch 环境）。
* ``synthetic`` —— 离线兜底。读 ``<case>_kpts.jsonl`` 里的关键点轨迹，
                   用于没有实拍素材时把 A→B→C 全链路先跑通（见 DATA_DICT.md 的诚实声明）。

``auto`` 的判定顺序：有 sidecar（合成轨迹）先用它 -> 有 ONNX -> 有 ultralytics -> 报错。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from .config import MODEL_DIR, VisionConfig, resolve_path
from .geometry import Person

__all__ = [
    "Frame",
    "VideoReader",
    "PoseBackend",
    "YoloPoseBackend",
    "OnnxPoseBackend",
    "SyntheticBackend",
    "make_backend",
    "iter_frames",
    "sidecar_path",
    "meta_path",
    "video_info",
]


# ---------------------------------------------------------------------------
# 帧结构
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """一帧：画面 + 这一帧的关键点 + 时间戳。"""

    index: int
    ts: int
    image: Optional[np.ndarray]
    persons: List[Person] = field(default_factory=list)
    backend: str = "unknown"
    infer_ms: float = 0.0
    reused: bool = False

    @property
    def has_image(self) -> bool:
        return self.image is not None


def sidecar_path(video_path: Path) -> Path:
    """合成关键点轨迹：``demo_faint.mp4`` -> ``demo_faint_kpts.jsonl``。"""
    return video_path.with_name(f"{video_path.stem}_kpts.jsonl")


def meta_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}_meta.json")


# ---------------------------------------------------------------------------
# 视频读入
# ---------------------------------------------------------------------------


class VideoReader:
    """OpenCV 解码 + 帧率/时间基准处理。"""

    def __init__(
        self,
        video_path: "str | Path",
        *,
        max_side: Optional[int] = None,
        base_ts: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> None:
        import cv2  # 局部导入：只在真的要读视频时才需要 opencv

        self.cv2 = cv2
        self.path = resolve_path(video_path)
        if not self.path.exists():
            raise FileNotFoundError(f"找不到视频：{self.path}")
        self.capture = cv2.VideoCapture(str(self.path))
        if not self.capture.isOpened():
            raise RuntimeError(f"无法打开视频（编码器缺失？）：{self.path}")

        self.fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 25.0)
        if not (1.0 <= self.fps <= 240.0):
            self.fps = 25.0
        self.raw_width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.raw_height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self.frame_count = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.max_side = max_side
        self.limit = limit
        self.base_ts = int(base_ts) if base_ts else self._infer_base_ts()

    # -- 元数据 ------------------------------------------------------------
    def _infer_base_ts(self) -> int:
        candidate = meta_path(self.path)
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                for key in ("start_ts_ms", "start_ts", "ts"):
                    if key in payload and payload[key]:
                        return int(payload[key])
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        # 没有元数据就锚到当前秒；离线批处理时同一段数据的 ts 是等差递增的
        return int(time.time() * 1000 // 1000) * 1000

    @property
    def size(self) -> Tuple[int, int]:
        return (self.raw_width, self.raw_height)

    def ts_of(self, index: int) -> int:
        return int(self.base_ts + round(index * 1000.0 / self.fps))

    # -- 读取 --------------------------------------------------------------
    def frames(self) -> Iterator[Tuple[int, np.ndarray]]:
        index = 0
        cv2 = self.cv2
        while True:
            ok, frame = self.capture.read()
            if not ok:
                break
            if self.max_side and max(frame.shape[:2]) > self.max_side:
                scale = self.max_side / float(max(frame.shape[:2]))
                frame = cv2.resize(
                    frame,
                    (int(round(frame.shape[1] * scale)), int(round(frame.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            yield index, frame
            index += 1
            if self.limit is not None and index >= self.limit:
                break

    def close(self) -> None:
        try:
            self.capture.release()
        except Exception:
            pass

    def __enter__(self) -> "VideoReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def video_info(video_path: "str | Path") -> Dict[str, Any]:
    """不打开画面的轻量元数据（写 DATA_DICT 与回归报告要用）。"""
    with VideoReader(video_path) as reader:
        info = {
            "path": str(reader.path),
            "fps": round(reader.fps, 3),
            "width": reader.raw_width,
            "height": reader.raw_height,
            "frames": reader.frame_count,
            "duration_sec": round(reader.frame_count / reader.fps, 2) if reader.frame_count else None,
            "base_ts": reader.base_ts,
        }
        # 修复：VideoReader 的 __init__ 已打开文件，frame_count 可能为 0，用实际解码兜底
        if not info["frames"]:
            count = 0
            for _ in reader.frames():
                count += 1
            info["frames"] = count
            info["duration_sec"] = round(count / reader.fps, 2) if reader.fps else None
    return info


# ---------------------------------------------------------------------------
# 后端基类
# ---------------------------------------------------------------------------


class PoseBackend:
    """关键点后端接口：一帧 -> 若干 Person。"""

    name = "base"
    needs_image = True

    def infer(self, image: np.ndarray) -> List[Person]:  # pragma: no cover - 接口
        raise NotImplementedError

    #: 合成后端用它直接给某一帧的关键点
    def at(self, index: int) -> List[Person]:  # pragma: no cover - 接口
        return []

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# ultralytics YOLO-Pose
# ---------------------------------------------------------------------------


class YoloPoseBackend(PoseBackend):
    name = "yolo"

    def __init__(self, cfg: VisionConfig, weights: Optional[str] = None) -> None:
        try:
            from ultralytics import YOLO  # 延迟导入，装不上也不影响其它后端
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RuntimeError(
                "未安装 ultralytics。请执行 `pip install -r vision/requirements-a.txt`，"
                "或改用 --backend onnx / synthetic。"
            ) from exc

        weight_path = resolve_path(weights or cfg.model.weights)
        self._weight_label = str(weight_path)
        if weight_path.exists():
            self.model = YOLO(str(weight_path))
        else:
            # 交给 ultralytics 按名字自动下载到权重目录
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            self.model = YOLO(Path(weight_path.name).stem)
        self.cfg = cfg

    def infer(self, image: np.ndarray) -> List[Person]:
        model_cfg = self.cfg.model
        results = self.model.predict(
            source=image,
            imgsz=int(model_cfg.imgsz),
            conf=float(model_cfg.conf),
            iou=float(model_cfg.iou),
            max_det=int(model_cfg.max_det),
            device=model_cfg.device,
            verbose=False,
            classes=[0],  # 只要人
            # rect=False：强制方形 letterbox。ultralytics 默认 rect=True 会按长宽比
            # 裁成一个"最小矩形"（例如 480x384），而我们的 ONNX 后端固定 480x480；
            # 两边预处理不一致时，同一帧的关键点会差 5~8 px（缩放回原图后放大），
            # 会让 .pt / .onnx 的 parity 检查得出错误结论。
            rect=False,
        )
        if not results:
            return []
        result = results[0]
        if result.keypoints is None or result.boxes is None or len(result.boxes) == 0:
            return []

        kpts = result.keypoints.data.detach().cpu().numpy().astype(np.float32)  # (N,17,3)
        boxes = result.boxes.xyxy.detach().cpu().numpy().astype(np.float32)
        scores = result.boxes.conf.detach().cpu().numpy().astype(np.float32)
        return [
            Person(keypoints=kpts[i], score=float(scores[i]), bbox=boxes[i])
            for i in range(kpts.shape[0])
        ]


# ---------------------------------------------------------------------------
# ONNX Runtime
# ---------------------------------------------------------------------------


class OnnxPoseBackend(PoseBackend):
    name = "onnx"

    def __init__(self, cfg: VisionConfig, onnx_path: Optional[str] = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 onnxruntime。执行 `pip install onnxruntime`，"
                "或改用 --backend synthetic。"
            ) from exc

        path = resolve_path(onnx_path or cfg.model.onnx)
        if not path.exists():
            raise FileNotFoundError(
                f"找不到 ONNX 权重 {path}；先运行 `python vision/onnx_export.py --export`。"
            )
        providers = ["CPUExecutionProvider"]
        available = ort.get_available_providers()
        for preferred in ("AscendExecutionProvider", "CUDAExecutionProvider"):
            if preferred in available:
                providers.insert(0, preferred)
        self.session = ort.InferenceSession(str(path), providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.cfg = cfg
        self._providers = providers

    # -- letterbox ---------------------------------------------------------
    @staticmethod
    def _letterbox(image: np.ndarray, size: int) -> Tuple[np.ndarray, float, int, int]:
        import cv2

        h, w = image.shape[:2]
        scale = min(size / w, size / h)
        new_w, new_h = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x = (size - new_w) // 2
        pad_y = (size - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return canvas, scale, pad_x, pad_y

    def infer(self, image: np.ndarray) -> List[Person]:
        import cv2

        size = int(self.cfg.model.imgsz)
        canvas, scale, pad_x, pad_y = self._letterbox(image, size)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        outputs = self.session.run(None, {self.input_name: blob})
        raw = np.asarray(outputs[0])
        if raw.ndim == 3:
            raw = raw[0]
        if raw.shape[0] < raw.shape[1]:          # (56, N) -> (N, 56)
            raw = raw.T
        return self._decode(raw, scale, pad_x, pad_y, image.shape)

    def _decode(self, preds: np.ndarray, scale: float, pad_x: int, pad_y: int, shape: Sequence[int]) -> List[Person]:
        import cv2

        if preds.size == 0:
            return []
        model_cfg = self.cfg.model
        scores = preds[:, 4]
        keep = scores >= model_cfg.conf
        preds, scores = preds[keep], scores[keep]
        if preds.shape[0] == 0:
            return []

        xywh = preds[:, :4]
        boxes = np.stack(
            [
                xywh[:, 0] - xywh[:, 2] / 2,
                xywh[:, 1] - xywh[:, 3] / 2,
                xywh[:, 0] + xywh[:, 2] / 2,
                xywh[:, 1] + xywh[:, 3] / 2,
            ],
            axis=1,
        )
        indices = cv2.dnn.NMSBoxes(
            boxes.tolist(), scores.tolist(), float(model_cfg.conf), float(model_cfg.iou)
        )
        if len(indices) == 0:
            return []
        indices = np.asarray(indices).reshape(-1)
        indices = indices[: int(model_cfg.max_det)]

        kpts = preds[:, 5:5 + 51].reshape(-1, 17, 3)
        h, w = shape[:2]
        persons: List[Person] = []
        for i in indices:
            box = boxes[i].copy()
            box[[0, 2]] = (box[[0, 2]] - pad_x) / scale
            box[[1, 3]] = (box[[1, 3]] - pad_y) / scale
            box[[0, 2]] = np.clip(box[[0, 2]], 0, w)
            box[[1, 3]] = np.clip(box[[1, 3]], 0, h)
            person_kpts = kpts[i].copy()
            person_kpts[:, 0] = (person_kpts[:, 0] - pad_x) / scale
            person_kpts[:, 1] = (person_kpts[:, 1] - pad_y) / scale
            persons.append(
                Person(keypoints=person_kpts.astype(np.float32), score=float(scores[i]), bbox=box.astype(np.float32))
            )
        return persons


# ---------------------------------------------------------------------------
# 合成关键点（离线兜底）
# ---------------------------------------------------------------------------


class SyntheticBackend(PoseBackend):
    """读 `<case>_kpts.jsonl`：每行 {frame, ts, persons:[17x[x,y,conf]], scores}。"""

    name = "synthetic"
    needs_image = False

    def __init__(self, video_path: "str | Path") -> None:
        self.path = sidecar_path(resolve_path(video_path))
        if not self.path.exists():
            raise FileNotFoundError(f"找不到关键点轨迹：{self.path}")
        self._by_frame: Dict[int, Tuple[List[Person], int]] = {}
        self.backend_note = "synthetic-keypoints"
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                persons = [
                    Person(keypoints=np.asarray(k, dtype=np.float32), score=float(s))
                    for k, s in zip(payload.get("persons", []), payload.get("scores", [1.0] * len(payload.get("persons", []))))
                ]
                self._by_frame[int(payload["frame"])] = (persons, int(payload.get("ts", 0)))
        self.frames_available = len(self._by_frame)

    def at(self, index: int) -> List[Person]:
        entry = self._by_frame.get(int(index))
        return list(entry[0]) if entry else []

    def infer(self, image: np.ndarray) -> List[Person]:  # pragma: no cover - 由 at() 驱动
        return []


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _weights_candidates(cfg: VisionConfig) -> List[Path]:
    return [resolve_path(cfg.model.onnx), resolve_path(cfg.model.weights)]


def make_backend(
    cfg: VisionConfig,
    video_path: "str | Path",
    backend: Optional[str] = None,
) -> PoseBackend:
    """按 ``--backend`` 或配置挑后端；auto 时按"能跑就先跑"的顺序回退。"""
    video = resolve_path(video_path)
    choice = (backend or cfg.model.backend or "auto").lower()

    if choice == "synthetic":
        return SyntheticBackend(video)
    if choice == "yolo":
        return YoloPoseBackend(cfg)
    if choice == "onnx":
        return OnnxPoseBackend(cfg)
    if choice != "auto":
        raise ValueError(f"未知 backend={choice!r}，可选 auto|yolo|onnx|synthetic")

    if sidecar_path(video).exists():
        return SyntheticBackend(video)

    onnx_file = resolve_path(cfg.model.onnx)
    if onnx_file.exists():
        try:
            return OnnxPoseBackend(cfg)
        except (RuntimeError, FileNotFoundError, ImportError):
            pass

    try:
        return YoloPoseBackend(cfg)
    except Exception as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            f"{video.name} 没有可用的姿态后端（{exc}）。"
            "选项：① 录真实视频后装 ultralytics；② 生成合成演示数据 "
            "`python data/scripts/build_demo_data.py --all`。"
        ) from exc


# ---------------------------------------------------------------------------
# 统一迭代器（含跳帧复用）
# ---------------------------------------------------------------------------


def iter_frames(
    video_path: "str | Path",
    cfg: VisionConfig,
    *,
    backend: Optional[PoseBackend] = None,
    limit: Optional[int] = None,
    max_side: Optional[int] = None,
    with_image: bool = True,
    progress: bool = False,
) -> Iterator[Frame]:
    """逐帧产出 ``Frame``；跳帧时中间帧复用上一次的关键点（电梯里动作慢，几乎无损）。"""
    path = resolve_path(video_path)
    backend = backend or make_backend(cfg, path)
    skip = max(1, int(cfg.runtime.frame_skip))
    reader = VideoReader(path, max_side=max_side, limit=limit, base_ts=cfg.runtime.base_ts)
    last_persons: List[Person] = []
    synthetic = isinstance(backend, SyntheticBackend)

    try:
        for index, image in reader.frames():
            ts = reader.ts_of(index)
            reused = False
            infer_ms = 0.0
            if synthetic:
                persons = backend.at(index)
            elif index % skip == 0 or not last_persons:
                stamp = time.perf_counter()
                persons = backend.infer(image)
                infer_ms = (time.perf_counter() - stamp) * 1000.0
                last_persons = persons
            else:
                persons = last_persons
                reused = True

            # 每帧都拷一份：下游会改 keypoints（缩放/平滑），不能让复用帧互相污染
            persons = [Person(keypoints=np.array(p.keypoints, copy=True), score=p.score, bbox=None if p.bbox is None else np.array(p.bbox, copy=True)) for p in persons]

            yield Frame(
                index=index,
                ts=ts,
                image=image if with_image else None,
                persons=persons,
                backend=backend.name,
                infer_ms=infer_ms,
                reused=reused,
            )
    finally:
        reader.close()
        backend.close()