"""用笔记本摄像头录一段"实拍演示素材"（D1 就能做的事）。

为什么提供这个：合成数据只能证明链路通，**真实素材才是加分项**。
找一间能进人的电梯（或就在平地上模拟轿厢构图），按下面 5 个场景各录 20 秒：

    python data/scripts/capture_session.py --case demo_normal  --seconds 20
    python data/scripts/capture_session.py --case demo_estop   --seconds 20
    python data/scripts/capture_session.py --case demo_trapped --seconds 20
    python data/scripts/capture_session.py --case demo_pry     --seconds 20
    python data/scripts/capture_session.py --case demo_faint   --seconds 20

录完自动转成契约要求的 1280x720 / 25fps / H.264，写到：
    data/raw/<case>.mp4             （原始素材，不进 Git）
    data/raw/<case>_label.json      （场景级标签草稿，录完自己补一句描述）
再把 data/raw/<case>.mp4 拷成 common/demo_data/<case>.mp4 覆盖合成版本即可，
推理会自动改用 YOLO-Pose（因为此时没有 <case>_kpts.jsonl 了）。

无图形界面（服务器上）就加 --no-preview，仅录不预览。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RAW_DIR = PROJECT_ROOT / "data" / "raw"
CST = timezone(timedelta(hours=8))

# 每个场景的"拍摄脚本"，录制时叠在画面上给拍摄者看
SHOOT_CARD = {
    "demo_normal": [
        "1-6s   正常站立或走动，自然看手机",
        "6-12s  中途蹲下捡一次东西（很重要，验证不误报）",
        "12-18s 起身、走向门口、离开画面",
    ],
    "demo_estop": [
        "全程 1 人站立，正对镜头",
        "第 4 秒时晃一下身体（想象电梯猛停）",
        "不要倒地，站稳继续站着",
    ],
    "demo_trapped": [
        "全程 2 人在轿厢内，门保持关闭",
        "来回踱步、按按钮、看手机",
        "至少一人蹲下看一次地面（验证不误报）",
    ],
    "demo_pry": [
        "一人走到门边，双手贴住门缝反复推 3 次",
        "每次保持 1 秒以上（贴门判据要求持续 >1s）",
        "另一人蹲在角落抱头",
    ],
    "demo_faint": [
        "两人先正常站立说话",
        "第 7 秒左右一人慢慢倒地并躺平（垫软垫）",
        "另一人走上去蹲下查看",
    ],
}

CASE_ORDER = list(SHOOT_CARD)


def capture(case: str, seconds: float, camera: int, preview: bool, fps: float = 25.0) -> Path:
    import cv2

    from data.scripts.video_io import H264Writer, transcode

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(camera)
    if not cap.isOpened():
        raise SystemExit(f"打不开摄像头 {camera}（被占用？换 --camera 1 试试）")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    temp_path = RAW_DIR / f"{case}_capture.mp4"
    out_path = RAW_DIR / f"{case}.mp4"

    card = SHOOT_CARD.get(case, ["自由拍摄"])
    total = int(round(seconds * src_fps))
    print(f"[capture] {case}：{seconds:.0f}s @ {src_fps:.0f}fps，共约 {total} 帧")
    for line in card:
        print("   ", line)

    temp_writer = cv2.VideoWriter(
        str(temp_path), cv2.VideoWriter_fourcc(*"mp4v"), src_fps, (1280, 720)
    )
    captured = 0
    started = time.perf_counter()
    try:
        while captured < total:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.resize(frame, (1280, 720))
            if preview:
                remaining = seconds - (time.perf_counter() - started)
                canvas = frame.copy()
                cv2.rectangle(canvas, (0, 0), (1280, 96), (20, 20, 24), -1)
                cv2.putText(canvas, f"{case}   {max(0, remaining):4.1f}s", (18, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (60, 220, 250), 2, cv2.LINE_AA)
                cv2.putText(canvas, card[int((time.perf_counter() - started) / max(1e-3, seconds) * len(card)) % len(card)][:70],
                            (18, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (235, 235, 240), 1, cv2.LINE_AA)
                cv2.imshow("capture (q to stop)", canvas)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            temp_writer.write(frame)
            captured += 1
    finally:
        temp_writer.release()
        cap.release()
        if preview:
            cv2.destroyAllWindows()

    print(f"[capture] 录到 {captured} 帧，转码成契约格式 ...")
    transcode(temp_path, out_path, fps=fps, size=(1280, 720))
    temp_path.unlink(missing_ok=True)

    start_ts_ms = int(datetime.now(tz=CST).timestamp() * 1000)
    (RAW_DIR / f"{case}_meta.json").write_text(
        json.dumps(
            {
                "case": case,
                "scene": "real",
                "fps": fps,
                "width": 1280,
                "height": 720,
                "start_ts_ms": start_ts_ms,
                "door_roi": "demo_cabin",
                "summary": f"实拍素材：{case}",
                "notes": card,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (RAW_DIR / f"{case}_label.json").write_text(
        json.dumps(
            {
                "case": case,
                "event_type": case.replace("demo_", ""),
                "label_level": "scene",
                "summary": "",
                "recorded_at": datetime.now(tz=CST).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[capture] 完成 -> {out_path.relative_to(PROJECT_ROOT)}")
    print("  下一步：① 用 vision/calibrate_roi.py 标定门框 ROI")
    print("          ② cp 到 common/demo_data/ 覆盖同名文件，并删掉对应的 _kpts.jsonl")
    return out_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="录实拍演示素材")
    parser.add_argument("--case", required=True, choices=CASE_ORDER)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args(argv)
    capture(args.case, args.seconds, args.camera, not args.no_preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())