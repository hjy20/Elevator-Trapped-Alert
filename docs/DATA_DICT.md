# 数据字典 · DATA_DICT（A · 数据与感知）

> 对应赛题初赛材料第 3 项「**数据集的选取与数据处理**」。
> 本文档说明：数据从哪来、长什么样、怎么处理、有多少、怎么标、哪里有坑。
> 配套代码：`data/scripts/`（生成与校验）、`vision/`（感知与规则）。

版本：v3 · 2026-11-17　｜　维护人：A　｜　契约依据：《00 协作总览与接口契约》第 6 节

---

## 0. 一页速览

| 项 | 内容 |
|---|---|
| 任务 | 把"电梯轿厢里的异常"变成模型/规则能消费的规范数据，并输出可解释的视觉感知结果 |
| 数据来源 | ① 程序化合成的轿厢场景库（5 类，链路自检用）② 自录实拍素材（加分项，D1 起可采）③ 公开跌倒数据集（倒地召回的外部参照） |
| 数据规模 | 5 类 × 20 s × 25 fps = **2 500 帧**；IMU 每段 30 s @20 Hz，切 **5 个 (200,3) 窗口** |
| 关键点 | COCO-17，YOLOv8n-Pose 输出，归一化到像素坐标 + 置信度 |
| 输出 | `VisionOut`（6 字段，每帧一条），JSON / JSONL |
| 处理链 | 解码 → 姿态关键点 → 几何规则 → 5 种时序滤波 → `VisionOut` |
| 自检 | `python data/scripts/validate_demo_data.py`（按 L1 契约逐条校验） |
| 局限 | 无真实困人数据；合成场景真值来自生成器；单一电梯型号/机位 |

---

## 1. 数据契约速查（L1，锁死）

### 1.1 IMU 输入张量

| 项 | 规定 | 本项目的落地 |
|---|---|---|
| 文件 | `common/demo_data/<case>_imu.npy` | ✅ |
| dtype | `float32` | ✅ |
| shape | `(N, 200, 3)` | ✅ N=5 |
| 时间步 | 200 步 @ 20 Hz = 10 s 窗口 | ✅ 窗口 10 s，步长 5 s（50% 重叠） |
| 通道顺序 | `[ax, ay, az]` | ✅ az 为竖直方向 |
| 单位 | `m/s²` ；并归一化到 `[-1,1]` | ✅ 原始值另存 `_imu_raw.npy`，归一化常数见 §5.3 |

### 1.2 视频输入

| 项 | 规定 | 本项目的落地 |
|---|---|---|
| 格式 | `.mp4`，H.264 | ✅ libx264 + yuv420p + faststart |
| 分辨率 | 1280×720 | ✅ |
| 帧率 | 25 fps | ✅ |
| 命名 | 全英文小写 | ✅ `demo_normal` / `demo_estop` / `demo_trapped` / `demo_pry` / `demo_faint` |

### 1.3 视觉输出 `VisionOut`（A → B）

```python
@dataclass
class VisionOut:
    ts: int                # 毫秒时间戳（由 meta 的 start_ts_ms + 帧号/25fps 推出）
    person_count: int      # 0-8，封顶
    fall_prob: float       # 0-1
    door_approach: float   # 0-1（腕部贴门缝，几何+时长，不是动作识别）
    crowded: float         # 0-1
    keypoints_ok: bool     # False 时 B 必须降级（只用人数 + dwell）
```

字段定义在 `common/schema.py`，带 `__post_init__` 范围钳制与 `validate()`，
A 这边任何越界值都会被夹紧，**脏数据不会流到 B**。

---

## 2. 数据来源：三条腿，各自的定位不同

| 来源 | 用途 | 现状 | 是否可对外声称"数据集" |
|---|---|---|---|
| **① 合成场景库**（`data/scripts/scene.py`） | 打通 A→B→C 链路、规则回归、演示素材 | ✅ 已生成 5 段 | 只能说"仿真场景库"，**不许说成实拍** |
| **② 自录实拍**（`data/scripts/capture_session.py`） | 真实素材、PPT 的真实截图、答辩可信度 | ⏳ 待团队按拍摄卡录制 | ✅ 这是真正要写进 PPT 的部分 |
| **③ 公开跌倒数据集**（UR Fall / Le2i） | 倒地判据的外部召回参照 | ⏳ 待下载评测 | ✅ 需注明来源与许可 |
| **④ 公开示例图**（ultralytics `bus.jpg`） | 仅用于 ONNX 一致性校验（.pt vs .onnx 的关键点误差） | ✅ 已用（`data/processed/external/`） | ❌ 不是训练/评测数据，只当标尺 |

