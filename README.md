# Elevator-Trapped-Alert · 电梯困人主动预警

> 不换电梯、不加布线，用**轿厢内视觉 + 电梯运行信号**判断"是否有人被困"，
> 输出**可解释的告警**：楼层、人数、紧急度、判定理由。
>
> 现有电梯物联网采集的是"**机器故障**"，我们采集的是"**人的处境**"。

华为 ICT 大赛 · 赛题 1「基于华为云产品打造 AI 创新应用」
华为云 ECS（FastAPI + 看板）+ RDS for MySQL（告警落库）+ Redis（去重 / 最新状态）+ 码道 CodeArts（开发与部署）。

---

## 数据流

```
视频帧 ──► A: vision/  ──► VisionOut ──┐
                                        ├──► B: model/ ──► Decision ──► C: POST /api/v1/edge/report
IMU 窗口 ─► B: model/  ──► IMU 事件 ───┘                                    │
                                                                            ▼
                                                          RDS/Redis 落库 + 去重
                                                                            │
                                                                            ▼
                                                              dashboard/ 看板 2s 轮询弹告警
```

三人各自只碰自己的目录，`common/schema.py` 是全队唯一契约（锁定，勿改）。

---

## 快速开始（服务端）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1                      # Git Bash: source .venv/Scripts/activate
pip install -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env                          # 默认 DB_MODE=sqlite / CACHE_MODE=memory
python scripts/check_env.py
python -m service.main
```

看到 `Uvicorn running on http://0.0.0.0:18080` 即成功。**这个窗口不要关**，另开一个窗口做测试：

```powershell
python scripts/smoke_test.py                    # 一键验收，应 11/11 通过
python scripts/play_case.py --case demo_faint --no-video
```

| 地址 | 用途 |
|---|---|
| <http://127.0.0.1:18080/> | 看板（告警列表 + 三数字卡） |
| <http://127.0.0.1:18080/docs> | 接口文档 |
| <http://127.0.0.1:18080/api/v1/health> | 健康检查 |

> Python 版本不是 3.10/3.11（比如只有 3.14）时，lock 里的 numpy/opencv/pydantic
> 没有预编译包会装失败 —— 见 **`docs/C_SERVICE_SETUP.md`** 的兜底路线。

---

## 仓库结构

```
Elevator-Trapped-Alert/
├── common/              ★ L1 契约（schema.py 锁定）+ demo_data/ 五段演示样本
├── vision/              A：视频 → VisionOut
├── model/               B：IMU 检测 / 状态机 / 融合 / 评测
├── results/             B 的评测产出 + 五段 demo_*_decision.json（C 直接消费）
├── tests/               B 的 unittest 套件 + 契约快照
├── service/             C：FastAPI + RDS/SQLite + Redis/内存 双适配
├── dashboard/           C：看板（单文件原生 HTML+JS，不装 Node）
├── scripts/             C：自检 / 造数据 / 回放 / 冒烟 / 部署
├── docs/                spec/（设计文档）+ DEPLOY / TECH_DOC / PPT / 视频脚本
├── requirements.lock.txt  ★ 全队唯一安装依据（LOCK-1.0，Python 3.10/3.11）
└── .env.example         双模式配置模板（真实 .env 不进 Git）
```

---

## 接口（契约 §7.2）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查，返回实际模式与是否降级 |
| POST | `/api/v1/edge/report` | 接收 B 的 `Decision`，去重后落库 |
| GET | `/api/v1/alerts?status=active` | 活动告警（`acked` / `all` 亦可） |
| POST | `/api/v1/alerts/{id}/ack` | 确认处置 |
| GET | `/api/v1/stats` | 看板统计（今日告警 / 未处理 / 平均响应 / 设备在线） |
| GET | `/api/v1/devices` | 设备在线状态（`last_seen` 超 2 分钟视为离线） |

`Decision` 字段见 `common/schema.py`；任何改动必须走契约流程，不得单方面修改。

---

## 双模式（保命设计）

