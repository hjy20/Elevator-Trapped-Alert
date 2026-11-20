# common/demo_data —— 三人共用的演示数据

> 由 A 生成（`python data/scripts/build_demo_data.py --all`）。
> **谁都不许手改这里的文件**：格式由 `data/scripts/validate_demo_data.py` 按 L1 契约强制校验，
> 手改会让 B/C 两边跑出不一样的结果。要改就改生成脚本，再重新生成。

## 每段 5 个文件

| 文件 | 内容 |
|---|---|
| `demo_<case>.mp4` | 视频：1280×720 / 25 fps / H.264 / 20 s |
| `demo_<case>_kpts.jsonl` | 逐帧关键点轨迹（合成场景专用；实拍素材没有这个文件） |
| `demo_<case>_meta.json` | `start_ts_ms`（时间轴锚点）、`door_roi`、场景说明 |
| `demo_<case>_imu.npy` | IMU：`(5,200,3)` float32，归一化到 `[-1,1]` |
| `demo_<case>_imu_raw.npy` | IMU 原始值：`(600,3)` float32，单位 m/s² |
| `demo_<case>_imu_meta.json` | 采样率、窗口、归一化常数、每个窗口的结束时间戳 |

## 5 段场景

| case | event_type | urgency | 期望看到的现象 |
|---|---|---|---|
| `demo_normal` | normal | 0 | 2 人乘梯，中途蹲下捡东西 → **不应报警** |
| `demo_estop` | estop | 1 | 轿厢急停，身体前倾 → `fall_prob` 短促抬升但**不越 0.7** |
| `demo_trapped` | trapped | 2 | 2 人滞留 20 s，门全程不开 → 靠 dwell 判困人 |
| `demo_pry` | pry | 2 | 反复扒门缝 → `door_approach` 持续 > 0.6 |
| `demo_faint` | faint | 3 | 站立 → 腿软坐下 → 躺平不起 → `fall_prob` 升到 0.90 |

## 怎么用

```python
# B：拿 VisionOut
from vision.infer import infer_video
outputs = infer_video("common/demo_data/demo_faint.mp4")

# 或者直接读已经跑好的结果
import json
rows = json.load(open("results/vision_demo_faint.json", encoding="utf-8"))

# IMU 窗口（B 的 IMUEvent 输入）
import numpy as np
windows = np.load("common/demo_data/demo_faint_imu.npy")      # (5,200,3)
meta = json.load(open("common/demo_data/demo_faint_imu_meta.json", encoding="utf-8"))
print(meta["window_end_ts_ms"])                                # 与 VisionOut.ts 同一时间轴
```

## 时间轴

所有段共享同一条时间轴：`start_ts_ms` 从 2026-11-17 19:30:00 +08:00 开始，每段间隔 5 分钟。
`VisionOut.ts`、`IMUEvent.ts`、`Decision.ts` 都是毫秒时间戳，可以直接比较、对齐、画在同一张图上。

## 格式自检

```bash
python data/scripts/validate_demo_data.py
```

11 项硬检查：命名、H.264、分辨率、帧率、dtype、shape、取值范围、无 NaN、ts 单调、
标签与 urgency 映射、meta 一致性。不通过就退不出 0。

## 换成实拍素材之后

实拍视频放进来时，`_kpts.jsonl` 会被删掉（`prepare_real_video.py --to-demo` 自动处理），
推理会自动切到 YOLO-Pose / ONNX 真推理；`_imu.npy` 需要另外采集或沿用合成数据，
`_meta.json` 的 `start_ts_ms` 会被重写成录制时间。详见 `docs/DATA_DICT.md` §10。