# A → B / C 交接说明（数据与感知）

> 交接人：A　｜　接收人：B（算法与决策）、C（云服务与交付）
> 日期：2026-11-17　｜　依据：《00 协作总览与接口契约》第 4、6 节
> 一句话：**A 这边"视频 → VisionOut"已经跑通并留了回归报告，B 可以直接拿 JSON 喂状态机，C 可以直接拿标注视频剪演示。**

---

## 1. 30 秒上手（B 最关心）

```bash
# 已经跑好了，产物就在这：
ls results/vision_demo_*.json      # 每段 500 条 VisionOut
ls results/vision_summary.json     # 5 段汇总
```

一行代码拿到 B 需要的东西：

```python
from vision.infer import infer_video
outputs = infer_video("common/demo_data/demo_faint.mp4")   # list[VisionOut]
```

`VisionOut` 六字段（契约锁死，A 不会擅自改）：

```python
{"ts": 1763379660000, "person_count": 2, "fall_prob": 0.90,
 "door_approach": 0.0, "crowded": 0.16, "keypoints_ok": true}
```

**B 注意两件事**：
1. `keypoints_ok=False` 时**不要用 `fall_prob`**，降级成"人数 + dwell"（详细见 §5）；
2. `door_approach` 是"手腕停在门缝附近 + 持续时间"的几何量，**不是动作识别**（任务卡禁止做动作识别，A 没有越界）。

数据是 20 s 一段，每段 500 条；IMU 每段 5 个窗口，窗口结束时间在 `demo_<case>_imu_meta.json` 的 `window_end_ts_ms` 里，可以直接和 `VisionOut.ts` 对齐。

---

## 2. 目录与职责边界（A 只碰这些）

```
vision/                  ← 训练无关的感知层：读视频/轨迹 → VisionOut
├── config.py            路径与配置（dataclass + json）
├── geometry.py          COCO-17、人体尺度、躯干夹角、门缝距离、聚类
├── temporal.py          滑动窗口、倒地投票与迟滞、贴门持续、中值、EMA
├── sources.py           帧源与后端（yolo / onnx / synthetic），时间戳重建
├── rules.py             VisionEngine.process(frame) -> VisionOut（唯一出口）
├── infer.py             公开 API infer_video() + 命令行
├── visualize.py         带 HUD 的标注视频 + 抽帧对比图
├── bench.py             分辨率×跳帧 的性能矩阵
├── onnx_export.py       pt → onnx 导出与一致性校验、昇腾清单
├── calibrate_roi.py     门框 ROI 标定（换机位必做）
├── doctor.py            环境自检（依赖/后端/数据/产物）
├── requirements-a.txt   锁版本依赖
└── configs/             default.json + rois/<name>.json

data/
├── scripts/build_demo_data.py   合成 5 段场景（视频+轨迹+IMU+标签+真值）
├── scripts/validate_demo_data.py L1 契约自检（一条命令验格式）
├── scripts/regression.py        逐帧真值回归 + 基线
├── scripts/make_stats.py        统计图与架构图
├── scripts/scene.py             轿厢程序化渲染 + 姿态库
├── scripts/video_io.py          ffmpeg 直管 H.264 写入
├── scripts/capture_session.py   摄像头录制（带拍摄卡）
├── scripts/prepare_real_video.py 实拍素材整理成契约格式
├── labels/              场景级弱标签
└── processed/           逐帧真值 + 抽样帧

common/demo_data/        三人共用的 5 段演示数据（A 生成，谁都不许手改）
docs/DATA_DICT.md        赛题硬要求「数据集选取与处理」
results/                 A 的产物：VisionOut / 回归 / 标注视频 / 图
```

**A 不碰**：`model/`、`service/`、`dashboard/`。也**没有改** L1 契约字段。

---

## 3. 交付清单（可勾选）