### 2.1 为什么先做合成场景库

比赛只有 15 天，而"实拍 + 标注"是最慢的一环。合成库解决三个真问题：

1. **格式零风险**：分辨率/帧率/编码/shape 全部脚本生成，天然合规；
2. **真值免费**：人数、倒地时刻、贴门时刻都是生成时就已知的，可以做逐帧回归；
3. **可复现**：同一条命令生成同一份数据，A/B/C 三人拿到的是同一份样本。

它**不能**替代实拍：像素分布与真实摄像头差异很大，YOLO-Pose 在合成画面上并不会被使用
（合成场景直接给关键点轨迹）。所以链路是"关键点来源可替换"，而不是"用合成数据糊弄"。

### 2.2 场景库的剧本（每段 20 s @25 fps）

| case | event_type | 剧情（时间轴） | 期望 `VisionOut` |
|---|---|---|---|
| `demo_normal` | normal | 2 人进出；6.0→11.0 s 一人**蹲下捡东西** | 人数 2→1，`fall_prob≈0`，`door_approach≈0` |
| `demo_estop` | estop | 1 人站立；4.1 s 轿厢猛停（画面抖动 + 身体前倾后站直） | 人数 1，`fall_prob` 有短促抬升但**不越 0.7** |
| `demo_trapped` | trapped | 2 人滞留 20 s；门全程不开；一人蹲下看地缝 | 人数 2，`fall_prob≈0`，`door≈0`，靠 dwell 判困人 |
| `demo_pry` | pry | 1 人 5.2→13.0 s 反复扒门缝/拍门，另一人蹲角落 | `door_approach` 持续 > 0.6 |
| `demo_faint` | faint | 6.4 s 站 → 6.9 s 腿软坐下 → 7.6 s **躺平不起**；另一人 9.8 s 蹲下查看 | `fall_prob` 在 ~10 s 后 ≥ 0.9 |

> 两处"故意埋的坑"：`demo_normal` 的蹲下、`demo_trapped` 的蹲下看地缝，
> 都是典型的倒地误报源，用来证明"几何 + 时序"确实能放过它们。

---

## 3. 目录与文件清单

```
common/demo_data/                    ← 三人共用的演示样本（A 生成）
├── demo_<case>.mp4                  视频 1280×720 / 25fps / H.264
├── demo_<case>_kpts.jsonl           逐帧 17 关键点轨迹（合成场景专用）
├── demo_<case>_meta.json            起始时间戳 / ROI / 场景说明
├── demo_<case>_imu.npy              (3,200,3) float32，[-1,1]
├── demo_<case>_imu_raw.npy          (600,3) float32，m/s²
└── demo_<case>_imu.meta.json        采样率 / 窗口 / 归一化常数 / start_ts_ms / 窗口结束时间戳

data/
├── raw/                             实拍原始素材（不进 Git）
├── processed/
│   ├── demo_<case>_gt.json          逐帧真值：人数 / 倒地 / 贴门
│   └── frames/                      抽样帧（人工核对用）
├── labels/demo_<case>.json          场景级弱标签（event_type / urgency / trapped）
└── scripts/                         生成、转码、校验、回归、出图
```

`<case>_kpts.jsonl` 每行：

```json
{"frame": 0, "ts": 1763379000000,
 "persons": [[[x,y,conf] × 17]], "scores": [0.93], "labels": ["p1"]}
```

---

## 4. 视觉数据处理流程

```
视频/轨迹 → ①解码与时间戳 → ②关键点 → ③几何规则 → ④时序滤波 → ⑤VisionOut
```

### 4.1 解码与时间戳

- `cv2.VideoCapture` 解码，`25 fps`；时间戳 `ts = start_ts_ms + round(frame / 25 × 1000)`；
- `start_ts_ms` 来自 `<case>_meta.json`（合成数据锚在 2026-11-17 19:30 +08:00，
  每段间隔 5 分钟），保证 A→B→C 三段数据在同一时间轴可比；
