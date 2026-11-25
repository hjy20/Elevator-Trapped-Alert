"""上传链路测试辅助（不匹配 test_*.py，不会被 discover 当作用例执行）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.upload as up  # noqa: E402


def make_video(path, seconds=4, black=False, size=(640, 360)):
    """现生成 mp4v 视频：默认白块在画面上部移动，black=True 全黑。"""
    import cv2
    import numpy as np

    width, height = size
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 25.0, (width, height))
    if not writer.isOpened():
        raise RuntimeError("VideoWriter 无法创建 mp4（mp4v）")
    total = max(1, int(seconds) * 25)
    try:
        for index in range(total):
            if black:
                frame = np.zeros((height, width, 3), dtype=np.uint8)
            else:
                frame = np.full((height, width, 3), 32, dtype=np.uint8)
                x = 20 + (index * 4) % max(1, width - 140)
                cv2.rectangle(frame, (x, 40), (x + 100, 200), (230, 230, 230), -1)
            writer.write(frame)
    finally:
        writer.release()
    return path


def upload(
    base,
    video,
    device="ELEV-3F-001",
    start_ts=None,
    location="3F",
    content_type="video/mp4",
    declared=None,
):
    data = Path(video).read_bytes()
    headers = {
        "content-type": content_type,
        "content-length": str(declared if declared is not None else len(data)),
        "x-upload-device-id": device,
        "x-upload-start-ts-ms": str(int(start_ts if start_ts is not None else up.now_ms())),
        "x-upload-location": location,
        "x-upload-name": "cabin.mp4",
    }
    intent = up.parse_upload_headers(headers, base_dir=base)
    sink = up.UploadSink(intent, base_dir=base)
    if declared is None:
        step = 65536
        for offset in range(0, len(data), step):
            sink.feed(data[offset: offset + step])
    return sink


def upload_result(base, video, **kwargs):
    return upload(base, video, **kwargs).finish()


def fake_record(base, upload_id="a" * 32, device="ELEV-3F-001", state="received", **overrides):
    now = int(up.now_ms())
    values = {
        "upload_id": upload_id,
        "source_id": up.source_id_of(upload_id),
        "device_id": device,
        "location": "3F",
        "original_name": "cabin.mp4",
        "bytes": 100,
        "sha256": "0" * 64,
        "fps": 25.0,
        "frames": 300,
        "width": 1280,
        "height": 720,
        "duration_ms": 12000,
        "start_ts_ms": now,
        "start_ts_source": "client",
        "door_roi": None,
        "feature_mode": "heuristic",
        "keypoints_ok": False,
        "received_ms": now,
        "expire_ms": now + up.ttl_ms(),
        "state": state,
    }
    values.update(overrides)
    return up.UploadRecord(**values)


def write_source_dir(base, record, source_bytes=b"\x00\x00\x00\x18ftypmp42fake", vision=True):
    directory = up.upload_dir(record.upload_id, base)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / up.SOURCE_NAME).write_bytes(source_bytes)
    if vision:
        (directory / up.VISION_NAME).write_text("[]", encoding="utf-8")
    up.write_manifest(directory / up.MANIFEST_NAME, record)
    return directory