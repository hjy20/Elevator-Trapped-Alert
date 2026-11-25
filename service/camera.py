"""现场画面（摄像头视图）· 契约单一来源（C 侧 / T-CAM-001~004）。

职责：
  1. 源登记（从 `common/demo_data/*_meta.json` 磁盘推导，不硬编码清单）；
  2. 时间基准（frame/wall/frame(alert)/drift 的唯一公式，全项目只此一套）；
  3. 播放会话（同一时刻只允许 1 个活动会话 + 10 分钟 TTL）；
  4. 对外契约（`/api/v1/camera/*` 的字段形态与错误体 helper）；
  5. 视频分发（Range 纯函数 + 分块生成器，零解码）；
  6. 关键点窗口（纯 Python bbox，不依赖 numpy）+ 视觉分数直通；
  7. 静态帧降级（cv2 函数内懒加载，8 帧内存缓存）。

硬约束（`docs/spec/camera_tasks.md` 0.1）：
  - Python 3.9：注解只用 Optional/List/Dict/Any/Tuple，禁止 PEP 604 与运行期内置泛型；
  - 模块顶层只 import logging/json/time/dataclasses/pathlib/typing + config，保证
    `python -c "import service.camera"` 在无 fastapi/无 cv2 的环境也能退出码 0；
  - 不新增第三方依赖；`cv2` 仅在 `frame_jpeg()` 内懒加载；
  - 内部磁盘绝对路径永不出现在任何对外响应里（避免暴露路径）。
"""
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config

log = logging.getLogger("elevator.camera")

# ---------------------------------------------------------------- 常量（单一来源）

DEFAULT_DEVICE_ID = "ELEV-DEMO-001"
SESSION_TTL_MS = 600000                     # 10 分钟无访问即失效
SUPPORTED_MODES: Tuple[str, ...] = ("demo", "file", "usb", "rtsp")
WATERMARK = "演示数据 · 非取证"
UPLOAD_WATERMARK = "用户上传 · 非取证"
UPLOAD_SOURCES_PARTS: Tuple[str, ...] = ("data", "raw", "uploads")
DEFAULT_WINDOW_COUNT = 25
MAX_WINDOW_COUNT = 120
VIDEO_CHUNK_SIZE = 262144                   # 256 KB，逐块读，不整文件入内存
FRAME_CACHE_SIZE = 8
FRAME_CACHE_TTL_MS = 1000
VIDEO_MEDIA_TYPE = "video/mp4"
VISION_FIELDS: Tuple[str, ...] = (
    "ts",
    "person_count",
    "fall_prob",
    "door_approach",
    "crowded",
    "keypoints_ok",
)

# 严格等于 `docs/spec/camera.md` 4.3 表的 11 个 code（前端映射键集合须与此相等）
ERROR_CODES: Tuple[str, ...] = (
    "CAM_BAD_REQUEST",
    "CAM_BAD_TIMESTAMP",
    "CAM_BAD_FRAME",
    "CAM_WINDOW_TOO_LARGE",
    "CAM_SOURCE_NOT_FOUND",
    "CAM_VIDEO_NOT_FOUND",
    "CAM_NOT_ALIGNED",
    "CAM_RANGE_INVALID",
    "CAM_TRANSPORT_UNSUPPORTED",
    "CAM_NOT_READY",
    "CAM_INTERNAL",
)

# 仅文案映射（不构成源清单硬编码；新增源不需要改这里）
_LABELS: Dict[str, str] = {
    "normal": "轿厢内 · 正常乘梯",
    "estop": "轿厢内 · 急停演示",
    "trapped": "轿厢内 · 困人演示",
    "pry": "轿厢内 · 扒门演示",
    "faint": "轿厢内 · 晕倒演示",
}

_REQUIRED_META_FIELDS: Tuple[str, ...] = (
    "fps",
    "frames",
    "width",
    "height",
    "duration_sec",
    "start_ts_ms",
    "event_type",
)
_DURATION_TOLERANCE_MS = 40


def now_ms() -> int:
    """墙钟毫秒（13 位）。"""
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class CameraSource:
    """一个可播放的画面源（离线演示下与 `common/demo_data/<case>` 一一对应）。"""

    source_id: str
    device_id: str
    label: str
    event_type: str
    fps: float
    frames: int
    width: int
    height: int
    anchor_ts_ms: int
    duration_ms: int
    video: bool
    keypoints: bool
    vision: bool
    transport: List[str]
    demo: bool = True
    video_path: Path = field(default=Path(""), repr=False, compare=False)
    kpts_path: Path = field(default=Path(""), repr=False, compare=False)
    vision_path: Path = field(default=Path(""), repr=False, compare=False)
    watermark: Optional[str] = None

    def as_public(self) -> Dict[str, Any]:
        """对外 14 字段 + `demo`；内部路径不输出。上传源额外带 `watermark`。"""
        payload: Dict[str, Any] = {
            "source_id": self.source_id,
            "device_id": self.device_id,
            "label": self.label,
            "event_type": self.event_type,
            "fps": float(self.fps),
            "frames": int(self.frames),
            "width": int(self.width),
            "height": int(self.height),
            "duration_ms": int(self.duration_ms),
            "anchor_ts_ms": int(self.anchor_ts_ms),
            "video": bool(self.video),
            "keypoints": bool(self.keypoints),
            "vision": bool(self.vision),
            "transport": list(self.transport),
            "demo": bool(self.demo),
        }
        if self.watermark is not None:
            payload["watermark"] = str(self.watermark)
        return payload


