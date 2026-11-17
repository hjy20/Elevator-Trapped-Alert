# 关键代码片段解读（2 段）

> 口径：本文引用的代码与仓库当前源码**逐字一致**，由 `tests/test_engineering_rules.py::test_walkthrough_snippets_in_sync` 校验；效果数字来自 `results/metrics.md` 与 `results/ablation.csv`，均为**合成场景库（mock，非真实采集）**结果。

## 片段 1 · 状态机推进链：先保护时间、再累加滞留、最后才敢判决

### 问题

困人判据是"停梯 + 门未开 + 有人 + 超时"，其中"超时"以毫秒时间差表达。真实视频会出现**重复帧与时间回退**（抽帧、重连、时间戳抖动）。若不先做时间保护，回退帧会让 `dwell` 倒退甚至为负，直接产生"时长不够却报警"或"永远不报警"两种极端错误。

### 思路

把每帧处理固定成"保护 → 累加 → 判决"三段，且保护段必须能**原样返回上一帧结论**：

1. `ts <= last_ts` 视为无效推进：只计数、只告警，不碰任何计时器；
2. 有效帧才做 `raw_dwell_ms = now_ts - stop_enter_ts`，并对超出一天的荒诞值报 `suspicious_time_unit`（防秒/毫秒混用）；
3. 没有滞留基准（门开着或电梯在跑）时，`raw` 与 `accel_extra` 一律清零。

### 代码

<!-- snippet-source: model/state_machine.py -->
```python
        ctx = self.ctx
        cfg = self.cfg
        th = cfg.thresholds
        now = int(evidence.ts)

        if ctx.last_ts is not None and now <= ctx.last_ts:
            ctx.dropped_frames += 1
            # suppress: 时序抖动造成重复告警 -> REQ-FUNC-021
            logger.warning(
                "event=ts_regression prev_ts=%s cur_ts=%s", ctx.last_ts, now
            )
            if self._last_decision is not None:
                return dataclasses.replace(self._last_decision, ts=ctx.last_ts)

        delta_ms = 0 if ctx.last_ts is None else max(0, now - ctx.last_ts)

        if ctx.stop_enter_ts is not None:
            raw = now - ctx.stop_enter_ts
            if raw < 0 or raw > MAX_SANE_DWELL_MS:
                logger.error("event=suspicious_time_unit raw_dwell_ms=%s", raw)
            ctx.raw_dwell_ms = max(0, raw)
        else:
            ctx.raw_dwell_ms = 0
            ctx.accel_extra_ms = 0
```

### 效果

- 输入 `ts = [T, T, T-1000, T+1000]` 时，`dropped_frames == 2`，`last_ts` 仍为 `T+1000`，`raw_dwell_ms` 单调不减——由 `tests/test_state_machine.py::test_ts_regression_dropped` 断言。
- 正常场景零误报：`results/gate_normal_zero_fp.json` 记录 `trapped_true = 0`、`frame_fp_rate = 0.0`。
- 消融对照：关闭时序约束（`no_temporal`）后，合成数据上未出现额外误报（`results/ablation.csv` 中 `no_temporal` 的 `fp_count = 0`）——这一项要在真实数据上重跑才有参考价值。

## 片段 2 · `reason` 九阶段拼接：为什么告警一定要能被人读懂

### 问题

评委与物业不看代码，只看告警理由。若 `reason` 写成"检测到异常，请及时处理"，既无法复核也无法追责；若把毫秒时间戳直接塞进理由，又会与阈值（秒）混排。

### 思路

固定九阶段顺序，空阶段不产出；`dwell` 永远排第一且必然出现（它是本作品的核心判据），并且：

- `normal` 判决最多只追加**一条**补充证据，保证条数 ≤ 2；
- 降级标记（`keypoints_unreliable` / `imu_unavailable`）优先于人数证据，确保可解释性不被截断；
- 演示标记 `demo_threshold=true` 只在演示配置下出现，且不影响判决。

### 代码

<!-- snippet-source: model/reason.py -->
```python
        reason: List[str] = []
        triggered = ">" if ctx.dwell_sec > ctx.dwell_threshold else "<="
        reason.append(
            f"dwell={_fmt_sec(ctx.dwell_sec)}s {triggered} {_fmt_sec(ctx.dwell_threshold)}s"
        )

        if verdict.event_type == "normal":
            if not ctx.keypoints_ok:
                reason.append("keypoints_unreliable=true")
            elif not ctx.imu_present:
                reason.append("imu_unavailable=true")
            elif ctx.demo_mode:
                reason.append("demo_threshold=true")
            elif ctx.person_count >= 1:
                reason.append(f"person_count={ctx.person_count} >= 1")
            self._self_check(verdict, reason)
            return reason

        reason.append(f"person_count={ctx.person_count} >= 1")

        if not ctx.keypoints_ok:
            reason.append("keypoints_unreliable=true")
        elif verdict.event_type == "faint":
            reason.append(f"fall_prob={ctx.fall_prob:.2f} > {ctx.fall_thresh:.2f}")

        if ctx.door_approach > ctx.door_thresh:
            reason.append(
                f"door_approach={ctx.door_approach:.2f} > {ctx.door_thresh:.2f}"
            )
```

### 效果

- 三条黄金示例**逐条字符串相等**（顺序敏感）：`tests/test_reason.py::test_unique_trapped_reason / test_pry_demo_reason / test_faint_reason`。
- `urgency=1 + trapped` 的组合直接抛 `ValueError`，`reason` 不可能出现"困人被降级为关注"。
- 正则断言 `\d{10,}` 不命中，毫秒时间戳不会泄漏进理由；`dwell` 只以秒呈现且由毫秒换算。

## 补充 · 演示/生产切换为什么只改配置

两套预设的唯一差异是 `dwell_threshold`（180 s / 8 s）与 `demo_threshold` 标记；算法模块内**不存在** `demo_threshold` 分支，由 AST 校验固化（`tests/test_preset_switch.py::test_no_demo_branch_in_algorithms`）。
同一份 22 帧演示数据：生产预设零告警、演示预设正常报出，见 `results/preset_switch_check.md`。这回答了答辩高频追问"是不是为了演示临时改代码"——没有改逻辑，只换了一个数。