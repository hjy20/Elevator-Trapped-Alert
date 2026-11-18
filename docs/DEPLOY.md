# 部署与云服务说明（C 负责 · 赛题材料）

> 本文档是初赛材料「华为云产品使用」的证据，需配合 CodeArts 截图一起提交。
> 对应评审项：技术架构完备且可行（应用价值 40%）、华为云产品使用（赛题硬要求）。

---

## 1. 云资源清单

| 服务 | 规格建议 | 用途 | 谁开通 |
|---|---|---|---|
| 码道 CodeArts 代码智能体 | 已有 | 开发、生成部署配置（**赛题必选**） | 三人 |
| RDS for MySQL | 8.0，最小规格（2C4G 单机即可） | 告警落库、历史查询 | C |
| Redis | 5.0+，1G 主备或单机 | 告警去重（30s）、最新状态缓存 | C |
| ECS | 2C4G，Ubuntu 22.04 LTS | 部署 FastAPI + 看板 | C |
| 代金券 | 报名后申请 | 覆盖上述费用 | 队长 |

**网络要求**：RDS / Redis 与 ECS **放在同一 VPC、同一子网**，走内网地址（不出公网、不计流量费、延迟低）。
ECS 安全组**入方向放行 18080**；RDS/Redis 安全组只对 ECS 内网 IP 放行 3306/6379，**不要对 0.0.0.0/0 开放**。

---

## 2. 开通步骤（有先后顺序）

### 2.1 申请代金券并实名认证
报名后由队长在赛事页面申请；华为云账号需完成**实名认证**，否则买不了按需资源。这一步有审核时间，**建议 D1 就提交**。

### 2.2 开通 RDS for MySQL
控制台 → 数据库 → 云数据库 RDS → 购买实例：
- 引擎 MySQL 8.0，实例类型"单机"，规格选最小档
- 网络选"与 ECS 同 VPC"
- 设置 root 密码（**写进 `.env`，不要提交 Git**）
- 创建数据库 `elevator`（字符集 `utf8mb4`）

开通后记录：**内网地址、端口 3306、用户名、密码、库名**。

### 2.3 开通 Redis
控制台 → 数据库 → 分布式缓存服务 Redis → 购买：
- 版本 5.0+，规格 1G，与 ECS 同 VPC
- 记录**内网地址、端口 6379、密码**（如有）

### 2.4 开通 ECS
控制台 → 计算 → 弹性云服务器 ECS → 购买：
- 镜像 Ubuntu 22.04 LTS，规格 2C4G（最低 1C2G 也能跑）
- 绑定弹性公网 IP（EIP），带宽按流量计费即可
- 安全组入方向：**TCP 18080 对 0.0.0.0/0**
- 登录方式用密钥对（比密码安全，也方便脚本免密）

### 2.5 📸 CodeArts 留证（赛题考察点，别漏）
在码道 CodeArts 里做这三件事，每件都截图：
1. 让它**生成部署脚本 / systemd 配置**（本仓库 `scripts/deploy_cloud.sh` 即由此产出）
2. 让它**解释 RDS/Redis 的接入方式与 `.env` 配置项**
3. 让它**检查部署后的日志报错**（贴报错原文）

截图存放建议：`docs/screenshots/01_codearts_deploy.png` 等，PPT P10 直接用。

---

## 3. 部署（venv + systemd，不用 Docker / Nginx）

### 3.1 本地准备
```bash
cp .env.example .env
# 编辑 .env：
#   DB_MODE=rds
#   CACHE_MODE=redis
#   RDS_HOST=<内网地址>  RDS_PORT=3306  RDS_USER=root  RDS_PASSWORD=***  RDS_DB=elevator
#   REDIS_HOST=<内网地址> REDIS_PORT=6379 REDIS_PASSWORD=***
pip freeze > requirements.lock.txt     # 服务器按 lock 装，保证版本一致
```

