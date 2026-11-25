"""命令行投放/上传真实视频（P1，REQ-UP-005）。

用法（项目根执行）：
  python scripts/upload_video.py --video D:\\cabin.mp4 --start-ts-ms 1763300180000
  python scripts/upload_video.py --video cabin.mp4 --start-ts-ms 1763300180000 --process

`--process` 同步跑 extract → judge（不写数据库）；HTTP 入口仍走 worker 线程。
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import service.upload as up  # noqa: E402
import service.upload_worker as worker  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="投放/上传真实电梯视频")
    parser.add_argument("--video", required=True, help="本地 mp4 路径")
    parser.add_argument("--device-id", default="ELEV-3F-001")
    parser.add_argument("--start-ts-ms", required=True, help="录制起始时间（13 位毫秒）")
    parser.add_argument("--location", default="3F")
    parser.add_argument("--door-roi", default="", help="x1,y1,x2,y2")
    parser.add_argument("--name", default="upload.mp4")
    parser.add_argument("--process", action="store_true", help="同步处理并输出判决")
    args = parser.parse_args(argv)

    video = Path(args.video)
    if not video.is_file():
        print("video not found: %s" % video)
        return 2
    data = video.read_bytes()
    headers = {
        "content-type": "video/mp4",
        "content-length": str(len(data)),
        "x-upload-device-id": args.device_id,
        "x-upload-start-ts-ms": str(args.start_ts_ms),
        "x-upload-location": args.location,
        "x-upload-name": args.name,
    }
    if args.door_roi:
        headers["x-upload-door-roi"] = args.door_roi
    try:
        intent = up.parse_upload_headers(headers)
        sink = up.UploadSink(intent, base_dir=BASE_DIR)
        for offset in range(0, len(data), up.chunk_size()):
            sink.feed(data[offset: offset + up.chunk_size()])
        result = sink.finish()
    except up.UploadError as exc:
        print(json.dumps({"ok": False, "detail": exc.detail()}, ensure_ascii=False))
        return 2

    print(json.dumps(result.body, ensure_ascii=False, indent=2))
    if not args.process:
        return 0

    manager = worker.UploadTaskManager(BASE_DIR)
    task_id = up.task_id_of(result.record)
    deadline = time.monotonic() + float(getattr(__import__("service.config", fromlist=["config"]), "UPLOAD_TASK_TIMEOUT_SEC", 600))
    record = manager._load_record(result.record.upload_id)
    record = manager.run_extract(record, task_id, deadline)
    record, decisions = manager.run_judge(record, task_id, deadline)
    manager.run_report(record, decisions, task_id)
    final = decisions[-1] if decisions else None
    print(
        json.dumps(
            {
                "task_id": task_id,
                "state": "done",
                "final_event_type": final.event_type if final else "normal",
                "trapped": bool(final and final.trapped),
                "urgency": int(final.urgency) if final else 0,
                "people_count": int(final.people_count) if final else 0,
                "reason": list(final.reason) if final else [],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())