# B · 算法与决策模块 模型卡（MODEL_CARD）

> 路径裁定（Q-08）：本文件放在 `model/MODEL_CARD.md`。总览目录结构写 `model/MODEL_CARD.md`，任务卡 D8–D10 写 `docs/MODEL_CARD.md`，二者冲突；此处以更靠近代码、便于同步的 `model/` 为准，D11 交材料时由 C 在终稿中引用本路径。
>
> 口径声明：本卡所有指标均**在自建场景库（5 段/5 类）上测得**，且当前跑在**合成数据（mock）**上，**不构成真实电梯环境的性能承诺，不可外推**。真实数据到位前不得对外引用具体数字。

## 1. 任务定义与输入输出

| 项 | 内容 |
|---|---|
| 任务 | 由电梯运行信号与轿厢视觉，推断"是否有人被困"，输出可解释分级告警 |
| 输入 1 | `list[VisionOut]`：逐帧人数、倒地概率、贴门程度、拥挤度、关键点质量 |
| 输入 2 | `list[IMUEvent]`：10 秒窗口的急停/振动/静止三分数 |
| 输入 3 | `RuntimeConfig`：阈值、预设、设备与楼层 |
| 输出 | `list[Decision]`：`ts / device_id / trapped / urgency / location / people_count / event_type / confidence / reason` |
| 不做 | 视觉推理（A 侧）、楼层识别（无法估计时填 `"?"`）、数据库与服务（C 侧） |

`urgency` 语义固定：`0 normal / 1 estop / 2 trapped|pry / 3 faint`；`trapped=True` 时 `urgency` 不得为 1。

## 2. 规则组成

本模块是**规则 + 信号处理**系统，不使用深度学习。四层职责：

| 层 | 模块 | 职责 |
|---|---|---|
| IMU 事件 | `imu_detect.py` | 每 10 秒窗口输出 `estop / vibration / idle`，**不做困人判定** |
| 派生信号 | `signals.py` | `motion_level`、门开代理、人数封顶、概率裁剪、降级标记 |
| 状态机 | `state_machine.py` | 六态 `IDLE→MOVING→STOPPED→DOOR_OPEN→DOOR_CLOSED→OCCUPIED`，毫秒计时 |
| 判决与解释 | `reason.py` | 判决树 V1–V5、四道硬约束、五通道置信度、九阶段 `reason` |

核心判据：`trapped = (state == OCCUPIED) ∧ (person_count ≥ 1) ∧ (effective_dwell_ms > dwell_threshold×1000)`。

`dwell` 拆两量：`raw_dwell_ms = now_ts − stop_enter_ts`（严格定义，可精确断言）与 `accel_extra_ms`（急停加速贡献），判决用 `effective = raw + extra`。

## 3. 数据

| 项 | 说明 |
|---|---|
| 训练数据 | **无**——规则系统，不训练参数 |
| 验证数据 | 5 段合成场景（`normal / estop / trapped / pry / faint`），由 `model/mock_scenario.py` 生成 |
| 数据来源 | **合成（mock），非真实采集**；`results/metrics.md` 头部与 `results/claims.json` 每个数字均带 `data_source` 标记 |
| 真实数据 | 待 A 提供 5 段真实 IMU（含 `<case>_imu.meta.json` 的 `start_ts_ms`）后重测；在此之前所有指标只作流程验证 |
| 场景规模 | 合成段合计约 650 秒；`trapped/pry/faint` 各 200 秒左右滞留 |

## 4. 指标与消融

**主指标（`results/metrics.md`）**：漏报率、段级/帧级误报率、平均响应时长、五类 P/R/F1、`trapped` 二分类。

当前在 mock 数据上的结果：五类 P/R/F1 均为 1.0，正常段 trapped 帧数 0，平均响应约 9.7 秒。
**这组数字只证明"管线自洽"，不能作为性能结论**——场景与判决由同一人编写，属于自测自评。

**四项消融（`results/ablation.csv`，指标为五类宏观平均）**：

| variant | precision | recall | f1 | fp_count | 结论 |
|---|---|---|---|---|---|
| baseline | 1.000 | 1.000 | 1.000 | 0 | 基准 |
| no_fall | 0.700 | 0.800 | 0.733 | 0 | 去掉倒地判据后 `faint` 不可检出，符合预期 |
| no_imu | 0.400 | 0.600 | 0.467 | 0 | 去掉 IMU 后 `estop` 与 `faint` 同时失效，退化最重 |
| no_temporal | 1.000 | 1.000 | 1.000 | 0 | **在合成数据上无显著差异** |

`no_temporal` 无差异的原因必须如实说明：合成场景的时间序列过于规整（段边界有 fade 包络、无真实抖动与蹲起噪声），关闭"1s/3s 持续约束"后没有可被误触发的样本。该项要在真实数据上重跑才有参考价值。

## 5. 已知局限与技术债

