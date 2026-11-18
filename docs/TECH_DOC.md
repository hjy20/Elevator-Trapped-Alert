# 技术文档（骨架 · C 统稿）

> 章节标记：✅ C 已写 ｜ ⏳A 待 A 补 ｜ ⏳B 待 B 补
> 提交前请把 ⏳ 全部替换为真实内容，**没做的部分写明"未实现"，不要含糊**。

---

## 1. 方案定义 ✅

> 不换电梯、不加布线，用轿厢内视觉 + 电梯运行信号，判断"是否有人被困在电梯里"，
> 输出可解释的告警：楼层、人数、紧急度、判定理由。

**与现有方案的核心差异**：现有电梯物联网采集"机器故障"，本方案采集"人的处境"。

**五类事件语义**（三人理解必须一致）：

| event_type | 语义 | 紧急度 |
|---|---|---|
| `normal` | 正常乘梯，到站开门进出 | 0 |
| `estop` | 电梯急停/异常停机，轿厢内人员无明显异常 | 1 |
| `trapped` | 停梯、门未开、人员滞留超阈值 | 2 |
| `pry` | 有人扒门/拍门求助 | 2 |
| `faint` | 有人倒地不起（最紧急） | 3 |

---

## 2. 系统架构 ✅

```
┌──────────────── A 数据与感知 ────────────────┐
│  视频帧 → YOLO-Pose → VisionOut              │
│  人数 / 倒地 / 贴门 / 拥挤度                  │
└──────────────────────┬───────────────────────┘
                       │ VisionOut（每帧一条）
┌──────────────────────▼──── B 算法与决策 ─────┐
│  IMU (N,200,3) → IMUEvent                    │
│  事件 + 视觉 + 滞留时长 dwell                 │
│  → 状态机 → Decision（reason 可解释）         │
└──────────────────────┬───────────────────────┘
                       │ Decision JSON
                       │ POST /api/v1/edge/report
┌──────────────────────▼──── C 云服务与交付 ───┐
│  华为云 ECS：FastAPI + 静态看板               │
│  华为云 RDS：告警落库、历史查询               │
│  华为云 Redis：30s 去重、最新状态缓存         │
└──────────────────────────────────────────────┘
```

**设计取舍**：
- **接口契约先行**：`VisionOut` / `Decision` 两个结构体先定死，三人并行开发，互不阻塞。
- **云端只跑服务与展示**：推理可在云上（离线批处理）也可在边缘；实时性要求高时降分辨率 + 跳帧。
- **不用 Docker / Nginx / 前端框架**：15 天项目，单进程 + systemd 足够，减少故障面。

---

## 3. 数据契约（L1 冻结）✅

字段定义见 `common/schema.py`，与 `00_协作总览与接口契约.md` §6.3 一致。

| 结构 | 生产者 → 消费者 | 关键字段 |
|---|---|---|
| `VisionOut` | A → B | `ts, person_count, fall_prob, door_approach, crowded, keypoints_ok` |
| `IMUEvent` | B 内部 | `ts, estop, vibration, idle, window_sec=10.0` |
| `Decision` | B → C | `ts, device_id, trapped, urgency, location, people_count, event_type, confidence, reason[]` |

⚠️ **改动规则**：任何字段增删改，必须在群里同步并更新 00 文档；C 侧 `service/models.py` 与契约逐字段对齐。

---

## 4. 模块说明

### 4.1 视觉感知 ⏳A
（A 补：模型选型、输入输出、人数统计方法、倒地判定公式与时序滤波、贴门规则、防误报设计、`keypoints_ok` 的降级策略）

### 4.2 IMU 事件检测 ⏳B
（B 补：窗口设计 200 步 @20Hz、特征、模型、三类事件阈值）

### 4.3 困人状态机与融合 ⏳B
（B 补：状态转移图、dwell 累积方式 180s 阈值依据、`reason` 生成规则、`urgency` 映射）

### 4.4 服务层（C）✅

| 文件 | 职责 |
|---|---|
| `service/main.py` | FastAPI 应用，4 个契约接口 + 2 个看板接口，静态托管 |
| `service/models.py` | `DecisionIn` 请求模型（Pydantic v2），字段与契约一一对应 |
| `service/config.py` | 手写 `.env` 解析，环境变量优先；统一路径与业务常量 |
| `service/db_local.py` | SQLite 实现，`DDL_SQLITE` |
| `service/db_rds.py` | 华为云 RDS MySQL 实现，`DDL_MYSQL` |
| `service/cache_memory.py` | 内存缓存（去重 + 状态） |
| `service/cache_redis.py` | 华为云 Redis（SETNX + TTL） |