@dataclass(frozen=True)
class CameraSession:
    """起播会话：对齐的唯一事实源。"""

    session_id: str
    source_id: str
    start_wall_ms: int
    offset_ms: int
    updated_ms: int


@dataclass(frozen=True)
class CameraAlignment:
    """对齐状态（无会话时 aligned=False / offset=0 / drift=0）。"""

    source_id: str
    aligned: bool
    offset_ms: int
    drift_ms: int


@dataclass(frozen=True)
class KptPerson:
    """一个人：17 个 [x, y, conf] + 分数 + 派生包围框。"""

    keypoints: List[List[float]]
    score: float
    bbox: List[float]

    def as_public(self) -> Dict[str, Any]:
        return {
            "keypoints": [list(point) for point in self.keypoints],
            "score": float(self.score),
            "bbox": [float(value) for value in self.bbox],
        }


@dataclass(frozen=True)
class KptFrame:
    """一帧：素材时间 ts（非墙钟）+ 人 + 视觉分数直通。"""

    frame: int
    ts: int
    persons: List[KptPerson]
    vision: Optional[Dict[str, Any]]

    def as_public(self) -> Dict[str, Any]:
        return {
            "frame": int(self.frame),
            "ts": int(self.ts),
            "persons": [person.as_public() for person in self.persons],
            "vision": dict(self.vision) if self.vision is not None else None,
        }


# ---------------------------------------------------------------- 错误体与异常


def error_detail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """统一错误体：`{"code","message",...}`（路由层包进 `detail`）。"""
    payload: Dict[str, Any] = {"code": str(code), "message": str(message)}
    payload.update(extra)
    return payload


class CameraError(Exception):
    """携带契约状态码/错误码的异常；由路由层转成 HTTPException。"""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.extra = dict(extra)

    def detail(self) -> Dict[str, Any]:
        return error_detail(self.code, self.message, **self.extra)


class FrameUnsupported(Exception):
    """cv2 缺失或编解码不可用（由调用方降级为 501）。"""


# ---------------------------------------------------------------- 源扫描


_CV2_STATE: Optional[bool] = None
_BOOTSTRAPPED = False
_SOURCES: List[CameraSource] = []
_SOURCES_BASE: Optional[Path] = None
_UPLOAD_CACHE: Optional[List[CameraSource]] = None
_SESSIONS: Dict[str, CameraSession] = {}
_WARNED: Dict[str, int] = {}

_FRAME_CACHE: Dict[Tuple[str, int], Tuple[int, bytes]] = {}
_DECODE_COUNT = 0


def _warn_once(key: str, message: str, *args: Any) -> None:
    """同类 WARN 60 秒内只记一次，避免日志风暴拖慢轮询（camera.md 4.6）。"""
    current = now_ms()
    last = _WARNED.get(key)
    if last is not None and current - last < 60000:
        return
    _WARNED[key] = current
    log.warning(message, *args)


def cv2_available() -> bool:
    """探测 cv2（函数内懒加载；结果缓存一次）。"""
    global _CV2_STATE
    if _CV2_STATE is None:
        try:
            import cv2  # noqa: F401  懒加载：模块顶层零 cv2 依赖
        except Exception:  # noqa: BLE001 import 失败/版本异常一律视为不可用
            _CV2_STATE = False
        else:
            _CV2_STATE = True
    return bool(_CV2_STATE)


def camera_enabled() -> bool:
    raw = str(getattr(config, "CAMERA_ENABLE", "1")).strip().lower()
    return raw not in ("0", "false", "off", "no")


def source_mode() -> str:
    raw = str(getattr(config, "CAMERA_SOURCE_MODE", "demo") or "demo").strip().lower()
    return raw or "demo"


def _valid_source_id(value: str) -> bool:
    if not value or len(value) > 20:
        return False
    return all(ch.islower() or ch.isdigit() or ch == "_" for ch in value)


def _source_from_meta(base: Path, demo_dir: Path, meta_path: Path) -> Optional[CameraSource]:
    stem = meta_path.name[: -len("_meta.json")]
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn_once(
            "meta_unreadable:%s" % stem,
            "camera_source_skipped case=%s reason=meta_unreadable err=%s",
            stem,
            type(exc).__name__,
        )
        return None
    if not isinstance(raw, dict):
        _warn_once("meta_shape:%s" % stem, "camera_source_skipped case=%s reason=meta_shape", stem)
        return None

    case = str(raw.get("case", "")).strip()
    if case != stem:
        _warn_once(
            "id_mismatch:%s" % stem,
            "camera_source_skipped case=%s reason=id_mismatch",
            stem,
        )
        return None

    missing = [name for name in _REQUIRED_META_FIELDS if raw.get(name) is None]
    if missing:
        _warn_once(
            "missing:%s" % stem,
            "camera_source_skipped case=%s reason=missing_field field=%s",
            stem,
            ",".join(missing),
        )
        return None

    if not _valid_source_id(case):
        _warn_once("bad_id:%s" % stem, "camera_source_skipped case=%s reason=bad_source_id", stem)
        return None

    try:
        fps = float(raw["fps"])
        frames = int(raw["frames"])
        width = int(raw["width"])
        height = int(raw["height"])
        duration_sec = float(raw["duration_sec"])
        anchor_ts_ms = int(raw["start_ts_ms"])
        event_type = str(raw["event_type"]).strip()
    except (TypeError, ValueError):
        _warn_once("bad_value:%s" % stem, "camera_source_skipped case=%s reason=bad_field_value", stem)
        return None

    if fps <= 0 or frames <= 0 or width <= 0 or height <= 0 or not event_type:
        _warn_once("bad_range:%s" % stem, "camera_source_skipped case=%s reason=bad_field_range", stem)
        return None

    # 13 位毫秒
    if anchor_ts_ms < 10 ** 12 or anchor_ts_ms >= 10 ** 13:
        _warn_once("bad_ts:%s" % stem, "camera_source_skipped case=%s reason=bad_anchor_ts", stem)
        return None

    duration_ms = int(round(frames * 1000.0 / fps))
    if abs(duration_ms - duration_sec * 1000.0) > _DURATION_TOLERANCE_MS:
        _warn_once(
            "bad_duration:%s" % stem,
            "camera_source_skipped case=%s reason=duration_mismatch",
            stem,
        )
        return None

    video_path = demo_dir / ("%s.mp4" % case)
    sidecar = str(raw.get("keypoint_sidecar") or "").strip()
    kpts_path = demo_dir / (sidecar if sidecar else "%s_kpts.jsonl" % case)
    vision_path = base / "results" / ("vision_%s.json" % case)

    video_ok = video_path.is_file()
    kpts_ok = kpts_path.is_file()
    vision_ok = vision_path.is_file()

    transport: List[str] = ["video"]
    if kpts_ok:
        transport.append("keypoints")
    if cv2_available():
        transport.append("frames")

    label = _LABELS.get(event_type) or ("演示场景 · %s" % event_type)
    return CameraSource(
        source_id=case,
        device_id=DEFAULT_DEVICE_ID,
        label=label[:24],
        event_type=event_type,
        fps=fps,
        frames=frames,
        width=width,
        height=height,
        anchor_ts_ms=anchor_ts_ms,
        duration_ms=duration_ms,
        video=video_ok,
        keypoints=kpts_ok,
        vision=vision_ok,
        transport=transport,
        demo=True,
        video_path=video_path,
        kpts_path=kpts_path,
        vision_path=vision_path,
    )