| 场景 | `DB_MODE` | `CACHE_MODE` | 说明 |
|---|---|---|---|
| 云端正式部署 | `rds` | `redis` | 体现华为云产品使用 |
| 答辩现场断网 | `sqlite` | `memory` | **断网也能演示，绝不翻车** |
| 本地开发 | `sqlite` | `memory` | 不需要连云 |

切换只改 `.env` 两行，代码零改动。RDS / Redis 连不上时**自动降级并打日志**，
`/api/v1/health` 的 `degraded` 字段与看板右上角状态可直接看到实际模式。

Redis 两个用途：告警去重（`alert:{device_id}:{urgency}`，SETNX + 30s TTL）、
最新状态缓存（`state:{device_id}`，TTL 60s）。

---

## 队友模块怎么跑

**B（模型与决策）** —— 全部测试不依赖网络 / GPU / 真实样本：

```powershell
python -m unittest discover -s tests -t . -p "test_*.py"
python model/imu_detect.py --npy results/_mock/demo_estop_imu.npy --verbose
python model/eval.py --cases all --mock --seed 0
python model/fuse.py --mock --report-health
```

**A（视觉）** —— 五段演示视频 + 关键点轨迹已交付在 `common/demo_data/`：

```powershell
python data/scripts/validate_demo_data.py     # L1 契约自检（11 项）
python vision/infer.py --all                  # 视频 -> VisionOut
python data/scripts/regression.py             # 人数/倒地/贴门回归
python vision/doctor.py                       # 环境自检
```

**全链路联调（A -> B -> C，合并后新增）** —— 需要服务端已启动：

```powershell
python scripts/e2e_full_chain.py --mode demo         # 演示阈值（dwell 8s）
python scripts/e2e_full_chain.py --mode production   # 生产阈值（dwell 180s）
```

合并过程中修掉的接口问题（IMU 元数据命名、窗口 stride、IMU 起始时刻、行驶段振动等）
都记在 `docs/INTEGRATION.md`，**改 A/B 交接的数据格式之前先看那份文档**。

---

## 演示数据的查找顺序

`scripts/play_case.py` / `mock_edge.py` 会按以下顺序找数据：

1. `results/<case>_decision.json` —— **B 的真实产出**（如 `results/demo_faint_decision.json`）
2. `results/decision_<case>.json` —— C 的合成数据，仅在 B 未产出时兜底

即 B 一交付，演示自动切到真实数据，脚本不用改。时间戳会平移到当前时刻，
所以看板的"今日告警 / 平均响应"不会因为历史数据变成 0 或天文数字。

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `WinError 10061 由于目标计算机积极拒绝` | **服务没启动**。先开一个窗口跑 `python -m service.main`，看到 `Uvicorn running` 再测 |
| `No module named 'fastapi'` | 没激活虚拟环境，看命令行前是否有 `(.venv)` |
| 装 numpy/pydantic 时开始编译并报 Rust / MSVC | Python 版本过高，见 `docs/C_SERVICE_SETUP.md` |
| `/docs` 404 | `app.mount("/")` 必须写在所有路由之后（已处理，勿调序） |
| `Address already in use` | 18080 被占用，`python scripts/check_ports.py` |
| 上报返回 422 | `Decision` 字段名/类型不符，对照 `common/schema.py` 逐字检查 |
| 看板显示"连接断开" | 服务没启动或已退出 |
| `degraded: true` | RDS/Redis 连不上已降级，检查 `.env` 与安全组 |
| `.sh` 在 Ubuntu 上报 `bad interpreter` | 换行符被转成 CRLF，仓库已用 `.gitattributes` 统一为 LF |

---

## 协作约定

| 角色 | 目录 | 分支 |
|---|---|---|
| A 数据与感知 | `data/` `vision/` | `feat/a` |
| B 算法与决策 | `model/` `results/` `tests/` | `feat/b` |
| C 云服务与交付 | `service/` `dashboard/` `scripts/` `docs/` | `feat/c` |

- 提交信息带角色前缀：`[A] …` `[B] …` `[C] …`
- `main` 只由队长合并，个人不上 `main` 直接提交
- 改 `common/` 或 `docs/` 前先在群里说一声
- 详细 Git 流程见 `docs/GIT_GUIDE.md`
