"""L1 数据契约自检：把"格式对不对"变成一条命令。

    python data/scripts/validate_demo_data.py            # 检查全部 5 段
    python data/scripts/validate_demo_data.py --case demo_faint
    python data/scripts/validate_demo_data.py --json results/contract_check.json

检查项（对照《00 协作总览与接口契约》第 6 节逐条实现）：
  · 文件名全英文小写，无中文
  · 视频：.mp4 / H.264 / 1280x720 / 25fps
  · IMU：float32 / (N,200,3) / 通道顺序 ax,ay,az / 归一化到 [-1,1] / 无 NaN
  · 关键点轨迹：每帧 17 点、ts 单调递增且与帧对齐
  · meta / 场景标签 / 逐帧真值齐全且互相一致

退出码：0 全通过；1 有硬错误。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from data.scripts.build_demo_data import CASE_ORDER  # noqa: E402
from data.scripts.video_io import ffmpeg_exe  # noqa: E402
from vision.config import DEMO_DIR, LABEL_DIR, PROCESSED_DIR, ROI_DIR  # noqa: E402

EXPECTED_EVENT_URGENCY = {"normal": 0, "estop": 1, "trapped": 2, "pry": 2, "faint": 3}
NAME_RE = re.compile(r"^[a-z0-9_\-]+$")


class Checker:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: Dict[str, Any] = {}

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # -- 具体检查 ----------------------------------------------------------
    def check_names(self, case: str) -> None:
        if not NAME_RE.match(case):
            self.error(f"[{case}] 名字必须是全英文小写/数字/下划线")

    def check_video(self, case: str) -> Optional[Dict[str, Any]]:
        import cv2

        path = DEMO_DIR / f"{case}.mp4"
        if not path.exists():
            self.error(f"[{case}] 缺视频 {path.name}")
            return None
        cap = cv2.VideoCapture(str(path))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        codec = ""
        if ffmpeg_exe():
            result = subprocess.run(
                [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, errors="ignore",
            )
            for line in result.stderr.splitlines():
                if "Video:" in line:
                    codec = line.split("Video:")[1].split(",")[0].strip()
                    break
        if abs(fps - 25.0) > 0.05:
            self.error(f"[{case}] 帧率 {fps:.2f} != 25")
        if (width, height) != (1280, 720):
            self.error(f"[{case}] 分辨率 {width}x{height} != 1280x720")
        if codec and "h264" not in codec.lower():
            self.error(f"[{case}] 编码 {codec} 不是 H.264")
        if frames <= 0:
            self.error(f"[{case}] 读不到帧数")
        return {"fps": round(fps, 2), "size": f"{width}x{height}", "frames": frames, "codec": codec or "?"}

    def check_imu(self, case: str) -> Optional[Dict[str, Any]]:
        path = DEMO_DIR / f"{case}_imu.npy"
        if not path.exists():
            self.error(f"[{case}] 缺 IMU {path.name}")
            return None
        try:
            arr = np.load(path)
        except Exception as exc:  # noqa: BLE001
            self.error(f"[{case}] IMU 读不出来：{exc}")
            return None
        if arr.dtype != np.float32:
            self.error(f"[{case}] IMU dtype={arr.dtype}，契约要求 float32")
        if arr.ndim != 3 or arr.shape[1] != 200 or arr.shape[2] != 3:
            self.error(f"[{case}] IMU shape={arr.shape}，契约要求 (N,200,3)")
        if not np.isfinite(arr).all():
            self.error(f"[{case}] IMU 含 NaN/Inf")
        lo, hi = float(np.min(arr)), float(np.max(arr))
        if lo < -1.001 or hi > 1.001:
            self.error(f"[{case}] IMU 取值范围 [{lo:.2f},{hi:.2f}] 超出 [-1,1]")
        # B 的读取约定：<case>_imu.meta.json 里的 start_ts_ms（model/config.py: sidecar_path）
        meta_file = DEMO_DIR / f"{case}_imu.meta.json"
        if not meta_file.exists():
            self.error(f"[{case}] 缺 {meta_file.name}（B 拿不到窗口时间戳，会落到默认基准）")
        else:
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                self.error(f"[{case}] {meta_file.name} 解析失败：{exc}")
                meta = None
            if isinstance(meta, dict):
                start = meta.get("start_ts_ms")
                if not isinstance(start, int) or isinstance(start, bool):
                    self.error(f"[{case}] {meta_file.name} 缺整型 start_ts_ms")
                elif start < 10**12:
                    self.error(f"[{case}] start_ts_ms={start} 不是毫秒量级")
                else:
                    ends = meta.get("window_end_ts_ms") or []
                    if ends:
                        expected = start + 10_000
                        if abs(int(ends[0]) - expected) > 1:
                            self.error(
                                f"[{case}] start_ts_ms 与首个窗口结束时间对不上："
                                f"{start}+10000={expected} vs {ends[0]}"
                            )
        return {"shape": list(arr.shape), "dtype": str(arr.dtype), "range": [round(lo, 3), round(hi, 3)]}

    def check_sidecar(self, case: str, video_frames: Optional[int]) -> Optional[Dict[str, Any]]:
        path = DEMO_DIR / f"{case}_kpts.jsonl"
        if not path.exists():
            self.warn(f"[{case}] 没有 {path.name}：会走 YOLO-Pose（实拍素材的正常状态）")
            return None
        last_ts = -1
        count = 0
        persons = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ts = int(row.get("ts", 0))
                if ts <= last_ts:
                    self.error(f"[{case}] 关键点轨迹 ts 不是单调递增（frame={row.get('frame')}）")
                    break
                last_ts = ts
                for person in row.get("persons", []):
                    arr = np.asarray(person)
                    if arr.shape != (17, 3):
                        self.error(f"[{case}] 关键点 shape={arr.shape}，应为 (17,3)")
                        return None
                persons = max(persons, len(row.get("persons", [])))
                count += 1
        if video_frames and count != video_frames:
            self.warn(f"[{case}] 轨迹帧数 {count} != 视频帧数 {video_frames}")
        return {"frames": count, "max_persons": persons}

    def check_meta(self, case: str, video: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        path = DEMO_DIR / f"{case}_meta.json"
        if not path.exists():
            self.error(f"[{case}] 缺 {path.name}")
            return None
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not meta.get("start_ts_ms"):
            self.error(f"[{case}] meta 缺 start_ts_ms（ts 会变成当前时间，跨人无法对齐）")
        roi_name = meta.get("door_roi")
        if roi_name and not (ROI_DIR / f"{roi_name}.json").exists():
            self.warn(f"[{case}] meta 指向的 ROI {roi_name} 不存在，会用内置默认矩形")
        if video and meta.get("frames") and int(meta["frames"]) != video["frames"]:
            self.warn(f"[{case}] meta.frames={meta['frames']} 与视频实际 {video['frames']} 不一致")
        return {"start_ts_ms": meta.get("start_ts_ms"), "door_roi": roi_name, "scene": meta.get("scene")}

    def check_labels(self, case: str) -> Optional[Dict[str, Any]]:
        path = LABEL_DIR / f"{case}.json"
        if not path.exists():
            self.warn(f"[{case}] 缺场景标签 data/labels/{case}.json")
            return None
        label = json.loads(path.read_text(encoding="utf-8"))
        event = label.get("event_type")
        urgency = label.get("urgency")
        if event not in EXPECTED_EVENT_URGENCY:
            self.error(f"[{case}] event_type={event!r} 不在五类内")
        elif EXPECTED_EVENT_URGENCY[event] != urgency:
            self.error(f"[{case}] event_type={event} 应对应 urgency={EXPECTED_EVENT_URGENCY[event]}，实际 {urgency}")
        gt = PROCESSED_DIR / f"{case}_gt.json"
        if not gt.exists():
            self.warn(f"[{case}] 缺逐帧真值 {gt.name}（回归测试跑不了）")
        return {"event_type": event, "urgency": urgency, "trapped": label.get("trapped")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="L1 契约自检")
    parser.add_argument("--case", choices=CASE_ORDER, default=None)
    parser.add_argument("--json", default=None, help="把结果写成 JSON")
    args = parser.parse_args(argv)

    cases = [args.case] if args.case else CASE_ORDER
    checker = Checker()
    report: Dict[str, Any] = {"cases": {}, "errors": [], "warnings": []}

    for case in cases:
        checker.check_names(case)
        video = checker.check_video(case)
        imu = checker.check_imu(case)
        sidecar = checker.check_sidecar(case, video["frames"] if video else None)
        meta = checker.check_meta(case, video)
        labels = checker.check_labels(case)
        report["cases"][case] = {
            "video": video, "imu": imu, "sidecar": sidecar, "meta": meta, "label": labels,
        }

    report["errors"] = checker.errors
    report["warnings"] = checker.warnings

    print(f"{'case':<15}{'video':<20}{'imu shape':<16}{'kpts':<10}{'event':<10}")
    for case in cases:
        row = report["cases"][case]
        video = row["video"] or {}
        imu = row["imu"] or {}
        label = row["label"] or {}
        print(
            f"{case:<15}"
            f"{(str(video.get('size')) + '@' + str(video.get('fps'))):<20}"
            f"{str(imu.get('shape')):<16}"
            f"{str((row['sidecar'] or {}).get('frames', '—')):<10}"
            f"{str(label.get('event_type', '—')):<10}"
        )

    if checker.warnings:
        print("\n[警告]")
        for warning in checker.warnings:
            print(f"  · {warning}")
    if checker.errors:
        print("\n[错误]")
        for error in checker.errors:
            print(f"  ✗ {error}")
        print(f"\n共 {len(checker.errors)} 个硬错误")
    else:
        print(f"\n✓ L1 契约全部通过（{len(cases)} 段）")

    if args.json:
        target = Path(args.json)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告 -> {target}")
    return 1 if checker.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())