def scan_sources(base_dir: Optional[Path] = None) -> List[CameraSource]:
    """从磁盘推导源清单（按 `source_id` 字典序稳定排序）。"""
    base = Path(base_dir) if base_dir is not None else Path(config.BASE_DIR)
    demo_dir = base / "common" / "demo_data"
    if not demo_dir.is_dir():
        _warn_once("no_demo_dir", "画面源目录缺失 dir=%s", demo_dir.name)
        return []

    items: List[CameraSource] = []
    for meta_path in sorted(demo_dir.glob("*_meta.json")):
        if meta_path.name.endswith("_imu_meta.json"):
            continue
        source = _source_from_meta(base, demo_dir, meta_path)
        if source is not None:
            items.append(source)
    items.sort(key=lambda item: item.source_id)
    return items


def _upload_label(location: str, state: str) -> str:
    place = "未知楼层" if not location or location == "?" else location
    label = "上传 · %s" % place
    if state in ("received", "extracted", "judged"):
        label += " · 处理中"
    elif state == "failed":
        label += " · 处理失败"
    return label[:24]


def _upload_source_from_manifest(
    manifest_path: Path, data: Dict[str, Any], now: int
) -> Optional[CameraSource]:
    """从 `upload.json` 构造上传源；过期/源缺失/字段非法一律不登记（D-UP-12）。"""
    try:
        upload_id = str(data.get("upload_id") or "")
        source_id = str(data.get("source_id") or "")
        fps = float(data.get("fps") or 0.0)
        frames = int(data.get("frames") or 0)
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        duration_ms = int(data.get("duration_ms") or 0)
        anchor_ts_ms = int(data.get("start_ts_ms") or 0)
        expire_ms = int(data.get("expire_ms") or 0)
        device_id = str(data.get("device_id") or "")
        state = str(data.get("state") or "")
    except (TypeError, ValueError):
        return None
    if expire_ms < int(now):
        return None
    if not upload_id or source_id != "up_%s" % upload_id[:16]:
        return None
    if not _valid_source_id(source_id) or not device_id:
        return None
    if fps <= 0 or frames <= 0 or width <= 0 or height <= 0:
        return None
    if anchor_ts_ms < 10 ** 12 or anchor_ts_ms >= 10 ** 13:
        return None
    if abs(duration_ms - int(round(frames * 1000.0 / fps))) > _DURATION_TOLERANCE_MS:
        return None

    directory = manifest_path.parent
    video_path = directory / "source.mp4"
    if not video_path.is_file():
        return None
    kpts_path = directory / "source_kpts.jsonl"
    if not kpts_path.is_file():
        kpts_path = directory / "kpts.jsonl"
    vision_path = directory / "vision.json"

    kpts_ok = bool(
        kpts_path.is_file() and kpts_path.name in ("source_kpts.jsonl", "kpts.jsonl")
    )
    transport: List[str] = ["video"]
    if kpts_ok:
        transport.append("keypoints")
    if cv2_available():
        transport.append("frames")

    return CameraSource(
        source_id=source_id,
        device_id=device_id,
        label=_upload_label(str(data.get("location") or ""), state),
        event_type=str(data.get("final_event_type") or "normal"),
        fps=fps,
        frames=frames,
        width=width,
        height=height,
        anchor_ts_ms=anchor_ts_ms,
        duration_ms=duration_ms,
        video=True,
        keypoints=kpts_ok,
        vision=vision_path.is_file(),
        transport=transport,
        demo=False,
        video_path=video_path,
        kpts_path=kpts_path,
        vision_path=vision_path,
        watermark=UPLOAD_WATERMARK,
    )