### 3.2 一键部署
```bash
bash scripts/deploy_cloud.sh root@<ECS公网IP>
```
脚本做四件事：建目录 → rsync 代码（排除 `.venv`/`.env`/`data/raw`）→ 建 venv 装 `requirements.lock.txt` → 写 systemd 单元（`Restart=always`）并启动。

### 3.3 服务器上补 `.env`（首次必须）
`.env` 有意不同步（避免密码进 Git）。首次部署后：
```bash
ssh root@<ECS公网IP>
cd /opt/elevator-rescue && vi .env      # 填入 RDS/Redis 内网地址与密码
sudo systemctl restart elevator-rescue
sudo journalctl -u elevator-rescue -n 50
```
日志里应看到：
```
数据层：华为云 RDS (MySQL) 已连接
缓存层：华为云 Redis 已连接
就绪：http://127.0.0.1:18080/docs
```

### 3.4 验证
```bash
curl http://<公网IP>:18080/api/v1/health
# 期望 {"status":"ok","time":...,"db_mode":"rds","cache_mode":"redis","degraded":false}
```
浏览器打开 `http://<公网IP>:18080/`，看板右上角显示「实时在线」，底部标签显示「数据层：华为云 RDS / 缓存层：华为云 Redis」。

> **不要绑定域名**：国内域名需备案，答辩阶段用 IP 直连即可。

---

## 4. 双模式与异常兜底（架构完备性的核心证据）

| 故障 | 系统行为 | 验证方法 |
|---|---|---|
| RDS 连不上 | 自动降级 SQLite，日志 WARN，`/health` 的 `degraded=true` | 改错 `RDS_HOST` 后重启，看板仍能用 |
| Redis 连不上 | 自动降级内存实现，去重仍生效（进程内） | 改错 `REDIS_HOST` 后重启 |
| 服务进程挂掉 | systemd `Restart=always` 3 秒内拉起 | `sudo kill -9 <pid>` 后观察 |
| 前端连不上后端 | 看板显示「连接断开」，列表灰化，**不白屏** | 停服务后刷新看板 |
| 设备不上报 | 看板顶部提示「N 台设备离线」 | 停止 mock 超过 2 分钟后观察 |
| 现场完全断网 | 切 `sqlite` + `memory`，本地演示 | 提前演练一次并录像 |

答辩话术：
> "云上生产 / 离线兜底双模式，任何网络环境下都能演示；数据库断了会自动降级本地库，不会白屏。"

---

## 5. 排错

| 现象 | 排查 |
|---|---|
| 公网打不开 | 安全组是否放行 18080；`systemctl status` 是否 running；`ss -lntp \| grep 18080` |
| `degraded: true` | `journalctl -u elevator-rescue -n 50` 看 RDS/Redis 报错；检查 VPC/子网/安全组/密码 |
| RDS 报 `Access denied` | 用户名密码错，或该账号无 `elevator` 库权限 |
| 中文字段乱码 | 库/表字符集需 `utf8mb4`，连接串已带 `charset=utf8mb4` |
| 装依赖超时 | 服务器上换源：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.lock.txt` |
| 时区不对 | 服务器 `timedatectl set-timezone Asia/Shanghai`；时间戳统一用毫秒 |

---

## 6. 部署验收清单（D10 前全绿）

- [ ] RDS 实例运行中，`elevator` 库已建，字符集 utf8mb4
- [ ] Redis 实例运行中，ECS 可内网连通
- [ ] ECS 安全组放行 18080，公网可访问
- [ ] `systemctl status elevator-rescue` 为 active (running)
- [ ] 公网 `/api/v1/health` 返回 `db_mode=rds`、`cache_mode=redis`、`degraded=false`
- [ ] 公网看板能显示告警，确认处置可用
- [ ] Redis 去重生效：连发 5 条相同 `device_id + urgency`，只入库 1 条
- [ ] 拔掉 RDS（改错地址）后自动降级，看板仍可用
- [ ] CodeArts 生成部署配置的截图已存 `docs/screenshots/`
- [ ] 换一台干净电脑，30 分钟内能按 README 跑起来