- 跳帧：每 2 帧跑一次模型，中间帧**复用**上一次关键点（电梯内动作慢，误差可忽略），
  这是 CPU 上做到 15 FPS 以上的关键手段。

### 4.2 关键点规范（COCO-17）

| idx | 名称 | idx | 名称 | idx | 名称 |
|---|---|---|---|---|---|
| 0 | nose | 6 | right_shoulder | 12 | right_hip |
| 1 | left_eye | 7 | left_elbow | 13 | left_knee |
| 2 | right_eye | 8 | right_elbow | 14 | right_knee |
| 3 | left_ear | 9 | left_wrist | 15 | left_ankle |
| 4 | right_ear | 10 | right_wrist | 16 | right_ankle |
| 5 | left_shoulder | 11 | left_hip | | |

每个点 3 个数 `(x, y, conf)`：x/y 为像素（画面左上角为原点，y 向下），conf ∈ [0,1]。
`conf < 0.30` 视为不可用。

### 4.3 人数（`person_count`）

不用 bbox 计数——轿厢拥挤时框会重叠、NMS 会把人合并掉。改为**数头**：

```
head_center_i  = 该人 conf>0.3 的面部点（鼻/双眼/双耳）的均值
heads          = 对 {head_center_i} 做并查集聚类（半径 = 0.9 × 肩宽中位数）
shoulders      = 对肩中点同样聚类
person_count   = min(8, max(heads, shoulders))
若两者都为 0  → 回退到检测框数（并记 note）
最后 3 帧中值滤波，消掉单帧漏检
```

### 4.4 倒地（`fall_prob`）

**单帧几何证据**（两条任一成立即"疑似"）：

```
肩中点 S, 髋中点 H
θ = arccos( (S − H)·(0,−1) / ‖S − H‖ )        # 与竖直方向夹角
A. θ ≥ 60°
B. 头中心 y > 髋中心 y + 0.02·L               # 整个人是倒着的，L=身体尺度

防蹲下： 髋高比 r = (地面 y − 髋 y) / (0.45·L)
         r < 0.50 且 θ < 75° → 判为蹲下/弯腰，不计为倒地（记 crouch_like）
```

**时序证据**（3 秒投票 + 迟滞）：

```
ratio = Σ(疑似帧) / Σ(可评估帧)    窗口 3 s，可评估帧 ≥ 4 且跨度 ≥ 0.8 s
ratio ≥ 0.70  → fall_prob = 0.90（置位）
ratio ≤ 0.30  → 解除置位
未置位       → fall_prob = 0.35 × ratio（低分过渡，不给假高分）
关键点丢失 > 3 s → 概率按秒衰减，避免"看不到人却一直报警"
```

> 为什么必须有时序：单帧的关键点抖动在逆光/遮挡下非常剧烈，
> 实测单帧判据的误报远高于"3 秒内 70% 帧都指向同一结论"。这正是工程化的部分。

### 4.5 贴门（`door_approach`）

```
门框 ROI（人工标定）: (x, y, w, h)，门缝中线 seam_x
d = |x_wrist − seam_x|
单帧分： d ≤ 0.28·w → 1.0 ；d ≥ 0.75·w → 0 ；中间线性衰减
   （要求腕部同时落在 ROI 矩形内）
时序分：最近 1 s 内 ≥70% 帧的"手在门区"占比，才把峰值分放出来
        手离开后 1.5 s 内线性衰减
```

注意：**这是几何位置 + 持续时长，不是动作识别**。任务卡明确禁止做动作识别，
所以抓门/拍门都归一到"手腕是否停在门缝附近"这一个可测量的量上。

### 4.6 拥挤度（`crowded`）

```
area_ratio = Σ 人体框面积 / 画面面积
crowded    = min(1, area_ratio / 0.55)        # 0.55 视为满员
再做 EMA(α=0.35) 平滑
```

### 4.7 关键点可信度（`keypoints_ok`）

