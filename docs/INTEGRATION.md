# 集成说明：三个人的代码合到一起（A + B + C）

> 这份文档回答一个问题：**分支怎么合的、合的时候踩了哪些坑、现在能跑成什么样、还差什么。**
> 写于合并当天，对应分支 `integration`（= `main` + `monke`(A) + `mason`(C)）。

---

## 1. 分支与合入方式

| 分支 | 内容 | 最新提交 |
|---|---|---|
| `main` | B 的主线：`model/`、`tests/`、`results/`、`docs/spec/` | `9b6001f` |
| `monke` | A：`vision/`、`data/`、`docs/DATA_DICT.md`、`common/demo_data/` | `a17b49a` |
| `mason` | C：`service/`、`dashboard/`、`scripts/`、`docs/DEPLOY.md` 等 | `668c4ba` |
| **`integration`** | **三者合并** | 见 `git log` |

合并方式：`mason` 已经包含 `main`，所以在 `mason` 上 merge `monke`，只产生两处 add/add 冲突。

**冲突解决原则**：合并双方内容，删除重复项，保留更强的约束。

| 冲突文件 | 处理 |
|---|---|
| `.gitattributes` | 合并两边规则：`* text=auto eol=lf` + 全部二进制扩展名（双方各有一份不同的二进制清单） |
| `.gitignore` | 合并：C 的 `.env`/`elevator.db`/`results/*.tmp` + A 的 `.devenv/`、`models/*.pt|*.onnx`、`data/raw/*`（保留 `.gitkeep`） |

**`common/schema.py` 是谁的？** 保持 B 的版本，A/C 都不改它。A 的代码原本调用了自己版本里的 `VisionOut.to_dict()`，
合并后会 `AttributeError`，已改成 `dataclasses.asdict()`（`vision/infer.py:_as_dict`），
这样共享文件谁都不用为对方让步。

---

## 2. 合并时发现并修掉的 6 个真实问题

这些都不是"跑一下就好"的小事，每一个都会让别人的代码直接失效。按发现顺序：

### 2.1 写了 `to_dict()`，但共享 schema 里没有
`common/schema.py` 是 B 的版本，只有 dataclass，没有辅助方法。A 的 `infer.py` 里有 3 处 `o.to_dict()`。
→ 改成 `dataclasses.asdict(o)`，两边都不用改共享文件。

### 2.2 IMU 元数据文件名/字段和 B 对不上
- B 读：`<case>_imu.meta.json` 里的 `start_ts_ms`（`model/config.py: sidecar_path`）
- A 写：`<case>_imu_meta.json` 里的 `window_end_ts_ms`

→ A 改成写 B 的约定文件名与字段（同时保留 `window_end_ts_ms` 等补充字段，B 会忽略多余键），
并在 `validate_demo_data.py` 里加硬校验：`start_ts_ms` 必须是毫秒量级，且 `start_ts_ms + 10000` 与首个窗口结束时间一致。

### 2.3 IMU 窗口 stride 不一致
- A 原来：`stride=100`（10 秒窗、5 秒步进 → 50% 重叠），30 秒数据出 5 个窗口
- B 假设：`ts_i = start_ts_ms + (i+1)×10000`（**不重叠**）

→ 两边时间戳逐毫秒对不上（相差 5、10、15、20 秒）。A 改为 `stride=200`（不重叠），
30 秒数据出 3 个窗口，与 B 的 `(3,200,3)` mock 完全一致，`ts_match=True`。

### 2.4 IMU 比视频晚开机 10 秒 → B 前 10 秒全是 warmup
B 的 `Aligner` 在收到第一个 IMU 窗口之前一律判 `warmup`，状态机不推进。
原来的 IMU 从视频第 0 秒开始记录，而窗口 10 秒一个 → **视频前 10 秒永远没有 IMU**，20 秒的片子一半废掉。

→ A 改成"传感器比摄像头先开机"：IMU 段从**视频开始前 10 秒**起，第一个窗口正好在视频第 0 帧结束。
这是真实场景（IMU 常开、摄像头按需启动），修完全程有 IMU。

### 2.5 A 的 IMU 太"安静"，B 判不出"电梯在动"
B 的 `motion_level = (1-idle)×(1-vibration)`，`idle` 由 10 秒窗口内**方差**决定
（`idle_var_center=0.02`，归一化单位）。A 原来的合成信号在行驶段只有 ±0.35 m/s² 的瞬时加减速包络，
窗口方差只有 0.0009 → B 判 `idle=0.98` → 状态机永远停在 `IDLE`，一个告警都出不来。

→ A 给"行驶段"补上 10~14 Hz 的导轨宽带振动（az ±1.2 m/s²、ax/ay ±0.5 m/s²，归一化后方差 ≈0.05），
与 B 自己 mock 里 "moving" 窗口的方差（0.0527）同一量级。修完 B 能正确判出 `idle≈0.51 → 运动中`。

> 这条也说明一件事：**A 的"物理合理性"要和 B 的"特征量纲"对齐**——
> 信号看起来像不像不重要，重要的是 B 的判据在它上面能不能触发。

### 2.6 换行符把 B 的契约锁测试搞挂了
B 的 `tests/test_engineering_rules.py` 断言 `common/schema.py` 与 `tests/test_schema_contract.py` 的 sha256
（防篡改）。`.gitattributes` 新加的 `eol=lf` 会让工作区重新规范化，但**老文件在 checkout 时已经是 CRLF**，
于是哈希对不上，168 个测试里挂 1 个。

→ `git checkout` 重新检出该文件（按 `.gitattributes` 规范化为 LF）后通过。
**新克隆的仓库不会遇到这个问题**（checkout 时就按 LF 落地）。

