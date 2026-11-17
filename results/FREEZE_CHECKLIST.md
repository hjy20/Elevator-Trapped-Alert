# D12 冻结清单（B · 算法与决策）

> 数据来源：合成场景库（mock，非真实采集）。冻结后只允许缺陷修复，不再新增功能。
> 冻结前必须逐项勾选；任一项未勾选禁止冻结。

## 1. 契约与依赖基线

- [x] `common/schema.py` 与基线哈希一致（`tests/contract_baseline.json`，sha256 `ce07d514…853e`）
- [x] `tests/test_schema_contract.py` 字节级未改（sha256 `71d76411…07b4`），11 项仍全绿
- [x] `requirements.lock.txt` 为 `LOCK-1.0`，B 三依赖（numpy / scikit-learn / matplotlib）均为 `==` 精确版本
- [x] lock 中无 PyTorch / TensorFlow / PaddlePaddle；`model/` 源码无 `onnxruntime`
- [x] `model/__init__.py` 导入链无 matplotlib（冷启动）

## 2. 测试矩阵（17 个文件 / 168 项）

| # | 文件 | 项数 | 命令 |
|---|---|---|---|
| 1 | `tests/test_schema_contract.py` | 11 | `python tests/test_schema_contract.py -v` |
| 2 | `tests/test_time_semantics.py` | 4 | `python tests/test_time_semantics.py -v` |
| 3 | `tests/test_time_base.py` | 7 | `python tests/test_time_base.py -v` |
| 4 | `tests/test_config.py` | 9 | `python tests/test_config.py -v` |
| 5 | `tests/test_mock_scenario.py` | 14 | `python tests/test_mock_scenario.py -v` |
| 6 | `tests/test_signals.py` | 9 | `python tests/test_signals.py -v` |
| 7 | `tests/test_reason.py` | 13 | `python tests/test_reason.py -v` |
| 8 | `tests/test_state_machine.py` | 18 | `python tests/test_state_machine.py -v` |
| 9 | `tests/test_acceptance.py` | 2 | `python tests/test_acceptance.py -v` |
| 10 | `tests/test_imu_detect.py` | 14 | `python tests/test_imu_detect.py -v` |
| 11 | `tests/test_fuse.py` | 16 | `python tests/test_fuse.py -v` |
| 12 | `tests/test_false_alarm.py` | 8 | `python tests/test_false_alarm.py -v` |
| 13 | `tests/test_preset_switch.py` | 6 | `python tests/test_preset_switch.py -v` |
| 14 | `tests/test_eval.py` | 19 | `python tests/test_eval.py -v` |
| 15 | `tests/test_no_false_alarm_gate.py` | 3 | `python tests/test_no_false_alarm_gate.py -v` |
| 16 | `tests/test_end_to_end.py` | 5 | `python tests/test_end_to_end.py -v` |
| 17 | `tests/test_engineering_rules.py` | 10 | `python tests/test_engineering_rules.py -v` |

- [x] 一键全量：`python -m unittest discover -s tests -t . -p "test_*.py"` → 168 项全绿
- [x] 三处阻断闸门均通过：`T-B-010`（D3 可达 `OCCUPIED`）、`T-B-024`（D7 零误报）、`T-B-034`（本次冻结）

## 3. 产物清单

| 产物 | 状态 | 说明 |
|---|---|---|
| `results/<case>_decision.json` × 5 | 已生成 | 九字段、UTF-8、无 NaN/Inf |
| `results/timeline_<case>.jsonl` × 5 | 已生成 | 逐帧状态、dwell、对齐方式 |
| `results/labels.json` | 已生成 | 人工真值，纳入版本管理 |
| `results/metrics.md` / `metrics.csv` | 已生成 | 含口径 + 数据来源（mock） |
| `results/confusion_matrix.png` | 已生成 | 5×5 + 2×2 |
| `results/ablation.csv` / `ablation.png` | 已生成 | 4 行 × 五类宏观指标 + delta |
| `results/threshold_calibration.md` / `threshold_grid.csv` | 已生成 | 162 组网格，结论"维持初值" |
| `results/claims.json` | 已生成 | 每个数字带 `source_file` + `key_path` + `data_source` |
| `results/gate_normal_zero_fp.json` | 已生成 | 零误报守门证据 |
| `results/preset_switch_check.md` | 已生成 | 双阈值只改配置 |
| `results/fusion_health.json` | 已生成 | 对齐健康度 |
| `results/d2_acceptance.log` | 已生成 | D3 闸门轨迹 |
| `results/EVAL_REPORT.md` | 已生成 | 五节齐全 + 守门记录 + 数据来源 |
| `results/KEY_CODE_WALKTHROUGH.md` | 已生成 | 2 段四段式，与源码逐字一致（测试校验） |
| `model/MODEL_CARD.md` | 已生成 | 六节 + 演示参数 + 裁定 + 实现细化 |

## 4. 确定性与可复现

- [x] 同 `--seed` 两次 `eval` 的 `metrics.csv` 逐格一致（浮点 1e-6）
- [x] 同输入三次 `fuse` 的 `Decision` 深度相等（`test_end_to_end.py::test_determinism_three_runs`）
- [x] 同 seed 两次假数据生成 `vision` 逐字段相等、`imu` 逐元素相等

## 5. 已知局限与责任人

| 项 | 内容 | 责任人 | 截止 |
|---|---|---|---|
| 技术债 | R-01 门状态代理 / R-02 样本 5 段 / R-03 合成 IMU / R-04 对齐依赖 `t0_ms` / R-08 髋高在 A 侧 | B（R-08 与 A 共担） | D12 已声明，不修复 |
| 阻塞项 | `T-B-015` IMU 标定文档需 A 的 5 段真实数据 | A 提供数据、B 标定 | 真实数据到位后 |
| 会签 | DEC-01（`urgency` 语义）、DEC-02（门状态代理）仍待 A/C 回签 | 队长（用户） | D1 遗留，冻结前补签 |
| 仓库卫生 | `.gitignore` 未补，`results/_mock/`、`__pycache__`、`.idea/` 仍在版本库中 | 队长（用户） | 冻结同批处理 |

## 6. P2 关闭确认

- [x] `AblationSwitch` 三开关默认全 `False`，不改变主路径行为
- [x] 1D-CNN（`T-B-OPT-01`）与昇腾一致性（`T-B-OPT-02`）未进入主线依赖链，也未改动 `requirements.lock.txt`
- [x] 关闭全部 P2 后，端到端 5 段与评测产物可复现

## 7. 冻结结论

B 侧主线 34 个任务中 **33 个完成**；唯一未完成的是 `T-B-015`（被 A 的真实数据阻塞），不影响代码与评测流程的自洽性。功能冻结后只允许缺陷修复。