```
某人的关键点可用 ⇔ 可见点数(conf≥0.3) ≥ 8 且 平均置信度 ≥ 0.35
keypoints_ok = 可用人数 ≥ ⌈0.5 × 检出人数⌉
人数为 0 时 → True（"没人"本身是可靠结论）
```

`keypoints_ok=False` 时，B 必须降级为"只看人数 + dwell"，不许用 `fall_prob`。
这是 A→B 的**降级开关**，写进接口文档。

---

## 5. IMU 数据

### 5.1 物理形态

| 场景 | 信号特征 |
|---|---|
| 正常运行 | `az≈9.81`，起停有 ±0.35 m/s² 的平滑加减速包络 |
| 急停 | `az` 0.4 s 内跌到 ≈6.2、回弹到 ≈15.5，随后 3–8 Hz 余振 |
| 停梯静止 | 三轴方差极小（只有传感器噪声 + 1 Hz 微晃） |
| 扒门 | 4–8 Hz 冲击串，持续 0.45 s，间隔 2 s |
| 倒地 | 一次性约 1.6 m/s² 的低频脉冲 |

> 标定关系：`estop` 靠 |Δ‖a‖|/Δt 的突增；`vibration` 靠 2–8 Hz 频段能量占比；
> `idle` 靠窗口内方差。**10 s 窗口只用于检测异常事件，不用于判困人**（困人是分钟级，差 18 倍）。

### 5.2 结构与元数据

```
_imu.npy       (N, 200, 3) float32      # N=5，窗口 = 200 步 @20Hz = 10 s，步长 5 s
_imu_raw.npy   (600, 3)    float32      # 30 s 连续原始采样，m/s²
_imu.meta.json {fs:20, window:200, stride:200,
                channels:["ax","ay","az"],
                normalize:{ax:3, ay:3, az_offset:9.81, az_scale:6},
                window_end_ts_ms:[...]}  # 每个窗口结束的毫秒时间戳，B 对齐用
```

### 5.3 归一化（可逆，跨段可比）

```
ax_n = clip(ax / 3.0,        -1, 1)
ay_n = clip(ay / 3.0,        -1, 1)
az_n = clip((az − 9.81) / 6, -1, 1)
```

用**固定常数**而不是按样本 min-max 缩放：不同段、边缘与云端看到的是同一套刻度，
归一化本身不会抹掉"急停比正常运行剧烈"这个事实。

---

## 6. 标注体系

| 层级 | 文件 | 内容 | 谁用 |
|---|---|---|---|
| **场景级弱标签** | `data/labels/demo_<case>.json` | `event_type` / `urgency` / `trapped` / `people_max` | B 做五分类评测、C 做 PPT |
| **逐帧真值** | `data/processed/demo_<case>_gt.json` | 每帧 `person_count` / `fallen` / `door_contact` / 在场人员 | A 的回归测试、指标量化 |
| **关键点（自动）** | `common/demo_data/demo_<case>_kpts.jsonl` | 每帧每人 17 个点 + 置信度 | 推理（合成场景）与误差分析 |

**为什么用弱标签**：困人这件事**没有公开数据集**，逐帧标 2500 帧也不现实。
场景级标签 + 规则层的可解释判据，是 15 天内唯一合理的选择；
逐帧真值只用于验证**感知能力**（有人/几人/倒地/贴门），而困人判定本身由规则 + 时长决定，
不依赖训练数据——这一点在答辩时要主动讲清。

---

## 7. 数据规模统计

| case | 帧数 | 时长 | 视频 | 关键点轨迹 | IMU 窗口 | 场景标签 |
|---|---|---|---|---|---|---|
| demo_normal | 500 | 20 s | 1280×720@25 | 500 行 | 5 | normal / 0 |
| demo_estop | 500 | 20 s | 1280×720@25 | 500 行 | 5 | estop / 1 |
| demo_trapped | 500 | 20 s | 1280×720@25 | 500 行 | 5 | trapped / 2 |
| demo_pry | 500 | 20 s | 1280×720@25 | 500 行 | 5 | pry / 2 |
| demo_faint | 500 | 20 s | 1280×720@25 | 500 行 | 5 | faint / 3 |
| **合计** | **2 500** | **100 s** | — | **2 500 行** | **25** | 5 类 |

