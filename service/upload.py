"""用户上传真实视频 · 契约/存储/接收层（C · T-UP-001~003）。

职责：
  1. 上传头解析（Content-Type/Length、设备、录制起始时间、楼层、门区、文件名）；
  2. 流式落盘（`.tmp/<uuid>/source.part`）+ sha256 流式与复算；
  3. `upload_id`/`source_id` 推导、幂等矩阵、配额与 24 h 清理；
  4. 视频探测（fps/frames/宽高/时长）与门区越界校验；
  5. `upload.json` 侧车原子读写、`/upload/items` 与 `DELETE` 的服务函数。

硬约束（`upload_video_design.md` 2.3 / 0.1）：
  - Python 3.9：注解只用 Optional/List/Dict/Any/Tuple，禁止 PEP 604 与运行期内置泛型；
  - 模块顶层只 import 标准库 + `service.config`；保证
    `python -c "import service.upload"` 在无 fastapi/cv2/numpy 的环境退出码 0；
  - `cv2` 仅在 `probe_video()` 内懒加载；不引 pydantic；
  - 不输出内部绝对路径。
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import config

log = logging.getLogger("elevator.upload")

# ---------------------------------------------------------------- 常量（单一来源）

UPLOAD_ROOT_PARTS: Tuple[str, ...] = ("data", "raw", "uploads")
TMP_DIR_NAME = ".tmp"
SOURCE_NAME = "source.mp4"
MANIFEST_NAME = "upload.json"
VISION_NAME = "vision.json"
DECISIONS_NAME = "decisions.json"
VISION_PART_NAME = "vision.part.jsonl"
WATERMARK = "用户上传 · 非取证"
SIDECAR_SOURCE_CLIENT = "client"
SIDECAR_SOURCE_SIDECAR = "sidecar"

ALLOWED_CONTENT_TYPES: Tuple[str, ...] = ("video/mp4", "application/octet-stream")

# 严格等于 `upload_video.md` 4.3 表的 19 个 code
ERROR_CODES: Tuple[str, ...] = (
    "UP_BAD_REQUEST",
    "UP_BAD_DEVICE_ID",
    "UP_BAD_TIMESTAMP",
    "UP_BAD_ROI",
    "UP_LENGTH_REQUIRED",
    "UP_TOO_LARGE",
    "UP_UNSUPPORTED_TYPE",
    "UP_NOT_DECODABLE",
    "UP_TOO_SHORT",
    "UP_TOO_LONG",
    "UP_NOT_FOUND",
    "UP_TASK_NOT_FOUND",
    "UP_DELETE_FAILED",
    "UP_HASH_MISMATCH",
    "UP_TIME_BASE_INVALID",
    "UP_INTERNAL",
    "UP_BUSY",
    "UP_TIMEOUT",
    "UP_QUOTA_EXCEEDED",
)

_STATES: Tuple[str, ...] = ("received", "extracted", "judged", "reported", "failed")
_DEVICE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,23}$")
_DEMO_DEVICE_ID = "ELEV-DEMO-001"
_DURATION_TOLERANCE_MS = 40
_TS_MIN = 10 ** 12
_TS_MAX = 10 ** 13

_CLEANUP_LOCK = threading.Lock()


def now_ms() -> int:
    """墙钟毫秒（13 位）。"""
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class UploadRecord:
    """`upload.json` 的主体（平铺 JSON 契约）。"""

    upload_id: str
    source_id: str
    device_id: str
    location: str
    original_name: str
    bytes: int
    sha256: str
    fps: float
    frames: int
    width: int
    height: int
    duration_ms: int
    start_ts_ms: int
    start_ts_source: str
    door_roi: Optional[List[int]]
    feature_mode: str
    keypoints_ok: bool
    received_ms: int
    expire_ms: int
    state: str
    roi_source: str = "default"


@dataclass(frozen=True)
class UploadTask:
    """内存任务（可重建，frozen）。"""

    task_id: str
    upload_id: str
    device_id: str
    state: str
    stage: str
    progress: int
    frames_total: int
    frames_done: int
    feature_mode: str
    error: Optional[Dict[str, Any]]
    updated_ms: int
    started_ms: int
    reported_alert_ts: List[int]


@dataclass(frozen=True)
class UploadIntent:
    """头解析结果。"""

    content_length: int
    content_type: str
    device_id: str
    start_ts_ms: int
    start_ts_source: str
    location: str
    door_roi: Optional[List[int]]
    original_name: str
    tmp_dir: Path


@dataclass(frozen=True)
class ProbeInfo:
    """视频探测结果。"""

    fps: float
    frames: int
    width: int
    height: int
    duration_ms: int


@dataclass(frozen=True)
class Quota:
    """配额快照。"""

    bytes_used: int
    bytes_limit: int
    items_used: int
    items_limit: int


@dataclass(frozen=True)
class UploadAcceptResult:
    """`UploadSink.finish()` 的返回值。"""

    record: UploadRecord
    status_code: int
    body: Dict[str, Any]
    created: bool


@dataclass(frozen=True)
class Accepted:
    """`UploadTaskManager.enqueue()` 的返回值。"""

    status_code: int
    body: Dict[str, Any]


class UploadError(Exception):
    """携带契约状态码/错误码的异常；由路由层转成 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.extra = dict(extra)

    def detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


