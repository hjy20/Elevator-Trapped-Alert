# C 服务端环境说明（service / dashboard / scripts）

> 本文件只讲 **C 这条线怎么装、怎么跑**。
> 全队统一的依赖版本以仓库根的 **`requirements.lock.txt`（LOCK-1.0）** 为唯一依据，
> 本文不新增依赖，只在 Python 版本不对时给兜底方案。

---

## 一、标准路线（推荐，与 A/B 一致）

**Python 3.10 / 3.11**：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1                  # Git Bash: source .venv/Scripts/activate
pip install -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env                      # DB_MODE=sqlite / CACHE_MODE=memory 即可本地跑
python scripts/check_env.py
python -m service.main
```

`requirements.lock.txt` 里已包含 C 需要的 `fastapi / uvicorn / mysql-connector-python / redis`，
**不需要另装**，更不要为了跑服务单独升级 fastapi。

---

## 二、兜底路线（机器上只有 Python 3.13 / 3.14 时）

LOCK-1.0 里的 `numpy==1.26.4`、`opencv-python==4.10.0.84`、
以及 `fastapi==0.115.0` 带出的 `pydantic 2.x` **在 Python 3.13+ 没有预编译包**，
`pip install` 会转去源码编译，Windows 上必然报 Rust / MSVC 相关错误。这不是网络问题。

已在 **Python 3.14.3 + Windows** 实测可用的一组兼容版本：

| 包 | lock 版（3.10/3.11） | 3.14 兼容版（实测） |
|---|---|---|
| fastapi | 0.115.0 | 0.163.2 |
| uvicorn | 0.32.0 | 0.54.0 |
| pydantic | （由 fastapi 带出） | 2.14.3 |
| mysql-connector-python | 9.0.0 | 26.10.0 |
| redis | 5.0.8 | 8.1.0 |
| numpy | 1.26.4 | 2.5.5 |
| opencv-python | 4.10.0.84 | 5.1.0.95 |

```powershell
pip install fastapi==0.163.2 uvicorn==0.54.0 pydantic==2.14.3 `
            mysql-connector-python==26.10.0 redis==8.1.0 `
            numpy==2.5.5 opencv-python==5.1.0.95 `
            -i https://pypi.tuna.tsinghua.edu.cn/simple
```

⚠️ 三条硬约束：

1. **这只是本地兜底，不能写进 `requirements.lock.txt`**——lock 是三人环境一致性的基线，
   改它必须群内同步（契约 §7.4）。
2. **A 的视觉脚本不建议用 3.14**：opencv 5.x / numpy 2.x 与 4.x / 1.26 存在 API 差异
   （常量改名、标量溢出行为变化），跑 A 的代码请用 3.11。
3. **C 的服务端不受影响**：实测 `smoke_test.py` 11/11 通过、`play_case` 全链路正常。

---

## 三、两种模式

| 场景 | `.env` | 说明 |
|---|---|---|
| 本地开发 / 答辩断网 | `DB_MODE=sqlite` `CACHE_MODE=memory` | 不需要连云，开箱即用 |
| 云端正式部署 | `DB_MODE=rds` `CACHE_MODE=redis` | 连不上会自动降级并打日志，不会白屏 |

切换只改 `.env` 两行，代码零改动。

---

## 四、演示数据用哪一份

`scripts/play_case.py` / `mock_edge.py` 的查找顺序：

1. `results/<case>_decision.json` —— **B 的真实产出**（如 `results/demo_faint_decision.json`）；
2. `results/decision_<case>.json` —— C 的合成数据，仅在 B 未产出时兜底。

即：**B 一交付，演示自动切到真实数据，脚本不用改。**

```powershell
python scripts/play_case.py --case demo_faint --no-video
python scripts/mock_edge.py --case demo_trapped --speed 4
```
