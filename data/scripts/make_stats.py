"""出图：数据统计图 + 曲线 + 架构图（C 做 PPT/视频的素材，A 的交付物之一）。

    python data/scripts/make_stats.py                 # 全部图
    python data/scripts/make_stats.py --only curves    # 只出曲线

产物（results/figures/）：
    fig_dataset_overview.png   数据集规模（帧数/时长/IMU 窗口数）
    fig_person_count.png       人数：规则输出 vs 逐帧真值
    fig_fall_prob.png          fall_prob 曲线 + 真值倒地时刻
    fig_door_approach.png      贴门曲线（demo_pry 应显著抬升）
    fig_imu.png                5 段 IMU 的 az 波形（A/B 都能用）
    fig_pipeline.png           视觉流水线架构图（PPT P7 直接用）

前置：先跑过 vision/infer.py --all（需要 results/vision_*.json）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from data.scripts.build_demo_data import CASE_ORDER  # noqa: E402
from vision.config import DEMO_DIR, PROCESSED_DIR, RESULTS_DIR  # noqa: E402

FIG_DIR = RESULTS_DIR / "figures"
CASE_CN = {
    "demo_normal": "正常乘梯",
    "demo_estop": "电梯急停",
    "demo_trapped": "停梯困人",
    "demo_pry": "扒门求助",
    "demo_faint": "轿厢晕倒",
}
COLORS = {
    "demo_normal": "#4c9f70",
    "demo_estop": "#e0a13b",
    "demo_trapped": "#d1743a",
    "demo_pry": "#c9a227",
    "demo_faint": "#c0392b",
}


def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    preferred = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    picked = [name for name in preferred if name in available] or ["DejaVu Sans"]
    plt.rcParams["font.sans-serif"] = picked
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.dpi"] = 140
    plt.rcParams["savefig.bbox"] = "tight"
    plt.rcParams["axes.edgecolor"] = "#888888"
    return plt


def _load_results() -> Dict[str, List[dict]]:
    data: Dict[str, List[dict]] = {}
    for case in CASE_ORDER:
        path = RESULTS_DIR / f"vision_{case}.json"
        if path.exists():
            data[case] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_gt(case: str) -> Optional[List[dict]]:
    path = PROCESSED_DIR / f"{case}_gt.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


# ---------------------------------------------------------------------------
# 图 1：数据集规模
# ---------------------------------------------------------------------------


def fig_dataset_overview(plt) -> Optional[Path]:
    rows = []
    for case in CASE_ORDER:
        meta = DEMO_DIR / f"{case}_meta.json"
        imu = DEMO_DIR / f"{case}_imu.npy"
        if not meta.exists():
            continue
        payload = json.loads(meta.read_text(encoding="utf-8"))
        windows = np.load(imu).shape[0] if imu.exists() else 0
        rows.append((case, payload.get("frames", 0), payload.get("duration_sec", 0), windows))
    if not rows:
        return None

    labels = [CASE_CN.get(r[0], r[0]) for r in rows]
    frames = [r[1] for r in rows]
    windows = [r[3] for r in rows]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8))
    bars = axes[0].bar(labels, frames, color=[COLORS.get(r[0], "#666") for r in rows])
    axes[0].set_title("每段演示数据的帧数（25fps，20s）")
    axes[0].set_ylabel("帧")
    axes[0].bar_label(bars, padding=2, fontsize=9)
    axes[0].set_ylim(0, max(frames) * 1.25)

    bars2 = axes[1].bar(labels, windows, color=[COLORS.get(r[0], "#666") for r in rows], alpha=0.85)
    axes[1].set_title("IMU 窗口数（(N,200,3)，20Hz / 10s 窗口 / 5s 步长）")
    axes[1].set_ylabel("窗口数")
    axes[1].bar_label(bars2, padding=2, fontsize=9)
    axes[1].set_ylim(0, max(windows) * 1.35 if windows else 1)
    for ax in axes:
        ax.tick_params(axis="x", rotation=12)
    total = sum(frames)
    fig.suptitle(f"自建场景库：5 类 × {rows[0][2]:.0f}s = {total} 帧 / {total / 25:.0f} 秒", fontsize=11)
    fig.tight_layout()
    out = FIG_DIR / "fig_dataset_overview.png"
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 图 2：人数曲线
# ---------------------------------------------------------------------------


def fig_person_count(plt, data: Dict[str, List[dict]]) -> Optional[Path]:
    if not data:
        return None
    fig, axes = plt.subplots(len(data), 1, figsize=(9.5, 1.85 * len(data)), sharex=False)
    if len(data) == 1:
        axes = [axes]
    for ax, (case, outputs) in zip(axes, data.items()):
        ts = [(o["ts"] - outputs[0]["ts"]) / 1000.0 for o in outputs]
        values = [o["person_count"] for o in outputs]
        ax.step(ts, values, where="post", color=COLORS.get(case, "#333"), linewidth=1.8, label="规则输出")
        gt = _load_gt(case)
        if gt:
            gt_ts = [row["t"] for row in gt[: len(ts)]]
            gt_vals = [row["person_count"] for row in gt[: len(ts)]]
            ax.step(gt_ts, gt_vals, where="post", color="#444", linewidth=1.0, linestyle="--", alpha=0.65, label="逐帧真值")
        ax.set_ylabel("人数")
        ax.set_yticks(range(0, 4))
        ax.set_ylim(-0.3, 3.2)
        ax.set_title(f"{CASE_CN.get(case, case)}（{case}）", fontsize=9, loc="left")
        ax.grid(alpha=0.25, linestyle=":")
        if ax is axes[0]:
            ax.legend(loc="upper right", fontsize=8, frameon=False)
    axes[-1].set_xlabel("时间 (s)")
    fig.suptitle("人数：头部/肩膀关键点聚类 vs 逐帧真值", fontsize=11)
    fig.tight_layout()
    out = FIG_DIR / "fig_person_count.png"
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 图 3/4：fall / door 曲线
# ---------------------------------------------------------------------------


def _curve_figure(plt, data: Dict[str, List[dict]], key: str, title: str, ylabel: str, fname: str, threshold: float, gt_key: Optional[str]) -> Optional[Path]:
    if not data:
        return None
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    for case, outputs in data.items():
        ts = [(o["ts"] - outputs[0]["ts"]) / 1000.0 for o in outputs]
        values = [o[key] for o in outputs]
        ax.plot(ts, values, color=COLORS.get(case, "#333"), linewidth=1.7, label=CASE_CN.get(case, case))
    ax.axhline(threshold, color="#c0392b", linestyle="--", linewidth=1.0, alpha=0.8, label=f"阈值 {threshold}")
    if gt_key == "fallen":
        for case in data:
            gt = _load_gt(case)
            if not gt:
                continue
            onset = next((row["t"] for row in gt if row["fallen"] > 0), None)
            if onset is not None:
                ax.axvline(onset, color=COLORS.get(case, "#333"), linestyle=":", linewidth=1.1, alpha=0.8)
                ax.annotate("真值倒地", (onset, 0.98), fontsize=8, color=COLORS.get(case, "#333"), rotation=90, va="top")
    ax.set_xlabel("时间 (s)")
    ax.set_ylabel(ylabel)
    ax.set_ylim(-0.03, 1.03)
    ax.grid(alpha=0.25, linestyle=":")
    ax.set_title(title, fontsize=11)
    ax.legend(fontsize=8, frameon=False, ncol=2, loc="lower right")
    fig.tight_layout()
    out = FIG_DIR / fname
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 图 5：IMU
# ---------------------------------------------------------------------------


def fig_imu(plt) -> Optional[Path]:
    files = [(case, DEMO_DIR / f"{case}_imu_raw.npy") for case in CASE_ORDER]
    files = [(c, p) for c, p in files if p.exists()]
    if not files:
        return None
    fig, axes = plt.subplots(len(files), 1, figsize=(9.5, 1.6 * len(files)), sharex=True)
    if len(files) == 1:
        axes = [axes]
    for ax, (case, path) in zip(axes, files):
        raw = np.load(path)
        t = np.arange(len(raw)) / 20.0
        ax.plot(t, raw[:, 2] - 9.81, color=COLORS.get(case, "#333"), linewidth=1.0)
        ax.set_ylabel("az-9.81\n(m/s²)", fontsize=8)
        ax.set_title(f"{CASE_CN.get(case, case)}（{case}）", fontsize=9, loc="left")
        ax.grid(alpha=0.22, linestyle=":")
    axes[-1].set_xlabel("时间 (s)")
    fig.suptitle("IMU 竖直加速度（去重力）：急停/拍门/倒地冲击在信号上的形态", fontsize=11)
    fig.tight_layout()
    out = FIG_DIR / "fig_imu.png"
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 图 6：流水线架构图
# ---------------------------------------------------------------------------


def fig_pipeline(plt) -> Optional[Path]:
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    fig, ax = plt.subplots(figsize=(11.5, 5.0))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 46)
    ax.axis("off")

    def box(x, y, w, h, title, body, color, text_color="#111"):
        ax.add_patch(
            FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.2",
                           linewidth=1.4, edgecolor=color, facecolor=color + "22")
        )
        ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="top", fontsize=10.5, fontweight="bold", color=text_color)
        ax.text(x + w / 2, y + h / 2 - 2.2, body, ha="center", va="center", fontsize=8.6, color="#333333", linespacing=1.5)

    def arrow(x1, y1, x2, y2, label=""):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=14,
                                     linewidth=1.3, color="#555555"))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 1.4, label, ha="center", fontsize=8, color="#444444")

    box(2, 26, 26, 15, "① 视频读入", "1280×720 · 25fps\n跳帧 2（中间帧复用关键点）", "#2b6cb0")
    box(34, 26, 30, 15, "② 姿态关键点", "YOLOv8n-Pose（CPU, 480）\n17 关键点 / 人或 ONNX / 合成轨迹", "#2f855a")
    box(70, 26, 28, 15, "③ 几何规则", "人数聚类 · 躯干夹角\n腕-门缝距离 · 面积占比", "#b7791f")
    box(34, 3, 30, 15, "④ 时序滤波", "3s 投票（倒地）\n1s 持续（贴门）· 中值滤波", "#9b2c2c")
    box(70, 3, 28, 15, "⑤ VisionOut", "ts / person_count\nfall_prob / door_approach\ncrowded / keypoints_ok", "#553c9a")

    arrow(28, 33.5, 34, 33.5, "帧")
    arrow(64, 33.5, 70, 33.5, "关键点")
    arrow(84, 26, 60, 18, "逐帧疑似/得分")
    arrow(70, 10.5, 84, 18, "") if False else None
    arrow(64, 10.5, 70, 10.5, "平滑后")
    arrow(49, 26, 49, 18, "时间戳")

    ax.text(2, 44, "A 的视觉流水线（双通道中的视觉主通道）", fontsize=12.5, fontweight="bold")
    ax.text(2, 40.4, "所有环节都在 CPU 上跑；合成轨迹只是一个可替换的关键点来源，规则层与真机完全一致",
            fontsize=8.8, color="#555555")
    fig.tight_layout()
    out = FIG_DIR / "fig_pipeline.png"
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成统计图与架构图")
    parser.add_argument("--only", nargs="*", choices=["dataset", "count", "fall", "door", "imu", "pipeline"])
    args = parser.parse_args(argv)

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    plt = _setup_matplotlib()
    data = _load_results()
    if not data:
        print("[warn] results/vision_*.json 不存在，先跑 `python vision/infer.py --all`")

    wanted = set(args.only or ["dataset", "count", "fall", "door", "imu", "pipeline"])
    made: List[Path] = []
    if "dataset" in wanted:
        made.append(fig_dataset_overview(plt))
    if "count" in wanted:
        made.append(fig_person_count(plt, data))
    if "fall" in wanted:
        made.append(_curve_figure(plt, data, "fall_prob", "倒地概率 fall_prob：3 秒滑动窗口投票", "fall_prob", "fig_fall_prob.png", 0.7, "fallen"))
    if "door" in wanted:
        made.append(_curve_figure(plt, data, "door_approach", "贴门程度 door_approach：腕部进门缝 ROI 且持续 >1s", "door_approach", "fig_door_approach.png", 0.6, None))
    if "imu" in wanted:
        made.append(fig_imu(plt))
    if "pipeline" in wanted:
        made.append(fig_pipeline(plt))

    for path in made:
        if path:
            print(f"  ✓ {path.relative_to(PROJECT_ROOT)}")
    if not made:
        print("[warn] 一张图都没生成（缺前置数据？）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())