class UploadTimeout(Exception):
    """处理超时；携带已产出的行，便于保留现场。"""

    def __init__(self, rows: List[Any], frames_done: int) -> None:
        super().__init__("UP_TIMEOUT")
        self.rows = list(rows)
        self.frames_done = int(frames_done)


# ---------------------------------------------------------------- 设置读取


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default))
    except (TypeError, ValueError):
        return int(default)


def max_bytes() -> int:
    return _int_setting("UPLOAD_MAX_BYTES", 209715200)


def quota_bytes() -> int:
    return _int_setting("UPLOAD_QUOTA_BYTES", 2147483648)


def quota_items() -> int:
    return _int_setting("UPLOAD_QUOTA_ITEMS", 20)


def ttl_ms() -> int:
    return _int_setting("UPLOAD_TTL_MS", 86400000)


def min_duration_ms() -> int:
    return _int_setting("UPLOAD_MIN_DURATION_MS", 4000)


def max_duration_ms() -> int:
    return _int_setting("UPLOAD_MAX_DURATION_MS", 600000)


def chunk_size() -> int:
    return _int_setting("UPLOAD_CHUNK_SIZE", 262144)


def frame_step() -> int:
    value = _int_setting("UPLOAD_FRAME_STEP", 2)
    return value if value >= 1 else 1


# ---------------------------------------------------------------- 路径


def uploads_root(base_dir: Optional[Path] = None) -> Path:
    base = Path(base_dir) if base_dir is not None else Path(config.BASE_DIR)
    return base.joinpath(*UPLOAD_ROOT_PARTS)


def upload_dir(upload_id: str, base_dir: Optional[Path] = None) -> Path:
    return uploads_root(base_dir) / str(upload_id)


# ---------------------------------------------------------------- 纯函数（头解析）


def parse_ts13(value: Any) -> Optional[int]:
    """13 位毫秒（纯数字串）；10 位、小数、负数一律 None。"""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    number = int(text)
    if number < _TS_MIN or number >= _TS_MAX:
        return None
    return number


