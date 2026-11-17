"""评测：5 段场景 → Decision JSON / timeline / 指标 / 混淆矩阵 / claims。

口径字符串由代码常量拼进产物（REQ-EVAL-005），任何手工修改都会被下一次运行覆盖。
"""

import argparse
import csv
import dataclasses
import json
import logging
import pathlib
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.schema import Decision  # noqa: E402
from model.config import (  # noqa: E402
    AblationSwitch,
    ConfigError,
    RuntimeConfig,
    build_runtime_config,
    config_snapshot,
)
from model.fuse import FusionRunner  # noqa: E402
from model.imu_detect import detect_imu  # noqa: E402
from model.mock_scenario import KINDS, generate_scenario, write_bundle  # noqa: E402

logger = logging.getLogger(__name__)

SCOPE_STATEMENT = "在自建场景库（5 段/5 类）上测得"
DATA_SOURCE_MOCK = "合成场景库（mock，非真实采集）"
DATA_SOURCE_REAL = "自建场景库（真实采集）"
EVENT_TYPES: Tuple[str, ...] = ("normal", "estop", "trapped", "pry", "faint")
EVENT_PRIORITY: Dict[str, int] = {
    "normal": 0,
    "estop": 1,
    "trapped": 2,
    "pry": 3,
    "faint": 4,
}
TRAPPED_LIKE = {"trapped", "pry", "faint"}


def _round(value: float) -> float:
    return round(float(value), 6)


def run_case(kind: str, cfg: RuntimeConfig, seed: int = 0) -> dict:
    bundle = generate_scenario(kind, cfg, seed=seed)
    paths = write_bundle(bundle)
    imu = detect_imu(paths["imu_npy"])
    runner = FusionRunner(cfg)
    decisions = runner.run(bundle.vision, imu)
    observed_ms = int(bundle.vision[-1].ts - bundle.vision[0].ts) + 1000
    stop_enter_ts = next(
        (row["ts"] for row in runner.trace if row["state"] == "STOPPED"), None
    )
    return {
        "case": bundle.case,
        "kind": kind,
        "decisions": decisions,
        "trace": runner.trace,
        "observed_ms": observed_ms,
        "stop_enter_ts": stop_enter_ts,
    }


