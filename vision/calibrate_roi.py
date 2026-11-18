"""门框 ROI 标定工具：换一台电梯/换一个机位，跑一次这个脚本就行。

为什么要单独标定：门缝位置是**几何**信息，和电梯、机位强相关，
写死在代码里既不准也不好维护。任务卡要求"用配置文件里的矩形坐标（人工标定）"。

用法（有图形界面）：
    python vision/calibrate_roi.py --video data/raw/elev_cam.mp4 --name elev_a
    1) 弹出第一帧，用鼠标框住门板（含门框）→ 回车
    2) 在新的窗口里点一下"门缝中线" → 回车保存
    3) 结果写到 vision/configs/rois/elev_a.json，并生成一张预览图

用法（无图形界面 / 在云上）：
    python vision/calibrate_roi.py --video x.mp4 --name elev_a --roi 940,94,288,516 --seam 1084
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vision.config import RESULTS_DIR, DoorRoi, list_rois, resolve_path, save_roi  # noqa: E402
from vision.sources import VideoReader  # noqa: E402


def grab_frame(video: Path, index: int):
    reader = VideoReader(video, limit=index + 1)
    frame = None
    try:
        for i, image in reader.frames():
            if i == index:
                frame = image
                break
    finally:
        reader.close()
    if frame is None:
        raise RuntimeError(f"取不到第 {index} 帧：{video}")
    return frame


def _parse_rect(text: str) -> Tuple[int, int, int, int]:
    parts = [int(float(p)) for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--roi 需要 x,y,w,h")
    return parts[0], parts[1], parts[2], parts[3]


def interactive(frame, name: str) -> DoorRoi:
    import cv2

    print("① 用鼠标框住门板/门框区域，然后按回车")
    rect = cv2.selectROI(f"1/2 door roi - {name}", frame, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    x, y, w, h = [int(v) for v in rect]
    if w <= 2 or h <= 2:
        raise SystemExit("没有选中区域，已退出")

    state = {"seam": x + w // 2}
    preview = frame.copy()

    def redraw():
        canvas = frame.copy()
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.line(canvas, (state["seam"], y), (state["seam"], y + h), (0, 255, 255), 2)
        cv2.putText(
            canvas,
            f"seam_x={state['seam']}  (click to set, ENTER to save, ESC to cancel)",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA,
        )
        cv2.imshow(f"2/2 door seam - {name}", canvas)
        preview[:] = canvas

    def on_mouse(event, cx, cy, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["seam"] = int(cx)
            redraw()

    print("② 在窗口里点一下门缝中线，回车保存，ESC 取消")
    cv2.namedWindow(f"2/2 door seam - {name}")
    cv2.setMouseCallback(f"2/2 door seam - {name}", on_mouse)
    redraw()
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10):
            break
        if key == 27:
            cv2.destroyAllWindows()
            raise SystemExit("已取消")
    cv2.destroyAllWindows()
    height, width = frame.shape[:2]
    return DoorRoi(roi=(x, y, w, h), seam_x=int(state["seam"]), frame_size=(width, height), name=name)


def draw_preview(frame, roi: DoorRoi, out_path: Path) -> Path:
    import cv2

    canvas = frame.copy()
    x, y, w, h = roi.roi
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 200, 255), 2)
    near = int(roi.seam_x - 0.28 * w)
    cv2.rectangle(canvas, (near, y), (int(roi.seam_x + 0.28 * w), y + h), (80, 80, 40), 1)
    for yy in range(y, y + h, 18):
        cv2.line(canvas, (roi.seam_x, yy), (roi.seam_x, min(yy + 9, y + h)), (60, 210, 240), 2)
    cv2.putText(canvas, f"door roi + seam_x={roi.seam_x}", (20, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 220, 255), 2, cv2.LINE_AA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), canvas)
    return out_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="门框 ROI 标定")
    parser.add_argument("--video", required=True)
    parser.add_argument("--name", required=True, help="ROI 名字，保存为 vision/configs/rois/<name>.json")
    parser.add_argument("--frame", type=int, default=30, help="用第几帧来标定（默认 30）")
    parser.add_argument("--roi", type=_parse_rect, default=None, help="无界面模式：x,y,w,h")
    parser.add_argument("--seam", type=int, default=None, help="无界面模式：门缝 x")
    parser.add_argument("--preview", default=None, help="预览图输出路径")
    args = parser.parse_args(argv)

    video = resolve_path(args.video)
    frame = grab_frame(video, args.frame)
    height, width = frame.shape[:2]

    if args.roi and args.seam is not None:
        roi = DoorRoi(roi=args.roi, seam_x=args.seam, frame_size=(width, height), name=args.name)
    else:
        roi = interactive(frame, args.name)

    saved = save_roi(roi, args.name)
    preview = resolve_path(args.preview) if args.preview else RESULTS_DIR / "figures" / f"roi_{args.name}.png"
    draw_preview(frame, roi, preview)
    print(f"[roi] 保存到 {saved}")
    print(f"[roi] 预览图 {preview}")
    print(f"[roi] 现有 ROI：{', '.join(list_rois())}")
    print(f"[roi] 推理时用 --roi {args.name} 或在 configs/default.json 里改 door.roi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())