| 编号 | 内容 | 影响 | 缓解 | 残留 |
|---|---|---|---|---|
| R-01 | 门状态无真实信号，用"人数变化 + 贴门骤降 + 运动骤降"三选二代理 | 门开/关判断可能滞后一帧 | 需 ≥2 条证据才置位；`reason` 输出 `technical_debt=door_signal_proxy` | 代理信号抖动仍可能造成一次多余的 `DOOR_OPEN` |
| R-02 | 仅 5 段样本、单场景库 | 指标无统计意义 | 明确标注口径与数据来源 | 需 A 的真实数据扩充 |
| R-03 | IMU 输入为合成信号（理想急停脉冲/单频振动） | 真实电梯的噪声谱未验证 | 参数集中在 `config.py`，可用真实数据标定回填 | `estop` 阈值仍需现场复核 |
| R-04 | 对齐依赖 `.npy` 段起点 `t0_ms` | 基线错则对齐全废（指标表面正常） | 三级解析 + `warmup` 帧单列 + `imu_alignment_degraded` 告警 | 缺 sidecar 时绝对时间戳非真实拍摄时刻 |
| R-08 | "髋部高度"条件在 A 的 `fall_prob` 内部 | B 侧只能保证 3 秒持续性这一半 | 在 `MODEL_CARD` 与 `EVAL_REPORT` 明示跨模块双约束 | 5 段数据无法独立验证髋高 |

## 6. 与现有方案的差异

现有电梯物联网采集的是**机器故障码**；本方案采集的是**人的处境**。

- 不依赖电梯厂商接口，用独立传感器 + 轿厢视觉做旁路推断；
- 断网可运行（端侧规则推理，无网络依赖）；
- 输出"停梯位置 + 被困人数 + 紧急程度 + 判定理由"，不只输出一个故障码。

## 7. 演示参数声明

| 项 | 生产 | 演示 |
|---|---|---|
| `dwell_threshold` | 180 s | 8 s |
| `demo_threshold` | `False` | `True` |
| 其余 9 项阈值 | 相同 | 相同 |
| 代码路径 | 完全相同 | 完全相同 |

`demo_threshold` **只作为 `ReasonBuilder` 的上下文**，让 `reason` 里出现 `demo_threshold=true`，不参与任何判决计算。该性质由 AST 测试固化：算法模块函数体内不得出现 `demo_threshold` 分支。

## 8. 设计裁定（开放问题落地）

| 编号 | 裁定 |
|---|---|
| Q-03 | `reason` 严格按 13 键白名单，**不输出** `state` / `door_state`；附录 C 的裸词写法生成端不产出 |
| Q-04 | 同时满足倒地与贴门时，`faint` 优先于 `pry`（公共安全优先） |
| Q-06 | "去时序滤波"= `fall_persist_sec` + 全部 `RunClock` 最短持续 + estop 短窗峰值；**门开清零不算滤波器** |
| Q-07 | 时序回退帧"推进语义丢弃、输出语义保留"：不推进状态，但输出上一有效判决的定格副本 |
| Q-10 | `dwell` 保留 `raw` 与 `effective` 双量；`reason` 展示秒，`raw` 与 `accel` 可留痕 |

## 9. 实现阶段对设计的细化（必须在答辩中能解释）

1. **`estop` 用「峰值 − P90」**：纯峰值会把正常运行的正弦加速度（变化率持续 1.5–2.6）判成急停；扣掉 P90 后运行段 `estop≈0.05`、脉冲段 `≈1.0`。`estop_rate_center` 初值相应从 0.55 标定到 3.0。
2. **`faint` 增加 `imu_present` 前置**：否则 IMU 缺失降级后会出现 `urgency=2 + event_type=faint` 的非法组合，违反 `URGENCY_EVENT_MAP`。
3. **门开代理需 ≥2 条证据**：单靠 `motion` 骤降会把"电梯停稳"误判为开门，导致永远进不了 `STOPPED`。
4. **`.npy` 窗口语义**：契约 `(N,200,3)` 的 `N` 是窗口数；实现同时兼容扁平 `(T,3)` 自动切窗。真实交付格式待 A 确认。
5. **对齐健康度口径**：`nearest` 占比天然约 5%（窗口 `ts` 是结束时刻，只有窗口末 500 ms 的帧能最近邻）；判定改为 `nearest + hold > 0.35`，并把首个窗口闭合前的帧单列为 `warmup`，不计入退化。
6. **`vibration` 扣除白噪声基线**：白噪声在 2–8 Hz 的期望占比约 0.6，不扣基线会直接饱和成 1.0。
7. **`motion_level = (1 − idle) × (1 − vibration)`**：原设计的加性公式把拍门振动算作"运动"，且 `idle` 上限仅 0.917 会让 `motion` 卡在 `move_thresh` 与 `stop_thresh` 之间的滞后带，状态机永远进不了 `STOPPED`。
8. **`idle_var_gain` 120 → 200**：配合上一条，使 `var≈0` 时 `idle≈0.98`。

## 10. 复现命令

```text
python model/imu_detect.py --npy results/_mock/demo_estop_imu.npy --verbose
python model/fuse.py --mock --report-health
python model/eval.py --cases all --mock --seed 0
python model/eval.py --cases all --mock --calibrate
python model/eval.py --cases all --mock --ablation
python -m unittest discover -s tests -t . -p "test_*.py"
```