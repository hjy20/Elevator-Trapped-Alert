"""出一张"效果总览图"：5 类场景 × 4 个时刻，直接看规则判得对不对。

    python data/scripts/make_overview.py                 # -> results/figures/overview_cases.png
    python data/scripts/make_overview.py --times 2 8 12 17

从带标注视频（results/annotated/）抽帧拼图，每行一个场景，行首写清
"场景 / 期望 / 实际峰值"，方便一眼看出：倒地只在 demo_faint 报警、
贴门只在 demo_pry 抬升、蹲下与急停都没误报。

前置：先跑 `python vision/visualize.py --all`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.scripts.build_demo_data import CASE_ORDER  # noqa: E402
from data.scripts.make_stats import CASE_CN, COLORS, FIG_DIR, _setup_matplotlib  # noqa: E402
from vision.config import RESULTS_DIR  # noqa: E402

EXPECT = {
    "demo_normal": "蹲下捡东西 → 不报警",
    "demo_estop": "急停前倾 → 不报警",
    "demo_trapped": "门不开 → 靠 dwell 判困人",
    "demo_pry": "扒门 → door_approach 抬升",
    "demo_faint": "躺倒 → fall_prob 报警",
}


def _grab(video: Path, times: Sequence[float]) -> List["object"]:
    import cv2

    frames = []
    cap = cv2.VideoCapture(str(video))
    try:
        for sec in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
            ok, image = cap.read()
            if ok:
                frames.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    return frames


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成效果总览图")
    parser.add_argument("--times", type=float, nargs="*", default=[2.0, 7.0, 12.0, 17.0])
    parser.add_argument("--out", default="results/figures/overview_cases.png")
    args = parser.parse_args(argv)

    rows = []
    for case in CASE_ORDER:
        video = RESULTS_DIR / "annotated" / f"{case}_annotated.mp4"
        outputs = RESULTS_DIR / f"vision_{case}.json"
        if not video.exists():
            print(f"[warn] 缺标注视频 {video.name}，先跑 vision/visualize.py --all")
            continue
        frames = _grab(video, args.times)
        if not frames:
            print(f"[warn] {video.name} 抽帧失败")
            continue
        fall_peak = door_peak = 0.0
        if outputs.exists():
            data = json.loads(outputs.read_text(encoding="utf-8"))
            fall_peak = max((o["fall_prob"] for o in data), default=0.0)
            door_peak = max((o["door_approach"] for o in data), default=0.0)
        rows.append({"case": case, "frames": frames, "fall": fall_peak, "door": door_peak})

    if not rows:
        print("[warn] 没有可用的标注视频")
        return 1

    plt = _setup_matplotlib()
    ncol = max(len(r["frames"]) for r in rows)
    fig, axes = plt.subplots(
        len(rows), ncol, figsize=(3.1 * ncol + 2.6, 1.95 * len(rows) + 0.9),
        gridspec_kw={"wspace": 0.03, "hspace": 0.05},
    )
    axes = axes.reshape(len(rows), ncol)

    for r, row in enumerate(rows):
        color = COLORS.get(row["case"], "#333333")
        for c in range(ncol):
            ax = axes[r, c]
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            if c < len(row["frames"]):
                ax.imshow(row["frames"][c])
                if r == 0:
                    ax.set_title(f"t = {args.times[c]:.0f}s", fontsize=9, pad=3, color="#444444")
            else:
                ax.axis("off")
        # 行首信息（写在第一格左侧）
        axes[r, 0].set_ylabel(
            f"{CASE_CN.get(row['case'], row['case'])}\n{row['case']}\n\n"
            f"期望：{EXPECT.get(row['case'], '')}\n"
            f"实测：fall={row['fall']:.2f}  door={row['door']:.2f}",
            fontsize=8.4, rotation=0, ha="right", va="center",
            color=color, labelpad=132,
        )

    fig.suptitle(
        "5 类场景的规则输出（画面上的 HUD 就是 A 交给 B 的 VisionOut 字段）",
        fontsize=12, y=0.985,
    )
    fig.text(
        0.5, 0.012,
        "合成场景库自检口径：真值来自生成器，本图证明规则实现与设计一致，不代表真实视频精度",
        ha="center", fontsize=8, color="#777777",
    )
    out = Path(args.out)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"  ✓ {out.relative_to(PROJECT_ROOT)}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())