---

## 3. 现在的状态：能跑成什么样

### 3.1 三个人各自的东西

| 检查 | 命令 | 结果 |
|---|---|---|
| A 数据契约 | `python data/scripts/validate_demo_data.py` | ✓ 5 段全过（11 项硬检查） |
| A 视觉推理 | `python vision/infer.py --all` | ✓ 5 段 × 500 帧 |
| A 回归 | `python data/scripts/regression.py` | ✓ 人数 MAE 0.00、倒地召回 80.3%、贴门 95.3%、误报 0 |
| B 单测 | `python -m pytest tests -q` | ✓ **168 passed, 61 subtests** |
| C 冒烟 | `python scripts/smoke_test.py` | ✓ **11/11** |

### 3.2 全链路（A → B → C）

```powershell
python -m service.main                    # 窗口 1
python scripts/e2e_full_chain.py --mode demo          # 窗口 2
python scripts/e2e_full_chain.py --mode production    # 生产阈值
```

`--mode demo`（B 的演示阈值 `dwell=8s`）实测：

| 场景 | A 的 VisionOut 峰值 | B 的判决 | C 落库 | reason 里的证据 |
|---|---|---|---|---|
| demo_normal | people=2, fall=0.00 | trapped(2) | ✓ | `dwell=9.4s > 8s`, `person_count=2 >= 1` |
| demo_estop | people=1, estop=1.00 | **estop(1)** | ✓ | `imu.estop=1.00 > 0.80` |
| demo_trapped | people=2, fall=0.00 | **trapped(2)** | ✓ | `dwell=9.4s > 8s`, `imu.idle=0.98 > 0.80` |
| demo_pry | people=2, door=1.00 | trapped(2) | ✓ | `imu.vibration=1.00` |
| demo_faint | people=2, fall=0.90 | **faint(3)** | ✓ | `fall_prob=0.90 > 0.70`, `dwell=9.4s > 8s` |

`--mode production`（`dwell=180s`）：只有 `demo_estop` 出告警（`estop(1)`），其余 4 段判 `normal(0)`、不入库。

### 3.3 怎么理解这两组结果（答辩口径）

- **生产阈值下 4 段判 normal 是正确行为**：困人的判据是"滞留 > 180 秒"，
  而演示片只有 20 秒——**物理上不可能被困 3 分钟**。这不是 bug，是数据时长不够。
- **演示阈值（8 秒）是为了让 20 秒的片子能演出完整链路**，B 自己就提供了这个预设
  （`PRESET_DEMO`），他们的 mock 场景库也用它。
- **demo_normal 在演示阈值下被判 trapped 是阈值副作用**：2 人、门关、停了 9.4 秒，
  按 8 秒判据就是"可疑滞留"。要演"正常乘梯"，应该用**生产阈值**（→ normal），
  或者把片段做长、让门开一次（B 从"人数变化/贴门值下降/由动转静"推断门开）。

---

## 4. 还差什么（按优先级）

| # | 事项 | 归谁 | 说明 |
|---|---|---|---|
| 1 | **演示片段时长** | A | 20 秒片子在**生产阈值**下不可能触发困人。要演"真·困人 3 分钟"，A 需出 200 秒长片（B 的 mock 就是 200 秒）；或答辩统一用 `--mode demo` 并说明这是演示阈值 |
| 2 | `demo_pry` 没判成 pry | A/B | 需要竖井门缝贴门值在 dwell 达标**之后**仍保持 >0.6；A 可把扒门动作延后到 12~18 秒 |
| 3 | 长片体积 | A | 200 秒 720p ≈ 12 MB/段 + 关键点轨迹 ≈ 10 MB/段，需要考虑是否只在本地生成、不进 Git |
| 4 | 昇腾真机 | A | 只有 ONNX + ATC 方案，无实测（`docs/A_ONNX_ASCEND.md` 已注明） |
| 5 | 云上部署联调 | C | 本次只在 `sqlite + memory` 双模式验证；`rds + redis` 需要真实云资源 |
| 6 | 依赖版本 | 全员 | 本机是 Python 3.14 / numpy 2.5 / opencv 5.0 / fastapi 0.165，与 lock（3.10/3.11、numpy 1.26、opencv 4.10、fastapi 0.115）不一致；接口没受影响，但**答辩机器建议按 lock 装** |

---

## 5. 一键自检（合并后新增）

```powershell
python scripts/e2e_full_chain.py --mode demo   # 全链路：视觉→融合→接口→看板
```

它替代了"三个人各自跑一遍再人工拼"的做法：4 步打勾，失败退出码非 0，可以直接进 CI。

---

## 6. 本次改动的文件清单

| 文件 | 改动 |
|---|---|
| `.gitattributes` | 合并双方规则（换行 + 二进制扩展名） |
| `.gitignore` | 合并双方规则 |
| `vision/infer.py` | `to_dict()` → `dataclasses.asdict()` |
| `data/scripts/build_demo_data.py` | IMU 元数据改名/加 `start_ts_ms`；stride 100→200；IMU 前移 10 秒；新增行驶段振动；`synth_imu` 改为视频相对时间轴 |
| `data/scripts/validate_demo_data.py` | 校验 `_imu.meta.json` 与 `start_ts_ms`，且与窗口结束时间对齐 |
| `scripts/e2e_full_chain.py` | **新增**：A→B→C 全链路联调 |
| `docs/INTEGRATION.md` | **新增**：本文档 |
| `README.md` | 补 A 已交付、全链路命令 |
| `common/demo_data/*` | 重新生成（尺寸不变：1280×720 / 25fps / 500 帧；IMU 变 `(3,200,3)`） |