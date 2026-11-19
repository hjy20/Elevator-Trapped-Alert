# ONNX 导出与昇腾适配（A · 数据与感知）

> 交付物里属"可选加分项"。原则：**能跑的就跑出真数字，不能跑的就写"方案 + 未验证"**，
> 不编截图、不编加速比。

---

## 1. 已完成的实测

### 1.1 导出

```bash
python vision/onnx_export.py --export
```

| 项 | 值 |
|---|---|
| 源权重 | `models/yolov8n-pose.pt`（预训练，未微调、未量化） |
| 目标 | `models/yolov8n-pose.onnx`（12.7 MB） |
| opset | 12 |
| imgsz | 480（固定，`dynamic=False`，batch=1） |
| 导出耗时 | 112.9 s（本机 CPU） |
| 工具 | ultralytics 8.4.215 + torch 2.10.0+cpu + onnx 1.23.2 |

固定输入 shape 是有意的：ATC（昇腾）转换对动态 shape 支持差，固定 480×480 成功率最高。

### 1.2 一致性校验（.pt vs .onnx）

```bash
python vision/onnx_export.py --verify --image data/processed/external/bus.jpg
```

| 指标 | 数值 |
|---|---|
| 对比样本 | ultralytics 公开示例图 `bus.jpg`（1080×810，4 人）+ 2 个裁剪变体 |
| 匹配到的人 | 5 人次 |
| 比对关键点 | 83 个（17 点 × 5 人） |
| **mean** | **0.073 px** |
| p95 | 0.248 px |
| max | 0.272 px |
| 判定 | ✅ 通过（目标 mean < 1 px） |

结果落在 `results/onnx_parity.json`。

### 1.3 为什么必须用真实照片验，不能用演示视频

演示视频里的人是**程序化画的胶囊人**，YOLO（.pt 和 .onnx）都检测不到（`person_count=0`）。
拿它做 parity 会得到"0 个关键点"——那不是通过，是没数据。所以：

- `--verify` 默认走演示视频，**检测不到人时会明确写 in the JSON**：
  `"0 个关键点 = 两边都没检到人（合成画面），不是通过"`；
- 真实 parity 必须 `--image` 指定含人的照片。

### 1.4 顺带修掉的一个真 bug（值得写进技术文档）

第一次测出来 **mean 6.25 px**，远超 1 px 目标。排查过程：

1. 先用 ultralytics 自己的 ONNX 推理路径对比我们的 `OnnxPoseBackend`
   → 差异 **0.00 px**，说明我们的 letterbox + decode 没错；
2. 再用 480×480 的原生裁剪图测 .pt vs .onnx → 差异 **0.000 px**；
3. 于是问题只可能出在**预处理不一致**：ultralytics `predict()` 默认 `rect=True`，
   会按长宽比把 480×480 输入裁成"最小矩形"（1080×810 的图 → 480×384），
   而我们的 ONNX 后端固定方形 480×480。两边喂给网络的像素不一样，
   关键点差 5~8 px，缩放宽高比回原图后放大成 6.25 px。

**修法**：`YoloPoseBackend` 显式传 `rect=False`，强制方形 letterbox，与 ONNX 路径对齐。
结果 6.25 px → **0.073 px**。

> 这条经验的价值：如果只在"精度看着还行"的层面验收，这个 bug 会一直藏着——
> 生产上切换 .pt / .onnx 两个运行时，同一个人的人数、腕部贴门距离会莫名抖动。
> 是 parity 检查把它逼出来了。

---

## 2. 昇腾（Ascend）适配：方案已备，未验证

```bash
python vision/onnx_export.py --ascend    # 打印下方清单
```

**我们没有昇腾真机，也没有云上 Ascend 实例的预算，所以这一段只有方案，没有数字。**
PPT 里只能写"预期 3–8 倍"，并明确标注"未验证"。

### 2.1 转换命令

```bash
atc --model=yolov8n-pose.onnx \
    --framework=5 \
    --output=yolov8n-pose_bs1 \
    --input_shape="images:1,3,480,480" \
    --soc_version=Ascend310P3 \
    --precision_mode=allow_fp32_to_fp16
```

### 2.2 精度验收口径（与 ONNX 那套一致）

1. 同一批帧分别用 `om` 模型与 onnxruntime 推理；
2. 按 17 个关键点的像素坐标算 L2；
3. 目标 mean < 1 px；若超差，**先关掉 `allow_fp32_to_fp16` 复测**，
   用来区分"是 fp16 量化误差"还是"算子/后处理实现差异"。

### 2.3 已知的坑（提前写下来，省得现场踩）

| 坑 | 说明 |
|---|---|
| 动态 shape | Resize/Concat 的动态维在 ATC 里会退化，必须固定 480×480 |
| NMS 位置 | 让模型只输出 raw（1,56,N），后处理留在 CPU —— 我们的实现本来就是这样 |
| 输出布局 | (1,56,N) 需要 transpose，别在 device 上做 |
| 数据拷贝 | device→host 一次拷完整 batch，别逐帧拷（PCIe 是瓶颈） |

### 2.4 交付物（若有真机）

转换日志 + 逐帧误差表 + 端到端 FPS 对比表。**没有真机就不编这三样。**

---

## 3. 附：ONNX 后端在我们流水线里的位置

`--backend auto` 的判定顺序：

```
① <case>_kpts.jsonl 存在  → synthetic（合成场景，不跑模型）
② models/yolov8n-pose.onnx 存在 → onnx（无 torch 也能推理，适合部署机/边缘盒子）
③ ultralytics 可用         → yolo（.pt，开箱即用）
④ 都不行                   → 明确报错，并提示怎么办
```

这条顺序的意义：**部署环境只需要 `onnxruntime`，不需要 2 GB 的 torch**。
在 4 GB 内存的云主机或边缘盒子上，这是能跑起来的关键。