- 关键点数量：约 2 500 帧 × 平均 1.6 人 × 17 点 ≈ **68 000 个关键点**
- IMU 采样：5 × 600 = **3 000 个采样点**（10 s 窗口共 25 个）
- 磁盘占用：视频约 5 × 1.5 MB，npy 约 5 × 12 KB，轨迹约 5 × 900 KB

实拍素材补齐后，同一张表按实际帧数刷新（脚本会打印）。

---

## 8. 质量自检与量化结果

### 8.1 契约自检

```bash
python data/scripts/validate_demo_data.py --json results/contract_check.json
```

逐条检查：命名规范、H.264、分辨率、帧率、dtype、shape、取值范围、ts 单调、
标签与 `urgency` 映射、meta 一致性。**任何一项不过就直接报错，不靠人眼看。**

### 8.2 规则回归（合成场景库）

```bash
python vision/infer.py --all
python data/scripts/regression.py
```

产出 `results/vision_regression.md`，口径：

- 人数误差 = |规则输出 − 逐帧真值| 的 MAE、±1 以内帧占比
- 倒地召回 = 真值倒地帧中 `fall_prob > 0.7` 的比例；误报率 = 非倒地帧中被判倒地的比例
- 贴门同理（阈值 0.6）
- 响应延迟 = 首次 `fall_prob>0.7` 的时刻 − 真值倒地时刻

> ⚠️ **口径声明**：合成场景的真值来自生成器本身，这套数字只证明"规则实现与设计一致"，
> **不能当作真实视频上的精度对外引用**。实拍素材补齐后用同一脚本重跑，才是可写进 PPT 的数字。

### 8.3 性能基准（本机 CPU 实测）

```bash
python vision/bench.py --case demo_faint --backend yolo   # -> results/bench_vision_yolo.md
python vision/bench.py --case demo_faint --backend synthetic  # -> results/bench_vision.md（只测规则层）
```

分辨率（640/480/320）× 跳帧（1/2/3）的端到端 FPS、模型 FPS、人数与 fall 峰值偏差。
本机（Python 3.14.6 + torch 2.10.0+cpu，200 帧，跳帧 2）实测：

| imgsz | 端到端 FPS | 模型 FPS | 是否满足 25 fps 视频流 |
|---|---|---|---|
| 640 | 约 21 | 约 22 | ✗ 不够 |
| **480（默认）** | **约 29（28.6–32.3）** | **约 32** | ✅ 有 15~28% 余量 |
| 320 | 约 43–47 | 约 50 | ✅ 但腕/踝关键点变糊，贴门先受害 |

- 规则层本身（synthetic 后端，无模型前向）≈ **390 FPS** —— 瓶颈全在模型，不在规则。
- ⚠️ 口径：合成演示画面里 YOLO 检不到人（程序化胶囊人，人数均值 0），
  后处理开销偏小，**真实画面会略低于上表**；实际部署前必须用实拍素材重测。

### 8.4 ONNX 一致性（.pt ↔ .onnx）

```bash
python vision/onnx_export.py --export
python vision/onnx_export.py --verify --image data/processed/external/bus.jpg
```

`results/onnx_parity.json`：83 个关键点、**mean 0.073 px / p95 0.248 px / max 0.272 px**（目标 <1 px，通过）。
导出后部署环境只需要 `onnxruntime`，不需要 torch（详见 `docs/A_ONNX_ASCEND.md`）。

---

## 9. 局限与偏差（主动写，不等评委问）

| 局限 | 说明 | 缓解 / 后续 |
|---|---|---|
| **无真实困人数据** | 现实中不可能为了比赛去困人；公开数据集没有"困人"这一类 | 感知层（有人/几人/倒地）用公开跌倒数据 + 自录验证；困人判定是规则层，靠时长与门状态，不依赖训练数据 |
| **合成场景** | 像素与真实摄像头差异大，YOLO-Pose 不会在合成画面上被调用 | 关键在于"关键点来源可替换"：换成实拍视频即自动切到 YOLO-Pose 真推理 |
| **单一电梯型号 / 单机位** | 门框 ROI、透视、光照都只覆盖一种构图 | ROI 是配置项（`calibrate_roi.py` 一键标定），换机位不改代码 |
| **门口状态是近似的** | 没有真实门磁信号，"门开/门关"由画面门缝区域变化近似 | 已在 MODEL_CARD/DATA_DICT 里承认；生产接入门磁后可直接替代 |
| **样本量小** | 5 段、2 500 帧，统计意义有限 | 主动声明"在自建场景库上测得"，不说"准确率 95%" |
| **关键点抖动** | 逆光 + 鱼眼畸变导致腕/踝抖动剧烈 | 时序滤波（3 s 投票 / 1 s 持续）+ 跳帧复用，是解法而不是掩盖 |
| **遮挡** | 人多时下半身不可见，倒地判据退化 | `keypoints_ok=False` 显式降级，让 B 只用人数 + dwell |