| # | 交付物 | 位置 | 状态 |
|---|---|---|---|
| 1 | 感知代码（人数/倒地/贴门/拥挤） | `vision/` | ✅ 跑通 |
| 2 | 5 段演示数据（视频+H.264+1280×720+25fps） | `common/demo_data/` | ✅ |
| 3 | IMU 演示数据 `(5,200,3)` float32 | `common/demo_data/*_imu.npy` | ✅ |
| 4 | 场景标签 + 逐帧真值 | `data/labels/`、`data/processed/` | ✅ |
| 5 | `docs/DATA_DICT.md` | `docs/` | ✅ |
| 6 | 契约自检脚本 + 报告 | `data/scripts/validate_demo_data.py`、`results/contract_check.json` | ✅ |
| 7 | 回归报告 + 基线 | `results/vision_regression.md`、`results/vision_baseline.json` | ✅ |
| 8 | 带标注视频（5 段） | `results/annotated/` | ✅ |
| 9 | 统计图/架构图（6 张） | `results/figures/` | ✅ |
| 10 | PPT 数据集章节文案 | `docs/ppt/A_data_section.md` | ✅ |
| 11 | 实拍素材通道（录制/转码/ROI/切换） | `capture_session.py`、`prepare_real_video.py`、`calibrate_roi.py` | ✅ 代码就绪，待录 |
| 12 | ONNX 导出 + 一致性校验 | `vision/onnx_export.py`、`models/yolov8n-pose.onnx`、`results/onnx_parity.json` | ✅ mean 0.073 px |
| 13 | 性能矩阵（真实 YOLO 数字） | `results/bench_vision_yolo.md` | ✅ 480+跳帧2 → 32 FPS |
| 14 | ONNX / 昇腾适配说明 | `docs/A_ONNX_ASCEND.md` | ✅ 昇腾部分标注"未验证" |
| 15 | 演示数据说明（给 B/C 看格式） | `common/demo_data/README.md` | ✅ |
| 16 | 环境自检脚本 | `vision/doctor.py` | ✅ 一条命令看清能不能跑 |

---

## 4. 关键数字（可直接进 PPT，但请看口径）

来源：`results/vision_regression.md`（5 段、2 500 帧）

| 指标 | 数值 | 口径 |
|---|---|---|
| 人数 MAE | **0.00 人** | 规则输出 vs 逐帧真值 |
| 人数 ±1 以内 | **100 %** 帧 | 同上 |
| 倒地召回 | **80.3 %** | `fall_prob>0.7` 的帧占真值倒地帧 |
| 倒地误报 | **0.0 %** | 非倒地帧中被判倒地 |
| 倒地响应延迟 | **2.6 s** | 首次 >0.7 的时刻 − 真值倒地时刻（3 s 投票窗口的必然代价） |
| 贴门召回 | **95.3 %** | `door_approach>0.6` |
| 贴门误报 | **0.0 %** | 已扣除 1.5 s 衰减窗口（设计行为） |
| 关键点可用率 | **100 %** | 5 段全部 `keypoints_ok=True` |
| 端到端性能（CPU） | **约 29 FPS** @480+跳帧2 | YOLOv8n-Pose 真推理，200 帧实测（640 档只有 21 FPS） |
| 规则层开销 | **≈ 390 FPS** | 相对模型可忽略，瓶颈在模型不在规则 |
| .pt / .onnx 一致性 | **mean 0.073 px**（p95 0.248） | 83 个关键点，目标 <1 px，通过 |

> ⚠️ **口径声明（B/C 转述时务必带上）**：真值来自合成场景生成器，
> 这组数字证明的是「规则实现与设计一致」，**不是真实视频精度**。
> 实拍素材补齐后用 `python data/scripts/regression.py` 重跑，才是能对外引用的数字。

---

## 5. 给 B 的三条硬约定

### 5.1 降级开关

```
keypoints_ok == False  →  丢弃 fall_prob，只用 person_count + dwell 判困人
```