### 4.5 看板（C）✅
单文件原生 HTML + JS，深色大字号，2 秒轮询，紧急度 0 灰 / 1 黄 / 2 橙 / 3 红（3 级脉冲），
一键确认处置，连接断开设提示条不白屏，设备离线单独提示。

---

## 5. 关键代码片段 ✅（C 部分，PPT P8/P12 可用）

### 5.1 双模式适配：一个环境变量切换两套后端

```python
def _load_db() -> None:
    if config.DB_MODE == "rds":
        try:
            from . import db_rds as mod
            mod.init_db()                      # 连不上会抛异常
            STATE.update(db=mod, db_mode="rds", db_error="")
            return
        except Exception as exc:
            STATE["db_error"] = f"{type(exc).__name__}: {exc}"   # 记录但不崩
            log.warning("RDS 连接失败，自动降级到 SQLite：%s", STATE["db_error"])
    from . import db_local as mod
    mod.init_db()
    STATE.update(db=mod, db_mode="sqlite")     # 兜底实现
```

**解读三点**：
1. 两个后端导出**完全相同的 5 个函数签名**，上层调用点不需要任何 `if` 分支。
2. 降级逻辑只有一处，`/api/v1/health` 的 `db_mode` 与 `degraded` 直接把真实状态暴露给看板。
3. 云端断库不会白屏、不会 500，答辩现场最怕的"演示翻车"被消灭。

### 5.2 Redis 去重：SETNX + TTL

```python
is_first = cache.setnx(f"alert:{device_id}:{urgency}", 30)
if not is_first:
    return {"ok": True, "alert": True, "dedup": True, "id": None}
alert_id = db.insert_alert(payload)
```

**解读**：状态机每帧都可能输出 `Decision`，若不做去重，一部电梯 1 分钟能写上千条重复告警。
用 `SET key 1 NX EX 30` 原子操作，同设备同紧急度 30 秒内只留一条，**且不依赖数据库唯一索引**（Redis 与 RDS 解耦）。

### 5.3 正常状态不落告警表

```python
if payload["urgency"] == 0 and not payload["trapped"]:
    return {"ok": True, "alert": False, ...}   # 只刷新设备心跳
```

**解读**：`normal` 只更新 `devices.last_seen`，不写 `alerts`，看板与"今日告警数"才干净。

---

## 6. 数据库设计 ✅

```sql
alerts(id, ts, device_id, trapped, urgency, location, people_count,
       event_type, confidence, reason, status, ack_ts)
devices(device_id, last_seen, model_version)
```

| 设计点 | 说明 |
|---|---|
| `ts BIGINT` | 毫秒时间戳，与契约一致；跨语言、跨库不丢精度 |
| `reason TEXT` | 存 `json.dumps(list)`，读出还原为数组；保留可解释性全文 |
| `status` | `active` / `acked`，不删记录，保留审计 |
| `urgency INT` | 0–3，可直接排序、统计、画趋势（原方案三档字符串做不到） |
| 双份 DDL | `DDL_SQLITE` / `DDL_MYSQL` 字段名与顺序一致，仅方言不同（`AUTOINCREMENT` vs `AUTO_INCREMENT`、`REAL` vs `FLOAT`） |

---

## 7. 部署 ✅
详见 `docs/DEPLOY.md`。

**架构**：华为云 ECS（Ubuntu 22.04）+ venv + systemd（`Restart=always`），
RDS / Redis 走同 VPC 内网，安全组仅放行 18080 与 ECS→数据库端口。

**双重保障**：
1. 进程级：systemd 自动拉起。
2. 依赖级：RDS/Redis 不可用自动降级，演示不中断。

---

## 8. 效果与指标 ⏳B

| 指标 | 数值 | 说明 |
|---|---|---|
| 困人识别召回率 | ⏳ | 测试集 N=XX |
| 误报率（每小时/部） | ⏳ | 正常乘梯样本 N=XX |
| 五类事件分类准确率 | ⏳ | |
| 端到端延迟 | ⏳ | 视频帧 → 看板告警 |
| 消融：去 IMU / 去贴门 / 去倒地 | ⏳ | 各降多少 |

> 数据以 `model/MODEL_CARD.md` 为准；PPT P9 与本表必须一致。

---

## 9. 创新点 ⏳B
（B 补：三条创新点 + 与现有方案的差异化对比 + 为什么这是"传统场景的新突破"）

---

## 10. 局限与后续 ✅
- 夜间低照度 / 遮挡严重时，姿态关键点不可信，系统标记 `keypoints_ok=False` 并降级到"人数 + 时长"判据，**不硬判**。
- 单摄像头存在盲区（轿厢角落），后续可增加广角或双摄。
- 楼层定位依赖电梯运行信号，信号缺失时 `location="?"`，不影响困人判定。
- 当前为单设备演示，规模化需引入设备注册、多租户隔离（15 天内不做）。