def scan_uploads(
    base_dir: Optional[Path] = None, now: Optional[int] = None
) -> List[CameraSource]:
    """扫描 `data/raw/uploads/*/upload.json`，只登记 `source.mp4` 存在且未过期的件。"""
    base = Path(base_dir) if base_dir is not None else Path(config.BASE_DIR)
    root = base.joinpath(*UPLOAD_SOURCES_PARTS)
    current = int(now if now is not None else now_ms())
    items: List[CameraSource] = []
    if not root.is_dir():
        return items
    for manifest_path in sorted(root.glob("*/upload.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        source = _upload_source_from_manifest(manifest_path, data, current)
        if source is not None:
            items.append(source)
    items.sort(key=lambda item: item.source_id)
    return items


def invalidate_sources() -> None:
    """上传完成/删除后失效上传源缓存（demo 缓存不动）。"""
    global _UPLOAD_CACHE
    _UPLOAD_CACHE = None
    _KPT_STORE.clear()


def _upload_sources(now: Optional[int] = None) -> List[CameraSource]:
    global _UPLOAD_CACHE
    if _UPLOAD_CACHE is None:
        _UPLOAD_CACHE = scan_uploads(now=now)
    return list(_UPLOAD_CACHE)


def bootstrap(base_dir: Optional[Path] = None) -> List[CameraSource]:
    """首次调用扫描磁盘并落 INFO 日志（幂等；每次启动只记一次「画面源载入」）。"""
    global _BOOTSTRAPPED, _SOURCES, _SOURCES_BASE
    base = Path(base_dir) if base_dir is not None else None
    if _BOOTSTRAPPED and base == _SOURCES_BASE:
        return list(_SOURCES)

    _SOURCES_BASE = base
    mode = source_mode()
    if not camera_enabled():
        _SOURCES = []
        _BOOTSTRAPPED = True
        log.info("画面源载入 %d 条 mode=%s", 0, mode)
        return []
    if mode not in ("demo", "file"):
        _SOURCES = []
        _BOOTSTRAPPED = True
        _warn_once("mode_unsupported:%s" % mode, "mode_unsupported mode=%s", mode)
        log.info("画面源载入 %d 条 mode=%s", 0, mode)
        return []

    _SOURCES = scan_sources(base)
    _BOOTSTRAPPED = True
    log.info("画面源载入 %d 条 mode=%s", len(_SOURCES), mode)
    return list(_SOURCES)


def load_sources() -> List[CameraSource]:
    """取当前源清单：demo 源 + 上传源（未 bootstrap 时惰性完成）。"""
    if not _BOOTSTRAPPED:
        bootstrap()
    return list(_SOURCES) + _upload_sources()


def find_source(source_id: str) -> Optional[CameraSource]:
    target = str(source_id or "")
    for source in load_sources():
        if source.source_id == target:
            return source
    return None


def require_source(source_id: str) -> CameraSource:
    source = find_source(source_id)
    if source is None:
        raise CameraError(
            404,
            "CAM_SOURCE_NOT_FOUND",
            "未找到该画面源：%s" % (source_id or ""),
            source_id=str(source_id or ""),
        )
    return source


def sources_payload(now: Optional[int] = None) -> Dict[str, Any]:
    """`GET /api/v1/camera/sources` 的响应体。"""
    items = [source.as_public() for source in load_sources()]
    payload: Dict[str, Any] = {
        "items": items,
        "server_time_ms": int(now if now is not None else now_ms()),
        "mode": source_mode(),
        "watermark": WATERMARK,
    }
    # 正常路径不得凭空多出 degraded；仅空清单才带
    if not items:
        payload["degraded"] = True
    return payload


# ---------------------------------------------------------------- 时间基准（唯一公式）


def frame_ms_of(source: CameraSource) -> int:
    """每帧毫秒（本素材恒 40），整数运算。"""
    return int(round(1000.0 / source.fps))


def wall_ms_of(source: CameraSource, frame: int, offset_ms: int) -> int:
    """素材第 frame 帧对应的墙钟毫秒；上传源用浮点 fps 反算（7.5）。"""
    if not source.demo and source.fps > 0:
        return int(offset_ms) + int(
            round(int(source.anchor_ts_ms) + int(frame) * 1000.0 / float(source.fps))
        )
    return int(offset_ms) + int(source.anchor_ts_ms) + int(frame) * frame_ms_of(source)


def frame_for_alert(source: CameraSource, alert_ts_ms: int, offset_ms: int) -> Tuple[int, bool]:
    """告警墙钟 → 帧号；越界钳到 [0, frames-1] 并返回 clamped=True。

    demo 源用整数帧步长（既有口径）；上传源用浮点 fps 反算（R3-UP-17）。
    """
    delta = int(alert_ts_ms) - int(offset_ms) - int(source.anchor_ts_ms)
    if not source.demo and source.fps > 0:
        frame = int(round(delta * float(source.fps) / 1000.0))
    else:
        frame = int(round(delta / float(frame_ms_of(source))))
    if frame < 0:
        return 0, True
    if frame >= int(source.frames):
        return int(source.frames) - 1, True
    return frame, False


def drift_ms_of(session: CameraSession, now: int) -> int:
    """当前墙钟 − 按会话推算的应播时刻（无源时 0）。"""
    source = find_source(session.source_id)
    if source is None:
        return 0
    if not source.demo and source.fps > 0:
        frame = int(
            round(
                (int(now) - session.offset_ms - int(source.anchor_ts_ms))
                * float(source.fps)
                / 1000.0
            )
        )
    else:
        step = frame_ms_of(source)
        frame = int((int(now) - session.offset_ms - source.anchor_ts_ms) // step)
    if frame < 0:
        frame = 0
    if frame >= source.frames:
        frame = source.frames - 1
    return int(now) - wall_ms_of(source, frame, session.offset_ms)


# ---------------------------------------------------------------- 会话（单一活动 + TTL）


def session_start(source_id: str, start_wall_ms: int, now: Optional[int] = None) -> CameraSession:
    """建立会话；同一时刻只允许 1 个活动会话（先清空再写入）。"""
    source = require_source(source_id)
    current = int(now if now is not None else now_ms())
    wall = int(start_wall_ms)
    offset_ms = wall - int(source.anchor_ts_ms)
    _SESSIONS.clear()
    session = CameraSession(
        session_id="%s-%d" % (source.source_id, current),
        source_id=source.source_id,
        start_wall_ms=wall,
        offset_ms=offset_ms,
        updated_ms=current,
    )
    _SESSIONS[source.source_id] = session
    log.info("会话建立 source=%s offset=%s", source.source_id, offset_ms)
    return session


def session_get(source_id: str, now: Optional[int] = None) -> Optional[CameraSession]:
    """取会话；超过 TTL 惰性过期并只记一次 INFO。"""
    target = str(source_id or "")
    current = int(now if now is not None else now_ms())
    session = _SESSIONS.get(target)
    if session is None:
        return None
    if current - session.updated_ms >= SESSION_TTL_MS:
        _SESSIONS.pop(target, None)
        log.info("会话过期 source=%s", target)
        return None
    return session


def session_stop(source_id: str, now: Optional[int] = None) -> Optional[CameraSession]:
    session = session_get(source_id, now)
    _SESSIONS.pop(str(source_id or ""), None)
    return session


def session_heartbeat(source_id: str, now: Optional[int] = None) -> Optional[CameraSession]:
    target = str(source_id or "")
    current = int(now if now is not None else now_ms())
    session = session_get(target, current)
    if session is None:
        return None
    refreshed = CameraSession(
        session_id=session.session_id,
        source_id=session.source_id,
        start_wall_ms=session.start_wall_ms,
        offset_ms=session.offset_ms,
        updated_ms=current,
    )
    _SESSIONS[target] = refreshed
    return refreshed


def session_count() -> int:
    """当前活动会话条数（服务端恒 ≤1；测试用）。"""
    return len(_SESSIONS)


def alignment(source_id: str, now: Optional[int] = None) -> CameraAlignment:
    """对齐状态；上传源无会话时隐式对齐（D-UP-05），demo 源保持 aligned=False。"""
    current = int(now if now is not None else now_ms())
    session = session_get(source_id, current)
    if session is None:
        source = find_source(source_id)
        if source is not None and not source.demo:
            return CameraAlignment(
                source_id=str(source_id or ""), aligned=True, offset_ms=0, drift_ms=0
            )
        return CameraAlignment(
            source_id=str(source_id or ""), aligned=False, offset_ms=0, drift_ms=0
        )
    return CameraAlignment(
        source_id=session.source_id,
        aligned=True,
        offset_ms=int(session.offset_ms),
        drift_ms=drift_ms_of(session, current),
    )


# ---------------------------------------------------------------- 视频分发（Range，零解码）


def _video_size(source: CameraSource) -> int:
    path = source.video_path
    if not path.is_file():
        raise CameraError(
            404,
            "CAM_VIDEO_NOT_FOUND",
            "画面文件缺失，仅显示叠加示意：%s" % source.source_id,
            source_id=source.source_id,
        )
    try:
        return int(path.stat().st_size)
    except OSError:
        raise CameraError(
            404,
            "CAM_VIDEO_NOT_FOUND",
            "画面文件缺失，仅显示叠加示意：%s" % source.source_id,
            source_id=source.source_id,
        )


def _range_invalid(size: int) -> Tuple[int, Dict[str, str], int, int]:
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Range": "bytes */%d" % int(size),
    }
    return 416, headers, 0, 0


def resolve_video(
    source: CameraSource, range_header: Optional[str]
) -> Tuple[int, Dict[str, str], int, int]:
    """Range 纯函数：返回 (status, headers, start, length)。

    合法形态：`bytes=a-b` / `bytes=a-` / `bytes=-suffix`；多段、越界、非法一律 416。
    """
    size = _video_size(source)
    headers: Dict[str, str] = {
        "Content-Type": VIDEO_MEDIA_TYPE,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    raw = "" if range_header is None else str(range_header).strip()
    if not raw:
        headers["Content-Length"] = str(size)
        return 200, headers, 0, size
    if not raw.lower().startswith("bytes="):
        return _range_invalid(size)
    spec = raw[len("bytes="):].strip()
    if not spec or "," in spec:
        return _range_invalid(size)
    head, sep, tail = spec.partition("-")
    if not sep:
        return _range_invalid(size)
    if size <= 0:
        return _range_invalid(size)

    if head.strip() == "":
        try:
            suffix = int(tail.strip())
        except ValueError:
            return _range_invalid(size)
        if suffix <= 0:
            return _range_invalid(size)
        start = 0 if suffix > size else size - suffix
        end = size - 1
    else:
        try:
            start = int(head.strip())
        except ValueError:
            return _range_invalid(size)
        if start < 0 or start >= size:
            return _range_invalid(size)
        if tail.strip() == "":
            end = size - 1
        else:
            try:
                end = int(tail.strip())
            except ValueError:
                return _range_invalid(size)
        if end < start:
            return _range_invalid(size)
        if end >= size:
            end = size - 1

    length = end - start + 1
    headers["Content-Length"] = str(length)
    headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    return 206, headers, start, length


def iter_video_chunks(
    path: Path, start: int, length: int, chunk_size: int = VIDEO_CHUNK_SIZE
) -> Iterator[bytes]:
    """按需分块读文件；不一次读全文件、不 import cv2。"""
    remaining = int(length)
    if remaining <= 0:
        return
    with open(str(path), "rb") as handle:
        handle.seek(int(start))
        while remaining > 0:
            chunk = handle.read(min(int(chunk_size), remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def video_payload(
    source_id: str, range_header: Optional[str]
) -> Tuple[int, Dict[str, str], Path, int, int]:
    """`GET /api/v1/camera/video` 的服务层入口（校验源 → Range 判定）。"""
    source = require_source(source_id)
    status, headers, start, length = resolve_video(source, range_header)
    return status, headers, source.video_path, start, length


# ---------------------------------------------------------------- 关键点与视觉分数


def bbox_from_points(
    points: Any, conf_thr: float = 0.15, pad_x: float = 0.08, pad_y: float = 0.04
) -> List[float]:
    """纯 Python 复刻 `vision/geometry.py:129 bbox_from_keypoints`（同口径）。"""
    xs: List[float] = []
    ys: List[float] = []
    for point in points or []:
        if not isinstance(point, (list, tuple)) or len(point) < 3:
            continue
        try:
            conf = float(point[2])
        except (TypeError, ValueError):
            continue
        if conf < conf_thr:
            continue
        try:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        except (TypeError, ValueError):
            continue
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    width = max(4.0, x2 - x1)
    height = max(4.0, y2 - y1)
    return [
        x1 - pad_x * width,
        y1 - pad_y * height,
        x2 + pad_x * width,
        y2 + pad_y * height,
    ]


def _parse_persons(raw: Dict[str, Any]) -> List[KptPerson]:
    """`persons` 为 17×[x,y,conf] 三层嵌套；`scores` 取同名下标；`labels` 忽略。"""
    persons_raw = raw.get("persons")
    scores_raw = raw.get("scores")
    people: List[KptPerson] = []
    if not isinstance(persons_raw, list):
        return people
    for index, points in enumerate(persons_raw):
        if not isinstance(points, list) or len(points) != 17:
            _warn_once(
                "bad_person:%s:%s" % (raw.get("frame"), index),
                "camera_kpt_person_skipped frame=%s reason=bad_length",
                raw.get("frame"),
            )
            continue
        keypoints: List[List[float]] = []
        bad = False
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 3:
                bad = True
                break
            try:
                keypoints.append([float(point[0]), float(point[1]), float(point[2])])
            except (TypeError, ValueError):
                bad = True
                break
        if bad:
            _warn_once(
                "bad_point:%s:%s" % (raw.get("frame"), index),
                "camera_kpt_person_skipped frame=%s reason=bad_point",
                raw.get("frame"),
            )
            continue
        score = 1.0
        if isinstance(scores_raw, list) and index < len(scores_raw):
            try:
                score = float(scores_raw[index])
            except (TypeError, ValueError):
                score = 1.0
        people.append(
            KptPerson(keypoints=keypoints, score=score, bbox=bbox_from_points(keypoints))
        )
    return people


def _vision_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """视觉分数六字段直通（不重算、不补 0）。"""
    return {name: raw[name] for name in VISION_FIELDS if name in raw}


def frame_ts_of(source: CameraSource, index: int) -> int:
    """帧号 → 素材时间；上传源用浮点 fps（R3-UP-17），demo 源走整数步长。"""
    if not source.demo and source.fps > 0:
        return int(
            round(int(source.anchor_ts_ms) + int(index) * 1000.0 / float(source.fps))
        )
    return int(source.anchor_ts_ms) + int(index) * frame_ms_of(source)


class KptStore:
    """按 `source_id` 缓存整源侧车（首次逐行解析，后续 O(1) 切片）。"""

    def __init__(self) -> None:
        self._frames: Dict[str, List[KptFrame]] = {}
        self._vision: Dict[str, List[Optional[Dict[str, Any]]]] = {}

    def clear(self) -> None:
        self._frames.clear()
        self._vision.clear()

    def _load_vision(self, source: CameraSource) -> List[Optional[Dict[str, Any]]]:
        cached = self._vision.get(source.source_id)
        if cached is not None:
            return cached
        rows: List[Optional[Dict[str, Any]]] = []
        if source.vision_path.is_file():
            try:
                raw = json.loads(source.vision_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, list):
                for row in raw:
                    rows.append(_vision_row(row) if isinstance(row, dict) else None)
        self._vision[source.source_id] = rows
        return rows

    def frames(self, source: CameraSource) -> List[KptFrame]:
        cached = self._frames.get(source.source_id)
        if cached is not None:
            return cached

        vision_rows = self._load_vision(source)
        step = frame_ms_of(source)
        parsed: Dict[int, KptFrame] = {}
        if source.kpts_path.is_file():
            try:
                handle = open(str(source.kpts_path), "r", encoding="utf-8")
            except OSError:
                handle = None
            if handle is not None:
                with handle:
                    for line in handle:
                        text = line.strip()
                        if not text:
                            continue
                        try:
                            raw = json.loads(text)
                        except ValueError:
                            continue
                        if not isinstance(raw, dict):
                            continue
                        try:
                            frame = int(raw.get("frame"))
                        except (TypeError, ValueError):
                            continue
                        if frame < 0 or frame >= source.frames:
                            continue
                        parsed[frame] = KptFrame(
                            frame=frame,
                            ts=frame_ts_of(source, frame),
                            persons=_parse_persons(raw),
                            vision=vision_rows[frame] if frame < len(vision_rows) else None,
                        )

        items: List[KptFrame] = []
        for index in range(int(source.frames)):
            hit = parsed.get(index)
            if hit is None:
                items.append(
                    KptFrame(
                        frame=index,
                        ts=frame_ts_of(source, index),
                        persons=[],
                        vision=vision_rows[index] if index < len(vision_rows) else None,
                    )
                )
            else:
                items.append(hit)
        self._frames[source.source_id] = items
        return items


_KPT_STORE = KptStore()


def kpt_store() -> KptStore:
    return _KPT_STORE


def _parse_int_param(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise CameraError(
            400, "CAM_BAD_REQUEST", "画面请求参数有误：%s 需为整数" % name, param=name
        )
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        raise CameraError(
            400, "CAM_BAD_REQUEST", "画面请求参数有误：%s 需为整数" % name, param=name
        )


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip() != ""


def kpts_payload(
    source_id: str,
    from_frame: Any = None,
    count: Any = None,
    ts: Any = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """`GET /api/v1/camera/kpts` 的响应体（手写校验，绝不 422）。"""
    source = require_source(source_id)
    total = int(source.frames)
    real_count = _parse_int_param(count, "count", DEFAULT_WINDOW_COUNT)
    if real_count > MAX_WINDOW_COUNT:
        raise CameraError(
            400,
            "CAM_WINDOW_TOO_LARGE",
            "请求帧数过多（上限 %d）" % MAX_WINDOW_COUNT,
            count=real_count,
        )
    if real_count < 1:
        raise CameraError(
            400, "CAM_BAD_REQUEST", "画面请求参数有误：count 必须 ≥1", count=real_count
        )

    state = alignment(source.source_id, now)
    clamped = False
    if _has_value(from_frame):
        index = _parse_int_param(from_frame, "from_frame", 0)
    elif _has_value(ts):
        alert_ts = _parse_int_param(ts, "ts", 0)
        if not state.aligned:
            raise CameraError(
                409,
                "CAM_NOT_ALIGNED",
                "画面未对齐（尚未开始播放）",
                source_id=source.source_id,
            )
        index, clamped = frame_for_alert(source, alert_ts, state.offset_ms)
    else:
        index = 0

    if index < 0:
        index = 0
        clamped = True
    if index >= total:
        index = total - 1
        clamped = True
    if real_count > total - index:
        real_count = total - index
        clamped = True

    frames = kpt_store().frames(source)[index: index + real_count]
    return {
        "source_id": source.source_id,
        "fps": float(source.fps),
        "frame_ms": frame_ms_of(source),
        "total_frames": total,
        "from_frame": index,
        "count": real_count,
        "clamped": clamped,
        "aligned": bool(state.aligned),
        "offset_ms": int(state.offset_ms),
        "items": [frame.as_public() for frame in frames],
    }


# ---------------------------------------------------------------- 静态帧降级（cv2 懒加载）


def frame_decode_count() -> int:
    """进程内解码次数（仅供测试观测，绝不出现在任何响应/日志）。"""
    return int(_DECODE_COUNT)


def frame_cache_size() -> int:
    return len(_FRAME_CACHE)


def _frame_cache_put(key: Tuple[str, int], cached_ms: int, data: bytes) -> None:
    _FRAME_CACHE.pop(key, None)
    _FRAME_CACHE[key] = (int(cached_ms), data)
    while len(_FRAME_CACHE) > FRAME_CACHE_SIZE:
        oldest = next(iter(_FRAME_CACHE))
        _FRAME_CACHE.pop(oldest, None)


def frame_jpeg(source: CameraSource, frame: int) -> Tuple[bytes, int]:
    """解码第 frame 帧为 JPEG；cv2 缺失/编解码不可用抛 FrameUnsupported。"""
    global _DECODE_COUNT
    path = source.video_path
    if not path.is_file():
        raise CameraError(
            404,
            "CAM_VIDEO_NOT_FOUND",
            "画面文件缺失，仅显示叠加示意：%s" % source.source_id,
            source_id=source.source_id,
        )
    try:
        import cv2  # noqa: F401  懒加载：模块顶层零 cv2 依赖
    except Exception as exc:  # noqa: BLE001
        raise FrameUnsupported("cv2 不可用：%s" % type(exc).__name__)

    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(frame))
        ok, image = capture.read()
    finally:
        capture.release()
    if not ok or image is None:
        raise FrameUnsupported("该帧无法解码")
    written, buffer = cv2.imencode(".jpg", image)
    if not written or buffer is None:
        raise FrameUnsupported("imencode 不可用")
    _DECODE_COUNT += 1
    return bytes(buffer.tobytes()), int(frame)


def frame_payload(
    source_id: str, frame: Any = None, ts: Any = None, now: Optional[int] = None
) -> Tuple[bytes, int, int]:
    """`GET /api/v1/camera/frame` 的服务层入口：越界不钳制（400 CAM_BAD_FRAME）。"""
    source = require_source(source_id)
    if not source.video_path.is_file():
        raise CameraError(
            404,
            "CAM_VIDEO_NOT_FOUND",
            "画面文件缺失，仅显示叠加示意：%s" % source.source_id,
            source_id=source.source_id,
        )
    current = int(now if now is not None else now_ms())
    step = frame_ms_of(source)
    if _has_value(frame):
        index = _parse_int_param(frame, "frame", 0)
    elif _has_value(ts):
        ts_value = _parse_int_param(ts, "ts", 0)
        index = int(round((ts_value - int(source.anchor_ts_ms)) / float(step)))
    else:
        raise CameraError(
            400,
            "CAM_BAD_REQUEST",
            "画面请求参数有误：需提供 frame 或 ts",
            source_id=source.source_id,
        )

    if index < 0 or index >= int(source.frames):
        raise CameraError(
            400,
            "CAM_BAD_FRAME",
            "该帧不存在：%d" % index,
            source_id=source.source_id,
            frame=index,
        )

    ts_ms = int(source.anchor_ts_ms) + index * step
    key = (source.source_id, index)
    cached = _FRAME_CACHE.get(key)
    if cached is not None and current - cached[0] < FRAME_CACHE_TTL_MS:
        return cached[1], index, ts_ms

    try:
        data, _ = frame_jpeg(source, index)
    except FrameUnsupported as exc:
        raise CameraError(
            501,
            "CAM_TRANSPORT_UNSUPPORTED",
            "当前环境不支持截帧：%s" % str(exc),
            source_id=source.source_id,
        )
    _frame_cache_put(key, current, data)
    return data, index, ts_ms


# ---------------------------------------------------------------- 自检


def health_payload(now: Optional[int] = None) -> Dict[str, Any]:
    """`GET /api/v1/camera/health`：只读自检，不建会话、不解码、不写盘。"""
    sources = load_sources()
    mode = source_mode()
    current = int(now if now is not None else now_ms())
    active_source: Optional[str] = None
    for source_id in list(_SESSIONS.keys()):
        if session_get(source_id, current) is not None:
            active_source = source_id
            break
    usable = cv2_available()
    degraded = (not sources) or mode not in ("demo", "file") or not usable
    return {
        "sources": len(sources),
        "mode": mode,
        "cv2": bool(usable),
        "frame_cache": len(_FRAME_CACHE),
        "session": {"active": active_source is not None, "source_id": active_source},
        "degraded": bool(degraded),
    }


# ---------------------------------------------------------------- 会话接口契约（T-CAM-006）
# `POST /api/v1/camera/session` 与 `GET /api/v1/camera/session` 的服务层入口。
# 字段与错误码严格取 camera.md 4.2.4 / 4.2.5 / 4.3；状态码由路由层转 HTTPException。

SESSION_EVENTS: Tuple[str, ...] = ("start", "stop", "heartbeat")
DRIFT_WARN_MS = 500


def _wall_ms_13(value: Any) -> Optional[int]:
    """13 位毫秒时间戳（数字或纯数字串）；其余一律 None（交给 CAM_BAD_TIMESTAMP）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if value != int(value):
            return None
        number = int(value)
    else:
        text = str(value if value is not None else "").strip()
        if not text.isdigit():
            return None
        number = int(text)
    if number < 1000000000000 or number > 9999999999999:
        return None
    return number


def session_public(session: Optional[CameraSession]) -> Optional[Dict[str, Any]]:
    """会话对外 5 字段（无会话为 None）。"""
    if session is None:
        return None
    return {
        "session_id": session.session_id,
        "source_id": session.source_id,
        "start_wall_ms": int(session.start_wall_ms),
        "offset_ms": int(session.offset_ms),
        "updated_ms": int(session.updated_ms),
    }


def session_action(payload: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """`POST /api/v1/camera/session`：start 覆盖旧会话 / stop / heartbeat。"""
    data = payload if isinstance(payload, dict) else {}
    source_id = str(data.get("source_id") or "")
    event = str(data.get("event") or "")
    if event not in SESSION_EVENTS:
        raise CameraError(
            400, "CAM_BAD_REQUEST", "画面请求参数有误：event 需为 start/stop/heartbeat", event=event
        )
    require_source(source_id)
    current = int(now if now is not None else now_ms())
    if event == "start":
        wall = _wall_ms_13(data.get("start_wall_ms"))
        if wall is None:
            raise CameraError(
                400,
                "CAM_BAD_TIMESTAMP",
                "起播时间格式异常：start_wall_ms 需为 13 位毫秒",
                source_id=source_id,
            )
        return {"ok": True, "session": session_public(session_start(source_id, wall, current))}
    if event == "stop":
        session_stop(source_id, current)
        return {"ok": True, "session": None}
    session_heartbeat(source_id, current)
    return {"ok": True, "session": session_public(session_get(source_id, current))}


def session_payload(source_id: str, now: Optional[int] = None) -> Dict[str, Any]:
    """`GET /api/v1/camera/session`：对齐状态（无会话时 aligned=False）。"""
    source = require_source(source_id)
    current = int(now if now is not None else now_ms())
    state = alignment(source.source_id, current)
    payload: Dict[str, Any] = {
        "source_id": source.source_id,
        "session": session_public(session_get(source.source_id, current)),
        "aligned": bool(state.aligned),
        "offset_ms": int(state.offset_ms),
        "drift_ms": int(state.drift_ms),
        "server_time_ms": current,
        "expire_sec": int(SESSION_TTL_MS // 1000),
    }
    if abs(int(state.drift_ms)) > DRIFT_WARN_MS:
        payload["warn"] = True
    return payload


# ---------------------------------------------------------------- 测试专用


def reset_state() -> None:
    """test-only：清空源/会话/缓存/解码计数/cv2 探测与 WARN 去重表。"""
    global _BOOTSTRAPPED, _SOURCES, _SOURCES_BASE, _CV2_STATE, _DECODE_COUNT
    global _UPLOAD_CACHE
    _SESSIONS.clear()
    _SOURCES = []
    _SOURCES_BASE = None
    _UPLOAD_CACHE = None
    _BOOTSTRAPPED = False
    _CV2_STATE = None
    _DECODE_COUNT = 0
    _FRAME_CACHE.clear()
    _KPT_STORE.clear()
    _WARNED.clear()