原因：逆光/遮挡时腕踝关键点抖动剧烈，`fall_prob` 不可信。A 已经在
`rules.py` 里把"可用人数 < 一半"标成 False，B 直接读这个布尔就行，不需要自己判。

### 5.2 `fall_prob` 不是一个"瞬时概率"

它是 3 秒窗口的投票比例再过迟滞（置位 0.90，解除 0.30）。所以：

- **会晚 2~3 秒**：这是抗误报的代价，`Decision` 的 `reason` 里可以写"倒地持续 3s 确认"；
- **不会自己抖回去**：短暂抬升后要跌到 0.30 以下才解除。

如果 B 想更灵敏，改 `vision/configs/default.json` 的 `fall.window_sec`（3.0 → 2.0）
与 `fall.vote_ratio`（0.70），不用改代码；但**要和 A 说一声**，回归基线会变。

### 5.3 时间轴

`VisionOut.ts` 与 `IMUEvent.ts` 都是**毫秒时间戳**，都来自 `<case>_meta.json` 的
`start_ts_ms`（合成数据锚在 2026-11-17 19:30+08:00，每段间隔 5 分钟）。
两路能直接按 `ts` 对齐，不需要任何换算。

---

## 6. 给 C 的三条硬约定

1. **演示视频素材**：`results/annotated/demo_<case>_annotated.mp4` 已经把人数、倒地概率、
   贴门分数、门缝 ROI 画在画面上了，剪视频直接用，不用再加工。
2. **图**：`results/figures/` 里 `fig_pipeline.png`（架构图）、`fig_dataset_overview.png`（数据规模）、
   `fig_fall_prob.png` / `fig_door_approach.png`（曲线）、`sheet_demo_*.png`（抽帧对比）都是 140 dpi PNG，可直接贴 PPT。
3. **`.gitignore` 要加**（我这边不能动根目录配置，属于 C 的交付范围）：

```gitignore
# A: 实拍原始素材与临时产物
data/raw/*
!data/raw/.gitkeep
.venv/
.devenv/
.tmp/
__pycache__/
*.pyc
results/annotated/*.mp4
results/bench_*.json
```

> `common/demo_data/` 里的 5 段演示数据**要进 Git**（每段视频约 1.2 MB，共 6 MB 左右，可接受），
> 否则 B/C 拉下来跑不了。

---

## 7. 已知问题与风险（不掩饰）

| 问题 | 影响 | 现状 / 对策 |
|---|---|---|
| **合成画面里 YOLO 检不到人** | `--backend yolo` 跑演示视频时 `person_count=0` | 正常：胶囊人是程序化画的，不是真人。所以合成场景走 `synthetic` 关键点后端，YOLO 只在实拍素材上验证；`bench_vision_yolo.md` 的 FPS 是"无人可检"的上界 |
| 合成场景 ≠ 真实画面 | 像素分布差异大 | 关键点来源可替换：换成实拍视频即自动切 YOLO（`auto` 后端先看 sidecar，没有就找 onnx，再找 yolo） |
| 没有真实困人数据 | 困人这一类无法用监督学习 | 困人由 B 的状态机 + 时长判定，A 只负责"有没有人/倒地/贴门"这些可测的量 |
| 门开/关是画面近似 | 没有门磁信号 | 已在 `DATA_DICT.md` §9 写明；生产接门磁即可替代 |
| 单一机位/单一电梯 | ROI 只覆盖一种构图 | `calibrate_roi.py` 一键重标，配置化，不改代码 |
| `door_approach` 只看腕部 | 用手肘/身体顶门会漏 | 阈值可调（`door.near_ratio`）；答辩时主动承认这是几何近似的边界 |
| 人数上限 8 | 超员场景会截断 | 电梯额定载客通常 ≤ 13 人，但演示场景 ≤ 4 人；需要时改 `rules.py` 的 `MAX_PERSONS` |
| **昇腾未验证** | 无真机、无预算 | `docs/A_ONNX_ASCEND.md` 给了 ATC 命令与验收口径，明确写"未验证"，不编加速比 |

