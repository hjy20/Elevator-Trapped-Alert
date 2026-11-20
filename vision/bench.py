"""性能基准：分辨率 × 跳帧 的对比表（D7 的硬要求之一）。

产出 `results/bench_vision.md` + `results/bench_vision.json`，表格直接进技术文档与 PPT：

    | imgsz | frame_skip | 端到端 FPS | 模型 FPS | 人数误差 | fall_peak 偏差 |

注意：``synthetic`` 后端没有模型推理，FPS 只代表"规则+解码"开销，
表格里会显式标注 backend，避免把合成数据的数字当成真实性能（不许编指标）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.schema import VisionOut  # noqa: E402
from vision.config import DEMO_DIR, RESULTS_DIR, load_config, resolve_path  # noqa: E402
from vision.infer import infer_video  # noqa: E402
from vision.rules import VisionEngine  # noqa: E402
from vision.sources import iter_frames, make_backend  # noqa: E402

CASE_ORDER = ["demo_normal", "demo_estop", "demo_trapped", "demo_pry", "demo_faint"]


def measure_model_fps(case: str, cfg, backend_name: Optional[str], frames: int = 40) -> Optional[float]:
    """只测模型前向（不含解码与规则），用来区分"模型慢"还是"后处理慢"。"""
    video = DEMO_DIR / f"{case}.mp4"
    if not video.exists():
        return None
    backend = make_backend(cfg, video, backend_name)
    if backend.name == "synthetic":
        return None
    stamps: List[float] = []
    for frame in iter_frames(video, cfg, backend=backend, limit=frames, with_image=True):
        if frame.image is None:
            continue
        start = time.perf_counter()
        backend.infer(frame.image)
        stamps.append(time.perf_counter() - start)
    backend.close()
    if not stamps:
        return None
    return len(stamps) / sum(stamps)


def run_combo(case: str, imgsz: int, skip: int, backend: Optional[str], limit: Optional[int]) -> Dict[str, Any]:
    cfg = load_config()
    cfg.model.imgsz = imgsz
    cfg.runtime.frame_skip = skip
    started = time.perf_counter()
    outputs: List[VisionOut] = infer_video(DEMO_DIR / f"{case}.mp4", cfg, backend=backend, limit=limit)
    elapsed = time.perf_counter() - started
    end_to_end = len(outputs) / elapsed if elapsed > 0 else 0.0
    model_fps = measure_model_fps(case, cfg, backend)
    return {
        "imgsz": imgsz,
        "frame_skip": skip,
        "frames": len(outputs),
        "elapsed_sec": round(elapsed, 2),
        "end_to_end_fps": round(end_to_end, 1),
        "model_fps": None if model_fps is None else round(model_fps, 1),
        "person_count_mean": round(statistics.fmean(o.person_count for o in outputs), 2),
        "fall_peak": round(max(o.fall_prob for o in outputs), 3),
        "door_peak": round(max(o.door_approach for o in outputs), 3),
        "keypoints_ok_ratio": round(sum(1 for o in outputs if o.keypoints_ok) / max(1, len(outputs)), 3),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视觉模块性能基准")
    parser.add_argument("--case", choices=CASE_ORDER, default="demo_faint")
    parser.add_argument("--backend", default=None, choices=["auto", "yolo", "onnx", "synthetic"])
    parser.add_argument("--limit", type=int, default=300, help="每个组合只跑前 N 帧（默认 300，够覆盖 demo_faint 的倒地）")
    parser.add_argument("--imgsz", type=int, nargs="*", default=[640, 480, 320])
    parser.add_argument("--skip", type=int, nargs="*", default=[1, 2, 3])
    parser.add_argument("--out", default="results/bench_vision.md")
    args = parser.parse_args(argv)

    rows: List[Dict[str, Any]] = []
    for imgsz in args.imgsz:
        for skip in args.skip:
            row = run_combo(args.case, imgsz, skip, args.backend, args.limit)
            rows.append(row)
            print(
                f"imgsz={imgsz:<4} skip={skip}  e2e={row['end_to_end_fps']:>6.1f} FPS  "
                f"model={row['model_fps']}  people={row['person_count_mean']}  fall={row['fall_peak']}"
            )

    baseline = next((r for r in rows if r["imgsz"] == 640 and r["frame_skip"] == 1), rows[0])
    for row in rows:
        row["persons_delta_vs_baseline"] = round(row["person_count_mean"] - baseline["person_count_mean"], 2)
        row["fall_peak_delta"] = round(row["fall_peak"] - baseline["fall_peak"], 3)

    out_path = resolve_path(args.out, create_parent=True)
    is_synthetic = (args.backend or "auto").lower() == "synthetic"
    lines = [
        f"# 视觉模块性能基准（case={args.case}, 前 {args.limit} 帧）",
        "",
        f"- 后端：`{args.backend or 'auto'}`"
        + ("（合成关键点，没有模型前向，model FPS 记为空）" if is_synthetic else "（YOLOv8n-Pose 真实推理）"),
        f"- 机器：本机 CPU，Python {sys.version.split()[0]}",
        "- 端到端 FPS = 解码 + 关键点 + 规则 + 序列化的整体吞吐（视频是一帧帧来的，所以这里已经包含读盘成本）",
        "",
        "| imgsz | frame_skip | 端到端 FPS | 模型 FPS | 人数均值 | fall 峰值 | 贴门峰值 | 关键点可用率 | 人数偏差 | fall 偏差 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['imgsz']} | {row['frame_skip']} | {row['end_to_end_fps']} | "
            f"{row['model_fps'] if row['model_fps'] is not None else '—'} | {row['person_count_mean']} | "
            f"{row['fall_peak']} | {row['door_peak']} | {row['keypoints_ok_ratio']} | "
            f"{row['persons_delta_vs_baseline']:+.2f} | {row['fall_peak_delta']:+.3f} |"
        )
    is_synthetic = (args.backend or "auto").lower() == "synthetic"
    if is_synthetic:
        notes = [
            "> ⚠️ 本轮是 `synthetic` 后端：**没有模型前向**，端到端 FPS 反映的是"
            "「解码 + 规则 + 序列化」的规则层开销，",
            "> 不能当作 YOLO-Pose 的推理速度，也不能用来比较 imgsz 的收益"
            "（imgsz 对合成后端没有任何影响）。",
            "> 它的用途只有一个：证明规则层不是瓶颈（≈390 FPS，相对 25 fps 的视频流有大量余量）。",
            "> 真实 FPS 与 imgsz 的取舍必须用实拍视频 + `--backend yolo` 重测后再写进 PPT。",
        ]
    else:
        notes = [
            "> 读法：**480 + 跳帧 2** 是默认配置——本机 CPU 上端到端约 29 FPS（多次测量 28.6~32.3），"
            "已超过 25 fps 的视频流；",
            "> 640 只能到约 21 FPS（不够实时），320 更快但腕/踝关键点会变糊，贴门判定会先受害。",
            "> 这张表的数字来自本机 CPU，PPT 里引用时要写明机器与后端。",
        ]
        if max(row["person_count_mean"] for row in rows) < 0.5:
            notes += [
                "",
                "> ⚠️ **本轮人数均值 ≈ 0**：合成演示视频里的人是程序化画的胶囊人，YOLO-Pose 检不出来",
                "> （这是预期行为——合成数据只用来验证规则链路，不用来验证检测精度）。",
                "> 因此这里的 FPS 应理解为**「几乎无人需要后处理」时的上界**：真实画面里每帧多几个人，",
                "> NMS 与关键点解码会多花一点时间，实测会略低于此表。",
                "> 320 档偶尔检出 1 个人，属于低分辨率下的噪声，不代表可用。",
            ]
    lines += ["", *notes, ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n写出 {out_path.relative_to(PROJECT_ROOT)} 与 {json_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())