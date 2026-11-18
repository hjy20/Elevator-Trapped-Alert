"""A 的对外接口：视频 -> ``list[VisionOut]``。

L2 契约里的签名就是这一个：

    def infer_video(video_path: str) -> list[VisionOut]

用法：
    python vision/infer.py --video common/demo_data/demo_trapped.mp4
    python vision/infer.py --case demo_faint --out results/vision_demo_faint.json
    python vision/infer.py --all                # 5 段全跑，产物进 results/
    python vision/infer.py --video x.mp4 --backend yolo --imgsz 640 --frame-skip 1

默认行为：结果写成 JSON 列表（UTF-8、无 BOM），字段严格等于契约里的 6 个，
不多不少；加 ``--debug`` 才会额外写一份带中间量的 ``*_debug.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.schema import VisionOut  # noqa: E402
from vision.config import DEMO_DIR, RESULTS_DIR, VisionConfig, load_config, resolve_path  # noqa: E402
from vision.rules import VisionEngine  # noqa: E402
from vision.sources import iter_frames, make_backend  # noqa: E402

__all__ = ["infer_video", "infer_case", "summarize", "CASE_ORDER"]

CASE_ORDER = ["demo_normal", "demo_estop", "demo_trapped", "demo_pry", "demo_faint"]


# ---------------------------------------------------------------------------
# 核心接口
# ---------------------------------------------------------------------------


def infer_video(
    video_path: "str | Path",
    cfg: Optional[VisionConfig] = None,
    *,
    backend: Optional[str] = None,
    limit: Optional[int] = None,
    debug: bool = False,
    debug_sink: Optional[List[Dict[str, Any]]] = None,
    progress: bool = False,
) -> List[VisionOut]:
    """对一段视频逐帧推理，返回契约要求的 ``list[VisionOut]``。

    Args:
        video_path: 视频路径（相对仓库根或绝对路径都行）。
        cfg: 视觉配置；不给就用 ``vision/configs/default.json``。
        backend: ``auto|yolo|onnx|synthetic``；默认按配置里的 ``auto``。
        limit: 只跑前 N 帧（调试用）。
        debug: 逐帧中间量（人数原值、门分、fall 投票比例、note）写进 ``debug_sink``。
        debug_sink: 调用方传入的 list，用来接收中间量（不改变返回值类型）。
        progress: 每 25 帧打一行进度。
    """
    cfg = cfg or load_config()
    path = resolve_path(video_path)
    engine = VisionEngine(cfg)
    pose_backend = make_backend(cfg, path, backend)

    outputs: List[VisionOut] = []
    debug_rows: List[Dict[str, Any]] = []
    started = time.perf_counter()
    total = 0

    for frame in iter_frames(path, cfg, backend=pose_backend, limit=limit, with_image=False):
        vision = engine.process(frame)
        outputs.append(vision)
        total += 1
        if debug and debug_sink is not None and engine.last_result is not None:
            row = {
                "ts": vision.ts,
                "frame": frame.index,
                "det_persons": engine.last_result.persons,
                "usable_persons": engine.last_result.usable_persons,
                "door_raw": round(engine.last_result.door_raw, 3),
                "door_ratio": round(engine.last_result.door_ratio, 3),
                "area_ratio": round(engine.last_result.area_ratio, 4),
                "notes": engine.last_result.notes,
                **{f"fall_{k}": v for k, v in engine.last_result.fall_state.items()},
            }
            debug_sink.append(row)
        if progress and total % 25 == 0:
            print(f"  ... {total} 帧  last={_as_dict(vision)}")

    elapsed = time.perf_counter() - started
    if progress:
        fps = total / elapsed if elapsed > 0 else 0.0
        print(f"  完成 {total} 帧，用时 {elapsed:.1f}s，端到端 {fps:.1f} FPS（backend={pose_backend.name}）")

    return outputs


def infer_case(case: str, cfg: Optional[VisionConfig] = None, **kwargs: Any) -> List[VisionOut]:
    """按名字跑 ``common/demo_data/<case>.mp4``。"""
    video = DEMO_DIR / f"{case}.mp4"
    if not video.exists():
        raise FileNotFoundError(
            f"找不到 {video}；先运行 `python data/scripts/build_demo_data.py --all` 生成演示数据。"
        )
    return infer_video(video, cfg, **kwargs)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def summarize(outputs: Sequence[VisionOut]) -> Dict[str, Any]:
    if not outputs:
        return {"frames": 0}
    fall_peak = max(o.fall_prob for o in outputs)
    door_peak = max(o.door_approach for o in outputs)
    crowd_peak = max(o.crowded for o in outputs)
    counts = [o.person_count for o in outputs]
    unreliable = sum(1 for o in outputs if not o.keypoints_ok)
    return {
        "frames": len(outputs),
        "duration_sec": round((outputs[-1].ts - outputs[0].ts) / 1000.0, 2),
        "person_count_max": max(counts),
        "person_count_mean": round(sum(counts) / len(counts), 2),
        "person_count_first": counts[0],
        "person_count_last": counts[-1],
        "fall_prob_peak": round(fall_peak, 3),
        "fall_frames_over_07": sum(1 for o in outputs if o.fall_prob > 0.7),
        "door_approach_peak": round(door_peak, 3),
        "door_frames_over_06": sum(1 for o in outputs if o.door_approach > 0.6),
        "crowded_peak": round(crowd_peak, 3),
        "keypoints_ok_ratio": round(1.0 - unreliable / len(outputs), 3),
        "ts_start": outputs[0].ts,
        "ts_end": outputs[-1].ts,
    }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def _as_dict(vision: VisionOut) -> Dict[str, Any]:
    """VisionOut -> dict。

    刻意用 ``dataclasses.asdict`` 而不是 ``vision.to_dict()``：
    ``common/schema.py`` 是三个人共用的文件，B 的版本只有 dataclass、没有辅助方法。
    A 这边不去要求共享文件提供什么，只要能序列化就行——这样两边都不用改对方的文件。
    """
    return asdict(vision)


def write_json(outputs: Sequence[VisionOut], path: "str | Path") -> Path:
    target = resolve_path(path, create_parent=True)
    payload = [_as_dict(o) for o in outputs]
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return target


def write_jsonl(outputs: Sequence[VisionOut], path: "str | Path") -> Path:
    target = resolve_path(path, create_parent=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in outputs:
            handle.write(json.dumps(_as_dict(record), ensure_ascii=False) + "\n")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _apply_cli(cfg: VisionConfig, args: argparse.Namespace) -> VisionConfig:
    if args.imgsz:
        cfg.model.imgsz = int(args.imgsz)
    if args.frame_skip:
        cfg.runtime.frame_skip = int(args.frame_skip)
    if args.conf is not None:
        cfg.model.conf = float(args.conf)
    if args.roi:
        cfg.door.roi = args.roi
    if args.base_ts:
        cfg.runtime.base_ts = int(args.base_ts)
    return cfg


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视频 -> VisionOut（A 的推理入口）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", help="视频路径")
    group.add_argument("--case", choices=CASE_ORDER, help="演示数据名（自动拼 common/demo_data/）")
    group.add_argument("--all", action="store_true", help="5 段演示数据全跑")
    parser.add_argument("--backend", default=None, choices=["auto", "yolo", "onnx", "synthetic"])
    parser.add_argument("--config", default=None, help="配置文件路径，默认 vision/configs/default.json")
    parser.add_argument("--roi", default=None, help="门框 ROI 名字或 json 路径")
    parser.add_argument("--out", default=None, help="输出 JSON 路径")
    parser.add_argument("--jsonl", action="store_true", help="额外写一份 JSONL（B 直接按行读）")
    parser.add_argument("--debug", action="store_true", help="额外输出中间量")
    parser.add_argument("--imgsz", type=int, default=None, help="模型输入尺寸，默认 480")
    parser.add_argument("--frame-skip", type=int, default=None, help="跳帧数，默认 2")
    parser.add_argument("--conf", type=float, default=None, help="检测置信度阈值")
    parser.add_argument("--base-ts", type=int, default=None, help="毫秒时间基准（默认读视频元数据）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 帧")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cfg = _apply_cli(load_config(args.config), args)
    targets: List[tuple[str, Path]] = []
    if args.all:
        targets = [(case, DEMO_DIR / f"{case}.mp4") for case in CASE_ORDER]
    elif args.case:
        targets = [(args.case, DEMO_DIR / f"{args.case}.mp4")]
    else:
        video = resolve_path(args.video)
        targets = [(video.stem, video)]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report: List[Dict[str, Any]] = []
    for name, video in targets:
        if not video.exists():
            print(f"[skip] {video} 不存在（先跑 data/scripts/build_demo_data.py）")
            continue
        if not args.quiet:
            print(f"[infer] {name} <- {video.relative_to(PROJECT_ROOT) if video.is_relative_to(PROJECT_ROOT) else video}")
        debug_rows: List[Dict[str, Any]] = []
        outputs = infer_video(
            video,
            cfg,
            backend=args.backend,
            limit=args.limit,
            debug=args.debug,
            debug_sink=debug_rows,
            progress=not args.quiet,
        )
        out_path = resolve_path(args.out) if (args.out and len(targets) == 1) else RESULTS_DIR / f"vision_{name}.json"
        write_json(outputs, out_path)
        if args.jsonl:
            write_jsonl(outputs, out_path.with_suffix(".jsonl"))
        if args.debug and debug_rows:
            debug_path = out_path.with_name(out_path.stem + "_debug.json")
            with debug_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(debug_rows, handle, ensure_ascii=False)
                handle.write("\n")
        summary = summarize(outputs)
        summary["case"] = name
        summary["out"] = str(out_path.relative_to(PROJECT_ROOT)) if out_path.is_relative_to(PROJECT_ROOT) else str(out_path)
        report.append(summary)
        if not args.quiet:
            print(
                f"  -> {summary['frames']} 帧  people_max={summary['person_count_max']}  "
                f"fall_peak={summary['fall_prob_peak']}  door_peak={summary['door_approach_peak']}  "
                f"kpt_ok={summary['keypoints_ok_ratio']}"
            )

    if len(report) > 1:
        print("\n=== 汇总 ===")
        print(f"{'case':<15}{'frames':>7}{'people':>8}{'fall':>7}{'door':>7}{'crowd':>7}{'kpt_ok':>8}")
        for row in report:
            print(
                f"{row['case']:<15}{row['frames']:>7}{row['person_count_max']:>8}"
                f"{row['fall_prob_peak']:>7.2f}{row['door_approach_peak']:>7.2f}"
                f"{row['crowded_peak']:>7.2f}{row['keypoints_ok_ratio']:>8.2f}"
            )
        (RESULTS_DIR / "vision_summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())