def materialize(result: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    case = result["case"]

    decision_path = out_dir / f"{case}_decision.json"
    decision_path.write_text(
        json.dumps(
            [item for item in result["decisions"]],
            ensure_ascii=False,
            indent=2,
            default=_asdict,
        ),
        encoding="utf-8",
    )

    timeline_path = out_dir / f"timeline_{case}.jsonl"
    with timeline_path.open("w", encoding="utf-8") as handle:
        for row in result["trace"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _asdict(obj):
    import dataclasses

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def segment_level(decisions: List[Decision]) -> str:
    if not decisions:
        return "normal"
    best = max(
        decisions, key=lambda item: (EVENT_PRIORITY.get(item.event_type, -1), item.urgency)
    )
    return best.event_type


def segment_trapped(decisions: List[Decision]) -> bool:
    return any(item.trapped for item in decisions)


def build_record(result: dict, label: dict) -> dict:
    decisions = result["decisions"]
    return {
        "case": result["case"],
        "expected": label["expected_event"],
        "predicted": segment_level(decisions),
        "expected_urgency": label.get("expected_urgency", 0),
        "max_urgency": max((item.urgency for item in decisions), default=0),
        "total_frames": len(decisions),
        "trapped_frames": sum(1 for item in decisions if item.trapped),
        "first_alarm_ts": next((item.ts for item in decisions if item.trapped), None),
        "stop_enter_ts": label.get("stop_enter_ts") or result["stop_enter_ts"],
        "observed_ms": int(label["observed_ms"]),
    }


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": _round(precision),
        "recall": _round(recall),
        "f1": _round(f1),
    }


def compute_metrics(records: List[dict], cfg: RuntimeConfig) -> dict:
    per_class = {}
    for event_type in EVENT_TYPES:
        tp = sum(
            1 for row in records if row["predicted"] == event_type and row["expected"] == event_type
        )
        fp = sum(
            1 for row in records if row["predicted"] == event_type and row["expected"] != event_type
        )
        fn = sum(
            1 for row in records if row["expected"] == event_type and row["predicted"] != event_type
        )
        per_class[event_type] = _prf(tp, fp, fn)

    abnormal = [row for row in records if row["expected"] != "normal"]
    missed = [row for row in abnormal if row["predicted"] == "normal"]
    miss_rate = len(missed) / len(abnormal) if abnormal else 0.0

    normal_rows = [row for row in records if row["expected"] == "normal"]
    fp_segments = [row for row in normal_rows if row["trapped_frames"] > 0]
    segment_fp_rate = len(fp_segments) / len(normal_rows) if normal_rows else 0.0
    normal_frames = sum(row["total_frames"] for row in normal_rows)
    normal_trapped_frames = sum(row["trapped_frames"] for row in normal_rows)
    frame_fp_rate = (
        normal_trapped_frames / normal_frames if normal_frames else 0.0
    )

    observed_hours = sum(row["observed_ms"] for row in records) / 3_600_000.0
    fp_per_day = (
        len(fp_segments) / observed_hours * 24.0 if observed_hours > 0 else 0.0
    )

    delays = []
    onset_offset = int(round(cfg.thresholds.dwell_threshold * 1000.0))
    for row in records:
        if row["first_alarm_ts"] is None or row["stop_enter_ts"] is None:
            continue
        onset = int(row["stop_enter_ts"]) + onset_offset
        delays.append(max(0.0, (int(row["first_alarm_ts"]) - onset) / 1000.0))
    mean_response_sec = sum(delays) / len(delays) if delays else 0.0

    trapped_tp = sum(
        1
        for row in records
        if row["expected"] in TRAPPED_LIKE and row["predicted"] in TRAPPED_LIKE
    )
    trapped_fp = sum(
        1
        for row in records
        if row["expected"] not in TRAPPED_LIKE and row["predicted"] in TRAPPED_LIKE
    )
    trapped_fn = sum(
        1
        for row in records
        if row["expected"] in TRAPPED_LIKE and row["predicted"] not in TRAPPED_LIKE
    )
    trapped_segment = _prf(trapped_tp, trapped_fp, trapped_fn)

    return {
        "segments": len(records),
        "observed_seconds": _round(sum(row["observed_ms"] for row in records) / 1000.0),
        "observed_hours": _round(observed_hours),
        "per_class": per_class,
        "miss_rate": _round(miss_rate),
        "segment_fp_rate": _round(segment_fp_rate),
        "frame_fp_rate": _round(frame_fp_rate),
        "normal_frames": normal_frames,
        "normal_trapped_frames": normal_trapped_frames,
        "fp_per_day": _round(fp_per_day),
        "mean_response_sec": _round(mean_response_sec),
        "trapped_segment": trapped_segment,
        "records": records,
    }


def render_metrics_md(
    metrics: dict, cfg: RuntimeConfig, command: str, data_source: str = DATA_SOURCE_MOCK
) -> str:
    observed = metrics["observed_seconds"]
    lines = [
        "# 指标表",
        "",
        f"> 口径：{SCOPE_STATEMENT}；样本量 {metrics['segments']} 段、5 类事件各 ≥1 段；",
        f"> 数据来源：{data_source}",
        f"> 观测总时长 {observed:.1f} 秒。本表不构成真实电梯环境的准确率承诺。",
        f"> 生成命令：{command}",
        "> 配置快照：results/config_snapshot.json",
        "",
        "## 1. 正常场景误报（优先验收）",
        "",
        f"- 正常段 trapped 帧数：{metrics['normal_trapped_frames']} / {metrics['normal_frames']}",
        f"- 段级误报率：{metrics['segment_fp_rate']:.6f}",
        f"- 帧级误报率：{metrics['frame_fp_rate']:.6f}",
        f"- 误报次数/天（基于 {observed:.1f} 秒观测换算，仅示意，不可外推）：{metrics['fp_per_day']:.2f}",
        "",
        "## 2. 检出能力（召回）",
        "",
        f"- 漏报率（非 normal 段）：{metrics['miss_rate']:.6f}",
        f"- 平均响应时长（秒）：{metrics['mean_response_sec']:.3f}",
        f"- trapped 二分类（段级）precision={metrics['trapped_segment']['precision']:.6f} "
        f"recall={metrics['trapped_segment']['recall']:.6f}",
        "",
        "## 3. 逐类指标",
        "",
        "| event_type | precision | recall | f1 |",
        "|---|---|---|---|",
    ]
    for event_type in EVENT_TYPES:
        row = metrics["per_class"][event_type]
        lines.append(
            f"| {event_type} | {row['precision']:.6f} | {row['recall']:.6f} | {row['f1']:.6f} |"
        )

    lines += [
        "",
        f"> 断言口径：正常场景误报必须为 0（{metrics['normal_trapped_frames']} 帧）；"
        "该顺序（先误报后召回）由 REQ-CONST-005 固化。",
        "",
    ]
    return "\n".join(lines)


def write_metrics(
    metrics: dict,
    out_dir: pathlib.Path,
    cfg: RuntimeConfig,
    command: str,
    data_source: str = DATA_SOURCE_MOCK,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.md").write_text(
        render_metrics_md(metrics, cfg, command, data_source), encoding="utf-8"
    )

    with (out_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerow(["segments", metrics["segments"]])
        writer.writerow(["observed_seconds", metrics["observed_seconds"]])
        writer.writerow(["normal_trapped_frames", metrics["normal_trapped_frames"]])
        writer.writerow(["segment_fp_rate", metrics["segment_fp_rate"]])
        writer.writerow(["frame_fp_rate", metrics["frame_fp_rate"]])
        writer.writerow(["fp_per_day", metrics["fp_per_day"]])
        writer.writerow(["miss_rate", metrics["miss_rate"]])
        writer.writerow(["mean_response_sec", metrics["mean_response_sec"]])
        for event_type in EVENT_TYPES:
            row = metrics["per_class"][event_type]
            writer.writerow([f"{event_type}_precision", row["precision"]])
            writer.writerow([f"{event_type}_recall", row["recall"]])
            writer.writerow([f"{event_type}_f1", row["f1"]])


def write_claims(
    metrics: dict,
    out_dir: pathlib.Path,
    source_file: str,
    data_source: str = DATA_SOURCE_MOCK,
) -> None:
    claims = []
    for metric, value, extrapolated in (
        ("normal_trapped_frames", metrics["normal_trapped_frames"], False),
        ("segment_fp_rate", metrics["segment_fp_rate"], False),
        ("frame_fp_rate", metrics["frame_fp_rate"], False),
        ("miss_rate", metrics["miss_rate"], False),
        ("mean_response_sec", metrics["mean_response_sec"], False),
        ("fp_per_day", metrics["fp_per_day"], True),
    ):
        entry = {
            "metric": metric,
            "value": value,
            "source_file": source_file,
            "key_path": f"metrics.{metric}",
            "scope": SCOPE_STATEMENT,
            "data_source": data_source,
            "extrapolated": extrapolated,
        }
        if extrapolated:
            entry["observed_hours"] = metrics["observed_hours"]
        claims.append(entry)

    for event_type in EVENT_TYPES:
        for field in ("precision", "recall", "f1"):
            claims.append(
                {
                    "metric": f"{event_type}_{field}",
                    "value": metrics["per_class"][event_type][field],
                    "source_file": source_file,
                    "key_path": f"per_class.{event_type}.{field}",
                    "scope": SCOPE_STATEMENT,
                    "data_source": data_source,
                    "extrapolated": False,
                }
            )

    (out_dir / "claims.json").write_text(
        json.dumps(claims, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def plot_confusion(records: List[dict], out_path: pathlib.Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    matrix = np.zeros((len(EVENT_TYPES), len(EVENT_TYPES)), dtype=int)
    for row in records:
        expected = row["expected"]
        predicted = row["predicted"]
        if expected in EVENT_TYPES and predicted in EVENT_TYPES:
            matrix[EVENT_TYPES.index(expected), EVENT_TYPES.index(predicted)] += 1

    binary = np.zeros((2, 2), dtype=int)
    for row in records:
        expected = 1 if row["expected"] in TRAPPED_LIKE else 0
        predicted = 1 if row["predicted"] in TRAPPED_LIKE else 0
        binary[expected, predicted] += 1

    figure, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].imshow(matrix, cmap="Blues")
    axes[0].set_xticks(range(len(EVENT_TYPES)))
    axes[0].set_yticks(range(len(EVENT_TYPES)))
    axes[0].set_xticklabels(EVENT_TYPES, rotation=45, ha="right")
    axes[0].set_yticklabels(EVENT_TYPES)
    axes[0].set_xlabel("predicted")
    axes[0].set_ylabel("expected")
    axes[0].set_title("Event confusion (5x5)")
    for i in range(len(EVENT_TYPES)):
        for j in range(len(EVENT_TYPES)):
            axes[0].text(j, i, str(matrix[i, j]), ha="center", va="center")

    labels = ["non_trapped", "trapped"]
    axes[1].imshow(binary, cmap="Greens")
    axes[1].set_xticks([0, 1])
    axes[1].set_yticks([0, 1])
    axes[1].set_xticklabels(labels)
    axes[1].set_yticklabels(labels)
    axes[1].set_xlabel("predicted")
    axes[1].set_ylabel("expected")
    axes[1].set_title("Trapped confusion (2x2)")
    for i in range(2):
        for j in range(2):
            axes[1].text(j, i, str(binary[i, j]), ha="center", va="center")

    figure.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, dpi=120)
    plt.close(figure)


CALIBRATION_GRID = {
    "dwell_threshold": (6.0, 8.0, 10.0, 150.0, 180.0, 200.0),
    "fall_thresh": (0.6, 0.7, 0.8),
    "door_thresh": (0.5, 0.6, 0.7),
    "estop_thresh": (0.7, 0.8, 0.9),
}

ABLATION_VARIANTS = (
    ("baseline", AblationSwitch()),
    ("no_fall", AblationSwitch(no_fall=True)),
    ("no_imu", AblationSwitch(no_imu=True)),
    ("no_temporal", AblationSwitch(no_temporal=True)),
)


def prepare_cases(cfg: RuntimeConfig, seed: int = 0, fps: int = 1) -> dict:
    """一次性生成并解析 5 段数据，供标定/消融反复重放（避免重复生成与 IO）。"""
    prepared = {}
    for kind in KINDS:
        bundle = generate_scenario(kind, cfg, seed=seed, fps=fps)
        paths = write_bundle(bundle)
        imu = detect_imu(paths["imu_npy"])
        prepared[kind] = {"bundle": bundle, "vision": bundle.vision, "imu": imu}
    return prepared


def evaluate_prepared(prepared: dict, cfg: RuntimeConfig, labels: dict) -> List[dict]:
    records = []
    for kind, data in prepared.items():
        runner = FusionRunner(cfg)
        decisions = runner.run(data["vision"], data["imu"])
        case = f"demo_{kind}"
        observed_ms = int(data["vision"][-1].ts - data["vision"][0].ts) + 1000
        stop_enter_ts = next(
            (row["ts"] for row in runner.trace if row["state"] == "STOPPED"), None
        )
        result = {
            "case": case,
            "decisions": decisions,
            "trace": runner.trace,
            "observed_ms": observed_ms,
            "stop_enter_ts": stop_enter_ts,
        }
        records.append(build_record(result, labels[case]))
    return records


def run_calibration(prepared: dict, cfg: RuntimeConfig, labels: dict, out_dir: pathlib.Path) -> dict:
    rows = []
    for dwell in CALIBRATION_GRID["dwell_threshold"]:
        for fall in CALIBRATION_GRID["fall_thresh"]:
            for door in CALIBRATION_GRID["door_thresh"]:
                for estop in CALIBRATION_GRID["estop_thresh"]:
                    candidate = dataclasses.replace(
                        cfg,
                        thresholds=dataclasses.replace(
                            cfg.thresholds,
                            dwell_threshold=dwell,
                            fall_thresh=fall,
                            door_thresh=door,
                            estop_thresh=estop,
                        ),
                    )
                    metrics = compute_metrics(evaluate_prepared(prepared, candidate, labels), candidate)
                    rows.append(
                        {
                            "dwell_threshold": dwell,
                            "fall_thresh": fall,
                            "door_thresh": door,
                            "estop_thresh": estop,
                            "normal_trapped_frames": metrics["normal_trapped_frames"],
                            "trapped_precision": metrics["trapped_segment"]["precision"],
                            "trapped_recall": metrics["trapped_segment"]["recall"],
                            "trapped_f1": metrics["trapped_segment"]["f1"],
                            "miss_rate": metrics["miss_rate"],
                        }
                    )

    valid = [row for row in rows if row["normal_trapped_frames"] == 0]
    th = cfg.thresholds
    # 四级排序：recall 降 → f1 降 → 离当前基线越近越优 → dwell 越保守越优
    def _key(row):
        deviation = (
            abs(row["fall_thresh"] - th.fall_thresh)
            + abs(row["door_thresh"] - th.door_thresh)
            + abs(row["estop_thresh"] - th.estop_thresh)
        )
        return (
            -row["trapped_recall"],
            -row["trapped_f1"],
            abs(row["dwell_threshold"] - th.dwell_threshold),
            deviation,
            -row["dwell_threshold"],
        )

    valid.sort(key=_key)
    best = valid[0] if valid else rows[0]

    out_dir.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with (out_dir / "threshold_grid.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    (out_dir / "threshold_calibration.md").write_text(
        render_calibration_md(rows, best, cfg, len(valid)), encoding="utf-8"
    )
    return {"rows": rows, "best": best, "valid_count": len(valid)}


def render_calibration_md(rows: list, best: dict, cfg: RuntimeConfig, valid_count: int) -> str:
    lines = [
        "# 阈值标定",
        "",
        f"> 口径：{SCOPE_STATEMENT}；数据来源：{DATA_SOURCE_MOCK}。",
        f"> 网格：dwell {list(CALIBRATION_GRID['dwell_threshold'])}、"
        f"fall {list(CALIBRATION_GRID['fall_thresh'])}、"
        f"door {list(CALIBRATION_GRID['door_thresh'])}、"
        f"estop {list(CALIBRATION_GRID['estop_thresh'])}，共 {len(rows)} 组。",
        "",
        "## 1. 正常场景误报（第一道过滤）",
        "",
        f"- 满足「normal 段 trapped 帧数 == 0」的候选：{valid_count} / {len(rows)}",
        "- 选择顺序固定为：先过滤零误报，再在剩余集内按 trapped recall → F1 排序（REQ-FUNC-050）。",
        "",
        "## 2. 选定阈值（召回）",
        "",
        f"- dwell_threshold = {best['dwell_threshold']}",
        f"- fall_thresh = {best['fall_thresh']}",
        f"- door_thresh = {best['door_thresh']}",
        f"- estop_thresh = {best['estop_thresh']}",
        f"- trapped recall = {best['trapped_recall']:.6f}，"
        f"precision = {best['trapped_precision']:.6f}，f1 = {best['trapped_f1']:.6f}",
        "",
        "## 3. 说明",
        "",
        "- 若选定值与 `model/config.py` 当前默认一致，则记录为「维持初值」。",
        "- 被淘汰候选的完整数字保存在 `results/threshold_grid.csv`，可逐行追溯。",
        "",
    ]
    return "\n".join(lines)


def _macro_scores(metrics: dict) -> dict:
    count = len(EVENT_TYPES)
    return {
        "precision": _round(sum(metrics["per_class"][c]["precision"] for c in EVENT_TYPES) / count),
        "recall": _round(sum(metrics["per_class"][c]["recall"] for c in EVENT_TYPES) / count),
        "f1": _round(sum(metrics["per_class"][c]["f1"] for c in EVENT_TYPES) / count),
    }


def run_ablation(prepared: dict, cfg: RuntimeConfig, labels: dict) -> List[dict]:
    rows = []
    for name, switch in ABLATION_VARIANTS:
        variant_cfg = dataclasses.replace(cfg, ablation=switch)
        metrics = compute_metrics(evaluate_prepared(prepared, variant_cfg, labels), variant_cfg)
        macro = _macro_scores(metrics)
        rows.append(
            {
                "variant": name,
                "precision": macro["precision"],
                "recall": macro["recall"],
                "f1": macro["f1"],
                "fp_count": metrics["normal_trapped_frames"],
                "faint_recall": metrics["per_class"]["faint"]["recall"],
                "estop_recall": metrics["per_class"]["estop"]["recall"],
                "trapped_recall": metrics["per_class"]["trapped"]["recall"],
            }
        )

    base = rows[0]
    for row in rows:
        for key in ("precision", "recall", "f1", "fp_count"):
            row[f"delta_{key}"] = _round(row[key] - base[key])
    return rows


def plot_ablation(rows: List[dict], out_path: pathlib.Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    variants = [row["variant"] for row in rows]
    metrics = ("precision", "recall", "f1")
    x = np.arange(len(variants))
    width = 0.22
    figure, axis = plt.subplots(figsize=(10, 5))
    for index, name in enumerate(metrics):
        values = [row[name] for row in rows]
        positions = x + (index - 1) * width
        axis.bar(positions, values, width, label=name)
        for position, value in zip(positions, values):
            axis.text(position, value, f"{value:.3g}", ha="center", va="bottom", fontsize=7)
    axis.set_xticks(x)
    axis.set_xticklabels(
        [f"{row['variant']}\nfp={row['fp_count']}" for row in rows], fontsize=8
    )
    axis.set_title("Ablation on 5-class macro scores (fp = normal trapped frames)")
    axis.legend()
    figure.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, dpi=120)
    plt.close(figure)


def write_ablation(rows: List[dict], out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "variant",
        "precision",
        "recall",
        "f1",
        "fp_count",
        "faint_recall",
        "estop_recall",
        "trapped_recall",
        "delta_precision",
        "delta_recall",
        "delta_f1",
        "delta_fp_count",
    ]
    with (out_dir / "ablation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    plot_ablation(rows, out_dir / "ablation.png")


def _load_labels(path: pathlib.Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"labels not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval", description="B 侧评测：5 段场景 → Decision 与指标")
    parser.add_argument("--cases", default="all", help="all 或逗号分隔的场景类别")
    parser.add_argument("--mock", action="store_true", help="使用合成场景库")
    parser.add_argument("--demo", action="store_true", help="使用演示阈值预设")
    parser.add_argument("--out", default="results", help="产物目录")
    parser.add_argument("--seed", type=int, default=0, help="合成数据随机种子")
    parser.add_argument("--labels", default="results/labels.json", help="人工标注真值")
    parser.add_argument("--calibrate", action="store_true", help="执行阈值标定网格")
    parser.add_argument("--ablation", action="store_true", help="执行四项消融")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    command = "python model/eval.py " + " ".join(argv if argv is not None else sys.argv[1:])
    args = _build_parser().parse_args(argv)

    config_argv: List[str] = []
    if args.demo:
        config_argv.append("--demo")
    config_argv += ["--seed", str(args.seed)]

    try:
        cfg = build_runtime_config(config_argv)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not args.mock:
        print(
            "real-data mode not available in this stage; use --mock "
            "(A 侧真实 VisionOut 与 npy 到位后接入)",
            file=sys.stderr,
        )
        return 2

    selected = list(KINDS) if args.cases == "all" else [c.strip() for c in args.cases.split(",")]
    unknown = [kind for kind in selected if kind not in KINDS]
    if unknown:
        print(f"unknown cases: {unknown}", file=sys.stderr)
        return 2

    data_source = DATA_SOURCE_MOCK
    out_dir = pathlib.Path(args.out)
    labels = _load_labels(pathlib.Path(args.labels))

    if args.calibrate:
        prepared = prepare_cases(cfg, seed=args.seed)
        outcome = run_calibration(prepared, cfg, labels, out_dir)
        print(
            f"标定完成：{outcome['valid_count']} / {len(outcome['rows'])} 组满足零误报，"
            f"最优 dwell={outcome['best']['dwell_threshold']} "
            f"fall={outcome['best']['fall_thresh']} "
            f"door={outcome['best']['door_thresh']} "
            f"estop={outcome['best']['estop_thresh']}"
        )
        print(f"产物：{out_dir / 'threshold_calibration.md'}、{out_dir / 'threshold_grid.csv'}")
        return 0

    if args.ablation:
        prepared = prepare_cases(cfg, seed=args.seed)
        rows = run_ablation(prepared, cfg, labels)
        write_ablation(rows, out_dir)
        for row in rows:
            print(
                f"{row['variant']:<12} precision={row['precision']:.3f} "
                f"recall={row['recall']:.3f} f1={row['f1']:.3f} fp={row['fp_count']}"
            )
        print(f"产物：{out_dir / 'ablation.csv'}、{out_dir / 'ablation.png'}")
        return 0

    results = []
    failed = 0
    for index, kind in enumerate(selected, start=1):
        try:
            result = run_case(kind, cfg, seed=args.seed)
            materialize(result, out_dir)
            results.append(result)
            event = segment_level(result["decisions"])
            trapped_frames = sum(1 for item in result["decisions"] if item.trapped)
            print(
                f"[{index}/{len(selected)}] {result['case']:<14} frames={len(result['decisions']):<5} "
                f"event={event:<8} trapped_frames={trapped_frames:<4} OK"
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.error("case failed: %s (%s)", kind, exc)
            print(f"[{index}/{len(selected)}] demo_{kind:<9} FAILED ({exc})")

    if not results:
        return 1

    records = [build_record(result, labels[result["case"]]) for result in results]
    metrics = compute_metrics(records, cfg)

    write_metrics(metrics, out_dir, cfg, command, data_source)
    write_claims(metrics, out_dir, "results/metrics.csv", data_source)
    plot_confusion(records, out_dir / "confusion_matrix.png")
    (out_dir / "config_snapshot.json").write_text(
        json.dumps(config_snapshot(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"汇总：见 {out_dir / 'metrics.md'}（口径：{SCOPE_STATEMENT}）")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())