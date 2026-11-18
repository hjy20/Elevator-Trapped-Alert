"""ONNX 导出与一致性校验（可选加分项：昇腾适配的第一步）。

    python vision/onnx_export.py --export                # yolov8n-pose.pt -> onnx（opset 12）
    python vision/onnx_export.py --verify --frames 60    # 逐帧对比 .pt 与 .onnx 的关键点误差
    python vision/onnx_export.py --ascend               # 打印昇腾 ATC 转换清单（不执行）

校验口径（写进技术文档）：同一帧分别用 ultralytics(.pt) 与 onnxruntime(.onnx) 推理，
按 17 个关键点的像素坐标算 L2 距离，报告 mean / p95 / max。目标 **mean < 1 px**。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from vision.config import DEMO_DIR, MODEL_DIR, RESULTS_DIR, load_config, resolve_path  # noqa: E402
from vision.sources import OnnxPoseBackend, VideoReader, YoloPoseBackend  # noqa: E402

ASCEND_CHECKLIST = """\
昇腾（CANN / ATC）适配清单 —— 没有真机时照实写"方案 + 未验证"，不许编截图
────────────────────────────────────────────────────────────────────────
1) 环境：Ascend 310/910 + CANN 7.x（云上可租 Ascend 推理实例，或 ModelArts 的 Ascend 规格）
2) 导出 ONNX（本脚本 --export）：固定 imgsz=480、opset 12、batch=1、dynamic=False
   —— 固定 shape 是 ATC 转换成功率最高的写法
3) ATC 转换：
   atc --model=yolov8n-pose.onnx \\
       --framework=5 \\
       --output=yolov8n-pose_bs1 \\
       --input_shape="images:1,3,480,480" \\
       --soc_version=Ascend310P3 \\
       --precision_mode=allow_fp32_to_fp16
4) 精度比对：对同一批帧，用 om 模型跑一次，与 onnxruntime 输出逐关键点比对
   —— 目标 mean < 1px；若超差，先关掉 allow_fp32_to_fp16 复测，定位是量化还是算子
5) 常见坑：
   - Resize/Concat 的动态 shape 在 ATC 里会退化，务必固定 480x480；
   - NMS 在 device 侧实现差异大，建议模型只出 raw 输出，后处理仍放 CPU（我们的实现就是这么做的）；
   - 输出布局 (1,56,N) 需要 transpose，别在 device 上做。
6) 交付物：转换日志 + 逐帧误差表 + 端到端 FPS（Ascend 上通常比 CPU 快 3-8 倍，
   但我们没有实测数据，PPT 里只能写"预期"，并注明未验证。）
"""


def export_onnx(imgsz: int = 480, opset: int = 12) -> Path:
    from ultralytics import YOLO

    cfg = load_config()
    weight = resolve_path(cfg.model.weights)
    if not weight.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        weight = MODEL_DIR / "yolov8n-pose.pt"
    model = YOLO(str(weight))
    exported = model.export(format="onnx", imgsz=imgsz, opset=opset, simplify=False, dynamic=False)
    produced = Path(str(exported))
    target = resolve_path(cfg.model.onnx)
    target.parent.mkdir(parents=True, exist_ok=True)
    if produced.resolve() != target.resolve():
        produced.replace(target)
    print(f"[export] {target}  ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def _gather_frames(video: Path, count: int, max_side: Optional[int] = None) -> List[np.ndarray]:
    frames: List[np.ndarray] = []
    reader = VideoReader(video, limit=count, max_side=max_side)
    try:
        for _index, image in reader.frames():
            frames.append(image)
    finally:
        reader.close()
    return frames


def verify(video_name: str = "demo_trapped", frames: int = 60, image_path: Optional[str] = None) -> Dict[str, Any]:
    """对比 .pt 与 .onnx 的关键点误差。

    ⚠️ 合成演示视频里的"人"是程序化画出来的胶囊人，YOLO 两边都检不到人，
    所以默认用例会得到 0 个可比对的关键点——那**不是** parity 通过，是没数据。
    真实画面请用 ``--image`` 指定一张含人的照片（推荐 ultralytics 的 bus.jpg）。
    """
    cfg = load_config()
    cfg.model.conf = 0.20          # 放宽阈值，多拿一些点来比对

    yolo = YoloPoseBackend(cfg)
    onnx = OnnxPoseBackend(cfg)

    if image_path:
        import cv2

        image = cv2.imread(str(resolve_path(image_path)))
        if image is None:
            raise FileNotFoundError(f"读不到图片：{image_path}")
        h, w = image.shape[:2]
        images = [
            image,                                  # 原图
            image[:, : max(2, w // 2)],             # 左半（裁剪后 letterbox 会重新缩放）
            image[h // 4 :, : max(2, int(w * 0.75))],  # 中间偏左
        ]
        source = Path(str(image_path)).name
    else:
        video = DEMO_DIR / f"{video_name}.mp4"
        if not video.exists():
            raise FileNotFoundError(video)
        images = _gather_frames(video, frames)
        source = video_name

    distances: List[float] = []
    matched = 0
    total = 0
    for image in images:
        a = yolo.infer(image)
        b = onnx.infer(image)
        total += 1
        if len(a) != len(b):
            continue
        for pa, pb in zip(a, b):
            ca = np.asarray(pa.center, dtype=np.float32)
            cb = np.asarray(pb.center, dtype=np.float32)
            if float(np.linalg.norm(ca - cb)) > 40:
                continue
            mask = (pa.keypoints[:, 2] > 0.25) & (pb.keypoints[:, 2] > 0.25)
            if not mask.any():
                continue
            diff = np.linalg.norm(pa.keypoints[mask, :2] - pb.keypoints[mask, :2], axis=1)
            distances.extend(diff.tolist())
            matched += 1

    result: Dict[str, Any] = {
        "source": source,
        "video": video_name if not image_path else None,
        "frames_compared": total,
        "persons_matched": matched,
        "keypoints_compared": len(distances),
        "mean_px": round(float(np.mean(distances)), 3) if distances else None,
        "p95_px": round(float(np.percentile(distances, 95)), 3) if distances else None,
        "max_px": round(float(np.max(distances)), 3) if distances else None,
        "pass_mean_lt_1px": bool(distances) and float(np.mean(distances)) < 1.0,
        "note": (
            "0 个关键点 = 两边都没检到人（合成画面），不是通过；请用 --image 指定含人的真实照片"
            if not distances
            else "同一帧分别用 ultralytics(.pt) 与 onnxruntime(.onnx) 推理，17 个关键点像素坐标的 L2 距离"
        ),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "onnx_parity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ONNX 导出与一致性校验")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--ascend", action="store_true", help="打印昇腾 ATC 适配清单")
    parser.add_argument("--imgsz", type=int, default=480)
    parser.add_argument("--opset", type=int, default=12)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--case", default="demo_trapped")
    parser.add_argument("--image", default=None,
                        help="用一张含人的真实照片做 parity（合成视频里 YOLO 检不到人，比对无意义）")
    args = parser.parse_args(argv)

    if args.ascend:
        print(ASCEND_CHECKLIST)
        return 0
    if args.export:
        export_onnx(args.imgsz, args.opset)
    if args.verify:
        verify(args.case, args.frames, args.image)
    if not (args.export or args.verify):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())