---

## 10. 复现步骤

```bash
# 0) 环境
python -m venv .venv && .venv\Scripts\activate
pip install -r vision/requirements-a.txt

# 1) 生成 5 段合规演示数据（视频 + 轨迹 + IMU + 标签 + 真值）
python data/scripts/build_demo_data.py --all

# 2) 契约自检
python data/scripts/validate_demo_data.py

# 3) 推理，产出 VisionOut
python vision/infer.py --all

# 4) 回归 + 出图
python data/scripts/regression.py --update-baseline
python data/scripts/make_stats.py

# 5) 带标注视频（交给 C 做演示视频素材）
python vision/visualize.py --all
```

### 换成实拍素材（推荐尽早做）

```bash
# ① 录（按屏幕上的拍摄卡演）
python data/scripts/capture_session.py --case demo_faint --seconds 20
# ② 标定门框 ROI（换机位必须重标）
python vision/calibrate_roi.py --video data/raw/demo_faint.mp4 --name elev_a
# ③ 整理成契约格式并放进演示目录（会删掉同名合成轨迹）
python data/scripts/prepare_real_video.py --input data/raw/demo_faint.mp4 --case demo_faint --to-demo --roi elev_a
# ④ 自检 + 重跑
python data/scripts/validate_demo_data.py --case demo_faint
python vision/infer.py --case demo_faint --backend yolo
```

放入实拍视频后：

- `<case>_kpts.jsonl` 被删除 → 推理自动切换到 **YOLO-Pose 真推理**（`--backend auto` 的判定顺序）；
- `<case>_gt.json` 仍是合成版的，逐帧真值需要人工抽标或直接删掉（回归脚本会跳过没有真值的段）。

---

## 11. 参数一览（改这里，不改代码）

`vision/configs/default.json`：

| 参数 | 默认 | 含义 | 调大的后果 |
|---|---|---|---|
| `model.imgsz` | 480 | 模型输入边长 | 更准、更慢（640 是基线对比值） |
| `model.conf` | 0.30 | 检测置信度阈值 | 漏检增多 |
| `runtime.frame_skip` | 2 | 跳帧数 | 更快、时序分辨率变粗 |
| `fall.angle_deg` | 60 | 躯干夹角阈值 | 更宽松（易误报） |
| `fall.window_sec` | 3.0 | 倒地投票窗口 | 更稳、响应更慢 |
| `fall.vote_ratio` | 0.70 | 窗口内需占比 | 更难触发 |
| `fall.hip_drop_ratio` | 0.50 | 髋高比（防蹲下） | 更多蹲姿被放过 |
| `door.near_ratio` | 0.28 | 距门缝多近算"贴" | 更容易判贴门 |
| `door.persist_sec` | 1.0 | 手需持续时长 | 更抗抖、响应更慢 |
| `crowd.saturation` | 0.55 | 面积占比满员值 | 拥挤度更敏感 |
| `quality.min_visible_kpts` | 8 | 判"可信"的可见点数 | 更容易判不可信（降级） |

门框 ROI：`vision/configs/rois/demo_cabin.json`（`roi=[940,94,288,516]`, `seam_x=1084`）。

---

## 12. 变更记录

| 日期 | 变更 | 影响方 |
|---|---|---|
| 2026-11-17 | 初版：5 段合成场景库 + VisionOut 全链路 + 自检/回归/出图脚本 | A / B / C |

> 改 `common/schema.py` 或 `common/demo_data/` 的格式，**必须先在群里同步**，
> 并同步更新本文件与 `00_协作总览与接口契约.md`。