"""把手机/相机拍的原始视频整理成 L1 契约要求的格式。

    python data/scripts/prepare_real_video.py --input data/raw/IMG_1234.MOV --case demo_faint
    python data/scripts/prepare_real_video.py --input x.mp4 --case demo_pry --start 3 --seconds 20 --to-demo

做四件事：
1. 转成 1280x720 / 25fps / H.264（契约 6.2）
2. 写 `<case>_meta.json`（起始毫秒时间戳 + 门框 ROI 名），推理时用它算 ts
3. 抽 12 帧到 data/processed/frames/，方便人工核对/标注
4. --to-demo 时直接放进 common/demo_data/，并提示删掉合成版本的 _kpts.jsonl
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.scripts.video_io import ffmpeg_exe, transcode  # noqa: E402

DEMO_DIR = PROJECT_ROOT / "common" / "demo_data"
RAW_DIR = PROJECT_ROOT / "data" / "raw"
FRAME_DIR = PROJECT_ROOT / "data" / "processed" / "frames"
CST = timezone(timedelta(hours=8))


def extract_frames(video: Path, case: str, count: int = 12) -> list[Path]:
    import cv2

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    saved: list[Path] = []
    if total <= 0:
        cap.release()
        return saved
    step = max(1, total // count)
    for i in range(count):
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(total - 1, i * step))
        ok, frame = cap.read()
        if not ok:
            break
        target = FRAME_DIR / f"{case}_{i:02d}.jpg"
        cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        saved.append(target)
    cap.release()
    return saved


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="整理实拍素材成契约格式")
    parser.add_argument("--input", required=True, help="原始视频（.MOV/.mp4/...）")
    parser.add_argument("--case", required=True, help="目标名字，例 demo_faint（全英文小写）")
    parser.add_argument("--start", type=float, default=0.0, help="从第几秒开始截")
    parser.add_argument("--seconds", type=float, default=20.0, help="截多长，默认 20s")
    parser.add_argument("--roi", default="demo_cabin", help="门框 ROI 名字（先用 calibrate_roi.py 标定）")
    parser.add_argument("--to-demo", action="store_true", help="直接放进 common/demo_data/")
    parser.add_argument("--frames", type=int, default=12, help="抽多少帧用于核对")
    args = parser.parse_args(argv)

    if not ffmpeg_exe():
        raise SystemExit("需要 ffmpeg：pip install imageio-ffmpeg")
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"找不到输入视频：{src}")

    if args.to_demo:
        out = DEMO_DIR / f"{args.case}.mp4"
    else:
        out = RAW_DIR / f"{args.case}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    # ffmpeg 裁剪 + 缩放 + 25fps
    import subprocess

    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(args.start), "-t", str(args.seconds), "-i", str(src),
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,fps=25",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True)

    import cv2

    cap = cv2.VideoCapture(str(out))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()

    start_ts_ms = int(datetime.now(tz=CST).timestamp() * 1000)
    meta = {
        "case": args.case,
        "scene": "real",
        "fps": round(fps, 3),
        "frames": frames,
        "width": width,
        "height": height,
        "duration_sec": round(frames / fps, 2) if fps else None,
        "start_ts_ms": start_ts_ms,
        "door_roi": args.roi,
        "source": src.name,
        "summary": f"实拍素材：{args.case}（源自 {src.name}，截取 {args.start}s 起 {args.seconds}s）",
    }
    meta_path = out.with_name(f"{out.stem}_meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    saved = extract_frames(out, args.case, args.frames)
    print(f"[ok] {out}  {width}x{height} @ {fps:.2f}fps  {frames} 帧")
    print(f"     meta -> {meta_path.name}")
    print(f"     抽样帧 -> {len(saved)} 张（data/processed/frames/）")

    if args.to_demo:
        stale = out.with_name(f"{out.stem}_kpts.jsonl")
        if stale.exists():
            stale.unlink()
            print(f"     已删除合成轨迹 {stale.name}（现在会走 YOLO-Pose 真推理）")
        label = DEMO_DIR / f"{out.stem}_label_hint.txt"
        label.write_text(
            "录完请到这里补场景级标签：data/labels/%s.json（event_type / urgency / trapped）\n" % args.case,
            encoding="utf-8",
        )
    print("\n下一步：python data/scripts/validate_demo_data.py --case %s" % args.case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())