---

## 8. 换机位 / 换素材的标准流程（C 录制时照做）

```bash
# ① 录（终端会打印拍摄卡：站哪、做什么动作、几秒）
python data/scripts/capture_session.py --case demo_faint --seconds 20

# ② 标定门框（换机位必做，否则贴门判据全错）
python vision/calibrate_roi.py --video data/raw/demo_faint.mp4 --name elev_a

# ③ 整理成契约格式并放进演示目录（会删掉同名合成轨迹，自动切 YOLO）
python data/scripts/prepare_real_video.py --input data/raw/demo_faint.mp4 \
    --case demo_faint --to-demo --roi elev_a

# ④ 自检 + 重跑
python data/scripts/validate_demo_data.py --case demo_faint
python vision/infer.py --case demo_faint --backend yolo
python vision/visualize.py --case demo_faint
```

拍摄要点（直接影响指标）：

- 机位固定在轿厢一角，**门框完整入画**，高度约 1.6 m；
- 光照尽量均匀，避免门口强逆光（关键点抖动的主要来源）；
- 倒地动作**真的躺下并保持 3 秒以上**（3 s 投票窗口，短了不会报警）；
- 扒门动作**手要真贴到门缝**（腕部离门缝 < 20 cm），否则 `door_approach` 不会起来；
- 每段单独录，一段一个场景，不要混。

---

## 9. 参数在哪、怎么调

配置文件 `vision/configs/default.json`（改完不用改代码）：

| 想要 | 改什么 | 代价 |
|---|---|---|
| 倒地更灵敏 | `fall.window_sec` 3.0→2.0、`fall.vote_ratio` 0.70→0.55 | 误报上升 |
| 倒地更稳 | `fall.window_sec` 3.0→4.0 | 响应更慢（延迟 ≈ 窗口长度） |
| 蹲下不误报 | `fall.hip_drop_ratio` 0.50→0.60 | 真倒地可能漏（躺着髋部也低） |
| 贴门更灵敏 | `door.near_ratio` 0.12→0.18 | 站在门边也可能触发 |
| 更快 | `runtime.frame_skip` 2→3、`model.imgsz` 480→320 | 关键点更抖，时序滤波压力变大 |
| CPU 更省 | `model.imgsz` 480→320 | 人数与腕部精度下降 |

---

## 10. 待办与催办

**A 待办**
- [x] 装好 torch/ultralytics，补齐 `results/bench_vision_yolo.md`（480+跳帧2 = 32 FPS）与 `results/onnx_parity.json`（0.073 px）
- [ ] 实拍素材（团队录制后）跑一遍全链路，刷新 `vision_regression.md` 为真实口径
- [ ] 昇腾真机验证（有资源才做，没有就保持"未验证"）
- [ ] 若时间允许：公开跌倒数据集（UR Fall / Le2i）上做一次外部召回参照

**需要 B 确认**
- [ ] `keypoints_ok=False` 的降级逻辑是否已在状态机里实现
- [ ] `fall.window_sec` 用 3.0 还是 2.0（影响延迟 2.6 s ↔ 1.8 s）
- [ ] 是否需要 A 额外输出"每帧人数变化的时刻"（入场/离场事件）

**需要 C 确认**
- [ ] 根 `.gitignore` 按 §6 加上
- [ ] `common/demo_data/` 进 Git（约 6 MB）
- [ ] `docs/DATA_DICT.md` 已放进初赛材料"数据集选取与处理"那一节
- [ ] 演示视频是否用 `results/annotated/` 的标注版

---

## 11. 一条命令验证"我这边是好的"

```bash
python vision/doctor.py
```

期望输出：依赖 ✓、`backend:synthetic ✓`、5 段数据 ✓、ROI ✓、产物 ✓。
`torch/ultralytics` 两项 ✗ 是**已知且不影响**合成链路的（见 §7）。