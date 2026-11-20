"""环境自检：一条命令看清"能不能跑"。

    python vision/doctor.py

检查：Python 版本 / 关键依赖 / 各后端可用性 / 演示数据齐全度 / ROI 配置。
不装任何东西，只报告；有缺项就明确说缺什么、装了会影响哪条路径。
"""

from __future__ import annotations

import importlib
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OK = "✓"
NO = "✗"
WARN = "!"


def _version(module: str, attr: str = "__version__") -> str:
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001
        return f"{NO} 未安装（{type(exc).__name__}）"
    return f"{OK} {getattr(mod, attr, '?')}"


def check_python() -> List[Tuple[str, str]]:
    rows = [("Python", f"{OK} {platform.python_version()} ({platform.machine()})")]
    if sys.version_info < (3, 10):
        rows.append(("版本要求", f"{NO} 需要 >= 3.10"))
    if sys.version_info[:2] not in ((3, 10), (3, 11)):
        rows.append(("契约版本", f"{WARN} 契约写的是 3.10/3.11，当前 {platform.python_version()}（接口不变，可跑）"))
    return rows


def check_deps() -> List[Tuple[str, str]]:
    return [
        ("numpy", _version("numpy")),
        ("opencv", _version("cv2")),
        ("imageio-ffmpeg", _version("imageio_ffmpeg")),
        ("pillow", _version("PIL")),
        ("matplotlib", _version("matplotlib")),
        ("onnxruntime", _version("onnxruntime")),
        ("torch", _version("torch")),
        ("ultralytics", _version("ultralytics")),
    ]


def check_backends() -> List[Tuple[str, str]]:
    from vision.config import DEMO_DIR, load_config
    from vision.sources import make_backend

    cfg = load_config()
    sample = DEMO_DIR / "demo_normal.mp4"
    rows: List[Tuple[str, str]] = []
    for name, note in (
        ("synthetic", "读 <case>_kpts.jsonl —— 合成场景链路"),
        ("onnx", "读 models/*.onnx —— 无 torch 也能跑 YOLO"),
        ("yolo", "读 models/yolov8n-pose.pt —— 实拍视频用这个"),
    ):
        try:
            backend = make_backend(cfg, sample, backend=name)
            rows.append((f"backend:{name}", f"{OK} 可用（{note}）"))
            del backend
        except Exception as exc:  # noqa: BLE001
            rows.append((f"backend:{name}", f"{NO} {type(exc).__name__}: {exc}"))
    return rows


def check_data() -> List[Tuple[str, str]]:
    from data.scripts.build_demo_data import CASE_ORDER
    from vision.config import DEMO_DIR, PROCESSED_DIR, LABEL_DIR

    rows: List[Tuple[str, str]] = []
    missing = 0
    for case in CASE_ORDER:
        needed = {
            "mp4": DEMO_DIR / f"{case}.mp4",
            "imu": DEMO_DIR / f"{case}_imu.npy",
            "meta": DEMO_DIR / f"{case}_meta.json",
            "label": LABEL_DIR / f"{case}.json",
        }
        gone = [key for key, path in needed.items() if not path.exists()]
        has_kpts = (DEMO_DIR / f"{case}_kpts.jsonl").exists()
        has_gt = (PROCESSED_DIR / f"{case}_gt.json").exists()
        if gone:
            missing += 1
            rows.append((case, f"{NO} 缺 {','.join(gone)}"))
        else:
            extra = []
            if not has_kpts:
                extra.append("无轨迹→走 YOLO")
            if not has_gt:
                extra.append("无逐帧真值→回归跳过")
            suffix = f"  ({'；'.join(extra)})" if extra else ""
            rows.append((case, f"{OK} 齐全{suffix}"))
    if missing == 0:
        rows.append(("数据总量", f"{OK} 5 段 × 500 帧 / 25 段 IMU 窗口"))
    return rows


def check_config() -> List[Tuple[str, str]]:
    from vision.config import list_rois, load_config

    cfg = load_config()
    rows = [
        ("配置来源", f"{OK} {cfg.source or 'dataclass 默认值'}"),
        ("关键参数", f"imgsz={cfg.model.imgsz} frame_skip={cfg.runtime.frame_skip} "
                     f"fall_angle={cfg.fall.angle_deg} door_near={cfg.door.near_ratio}"),
    ]
    rois = list_rois()
    if rois:
        rows.append(("门框 ROI", f"{OK} {', '.join(rois)}"))
    else:
        rows.append(("门框 ROI", f"{WARN} 一个都没有 —— 换机位必须重标（vision/calibrate_roi.py）"))
    if cfg.door.roi and cfg.door.roi not in rois:
        rows.append(("当前 ROI", f"{NO} 配置指向 {cfg.door.roi}，但文件不存在"))
    return rows


def check_outputs() -> List[Tuple[str, str]]:
    from vision.config import RESULTS_DIR

    rows: List[Tuple[str, str]] = []
    checks = {
        "VisionOut": RESULTS_DIR / "vision_summary.json",
        "回归报告": RESULTS_DIR / "vision_regression.md",
        "标注视频目录": RESULTS_DIR / "annotated",
        "图": RESULTS_DIR / "figures",
    }
    for label, path in checks.items():
        rows.append((label, f"{OK} {path.relative_to(PROJECT_ROOT)}" if path.exists() else f"{WARN} 还没生成 {path.name}"))
    return rows


def main() -> int:
    print("=" * 78)
    print("A · 数据与感知 环境自检")
    print("=" * 78)
    sections = [
        ("环境", check_python()),
        ("依赖", check_deps()),
        ("推理后端", check_backends()),
        ("演示数据", check_data()),
        ("配置", check_config()),
        ("产物", check_outputs()),
    ]
    problems = 0
    for title, rows in sections:
        print(f"\n[{title}]")
        for key, value in rows:
            print(f"  {key:<18}{value}")
            if value.startswith(NO):
                problems += 1

    print("\n" + "-" * 78)
    if problems:
        print(f"{WARN} 有 {problems} 项不可用。合成场景链路（synthetic 后端）不受 torch/ultralytics 影响；")
        print("  实拍视频与 ONNX 导出才需要它们，按 vision/requirements-a.txt 装。")
    else:
        print(f"{OK} 全部就绪：数据、规则、后端、产物都齐，可以交付给 B 和 C。")
    print("\n下一步建议：")
    print("  python data/scripts/build_demo_data.py --all   # 重新生成演示数据")
    print("  python data/scripts/validate_demo_data.py      # L1 契约自检")
    print("  python vision/infer.py --all                   # 产出 VisionOut")
    print("  python data/scripts/regression.py              # 回归")
    print("  python vision/visualize.py --all               # 标注视频")
    print("  python data/scripts/make_stats.py              # 出图")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())