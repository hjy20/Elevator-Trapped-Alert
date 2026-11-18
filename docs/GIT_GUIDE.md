# Git 协作手册（本仓库专用）

- 远程仓库：<https://github.com/hjy20/Elevator-Trapped-Alert>
- 现有分支：`main`、`mason`、`monke`
- **C 的工作分支：`mason`**（A/B 的代码已在此分支上，C 的服务端叠加进来）

> 命令在 **PowerShell** 里执行。`git` 用你本机 `C:\mzy\app\Git1\Git`（自带 GitHub 登录组件）。

---

## 一、第一次把本地代码推上去

### 第 1 步 · 关联远程仓库

```powershell
cd C:\Users\senbomen\WorkBuddy\2026-11-18-22-29-17\elevator-rescue
git remote add origin https://github.com/hjy20/Elevator-Trapped-Alert.git
git remote -v
```

### 第 2 步 · 拉取远程分支（必须先做，否则会覆盖队友代码）

```powershell
git fetch origin
git branch -a
```

应能看到 `remotes/origin/main`、`remotes/origin/mason`、`remotes/origin/monke`。

### 第 3 步 · 推送到 mason

```powershell
git push -u origin mason:mason
```

首次会弹出浏览器登录 GitHub（`Sign in with your browser`）——**这是正常的**，登录一次以后不再问。

### 第 4 步 · 验证

浏览器打开 <https://github.com/hjy20/Elevator-Trapped-Alert/tree/mason>，
确认能看到 `service/`、`dashboard/`、`scripts/`、`docs/`，且 `model/`、`tests/`、`results/` 仍是队友原样。

---

## 二、日常节奏（C）

```powershell
git status                          # 先看改了什么
git add -A
git commit -m "[C] 看板加设备离线条"
git pull --rebase origin mason      # 先拉队友的改动
git push
```

**提交信息格式**：`[角色] 做了什么` → `[A] …` `[B] …` `[C] …`
答辩讲分工时，提交记录可以直接当证据。

---

## 三、绝不要做的事

| 别做 | 后果 |
|---|---|
| `git push --force` | 会抹掉队友在 `mason` 上的提交，**不可恢复** |
| 直接往 `main` 提交 | `main` 是发布线，只由队长合并 |
| 改 `common/schema.py` | 全队契约，改前必须群里同步 |
| 提交 `.env` | 华为云数据库密码泄露，删文件也删不掉历史 |
| 提交 `.venv/` | 256MB，仓库直接被撑爆（`.gitignore` 已拦） |

---

## 四、排错表

| 报错 | 原因 | 解决 |
|---|---|---|
| `remote: Repository not found` | 地址错 / 没有协作权限 | 让仓库主人在 Settings → Collaborators 邀请你 |
| `Authentication failed` | 弹窗被关 / 用了密码登录 | `git credential-manager github logout` 后重推，走浏览器授权 |
| `Updates were rejected ... non-fast-forward` | 远程有你没有的提交 | `git pull --rebase origin mason` 再 `git push` |
| `refusing to merge unrelated histories` | 两套无关历史被合并 | 不要硬合，从远程分支重新建工作分支（见第五节） |
| `fatal: not a git repository` | 不在项目目录 | `cd` 到 `elevator-rescue` |
| `.sh` 在 Ubuntu 报 `bad interpreter` | CRLF 换行 | 仓库已用 `.gitattributes` 统一 LF，别删它 |
| `schannel: CRYPT_E_NO_REVOCATION_CHECK` | 证书吊销检查失败 | 换成 `C:\mzy\app\Git1\Git\cmd\git.exe` 或加 `-c http.schannelCheckRevoke=false` |

---

## 五、C 的代码是怎么进 mason 的（操作记录）

本地这套是独立初始化的仓库，和 `mason` **没有共同祖先**，所以不能直接 merge。
采用"**在 mason 之上叠加 C 的文件**"的方式：

```powershell
# 1. 拉远程，从 mason 建本地工作分支
git fetch origin
git checkout -b mason origin/mason

# 2. 只把 C 的文件从 main 拷过来（不碰 common/ model/ tests/ results/）
git checkout main -- service dashboard scripts vision
git checkout main -- docs/DEPLOY.md docs/TECH_DOC.md docs/PPT_OUTLINE.md docs/VIDEO_SCRIPT.md docs/GIT_GUIDE.md docs/C_SERVICE_SETUP.md
git checkout main -- README.md .gitignore .gitattributes "00_协作总览与接口契约.md"

# 3. 提交并推送
git add -A
git commit -m "[C] 服务端 + 看板 + 脚本接入 mason"
git push -u origin mason
```

**刻意没带过去的文件**（避免覆盖队友 / 避免版本分叉）：

| 文件 | 原因 |
|---|---|
| `common/schema.py` | B 已按契约实现，C 不碰 |
| `requirements.txt` / `requirements.lock.txt` | 团队以 LOCK-1.0 为准，C 不另立一套 |
| `model/` `tests/` `results/` | B 的产出，只读消费 |
| `results/decision_demo_*.json` | B 已有真实 `demo_*_decision.json`，合成数据不重复入库 |

---

## 六、常用查看命令

```powershell
git log --oneline --graph --all -20   # 图形化历史
git status                            # 当前改动
git branch -vv                        # 本地分支与跟踪关系
git diff                              # 未暂存改动
git remote -v                         # 远程地址
```