def parse_device_id(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or _DEVICE_RE.match(text) is None or text == _DEMO_DEVICE_ID:
        raise UploadError(
            400,
            "UP_BAD_DEVICE_ID",
            "设备编号格式不对（例：ELEV-3F-001）",
            device_id=text,
        )
    return text


def parse_location(value: Any) -> str:
    text = "".join(ch for ch in str(value if value is not None else "") if ch >= " ")
    text = text.strip()
    if len(text) > 8:
        raise UploadError(400, "UP_BAD_REQUEST", "楼层标识过长（最多 8 个字符）", location=text)
    return text or "?"


def parse_door_roi(value: Any) -> Optional[List[int]]:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise UploadError(400, "UP_BAD_ROI", "门区坐标不合法：需 4 个整数", door_roi=text)
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        raise UploadError(400, "UP_BAD_ROI", "门区坐标不合法：需 4 个整数", door_roi=text)
    x1, y1, x2, y2 = numbers
    if min(numbers) < 0 or x1 >= x2 or y1 >= y2:
        raise UploadError(400, "UP_BAD_ROI", "门区坐标不合法：需非负且左上小于右下", door_roi=text)
    return numbers


def sanitize_filename(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    for token in ("/", "\\", ".."):
        text = text.replace(token, "_")
    text = "".join(ch for ch in text if ch >= " ").strip()
    if not text:
        return "upload.mp4"
    return text[:64]


def _headers_lower(headers: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        out[str(key).lower()] = str(value)
    return out


# ---------------------------------------------------------------- 纯函数（时间/标识）


def build_upload_id(device_id: str, file_sha256: str) -> str:
    """D-UP-01：同设备同文件稳定，跨设备不同。"""
    seed = "%s\x00%s" % (str(device_id), str(file_sha256))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def source_id_of(upload_id: str) -> str:
    """`up_` + 前 16 位 = 19 字符（≤20，`[a-z0-9_]`）。"""
    return "up_%s" % str(upload_id)[:16]


def parse_task_id(task_id: str) -> Tuple[str, int]:
    text = str(task_id or "")
    head, sep, tail = text.rpartition("-")
    if not sep or not head or not tail.isdigit():
        raise UploadError(404, "UP_TASK_NOT_FOUND", "处理任务不存在", task_id=text)
    return head, int(tail)


def task_id_of(record: UploadRecord) -> str:
    return "%s-%d" % (record.upload_id, int(record.received_ms))


def ts_of_frame(start_ts_ms: int, fps: float, frame: int) -> int:
    """唯一时间公式：`start + round(frame * 1000 / fps)`。"""
    return int(start_ts_ms) + int(round(int(frame) * 1000.0 / float(fps)))


def frame_of_ts(start_ts_ms: int, fps: float, ts: int) -> int:
    return int(round((int(ts) - int(start_ts_ms)) * float(fps) / 1000.0))


def duration_ms_of(frames: int, fps: float) -> int:
    return int(round(int(frames) * 1000.0 / float(fps)))


def resolve_door_roi(
    door_roi: Optional[List[int]], width: int, height: int
) -> Tuple[List[int], str]:
    """客户端门区合法则用；否则下部 1/3（`roi_source=default`）。"""
    if door_roi and len(door_roi) == 4:
        x1, y1, x2, y2 = [int(v) for v in door_roi]
        if 0 <= x1 < x2 <= int(width) and 0 <= y1 < y2 <= int(height):
            return [x1, y1, x2, y2], SIDECAR_SOURCE_CLIENT
    return [0, int(round(int(height) * 2.0 / 3.0)), int(width), int(height)], "default"


def validate_time_window(
    decisions: List[Any], start_ts_ms: int, duration_ms: int
) -> None:
    """判决 ts 必须落在 `[start-1000, start+duration+1000]`，否则阻断上报。"""
    lower = int(start_ts_ms) - 1000
    upper = int(start_ts_ms) + int(duration_ms) + 1000
    for item in decisions or []:
        ts = int(getattr(item, "ts", item.get("ts") if isinstance(item, dict) else 0))
        if ts < lower or ts > upper:
            raise UploadError(
                500,
                "UP_TIME_BASE_INVALID",
                "时间基准异常，已阻断上报",
                ts=ts,
            )


# ---------------------------------------------------------------- 磁盘读写


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))


def read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def record_from_manifest(data: Mapping[str, Any]) -> Optional[UploadRecord]:
    try:
        state = str(data.get("state") or "")
        if state not in _STATES:
            return None
        door_roi = data.get("door_roi")
        if door_roi is not None:
            if not isinstance(door_roi, list) or len(door_roi) != 4:
                return None
            door_roi = [int(value) for value in door_roi]
        return UploadRecord(
            upload_id=str(data["upload_id"]),
            source_id=str(data["source_id"]),
            device_id=str(data["device_id"]),
            location=str(data.get("location") or "?"),
            original_name=str(data.get("original_name") or "upload.mp4"),
            bytes=int(data["bytes"]),
            sha256=str(data["sha256"]),
            fps=float(data["fps"]),
            frames=int(data["frames"]),
            width=int(data["width"]),
            height=int(data["height"]),
            duration_ms=int(data["duration_ms"]),
            start_ts_ms=int(data["start_ts_ms"]),
            start_ts_source=str(data.get("start_ts_source") or "client"),
            door_roi=door_roi,
            feature_mode=str(data.get("feature_mode") or "heuristic"),
            keypoints_ok=bool(data.get("keypoints_ok", False)),
            received_ms=int(data["received_ms"]),
            expire_ms=int(data["expire_ms"]),
            state=state,
            roi_source=str(data.get("roi_source") or "default"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def manifest_payload(record: UploadRecord, **summary: Any) -> Dict[str, Any]:
    payload = asdict(record)
    payload.update(
        {
            "vision_file": VISION_NAME,
            "decisions_file": DECISIONS_NAME,
            "reported_alert_ids": list(summary.get("reported_alert_ids") or []),
            "reported_alert_ts": list(summary.get("reported_alert_ts") or []),
            "final_event_type": str(summary.get("final_event_type") or "normal"),
            "roi_source": str(summary.get("roi_source") or record.roi_source),
        }
    )
    return payload


def write_manifest(
    path: Path,
    record: UploadRecord,
    **summary: Any
) -> Dict[str, Any]:
    payload = manifest_payload(record, **summary)
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def scan_manifests(
    base_dir: Optional[Path] = None, now: Optional[int] = None, include_expired: bool = False
) -> List[Tuple[Path, Dict[str, Any]]]:
    """扫描 `uploads/*/upload.json`；默认只返回未过期件（按 received_ms 倒序）。"""
    root = uploads_root(base_dir)
    current = int(now if now is not None else now_ms())
    items: List[Tuple[Path, Dict[str, Any]]] = []
    if not root.is_dir():
        return items
    for manifest_path in root.glob("*/" + MANIFEST_NAME):
        data = read_manifest(manifest_path)
        if data is None:
            continue
        try:
            expire = int(data.get("expire_ms") or 0)
            received = int(data.get("received_ms") or 0)
        except (TypeError, ValueError):
            continue
        if not include_expired and expire < current:
            continue
        items.append((manifest_path, data))
    items.sort(
        key=lambda pair: (-int(pair[1].get("received_ms") or 0), str(pair[1].get("upload_id") or ""))
    )
    return items


# ---------------------------------------------------------------- 配额与清理


def quota_snapshot(now: Optional[int] = None, base_dir: Optional[Path] = None) -> Quota:
    used_bytes = 0
    used_items = 0
    for manifest_path, data in scan_manifests(base_dir, now, include_expired=False):
        used_items += 1
        source = manifest_path.parent / SOURCE_NAME
        if source.is_file():
            try:
                used_bytes += int(source.stat().st_size)
            except OSError:
                pass
    return Quota(
        bytes_used=int(used_bytes),
        bytes_limit=quota_bytes(),
        items_used=int(used_items),
        items_limit=quota_items(),
    )


def cleanup_expired(now: Optional[int] = None, base_dir: Optional[Path] = None) -> int:
    """只删过期件的 `source.mp4`，保留派生与 manifest；失败 WARN 不阻断。"""
    current = int(now if now is not None else now_ms())
    with _CLEANUP_LOCK:
        removed = 0
        for manifest_path, data in scan_manifests(base_dir, current, include_expired=True):
            expire = int(data.get("expire_ms") or 0)
            if expire >= current:
                continue
            source = manifest_path.parent / SOURCE_NAME
            if not source.exists():
                continue
            try:
                source.unlink()
                removed += 1
            except OSError as exc:  # noqa: BLE001 - 占用/权限不阻断
                log.warning(
                    "upload_cleanup_failed upload=%s err=%s",
                    data.get("upload_id"),
                    type(exc).__name__,
                )
        if removed:
            log.info("上传清理过期 %d 条", removed)
        return removed


# ---------------------------------------------------------------- 视频探测


def probe_video(path: Path, start_ts_ms: int) -> ProbeInfo:
    """严格读原始 CAP_PROP_*（不复用 video_info 的回退与时间锚点）。"""
    try:
        import cv2  # noqa: F401  懒加载
    except Exception as exc:  # noqa: BLE001
        raise UploadError(
            422,
            "UP_NOT_DECODABLE",
            "当前环境无法解码视频（缺少 cv2）",
            reason=type(exc).__name__,
        )
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise UploadError(422, "UP_NOT_DECODABLE", "视频无法解码，请换 MP4(H.264)", reason="unreadable")
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        except (TypeError, ValueError):
            raise UploadError(422, "UP_NOT_DECODABLE", "视频参数无法读取，请换 MP4(H.264)")
        if frames <= 0:
            frames = 0
            while True:
                ok, _image = capture.read()
                if not ok:
                    break
                frames += 1
    finally:
        capture.release()

    if not (1.0 <= fps <= 60.0) or frames <= 0:
        raise UploadError(422, "UP_NOT_DECODABLE", "视频参数异常，请换 MP4(H.264)", fps=fps, frames=frames)
    if not (0 < width <= 1920 and 0 < height <= 1080):
        raise UploadError(
            422,
            "UP_NOT_DECODABLE",
            "视频分辨率超出范围（≤1920×1080）",
            width=width,
            height=height,
        )
    duration = duration_ms_of(frames, fps)
    if duration < min_duration_ms():
        raise UploadError(422, "UP_TOO_SHORT", "视频太短（至少 4 秒）", duration_ms=duration)
    if duration > max_duration_ms():
        raise UploadError(422, "UP_TOO_LONG", "视频超过 10 分钟", duration_ms=duration)
    if parse_ts13(start_ts_ms) is None:
        raise UploadError(400, "UP_BAD_TIMESTAMP", "录制时间缺失或格式异常")
    return ProbeInfo(
        fps=float(fps),
        frames=int(frames),
        width=int(width),
        height=int(height),
        duration_ms=int(duration),
    )


# ---------------------------------------------------------------- 头解析


def parse_upload_headers(
    headers: Mapping[str, Any], now: Optional[int] = None, base_dir: Optional[Path] = None
) -> UploadIntent:
    """头 → `UploadIntent`；所有失败都抛 `UploadError`，不落盘。"""
    current = int(now if now is not None else now_ms())
    raw = _headers_lower(headers)
    cleanup_expired(current, base_dir)

    if "x-upload-rebase-ts" in raw:
        raise UploadError(400, "UP_BAD_REQUEST", "不支持请求平移录制时间")

    content_type = raw.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadError(
            415,
            "UP_UNSUPPORTED_TYPE",
            "只支持 MP4 视频（Content-Type: video/mp4）",
            content_type=content_type,
        )

    raw_length = raw.get("content-length", "").strip()
    if not raw_length:
        raise UploadError(411, "UP_LENGTH_REQUIRED", "上传需声明文件大小")
    if not raw_length.isdigit():
        raise UploadError(400, "UP_BAD_REQUEST", "上传参数有误：Content-Length 非法")
    length = int(raw_length)
    if length <= 0:
        raise UploadError(400, "UP_BAD_REQUEST", "上传参数有误：文件为空")
    if length > max_bytes():
        raise UploadError(
            413,
            "UP_TOO_LARGE",
            "文件超过 200 MB 上限",
            limit_bytes=max_bytes(),
        )

    device_id = parse_device_id(raw.get("x-upload-device-id"))
    start_ts = parse_ts13(raw.get("x-upload-start-ts-ms"))
    if start_ts is None:
        raise UploadError(400, "UP_BAD_TIMESTAMP", "请提供录制起始时间（13 位毫秒）")
    location = parse_location(raw.get("x-upload-location"))
    door_roi = parse_door_roi(raw.get("x-upload-door-roi"))
    name = sanitize_filename(raw.get("x-upload-name"))

    tmp_dir = uploads_root(base_dir) / TMP_DIR_NAME / uuid.uuid4().hex
    return UploadIntent(
        content_length=int(length),
        content_type=content_type,
        device_id=device_id,
        start_ts_ms=int(start_ts),
        start_ts_source=SIDECAR_SOURCE_CLIENT,
        location=location,
        door_roi=door_roi,
        original_name=name,
        tmp_dir=tmp_dir,
    )


# ---------------------------------------------------------------- 接收（流式落盘）


class UploadSink:
    """流式接收 + 复算 + 幂等 + 配额 + 落盘 + 探测。"""

    def __init__(
        self, intent: UploadIntent, base_dir: Optional[Path] = None, now: Optional[int] = None
    ) -> None:
        self.intent = intent
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.now = int(now if now is not None else now_ms())
        self.part_path = intent.tmp_dir / "source.part"
        self._hasher = hashlib.sha256()
        self._written = 0
        self._head = b""
        self._container_checked = False
        self._closed = False

    # -- 写盘 ---------------------------------------------------------

    def _ensure_dir(self) -> None:
        self.part_path.parent.mkdir(parents=True, exist_ok=True)

    def _check_container(self) -> None:
        if self._container_checked or len(self._head) < 12:
            return
        if self._head[4:8] != b"ftyp":
            raise UploadError(415, "UP_UNSUPPORTED_TYPE", "只支持 MP4 视频", reason="ftyp")
        self._container_checked = True

    def feed(self, chunk: bytes) -> None:
        if self._closed:
            raise UploadError(500, "UP_INTERNAL", "上传流已关闭")
        if not chunk:
            return
        try:
            self._written += len(chunk)
            if self._written > self.intent.content_length:
                raise UploadError(
                    413, "UP_TOO_LARGE", "上传字节超过声明大小", limit_bytes=self.intent.content_length
                )
            if self._written > max_bytes():
                raise UploadError(413, "UP_TOO_LARGE", "文件超过 200 MB 上限", limit_bytes=max_bytes())
            if len(self._head) < 12:
                self._head += bytes(chunk[: 12 - len(self._head)])
                self._check_container()
            self._ensure_dir()
            with open(str(self.part_path), "ab") as handle:
                handle.write(chunk)
            self._hasher.update(chunk)
        except UploadError:
            self.abort()
            raise
        except OSError as exc:
            self.abort()
            raise UploadError(500, "UP_INTERNAL", "文件写入失败", reason=type(exc).__name__)

    def abort(self) -> None:
        self._closed = True
        shutil.rmtree(str(self.intent.tmp_dir), ignore_errors=True)

    # -- 收尾 ---------------------------------------------------------

    def finish(self) -> UploadAcceptResult:
        if self._closed:
            raise UploadError(500, "UP_INTERNAL", "上传流已关闭")
        self._closed = True
        try:
            if self._written != self.intent.content_length:
                raise UploadError(
                    400,
                    "UP_BAD_REQUEST",
                    "上传字节与声明不一致",
                    declared=self.intent.content_length,
                    received=self._written,
                )
            if not self._container_checked:
                self._check_container()
            if not self.part_path.is_file():
                raise UploadError(400, "UP_BAD_REQUEST", "上传参数有误：文件为空")

            digest = self._hash_file(self.part_path)
            if digest != self._hasher.hexdigest():
                raise UploadError(500, "UP_HASH_MISMATCH", "文件写入校验失败")
            upload_id = build_upload_id(self.intent.device_id, digest)
            target_dir = upload_dir(upload_id, self.base_dir)
            manifest_path = target_dir / MANIFEST_NAME

            existing = read_manifest(manifest_path)
            if existing is not None:
                existing_record = record_from_manifest(existing)
                expire = int(existing.get("expire_ms") or 0)
                if existing_record is not None and expire >= self.now and existing.get("state") != "failed":
                    self.abort()
                    body = accept_body(existing_record, _state_for_body(existing_record.state))
                    return UploadAcceptResult(existing_record, 200, body, False)
                if existing_record is not None and expire >= self.now and existing.get("state") == "failed":
                    return self._accept_retry(existing_record, manifest_path, digest)

            quota = quota_snapshot(self.now, self.base_dir)
            if quota.items_used + 1 > quota.items_limit or (
                quota.bytes_used + self._written > quota.bytes_limit
            ):
                raise UploadError(
                    507,
                    "UP_QUOTA_EXCEEDED",
                    "上传空间已满，请先清理",
                    bytes_limit=quota.bytes_limit,
                    items_limit=quota.items_limit,
                )

            target_dir.mkdir(parents=True, exist_ok=True)
            source_path = target_dir / SOURCE_NAME
            os.replace(str(self.part_path), str(source_path))
            shutil.rmtree(str(self.intent.tmp_dir), ignore_errors=True)

            probe = probe_video(source_path, self.intent.start_ts_ms)
            if self.intent.door_roi is not None:
                _x1, _y1, x2, y2 = self.intent.door_roi
                if x2 > probe.width or y2 > probe.height:
                    shutil.rmtree(str(target_dir), ignore_errors=True)
                    raise UploadError(400, "UP_BAD_ROI", "门区坐标超出画面范围")

            roi_source = SIDECAR_SOURCE_CLIENT if self.intent.door_roi else "default"
            feature_mode, keypoints_ok = _feature_mode(self.base_dir)
            record = UploadRecord(
                upload_id=upload_id,
                source_id=source_id_of(upload_id),
                device_id=self.intent.device_id,
                location=self.intent.location,
                original_name=self.intent.original_name,
                bytes=int(self._written),
                sha256=digest,
                fps=probe.fps,
                frames=probe.frames,
                width=probe.width,
                height=probe.height,
                duration_ms=probe.duration_ms,
                start_ts_ms=self.intent.start_ts_ms,
                start_ts_source=self.intent.start_ts_source,
                door_roi=self.intent.door_roi,
                feature_mode=feature_mode,
                keypoints_ok=keypoints_ok,
                received_ms=self.now,
                expire_ms=self.now + ttl_ms(),
                state="received",
                roi_source=roi_source,
            )
            write_manifest(manifest_path, record)
            log.info(
                "上传接收 upload=%s device=%s bytes=%s start_ts_source=%s mode=%s",
                upload_id,
                record.device_id,
                record.bytes,
                record.start_ts_source,
                record.feature_mode,
            )
            return UploadAcceptResult(record, 202, accept_body(record, "queued"), True)
        except Exception:
            self.abort()
            raise

    def _accept_retry(
        self, record: UploadRecord, manifest_path: Path, digest: str
    ) -> UploadAcceptResult:
        """同一失败件重传 → 202 重试（复用目录、清派生、重置时间）。"""
        target_dir = manifest_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(str(self.part_path), str(target_dir / SOURCE_NAME))
        shutil.rmtree(str(self.intent.tmp_dir), ignore_errors=True)
        for name in (VISION_NAME, DECISIONS_NAME, VISION_PART_NAME):
            path = target_dir / name
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        retried = UploadRecord(
            upload_id=record.upload_id,
            source_id=record.source_id,
            device_id=self.intent.device_id,
            location=self.intent.location,
            original_name=self.intent.original_name,
            bytes=int(self._written),
            sha256=digest,
            fps=record.fps,
            frames=record.frames,
            width=record.width,
            height=record.height,
            duration_ms=record.duration_ms,
            start_ts_ms=self.intent.start_ts_ms,
            start_ts_source=self.intent.start_ts_source,
            door_roi=self.intent.door_roi,
            feature_mode=record.feature_mode,
            keypoints_ok=record.keypoints_ok,
            received_ms=self.now,
            expire_ms=self.now + ttl_ms(),
            state="received",
            roi_source=record.roi_source,
        )
        write_manifest(manifest_path, retried)
        log.info("上传重试 upload=%s device=%s", retried.upload_id, retried.device_id)
        return UploadAcceptResult(retried, 202, accept_body(retried, "queued"), True)

    @staticmethod
    def _hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(str(path), "rb") as handle:
            while True:
                chunk = handle.read(chunk_size())
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()


def _feature_mode(base_dir: Optional[Path]) -> Tuple[str, bool]:
    from . import upload_features

    return upload_features.detect_feature_mode(Path(base_dir) if base_dir is not None else None)


def _state_for_body(state: str) -> str:
    return {
        "received": "queued",
        "extracted": "running",
        "judged": "running",
        "reported": "done",
        "failed": "failed",
    }.get(str(state), "queued")


# ---------------------------------------------------------------- 响应体


def accept_body(record: UploadRecord, state: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "upload_id": record.upload_id,
        "source_id": record.source_id,
        "task_id": task_id_of(record),
        "device_id": record.device_id,
        "state": str(state),
        "bytes": int(record.bytes),
        "sha256": record.sha256,
        "probe": {
            "fps": float(record.fps),
            "frames": int(record.frames),
            "width": int(record.width),
            "height": int(record.height),
            "duration_ms": int(record.duration_ms),
        },
        "start_ts_ms": int(record.start_ts_ms),
        "start_ts_source": record.start_ts_source,
        "feature_mode": record.feature_mode,
        "degraded": record.feature_mode == "heuristic",
        "limits": {
            "max_bytes": max_bytes(),
            "max_seconds": int(max_duration_ms() // 1000),
            "min_seconds": int(min_duration_ms() // 1000),
            "ttl_hours": int(ttl_ms() // 3600000),
            "quota_bytes": quota_bytes(),
            "quota_items": quota_items(),
        },
        "watermark": WATERMARK,
    }
    return payload


def item_payload_dict(manifest_path: Path, data: Mapping[str, Any]) -> Dict[str, Any]:
    """`/upload/items` 的单件 19 字段（不输出绝对路径）。"""
    alert_ids = data.get("reported_alert_ids")
    video = (manifest_path.parent / SOURCE_NAME).is_file()
    return {
        "upload_id": str(data.get("upload_id") or ""),
        "source_id": str(data.get("source_id") or ""),
        "device_id": str(data.get("device_id") or ""),
        "location": str(data.get("location") or "?"),
        "original_name": str(data.get("original_name") or "upload.mp4"),
        "bytes": int(data.get("bytes") or 0),
        "duration_ms": int(data.get("duration_ms") or 0),
        "frames": int(data.get("frames") or 0),
        "fps": float(data.get("fps") or 0.0),
        "start_ts_ms": int(data.get("start_ts_ms") or 0),
        "start_ts_source": str(data.get("start_ts_source") or "client"),
        "feature_mode": str(data.get("feature_mode") or "heuristic"),
        "keypoints_ok": bool(data.get("keypoints_ok", False)),
        "state": str(data.get("state") or "received"),
        "received_ms": int(data.get("received_ms") or 0),
        "expire_ms": int(data.get("expire_ms") or 0),
        "alert_ids": list(alert_ids) if isinstance(alert_ids, list) else [],
        "video": bool(video),
        "vision": (manifest_path.parent / VISION_NAME).is_file(),
    }


def items_payload(now: Optional[int] = None, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    current = int(now if now is not None else now_ms())
    items = [
        item_payload_dict(manifest_path, data)
        for manifest_path, data in scan_manifests(base_dir, current, include_expired=False)
    ]
    quota = quota_snapshot(current, base_dir)
    return {
        "items": items,
        "total": len(items),
        "quota": {
            "bytes_used": int(quota.bytes_used),
            "bytes_limit": int(quota.bytes_limit),
            "items_used": int(quota.items_used),
            "items_limit": int(quota.items_limit),
        },
        "server_time_ms": current,
        "watermark": WATERMARK,
    }


def _find_manifest(upload_id: str, now: int, base_dir: Optional[Path]) -> Tuple[Path, Dict[str, Any]]:
    target = str(upload_id or "").strip()
    manifest_path = upload_dir(target, base_dir) / MANIFEST_NAME
    data = read_manifest(manifest_path)
    if data is None or int(data.get("expire_ms") or 0) < int(now):
        raise UploadError(404, "UP_NOT_FOUND", "上传记录不存在或已过期", upload_id=target)
    return manifest_path, data


def item_payload(
    upload_id: str, now: Optional[int] = None, base_dir: Optional[Path] = None
) -> Dict[str, Any]:
    current = int(now if now is not None else now_ms())
    manifest_path, data = _find_manifest(upload_id, current, base_dir)
    return {
        "item": item_payload_dict(manifest_path, data),
        "manifest": {
            "vision_file": str(data.get("vision_file") or VISION_NAME),
            "decisions_file": str(data.get("decisions_file") or DECISIONS_NAME),
            "reported_alert_ids": list(data.get("reported_alert_ids") or []),
            "reported_alert_ts": list(data.get("reported_alert_ts") or []),
            "final_event_type": str(data.get("final_event_type") or "normal"),
        },
    }


def delete_item(
    upload_id: str,
    purge: bool = False,
    now: Optional[int] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """默认只删 `source.mp4`；`purge=True` 删整目录。"""
    from . import camera as camera_svc

    current = int(now if now is not None else now_ms())
    manifest_path, _data = _find_manifest(upload_id, current, base_dir)
    target_dir = manifest_path.parent
    deleted: Dict[str, bool] = {}
    try:
        if purge:
            for name in (SOURCE_NAME, VISION_NAME, DECISIONS_NAME, VISION_PART_NAME, MANIFEST_NAME):
                path = target_dir / name
                existed = path.exists()
                if existed:
                    path.unlink()
                deleted[name] = bool(existed)
            shutil.rmtree(str(target_dir), ignore_errors=True)
        else:
            source = target_dir / SOURCE_NAME
            existed = source.exists()
            if existed:
                source.unlink()
            deleted[SOURCE_NAME] = bool(existed)
            deleted[VISION_NAME] = False
    except OSError as exc:
        raise UploadError(409, "UP_DELETE_FAILED", "删除失败，请稍后重试", reason=type(exc).__name__)

    camera_svc.invalidate_sources()
    return {"ok": True, "upload_id": str(upload_id), "deleted": deleted}