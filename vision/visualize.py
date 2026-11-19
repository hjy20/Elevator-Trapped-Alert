"""带标注视频：C 做演示视频要用的素材，也是答辩现场最直观的一屏。

画什么（每一处都能在技术文档里对应上）：
* 每个人一根骨架：正常=青绿，疑似倒地=红色，蹲下=黄（说明被规则放过了）
* 门框 ROI：白框 + 门缝虚线；手落进"贴门"范围时手腕画黄点并画出到门缝的连线
* 左上角 HUD：人数 / fall_prob / door_approach / crowded / keypoints_ok（字号按 720p 放大）
* 底部：fall_prob 的滚动曲线（评委一眼能看出"3 秒持续"这件事）
* 进度条与时间码

用法：
    python vision/visualize.py --case demo_faint
    python vision/visualize.py --all
    python vision/visualize.py --case demo_pry --sheet results/figures/pry_sheet.png
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path
from typing import Deque, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from vision.config import DEMO_DIR, RESULTS_DIR, VisionConfig, load_config, resolve_path  # noqa: E402
from vision.geometry import (  # noqa: E402
    FACE_IDX,
    L_ANK,
    L_HIP,
    L_KNE,
    L_SHO,
    L_WRI,
    NOSE,
    R_ANK,
    R_HIP,
    R_KNE,
    R_SHO,
    R_WRI,
    SKELETON_EDGES,
    Person,
    fall_features,
    mean_conf,
    visible_mask,
)
from vision.rules import VisionEngine  # noqa: E402
from vision.sources import iter_frames, make_backend  # noqa: E402

CASE_ORDER = ["demo_normal", "demo_estop", "demo_trapped", "demo_pry", "demo_faint"]

_COLOR_OK = (120, 220, 90)        # 正常骨架
_COLOR_FALL = (60, 60, 235)       # 疑似倒地
_COLOR_CROUCH = (60, 200, 230)    # 蹲下（被规则放过）
_COLOR_DOOR = (240, 210, 60)      # 贴门
_COLOR_HUD_BG = (28, 28, 32)


def _bgr(color: Tuple[int, int, int], factor: float) -> Tuple[int, int, int]:
    return tuple(int(max(0, min(255, c * factor))) for c in color)


class HudPainter:
    """把 VisionOut 画到画面上；字号/位置全部按画面尺寸等比缩放。"""

    def __init__(self, width: int, height: int) -> None:
        self.w = width
        self.h = height
        self.s = max(0.75, width / 1280.0)
        self.trail: Deque[float] = deque(maxlen=160)

    def _put(self, img, text, org, color, scale=0.9, thickness=2):
        import cv2

        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale * self.s, color, max(1, int(thickness * self.s)), cv2.LINE_AA)

    def draw(self, img, vision, door_roi, frame_index: int, total: int) -> np.ndarray:
        import cv2

        s = self.s
        # ---- 左上 HUD ----
        box_w, box_h = int(430 * s), int(196 * s)
        overlay = img.copy()
        cv2.rectangle(overlay, (int(16 * s), int(16 * s)), (int(16 * s) + box_w, int(16 * s) + box_h), _COLOR_HUD_BG, -1)
        img[:] = cv2.addWeighted(overlay, 0.62, img, 0.38, 0)
        cv2.rectangle(img, (int(16 * s), int(16 * s)), (int(16 * s) + box_w, int(16 * s) + box_h), (90, 90, 96), 2)

        lines = [
            (f"PEOPLE   {vision.person_count}", (235, 235, 240)),
            (f"FALL     {vision.fall_prob:.2f}", _COLOR_FALL if vision.fall_prob > 0.7 else (235, 235, 240)),
            (f"DOOR     {vision.door_approach:.2f}", _COLOR_DOOR if vision.door_approach > 0.6 else (235, 235, 240)),
            (f"CROWDED  {vision.crowded:.2f}", (200, 200, 205)),
            (f"KPTS_OK  {'yes' if vision.keypoints_ok else 'NO (degrade)'}",
             (120, 220, 120) if vision.keypoints_ok else (80, 160, 255)),
        ]
        for i, (text, color) in enumerate(lines):
            self._put(img, text, (int(34 * s), int(58 * s) + int(i * 33 * s)), color, 0.78, 2)

        # ---- 门框 ROI ----
        x, y, w, h = door_roi.roi
        cv2.rectangle(img, (x, y), (x + w, y + h), (200, 200, 210), 2, cv2.LINE_AA)
        near = int(door_roi.seam_x - 0.28 * w), int(door_roi.seam_x + 0.28 * w)
        cv2.rectangle(img, (near[0], y), (near[1], y + h), (90, 80, 40), 1, cv2.LINE_AA)
        for yy in range(y, y + h, 18):
            cv2.line(img, (door_roi.seam_x, yy), (door_roi.seam_x, min(yy + 9, y + h)), (60, 210, 240), 2)
        self._put(img, "DOOR", (x + 6, y + int(22 * s)), (220, 220, 226), 0.6, 2)

        # ---- 底部曲线 ----
        self.trail.append(float(vision.fall_prob))
        strip_h = int(74 * s)
        strip_w = int(430 * s)
        x0, y0 = int(16 * s), self.h - strip_h - int(20 * s)
        overlay = img.copy()
        cv2.rectangle(overlay, (x0, y0), (x0 + strip_w, y0 + strip_h), _COLOR_HUD_BG, -1)
        img[:] = cv2.addWeighted(overlay, 0.6, img, 0.4, 0)
        cv2.rectangle(img, (x0, y0), (x0 + strip_w, y0 + strip_h), (80, 80, 86), 1)
        self._put(img, "fall_prob (3s window)", (x0 + 8, y0 + int(18 * s)), (200, 200, 206), 0.5, 1)
        values = list(self.trail)
        if len(values) > 1:
            step = strip_w / max(1, len(values) - 1)
            pts = [
                (int(x0 + i * step), int(y0 + strip_h - 6 - v * (strip_h - 24)))
                for i, v in enumerate(values)
            ]
            cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, (60, 120, 240), 2, cv2.LINE_AA)
        # 0.7 阈值线
        thr_y = int(y0 + strip_h - 6 - 0.7 * (strip_h - 24))
        cv2.line(img, (x0 + 4, thr_y), (x0 + strip_w - 4, thr_y), (90, 90, 200), 1)

        # ---- 进度条 ----
        bar_y = self.h - int(14 * s)
        cv2.line(img, (0, bar_y), (self.w, bar_y), (60, 60, 66), 3)
        ratio = frame_index / max(1, total - 1)
        cv2.line(img, (0, bar_y), (int(self.w * ratio), bar_y), (90, 190, 250), 3)
        self._put(img, f"frame {frame_index + 1}/{total}", (self.w - int(240 * s), self.h - int(24 * s)), (215, 215, 220), 0.55, 1)
        return img


def _draw_skeleton(img, person: Person, color: Tuple[int, int, int], conf_thr: float = 0.3) -> None:
    import cv2

    kpts = person.keypoints
    for a, b in SKELETON_EDGES:
        if kpts[a, 2] >= conf_thr and kpts[b, 2] >= conf_thr:
            cv2.line(
                img,
                (int(kpts[a, 0]), int(kpts[a, 1])),
                (int(kpts[b, 0]), int(kpts[b, 1])),
                _bgr(color, 0.55), 5, cv2.LINE_AA,
            )
            cv2.line(
                img,
                (int(kpts[a, 0]), int(kpts[a, 1])),
                (int(kpts[b, 0]), int(kpts[b, 1])),
                color, 2, cv2.LINE_AA,
            )
    for index in range(17):
        if kpts[index, 2] >= conf_thr:
            radius = 5 if index in (NOSE, L_SHO, R_SHO, L_HIP, R_HIP) else 3
            cv2.circle(img, (int(kpts[index, 0]), int(kpts[index, 1])), radius, color, -1, cv2.LINE_AA)


def annotate_case(
    case: str,
    cfg: Optional[VisionConfig] = None,
    *,
    backend: Optional[str] = None,
    out_path: Optional[Path] = None,
    limit: Optional[int] = None,
    sheet_path: Optional[Path] = None,
    sheet_frames: int = 8,
) -> Path:
    import cv2

    from data.scripts.video_io import H264Writer

    cfg = cfg or load_config()
    video = DEMO_DIR / f"{case}.mp4"
    if not video.exists():
        raise FileNotFoundError(f"找不到 {video}；先跑 build_demo_data.py --all")

    engine = VisionEngine(cfg)
    pose_backend = make_backend(cfg, video, backend)
    out_path = out_path or (RESULTS_DIR / "annotated" / f"{case}_annotated.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    hud: Optional[HudPainter] = None
    writer: Optional[H264Writer] = None
    sheet_frames = max(1, sheet_frames)
    wanted = set()
    sheet_images: List[np.ndarray] = []
    total = 0

    print(f"[annotate] {case} -> {out_path.relative_to(PROJECT_ROOT) if out_path.is_relative_to(PROJECT_ROOT) else out_path}")
    for frame in iter_frames(video, cfg, backend=pose_backend, limit=limit, with_image=True):
        vision = engine.process(frame)
        img = frame.image
        h, w = img.shape[:2]
        if hud is None:
            hud = HudPainter(w, h)
        if writer is None:
            total = int(round(20.0 * 25)) if limit is None else limit
            try:
                import json

                meta = json.loads((DEMO_DIR / f"{case}_meta.json").read_text(encoding="utf-8"))
                total = int(meta.get("frames", total))
            except Exception:
                pass
            writer = H264Writer(out_path, w, h, fps=25.0)

        # 骨架配色：倒地红 / 蹲黄 / 正常绿
        for person in frame.persons:
            feats = fall_features(person, cfg.fall)
            color = _COLOR_OK
            if feats.suspect:
                color = _COLOR_FALL
            elif feats.crouch_like:
                color = _COLOR_CROUCH
            _draw_skeleton(img, person, color)

            # 手腕贴门标注
            roi = engine.roi
            for wrist in (L_WRI, R_WRI):
                kp = person.keypoints[wrist]
                if kp[2] < cfg.door.wrist_conf:
                    continue
                if abs(float(kp[0]) - roi.seam_x) <= cfg.door.near_ratio * roi.roi[2] and roi.roi[1] <= kp[1] <= roi.roi[1] + roi.roi[3]:
                    cv2.circle(img, (int(kp[0]), int(kp[1])), 9, _COLOR_DOOR, -1, cv2.LINE_AA)
                    cv2.line(img, (int(kp[0]), int(kp[1])), (roi.seam_x, int(kp[1])), _COLOR_DOOR, 1, cv2.LINE_AA)

        img = hud.draw(img, vision, engine.roi, frame.index, max(total, frame.index + 1))
        if writer is not None:
            writer.write(img)

        if sheet_path is not None and len(sheet_images) < sheet_frames:
            if frame.index % max(1, int(total / sheet_frames)) == 0:
                sheet_images.append(img.copy())

    if writer is not None:
        writer.close()

    if sheet_path is not None and sheet_images:
        _write_contact_sheet(sheet_images, sheet_path, case)
    return out_path


def _write_contact_sheet(images: Sequence[np.ndarray], path: Path, title: str) -> None:
    import cv2

    thumb_w = 440
    thumbs = []
    for img in images:
        scale = thumb_w / img.shape[1]
        thumbs.append(cv2.resize(img, (thumb_w, int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA))
    cols = 2 if len(thumbs) <= 4 else 3
    rows = (len(thumbs) + cols - 1) // cols
    th, tw = thumbs[0].shape[:2]
    pad = 10
    canvas = np.full((rows * th + pad * (rows + 1), cols * tw + pad * (cols + 1), 3), 24, dtype=np.uint8)
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, cols)
        y = pad + r * (th + pad)
        x = pad + c * (tw + pad)
        canvas[y:y + th, x:x + tw] = thumb
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), canvas)
    print(f"  contact sheet -> {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成带标注的演示视频")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", help="视频路径")
    group.add_argument("--case", choices=CASE_ORDER)
    group.add_argument("--all", action="store_true")
    parser.add_argument("--backend", default=None, choices=["auto", "yolo", "onnx", "synthetic"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=None, help="输出视频路径")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sheet", default=None, help="同时输出一张关键帧拼图 PNG")
    parser.add_argument("--sheet-frames", type=int, default=8)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cases = CASE_ORDER if args.all else [args.case]
    if args.video:
        # 支持任意视频：用文件名当 case 名
        video = resolve_path(args.video)
        cfg.runtime.base_ts = None
        from vision.rules import VisionEngine as _VE  # noqa: F401

        engine = None
        del engine
        out = resolve_path(args.out) if args.out else (RESULTS_DIR / "annotated" / f"{video.stem}_annotated.mp4")
        _annotate_arbitrary(video, cfg, args.backend, out, args.limit, args.sheet)
        return 0

    for case in cases:
        out = resolve_path(args.out) if (args.out and len(cases) == 1) else None
        sheet = resolve_path(args.sheet) if args.sheet and len(cases) == 1 else None
        annotate_case(
            case,
            cfg,
            backend=args.backend,
            out_path=out,
            limit=args.limit,
            sheet_path=sheet,
            sheet_frames=args.sheet_frames,
        )
    return 0


def _annotate_arbitrary(video: Path, cfg: VisionConfig, backend: Optional[str], out: Path, limit: Optional[int], sheet: Optional[str]) -> None:
    """任意视频的标注（不读 demo meta，帧数按实际解码算）。"""
    import cv2

    from data.scripts.video_io import H264Writer

    engine = VisionEngine(cfg)
    pose_backend = make_backend(cfg, video, backend)
    writer = None
    hud = None
    count = 0
    sheet_images: List[np.ndarray] = []
    for frame in iter_frames(video, cfg, backend=pose_backend, limit=limit, with_image=True):
        vision = engine.process(frame)
        img = frame.image
        h, w = img.shape[:2]
        hud = hud or HudPainter(w, h)
        writer = writer or H264Writer(out, w, h, fps=25.0)
        for person in frame.persons:
            feats = fall_features(person, cfg.fall)
            color = _COLOR_FALL if feats.suspect else (_COLOR_CROUCH if feats.crouch_like else _COLOR_OK)
            _draw_skeleton(img, person, color)
        img = hud.draw(img, vision, engine.roi, count, count + 1)
        writer.write(img)
        if sheet and count % 40 == 0 and len(sheet_images) < 6:
            sheet_images.append(img.copy())
        count += 1
    if writer:
        writer.close()
    print(f"[annotate] {count} 帧 -> {out}")
    if sheet and sheet_images:
        _write_contact_sheet(sheet_images, resolve_path(sheet), video.stem)


if __name__ == "__main__":
    raise SystemExit(main())