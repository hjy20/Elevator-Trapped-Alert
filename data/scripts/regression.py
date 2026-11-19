"""回归测试：5 段数据跑一遍，和逐帧真值、和上一次基线对比。

    python data/scripts/regression.py                 # 跑一遍，和 results/vision_baseline.json 比
    python data/scripts/regression.py --update-baseline
    python data/scripts/regression.py --cases demo_faint demo_pry

口径（写进 DATA_DICT / 技术文档，别含糊）：
* 人数误差：与生成器逐帧真值的平均绝对误差（MAE）与"±1 以内帧占比"
* 倒地：真值 fallen>0 的帧中，fall_prob>0.7 的比例 = **召回**；
        真值 fallen==0 的帧中 fall_prob>0.7 的比例 = **误报率**
* 贴门：同理，判据 door_approach>0.6
* 响应延迟：第一个 fall_prob>0.7 的时间点 − 真值第一次倒地的时点

⚠️ 合成场景的真值来自生成器本身，**这套数字只能说明"规则实现符合设计"，
不能说明真实视频上的精度**。真实精度要用实拍素材重跑同一套脚本（见 DATA_DICT 第 8 节）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.schema import VisionOut  # noqa: E402
from data.scripts.build_demo_data import CASE_ORDER  # noqa: E402
from vision.config import DEMO_DIR, PROCESSED_DIR, RESULTS_DIR, load_config  # noqa: E402
from vision.infer import infer_video  # noqa: E402

BASELINE = RESULTS_DIR / "vision_baseline.json"
REPORT_JSON = RESULTS_DIR / "vision_regression.json"
REPORT_MD = RESULTS_DIR / "vision_regression.md"

FALL_THR = 0.70
DOOR_THR = 0.60


def _rate(hits: int, total: int) -> Optional[float]:
    return None if total == 0 else round(hits / total, 3)


def evaluate_case(case: str, outputs: Sequence[VisionOut], gt_rows: Sequence[dict]) -> Dict[str, Any]:
    by_frame = {row["frame"]: row for row in gt_rows}
    # 逐帧对齐：VisionOut 与 gt 行都按帧序排列
    pairs = [(outputs[i], gt_rows[i]) for i in range(min(len(outputs), len(gt_rows)))]

    errors = [abs(o.person_count - g["person_count"]) for o, g in pairs]
    within_one = sum(1 for e in errors if e <= 1)
    exact = sum(1 for e in errors if e == 0)

    fall_pos = [(o, g) for o, g in pairs if g["fallen"] > 0]
    fall_neg = [(o, g) for o, g in pairs if g["fallen"] == 0]
    fall_hits = sum(1 for o, _ in fall_pos if o.fall_prob > FALL_THR)
    fall_fp = sum(1 for o, _ in fall_neg if o.fall_prob > FALL_THR)

    door_pos = [(o, g) for o, g in pairs if g["door_contact"] == 1]
    # 贴门判据带 1.5 s 衰减（手离开后 score 是慢慢落下去的，这是设计而不是抖动），
    # 所以统计"误报"时要扣掉接触结束后的衰减窗口，否则会把设计行为算成错误。
    grace_frames = int(round(1.5 * 25))
    allowed = set()
    last_contact = -10**9
    for index, (_o, g) in enumerate(pairs):
        if g["door_contact"] == 1:
            last_contact = index
        if index - last_contact <= grace_frames:
            allowed.add(index)
    door_neg = [(o, g) for index, (o, g) in enumerate(pairs) if g["door_contact"] == 0 and index not in allowed]
    door_hits = sum(1 for o, _ in door_pos if o.door_approach > DOOR_THR)
    door_fp = sum(1 for o, _ in door_neg if o.door_approach > DOOR_THR)

    latency = None
    first_gt = next((g["t"] for _, g in pairs if g["fallen"] > 0), None)
    first_pred = next((o.ts for o, _ in pairs if o.fall_prob > FALL_THR), None)
    if first_gt is not None and first_pred is not None and outputs:
        first_pred_t = (first_pred - outputs[0].ts) / 1000.0
        latency = round(first_pred_t - float(first_gt), 2)

    return {
        "case": case,
        "frames": len(pairs),
        "people_mae": round(statistics.fmean(errors), 3) if errors else None,
        "people_exact_ratio": _rate(exact, len(errors)),
        "people_within1_ratio": _rate(within_one, len(errors)),
        "people_max_error": max(errors) if errors else None,
        "fall_recall": _rate(fall_hits, len(fall_pos)),
        "fall_false_positive_rate": _rate(fall_fp, len(fall_neg)),
        "fall_peak": round(max((o.fall_prob for o in outputs), default=0.0), 3),
        "door_recall": _rate(door_hits, len(door_pos)),
        "door_false_positive_rate": _rate(door_fp, len(door_neg)),
        "door_peak": round(max((o.door_approach for o in outputs), default=0.0), 3),
        "fall_alarm_latency_sec": latency,
        "keypoints_ok_ratio": _rate(sum(1 for o in outputs if o.keypoints_ok), len(outputs)),
        "gt_frames": len(gt_rows),
    }


def run(cases: Sequence[str], backend: Optional[str] = None) -> Dict[str, Any]:
    cfg = load_config()
    rows: List[Dict[str, Any]] = []
    for case in cases:
        video = DEMO_DIR / f"{case}.mp4"
        gt_file = PROCESSED_DIR / f"{case}_gt.json"
        if not video.exists() or not gt_file.exists():
            print(f"[skip] {case}: 缺视频或真值（先跑 build_demo_data.py）")
            continue
        outputs = infer_video(video, cfg, backend=backend)
        gt_rows = json.loads(gt_file.read_text(encoding="utf-8"))["rows"]
        rows.append(evaluate_case(case, outputs, gt_rows))

    summary = {
        "cases": rows,
        "aggregate": {
            "cases": len(rows),
            "people_mae_mean": round(statistics.fmean(r["people_mae"] for r in rows), 3) if rows else None,
            "people_within1_mean": round(statistics.fmean(r["people_within1_ratio"] for r in rows), 3) if rows else None,
            "fall_recall_mean": round(
                statistics.fmean([r["fall_recall"] for r in rows if r["fall_recall"] is not None]), 3
            ) if any(r["fall_recall"] is not None for r in rows) else None,
            "fall_fp_rate_max": max([r["fall_false_positive_rate"] or 0.0 for r in rows], default=None),
            "door_recall_mean": round(
                statistics.fmean([r["door_recall"] for r in rows if r["door_recall"] is not None]), 3
            ) if any(r["door_recall"] is not None for r in rows) else None,
            "door_fp_rate_max": max([r["door_false_positive_rate"] or 0.0 for r in rows], default=None),
            "note": "合成场景自检口径，不能当真实视频精度使用",
        },
    }
    return summary


def compare_with_baseline(summary: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    """返回退化清单；空列表说明没有退化。"""
    problems: List[str] = []
    old = {row["case"]: row for row in baseline.get("cases", [])}
    for row in summary["cases"]:
        prev = old.get(row["case"])
        if not prev:
            continue
        if (row.get("people_mae") or 0) > (prev.get("people_mae") or 0) + 0.10:
            problems.append(f"{row['case']}: 人数 MAE 从 {prev.get('people_mae')} 升到 {row.get('people_mae')}")
        for key, label, tol in (("fall_recall", "倒地召回", 0.05), ("door_recall", "贴门召回", 0.05)):
            if row.get(key) is not None and prev.get(key) is not None and row[key] < prev[key] - tol:
                problems.append(f"{row['case']}: {label} 从 {prev[key]} 掉到 {row[key]}")
        for key, label in (("fall_false_positive_rate", "倒地误报率"), ("door_false_positive_rate", "贴门误报率")):
            if row.get(key) is not None and prev.get(key) is not None and row[key] > prev[key] + 0.02:
                problems.append(f"{row['case']}: {label} 从 {prev[key]} 升到 {row[key]}")
    return problems


def write_markdown(summary: Dict[str, Any], problems: Sequence[str]) -> Path:
    lines = [
        "# 视觉模块回归报告（5 段场景库）",
        "",
        "| case | 帧数 | 人数 MAE | ±1 帧占比 | 倒地召回 | 倒地误报率 | 贴门召回 | 贴门误报率 | 倒地响应延迟(s) | 关键点可用率 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["cases"]:
        def fmt(value: Any, digits: int = 2) -> str:
            if value is None:
                return "—"
            if isinstance(value, float):
                return f"{value:.{digits}f}"
            return str(value)

        lines.append(
            f"| {row['case']} | {row['frames']} | {fmt(row['people_mae'])} | {fmt(row['people_within1_ratio'])} | "
            f"{fmt(row['fall_recall'])} | {fmt(row['fall_false_positive_rate'])} | {fmt(row['door_recall'])} | "
            f"{fmt(row['door_false_positive_rate'])} | {fmt(row['fall_alarm_latency_sec'], 1)} | {fmt(row['keypoints_ok_ratio'])} |"
        )
    agg = summary["aggregate"]
    lines += [
        "",
        f"汇总：人数 MAE 均值 {agg['people_mae_mean']}，±1 帧占比 {agg['people_within1_mean']}，"
        f"倒地召回均值 {agg['fall_recall_mean']}，贴门召回均值 {agg['door_recall_mean']}。",
        "",
        "> ⚠️ 口径：真值来自合成场景生成器，本表证明的是「规则实现与设计一致」，不是真实视频精度。",
        "> 换成实拍素材后，用同一脚本重跑即可得到可对外引用的数字。",
    ]
    if problems:
        lines += ["", "## 退化检查：发现以下问题", ""] + [f"- {p}" for p in problems]
    else:
        lines += ["", "## 退化检查：通过（无指标退化）"]
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT_MD


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视觉模块回归测试")
    parser.add_argument("--cases", nargs="*", default=CASE_ORDER)
    parser.add_argument("--backend", default=None, choices=["auto", "yolo", "onnx", "synthetic"])
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)

    summary = run(args.cases, args.backend)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    problems: List[str] = []
    if BASELINE.exists() and not args.update_baseline:
        problems = compare_with_baseline(summary, json.loads(BASELINE.read_text(encoding="utf-8")))
    if args.update_baseline:
        BASELINE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[baseline] 已更新 {BASELINE.relative_to(PROJECT_ROOT)}")

    write_markdown(summary, problems)
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2))
    if problems:
        print("\n[退化]")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\n[ok] 报告 -> {REPORT_MD.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())