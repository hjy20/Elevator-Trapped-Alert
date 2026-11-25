# C · 用户上传真实视频（困人判定）—— 编码任务拆解（upload_video_tasks.md）

> 上游需求：`docs/spec/upload_video.md`（`REQ-UP-001~027`，含已决项 `DEC-UP-01/02`：以录制时间为准、禁止平移）。
> 上游设计：`docs/spec/upload_video_design.md`（`DESIGN-ELEV-RESCUE-UP-001`，含 `D-UP-01~15` 与 5 条开放风险）。
> 体例来源：`docs/spec/tasks.md`（编号/表格/每任务字段/附录冻结命令）与 `docs/spec/camera_tasks.md`（三档沙箱标注、闸门分组、人工清单）。
> 既有基线：`service/main.py`（313 行，`app.mount("/")` 在 305）、`service/camera.py`（`scan_sources`/`load_sources`/`_BOOTSTRAPPED`/`iter_video_chunks`）、`model/eval.py:119`（`load_vision_sequence`）、`model/fuse.py`（`FusionRunner`）、`scripts/check_camera.py`（脚本纪律范式）。
>
> 本文档**只做任务规划**：不含实现代码；本轮不改 `service/`、`scripts/`、`tests/`、`dashboard/`、`model/`、`common/`、`results/`、`data/labels/`、`requirements*`，也不改任何既有文档。

## 0. 范围、约束与阅读方式

### 0.1 范围边界（硬约束）

| 项 | 本任务规划范围 | 明确不做 |
|---|---|---|
| 新增文件 | `service/upload.py`（L0 契约/接收/存储）、`service/upload_features.py`（L1 特征后端）、`service/upload_worker.py`（L2 队列/判决）、`scripts/check_upload.py`、`tests/test_upload_*.py` | 新建 `service/upload_models.py`（不引 pydantic）、`vision/heuristic.py`（A 侧冻结区）、`scripts/upload_video.py`（P1 可选，不进主线） |
| 追加文件 | `service/main.py`（仅 import + 路由 + lifespan 插入行，**既有行删除数 = 0**）、`service/camera.py`（仅 `scan_uploads`/`invalidate_sources`/上传分支，demo 分支一行不改）、`service/config.py`（只追加 `UPLOAD_*` 常量） | 改 `common/schema.py`、`model/reason.py`、`model/fuse.py`、`dashboard/index.html`、`common/demo_data/**`、`results/**`、`data/labels/**`、`requirements*.txt` |
| 落盘 | 仅 `data/raw/uploads/`（`.gitignore` 已忽略） | 写 `results/`、`common/demo_data/`、`data/processed/` |
| 技术栈 | Python **3.9.12**（本机实测）；标准库 + 已锁依赖；`cv2` 仅函数内懒加载；`@dataclass(frozen=True)`；注解只用 `Optional/List/Dict/Any/Tuple` | `X \| Y`、运行期 `dict[str, ...]`、`python-multipart`、`UploadFile`/`File()`、新增第三方依赖、新 pydantic 模型 |
| 判决 | 复用 `FusionRunner.run(vision, [])` + `RuntimeConfig`（`dataclasses.replace` 注入 `device_id`/`location`） | 另写 dwell/空梯复位/第二套困人判定 |
| 验收工具 | 既有 `unittest` 体例 + 新建 `scripts/check_upload.py`（只调真实服务函数/纯函数，不复制公式） | `pytest`（不在 lock）、`fastapi.TestClient`（沙箱 `socketpair` 报 `WinError 10051`，见 `supplement_ac.md` 7.3） |
| 看板 | 零改动（上传页属 `REQ-UP-026` P2，本期不进主线） | 改单文件看板、引入 CDN/播放器 SDK、上传请求进 2 s 轮询 |

### 0.2 编号与追溯规则

- 主任务沿用 `upload_video.md` 6.1 与设计第 12 章：**`T-UP-001~009`**，编号与标题一字不改，双向可追溯；**不复用** `T-CAM-*`/`T-UI-*`/`T-B-*`/`T-INT-*`/`T-AC-*` 任何编号。
- 任务内子项用 `T-UP-00X-S1…Sn` 标识（仅文档内引用，不占编号段），`- [ ]` 勾选；**层级最多两级**（主任务 → 子项）。
- 每个任务必须由「`REQ-UP-NNN`」+「设计章节号」共同兜住；无追溯不立任务。
- 验收命令编号沿用 `upload_video.md` 第 8 章 `CMD-UP-01~18`；本文档新增 **`CMD-UP-19~23`**（见附录 A），全部是「既有脚本/`git` 命令的组合」，**不虚构任何脚本**。
- `scripts/check_upload.py` 由 **T-UP-001 创建骨架**、T-UP-009 收口；其子命令集合以本文档附录 A 为准，**不存在附录 A 之外的下拉子命令**。未实现的子命令必须打印「未实现」并退出码 2（同 `check_camera.py` 脚手架体例），不得静默通过。

### 0.3 里程碑与实施阶段

> 「M」为交付里程碑，不绑定日历；日期由实施者按答辩日期倒排。每条任务标注其所属里程碑与并行资格。

| 里程碑 | 目标（用户可感知） | 任务 | 可与谁并行 | 出口条件（一句话） |
|---|---|---|---|---|
| **M1** | 可接收 + 可探测：真实字节落盘、有记录与设备归属、有合法时间基准 | T-UP-001 → 002 → 003 | 无（全项目第一个开工链） | `--accept` + `--probe` + 3 个单测文件全绿 |
| **M2** | 可产出六字段：启发式后端跑出严格 `List[VisionOut]` | T-UP-004（**唯一长杆**）；T-UP-005 可选并行 | T-UP-005 可与 T-UP-006 同期插空 | `--heuristic` 全绿 + `vision.json` 条数 = `frames` |
| **M3** | 可判决上报：异步任务 + `FusionRunner` + 边沿入库 | T-UP-006 → T-UP-007（006 可在 003 后与 004 并行） | 004 与 006 不同文件，可并行；007 依赖两者 | `--task` + `--judge` + `--e2e`（12 s fixture）全绿 |
| **M4** | 可看板可见：`up_*` 进 `/camera/sources`、可播、可追溯、水印正确 | T-UP-008 | 无（依赖 007 的 `final_event_type`） | `--sources` 全绿 + demo 5 条逐字节不变 |
| **M5** | 闸门全绿：脚本收口、错误码集合、依赖与冻结面、实测回填 | T-UP-009 | 依赖 001~008 全绿 | `--strict` + 四道回归闸门 + 冻结集合相等 |

**关键路径**：`T-UP-001 → 002 → 003 → 004 → 006 → 007 → 008 → 009`。
**并行点**：`T-UP-005`（依赖 004，P1 增益，可插空）；`T-UP-004` 与 `T-UP-006` 在 003 之后可并行（不同文件），但关键路径按 004 → 006 计。
**唯一阻断点**：`T-UP-004` 未完成前，「真实视频判困人」不成立，其余任务只是管道。

### 0.4 起点快照（本轮实测，实现轮比较基准）

> 全部在项目根执行；以下为编写本文档时的真实测量（**本轮未改任何代码/文档**）。

| 项 | 实测值 | 复现命令 |
|---|---|---|
| Python | **3.9.12** | `python -V` |
| 既有单元测试 | **192 项 OK**（6.48 s） | `python -m unittest discover -s tests -t . -p "test_*.py"` |
| 服务端直通 | **11/11** | `python scripts/passthrough_test.py` |
| 前端静态闸门 | **29/29** | `python scripts/check_frontend.py --strict` |
| 画面静态闸门 | **14/14** | `python scripts/check_camera.py --strict` |
| `service/main.py` | **313 行**；13 条路由；`app.mount("/")` 在 **305** | `wc -l service/main.py`、`grep -n "@app\\." service/main.py` |
| `service/main.py` 改动 | `103 0`（camera 轮纯追加，**删除列 0**） | `git diff --numstat -- service/main.py` |
| 冻结区 | `common results data/labels` 共 **25** 条既有改动 | `git status --short common results data/labels \| wc -l` |
| 依赖文件 | `requirements.txt`/`requirements.lock.txt` 未改；`requirements-c39.txt` **既有 staged**（`A`） | `git status --short -- requirements*.txt` |
| 本机 `cv2` | **5.1.0**（lock 为 4.10.0.84） | `python -c "import cv2;print(cv2.__version__)"` |
| `python-multipart` | **未安装** | `python -c "import multipart"` → `ModuleNotFoundError` |
| `ultralytics`/`onnxruntime`/`models/` | **均不存在**（默认 L2） | `python -c "import ultralytics"`、`ls models` |
| `scripts/check_upload.py` | **不存在**（本轮由 T-UP-001 创建） | `ls scripts` |
| `data/raw/` | 空目录（`.gitignore` 已忽略） | `ls data/raw` |

> **基线使用规则**：实现开始第一件事是把冻结区写进基线文件（`data/raw` 已被忽略，不污染仓库）：
> ```bash
> git status --short common results data/labels | sort > data/raw/.upload_baseline.txt
> ```
> 结束后做**集合相等**比较，而不是要求为空（25 条既有改动属 A 侧，不是本轮引入）。

---

## 1. 任务总览表

> 「关键路径」列：🔴 = 阻断主路径；✅ = 关键但可并行；— = P1 增益/收口。

| 编号 | 标题 | 里程碑 | 依赖前置 | 交付物 | 主验收命令 | 沙箱档位 | 关键路径 |
|---|---|---|---|---|---|---|---|
| **T-UP-001** | 上传入口与流式落盘 | M1 | 无（第一个开工） | `service/upload.py`（新增）、`service/main.py`（纯追加 5 路由）、`service/config.py`（追加常量）、`scripts/check_upload.py`（骨架）、`tests/test_upload_accept.py` | `python scripts/check_upload.py --accept` | 机器 + 需端口 | 🔴 阻断 |
| **T-UP-002** | 记录与设备归属 | M1 | T-UP-001 | `service/upload.py` 追加（`UploadRecord`/`UploadTask`/幂等/侧车/items/delete）、`tests/test_upload_record.py` | `python -m unittest tests.test_upload_record -v` | 机器 | 🔴 阻断 |
| **T-UP-003** | 探测与时间基准 | M1 | T-UP-002 | `service/upload.py` 追加（`probe_video`/`parse_ts13`/`ts_of_frame`/`frame_of_ts`/`validate_time_window`）、`tests/test_upload_time.py` | `python scripts/check_upload.py --probe` | 机器 + 换环境 | 🔴 阻断 |
| **T-UP-004** | **L2 启发式特征后端** | M2 | T-UP-003 | `service/upload_features.py`（新增）、`tests/test_upload_heuristic.py` | `python scripts/check_upload.py --heuristic` | 机器 + 需人工 | 🔴 **唯一长杆** |
| **T-UP-005** | L1 关键点接线（P1） | M2（可选并行） | T-UP-004 | `service/upload_features.py` 追加（`detect_feature_mode`/`PoseFeatureBackend`/侧车）、`tests/test_upload_pose.py` | `python scripts/check_upload.py --pose` | 机器（SKIP 合法） | ✅ 可并行 |
| **T-UP-006** | 异步任务与进度 | M3 | T-UP-003 | `service/upload_worker.py`（新增）、`service/main.py`（追加 `/tasks/{id}` + lifespan 插入两行）、`tests/test_upload_task.py` | `python scripts/check_upload.py --task` | 机器 + 需端口 | 🔴 阻断 |
| **T-UP-007** | 判决与上报 | M3 | T-UP-004、T-UP-006 | `service/upload_worker.py` 追加（`run_judge`/`edge_alerts`/`decorate_reason`/`report_alert`）、`tests/test_upload_judge.py` | `python scripts/check_upload.py --judge` | 机器 + 需端口/人工 | 🔴 阻断 |
| **T-UP-008** | 画面源与隐私 | M4 | T-UP-007 | `service/camera.py`（只追加）、`service/upload.py`（回填摘要）、`tests/test_upload_sources.py` | `python scripts/check_upload.py --sources` | 机器 + 需端口/人工 | 🔴 阻断 |
| **T-UP-009** | 验收脚本与收口 | M5 | T-UP-001~008 | `scripts/check_upload.py` 全量、`tests/test_upload_*.py` 收口、本文档第 12 章实测回填 | `python scripts/check_upload.py --strict` | 机器 + 换环境 | —（收口） |

> **统计**：主线 **9** 个任务（M1：3 / M2：2（含 1 可选）/ M3：2 / M4：1 / M5：1），合计约 **9~10 人日**。
> **P0 需求 20 条全部有任务兜住**（见附录 B）；P1 5 条由 T-UP-002/003/005/006 覆盖；P2 2 条不进主线。

---

## 2. 里程碑 M1 · 可接收 + 可探测

### T-UP-001 · 上传入口与流式落盘

- **目标**：把「真实字节」接收进来——`POST /api/v1/upload/video` 按 `Content-Length` + 头部白名单**边读边写**（≤256 KB/块）落 `data/raw/uploads/<upload_id>/source.mp4`，落盘后复算 `sha256`，并完成配额（2 GiB / 20 件）与 24 h 清理。**禁止**出现整文件入内存、`python-multipart`、写冻结区。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload.py` | **新增** | `UploadError`、`ERROR_CODES`（19 条）、`UploadIntent`、`parse_upload_headers`、`UploadSink.feed/finish`、`quota_snapshot`、`cleanup_expired`、`upload_dir_of` |
| `service/main.py` | **纯追加** | 5 条 `/api/v1/upload/*` 路由（注册在 305 行 `app.mount("/")` **之前**）+ import 行；既有行零删除 |
| `service/config.py` | **只追加** | 12 个 `UPLOAD_*` 常量（`MAX_BYTES=209715200`、`QUOTA_BYTES=2147483648`、`QUOTA_ITEMS=20`、`TTL_MS=86400000`、`MIN/MAX_DURATION_MS=4000/600000`、`TASK_TIMEOUT_SEC=600`、`QUEUE_SIZE=3`、`CHUNK_SIZE=262144`、`FRAME_STEP=2`、`DWELL_SECONDS=8.0`、`REPORT=1`、`ENABLE=1`） |
| `scripts/check_upload.py` | **新增（骨架）** | argparse + `--make-fixture`、`--accept`；其余子命令打印「未实现」退出 2 |
| `tests/test_upload_accept.py` | **新增** | 头解析/411/413/415/400/魔数/半成品清理/配额 507 |

- **依赖**：无。
- **实现要点**：
  - [ ] **T-UP-001-S1｜模块边界**：`service/upload.py` 顶层只允许 `logging`/`json`/`hashlib`/`os`/`re`/`time`/`dataclasses`/`pathlib`/`typing`/`from . import config`/`from .camera import invalidate_sources`；**禁止**顶层 `fastapi`/`cv2`/`numpy`/`model.*`（保证 `python -c "import service.upload"` 退出码 0）。
  - [ ] **T-UP-001-S2｜头解析（8.3 表逐行）**：`Content-Type` 取 `split(";")[0].strip().lower()` ∈ `{video/mp4, application/octet-stream}`；`Content-Length` 必填且 `>0 ≤ max`；`X-Upload-Device-Id` 正则 `^[A-Z0-9][A-Z0-9-]{2,23}$` 且 ≠ `ELEV-DEMO-001`；`X-Upload-Start-Ts-Ms` 13 位；`X-Upload-Location` ≤8 字符、空 → `?`；`X-Upload-Door-Roi` 4 元非负且顺序正确；`X-Upload-Name` 强制消毒（去 `/`、`\`、`..`、截 64）；**`X-Upload-Rebase-Ts` 出现即 `400 UP_BAD_REQUEST`**。
  - [ ] **T-UP-001-S3｜流式落盘**：写 `data/raw/uploads/.tmp/<uuid>/source.part`；`feed()` 分块 ≤256 KB；累计字节 > `Content-Length` 或 > 200 MiB → 立即中止 + 删半成品 + `413`；实际字节 < 声明 → `400`；前 12 字节校验 `bytes[4:8] == b"ftyp"` → 否则 `415`。
  - [ ] **T-UP-001-S4｜finish 顺序**：复算 `sha256` → 比对（不一致 `500 UP_HASH_MISMATCH`）→ `upload_id` → 幂等查找 → 配额 → `os.replace` 到 `<upload_id>/source.mp4`。**幂等命中删 `.tmp` 并返回 200（不重复落盘）**。
  - [ ] **T-UP-001-S5｜路由与错误体**：路由层只做「头 → 流 → 服务函数 → 错误转换」；`UploadError.detail()` → `{"detail": {"code","message",...extra}}`；**既有 `POST /ack` 的 404 仍为字符串 `detail`，不得改**；`request.stream()` 每块用 `run_in_threadpool(sink.feed, chunk)`。
  - [ ] **T-UP-001-S6｜配额与清理**：`quota_snapshot(now_ms)` 统计未过期件（已删视频计 0 字节）；超限 `507 UP_QUOTA_EXCEEDED` 且不覆盖；`cleanup_expired` 只在启动 + 每次头解析后、带 `_CLEANUP_LOCK`，**只删 `source.mp4`**，保留 `vision.json`/`decisions.json`/`upload.json`；失败 WARN 不阻断。
  - [ ] **T-UP-001-S7｜失败回滚**：任何 4xx/5xx 分支必须删除本次 `.tmp` 或新建目录（`415/422/413/400/500` 一律无残留），`data/raw/uploads/` 目录数不变。
  - [ ] **T-UP-001-S8｜日志纪律**：单件日志 ≤60 行；只记 `upload_id/device_id/bytes/mode/alerts`；**禁止**像素、坐标数组、文件内容。
  - [ ] **T-UP-001-S9｜`scripts/check_upload.py` 骨架**：`check(name, ok, detail)` 计数 + 退出码（同 `check_frontend.py`）；`--make-fixture --seconds 4|12 --out <path>` 用 `cv2.VideoWriter` 现生成（`mp4v`），打印 `fps/frames/size`；`--accept` 断言合法 → 202、缺 `Content-Length` → 411、超限 → 413 且 `.tmp` 空、非 mp4 → 415、`Content-Length: 0` → 400、sha256 一致、幂等第二次 200 且目录数不变。
  - [ ] **T-UP-001-S10｜单测**：`tests/test_upload_accept.py` 用 `tmp_path`/`TemporaryDirectory` 注入 `base_dir`，**不写真实 `data/raw/uploads/` 与 `elevator.db`**。

- **验收命令**：

```bash
# 1) 现生成 fixture（落 data/raw/.fixtures/，已被 gitignore）
python scripts/check_upload.py --make-fixture --seconds 4 --out data/raw/.fixtures/u4.mp4
python scripts/check_upload.py --make-fixture --seconds 12 --out data/raw/.fixtures/u12.mp4
# 2) 接收 / 错误码 / 幂等 / 配额 / 半成品清理
python scripts/check_upload.py --accept
# 3) 单测
python -m unittest tests.test_upload_accept -v
# 4) 3.9 最小依赖导入
python -c "import service.upload"
# 5) 依赖零新增（上传可用但 multipart 仍缺失）
python -c "import multipart"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 头解析全部错误码、`UploadSink` 分块/复算/幂等/配额、清理只删视频、`import service.upload` | `--accept`（进程内直调 + 模拟 chunk 迭代器）、`test_upload_accept` |
| **需端口（换环境）** | 真实 `411/413/415/202` 响应语义、真实 200 MiB 上传耗时与 RSS | `python -m service.main` + `curl`（CMD-UP-18） |
| **需人工** | 无（本任务不涉及画面判读） | — |

- **完成判据（DoD）**：
  1. 合法 4 s/12 s fixture → **202**，`data/raw/uploads/<id>/source.mp4` 存在且 `sha256` 与源文件一致；`upload.json` 存在。
  2. `Content-Length` 缺失 → `411 UP_LENGTH_REQUIRED`；`0`/非整数 → `400 UP_BAD_REQUEST`；> 200 MiB → `413 UP_TOO_LARGE`；`Content-Type` 非白名单或魔数非 `ftyp` → `415 UP_UNSUPPORTED_TYPE`。
  3. 任一拒绝分支后 `data/raw/uploads/` 目录数与 `.tmp` 条目数与请求前相等（无半成品）。
  4. 同设备同文件二次上传 → **200** + 同 `upload_id`，目录数不变（幂等语义由 T-UP-002 定稿，本任务先保证不重复落盘）。
  5. 第 21 件 / 累计 >2 GiB → `507 UP_QUOTA_EXCEEDED`，前 20 件文件完整。
  6. 伪造 `expire_ms` 为过去 → 清理后 `source.mp4` 消失、`vision.json`（若存在）保留，`/upload/items` 不再列出。
  7. 分块 ≤256 KB；200 MiB 上传 `tracemalloc` 峰值增量 ≤64 MiB。
  8. `service/main.py` `git diff --numstat` **删除列 = 0**，且 5 条 upload 路由的 `grep -n` 行号 **小于 305**。
  9. `git status --short` 不含 `data/raw/`；`git status --short -- requirements*.txt` 与起点一致（仅 `A  requirements-c39.txt`）。
  10. `python -c "import multipart"` 仍失败（证明未靠新增依赖实现）。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.2「改 `service/main.py`」项 + 第 7.3 冻结区集合相等。
- **前置确认**：**PC-UP-01**（`upload_id` 组合摘要，需项目负责人）；**PC-UP-04**（`cv2` 双版本，需环境负责人）。
- **回归风险**：① 顶层 import `cv2`/`fastapi` → 3.9 最小环境 `import service.upload` 失败（REQ-UP-025 红）；② 一次性 `read()` 全文件 → 内存峰值超标；③ 用 `UploadFile`/`File()` → 缺 `python-multipart` 直接 500；④ 路由插到 `app.mount("/")` 之后 → 被静态页兜住；⑤ 改写既有 import 行 → 删除列 > 0；⑥ 拒绝分支未删 `.tmp` → 磁盘残留、配额虚高。
- **预估工作量**：一天半。

### T-UP-002 · 记录与设备归属

- **目标**：把「一次上传」变成自包含、可查询、可幂等的记录：`UploadRecord`/`UploadTask`（`frozen=True`）、`upload_id`/`source_id` 生成、`device_id` 校验与 demo 隔离、`upload.json` 原子写、幂等矩阵、`/items` 与 `DELETE` 服务函数。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload.py` | 追加 | `UploadRecord`、`UploadTask`、`UploadManifest` 读写、`build_upload_id`、`source_id_of`、`parse_task_id`、`parse_device_id`、`parse_location`、`items_payload`、`item_payload`、`delete_item`、幂等 `find_existing` |
| `tests/test_upload_record.py` | 新增 | 记录/幂等/排序/删除/字段集合 |
| `scripts/check_upload.py` | 追加 `--accept` 的幂等段与 items 段 | 200/202、目录数、排序稳定性 |

- **依赖**：T-UP-001。
- **实现要点**：
  - [ ] **T-UP-002-S1｜`upload_id`（D-UP-01）**：`upload_id = sha256(device_id + "\x00" + file_sha256).hexdigest()[:32]`；同设备同文件稳定、跨设备不同；`sha256` 字段仍存文件裸摘要（64 hex）。
  - [ ] **T-UP-002-S2｜`source_id`**：`up_` + `upload_id[:16]` = **19** 字符，过 `[a-z0-9_]{1,20}` 校验（`service/camera.py:269-273` 同口径），**禁止**用 32 hex 拼 35 字符（R-UP-09）。
  - [ ] **T-UP-002-S3｜`device_id`/`location`/`original_name`**：`device_id` 正则且 ≠ `ELEV-DEMO-001`；`location` ≤8、缺省 `?`；`original_name` 消毒后不含 `/`、`\`、`..`，≤64；空 → `upload.mp4`。
  - [ ] **T-UP-002-S4｜数据模型**：`UploadRecord` 全字段严格等于 3.1/5.2（24 项），`UploadTask` 13 项，全部 `@dataclass(frozen=True)`；序列化用 `dataclasses.asdict`，**不引 pydantic**；`UploadManifest` = 记录全字段 + `vision_file`/`decisions_file`/`reported_alert_ids`/`reported_alert_ts`/`final_event_type`。
  - [ ] **T-UP-002-S5｜侧车原子写**：`upload.json` 先写 `upload.json.tmp` 再 `os.replace`；`scan_uploads()` 只读完整文件；**不输出绝对路径**。
  - [ ] **T-UP-002-S6｜幂等矩阵（设计 4.5，6 行）**：不存在 → 202；`received/extracted/judged` → 200 + 既有 `task_id` + 真实状态；`reported` → 200 + `state=done`；`failed` → **202 重试**（复用目录、删旧派生、重置 `received_ms/expire_ms`、新 `task_id`）；已过期 → 视为不存在（先清视频再新建）；不同设备同文件 → 独立 202。
  - [ ] **T-UP-002-S7｜派生时间字段**：`duration_ms = round(frames*1000/fps)`、`expire_ms = received_ms + 86400000`、`received_ms` 13 位且**不参与 `ts`**；`task_id = <upload_id>-<received_ms>`（D-UP-10，可由侧车重建）。
  - [ ] **T-UP-002-S8｜`/items` 与 `/items/{id}`**：`items` 字段 19 项；排序 `received_ms` 倒序、同值 `upload_id` 字典序（两次请求 `json.dumps(items, sort_keys=True)` 相等）；空清单 `200 + {"items": [], "total": 0}`；过期件不出现；`quota` 4 项 + `server_time_ms` + `watermark="用户上传 · 非取证"`。
  - [ ] **T-UP-002-S9｜`DELETE`（设计 5.4）**：默认只删 `source.mp4`（`deleted` 明细逐项布尔）；`purge=1` 删整目录；未知/过期 → `404 UP_NOT_FOUND`；`OSError` → `409 UP_DELETE_FAILED`（不得静默 ok）；成功后调 `invalidate_sources()`（T-UP-008 接线，本任务先留钩子）。
  - [ ] **T-UP-002-S10｜单测**：`tests/test_upload_record.py` 覆盖「同设备稳定 / 跨设备不同 / `source_id` 长度 19 / 正则 / `duration_ms` 误差 ≤40 ms / `expire_ms` / `task_id` 往返 / 幂等 6 行 / 排序稳定 / 删除 3 态」，磁盘用例用临时目录。

- **验收命令**：

```bash
python -m unittest tests.test_upload_record -v
python scripts/check_upload.py --accept          # 含 202/200 幂等段与目录数断言
python -c "import service.upload"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 全部记录/幂等/排序/删除断言 | 单测 + `--accept` 直调服务函数 |
| **需端口（换环境）** | 真实 `DELETE` 409（Windows 文件占用）、`GET /items` 真实响应 | `curl`（CMD-UP-18 组合） |
| **需人工** | `original_name` 为真实文件名可见 | 看板/接口人工核对（P1） |

- **完成判据（DoD）**：
  1. `test_upload_record` 全绿；`upload_id`/`source_id`/`task_id` 生成规则与 D-UP-01/D-UP-10 逐字一致。
  2. `source_id` 长度 = 19 ≤20 且字符集合法；`upload_id` 32 位小写 hex。
  3. `ELEV-DEMO-001` → `400 UP_BAD_DEVICE_ID`；同一 `device_id` 两次上传 → `/devices` 只有 1 行（`upsert_device` 在 T-UP-007 接线，本任务保证 record 字段一致）。
  4. 幂等矩阵 6 行逐行可验（202/200/200/202/202/202），命中时**目录数不增**。
  5. `upload.json` 键集合 = `UploadRecord` 24 项 + 5 项摘要；`vision_file`/`decisions_file` 为相对文件名。
  6. `items` 两次请求 `items` 数组逐字节一致；空清单 200 而非 404；过期件不出现。
  7. `DELETE` 默认后 `source.mp4` 不存在、`vision.json` 仍在、`/items` 该件 `video=false`；`purge=1` 整目录消失。
  8. `upload.json` 中 **不存在** 任何平移偏移字段（`rebase`/`offset` 语义字段）。
  9. `tests/` 既有 192 项不受影响。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.3 冻结集合 + `requirements*` 零改动。
- **前置确认**：**PC-UP-01**（跨设备不同 id 是否接受；不接受则幂等语义与目录命名需返工）。
- **回归风险**：① `upload_id` 用裸 `sha256[:32]` → 跨设备撞号，REQ-UP-006 验收「不同设备不同 id」红；② `source_id` 用 32 hex → 35 字符被 `camera` 拒绝，M4 不可见；③ `dataclasses.replace` 未用于 `UploadTask` 更新 → 状态被原地改写，并发读到半态；④ 幂等命中仍 `os.replace` → 覆盖正在处理的 `source.mp4`；⑤ 删除失败返回 `{"ok": true}` → REQ-UP-024「不得静默 ok」红。
- **预估工作量**：一天。

### T-UP-003 · 探测与时间基准

- **目标**：定死「上传件的第 0 帧是什么时刻」——`start_ts_ms` 必填且只来自录制时间（HTTP 头部 / 侧车），并给出唯一换算公式 `ts(frame) = start_ts_ms + round(frame*1000/fps)` 与窗口校验 `[start_ts_ms-1000, start_ts_ms+duration_ms+1000]`。**禁止**用接收/处理时刻顶替，**禁止**任何平移。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload.py` | 追加 | `parse_ts13`、`probe_video`（直接读 `CAP_PROP_*`）、`ts_of_frame`、`frame_of_ts`、`duration_ms_of`、`validate_time_window`、`ProbeInfo` |
| `tests/test_upload_time.py` | 新增 | `parse_ts13` 三态、正反算、窗口、历史不平移、`Rebase` 拒绝 |
| `scripts/check_upload.py` | 追加 `--probe` | 来源优先级、10 位拒绝、时长/帧率边界、窗口阻断 |

- **依赖**：T-UP-002。
- **实现要点**：
  - [ ] **T-UP-003-S1｜`parse_ts13`**：纯数字、无小数点、`10^12 ≤ x < 10^13`；`1763300180`（10 位）、`1763300180000.0`、负数、空 → `None`。与 `service/camera.py` 的 13 位口径一致但**不复用其私有函数**。
  - [ ] **T-UP-003-S2｜来源优先级（D-UP-14）**：HTTP 路径**只认** `X-Upload-Start-Ts-Ms`；缺失/非法 → `400 UP_BAD_TIMESTAMP`，删半成品、**不落盘、不建任务、不建目录**；`sidecar` 来源只用于目录投放/命令行/重启恢复。
  - [ ] **T-UP-003-S3｜`start_ts_source`**：只能是 `client`/`sidecar`；写入 `upload.json` 与 `POST` 响应；日志记 `start_ts_source=<...>`。
  - [ ] **T-UP-003-S4｜`probe_video`（R3-UP-12）**：直接读 `CAP_PROP_FPS/FRAME_COUNT/WIDTH/HEIGHT`；**不调用 `vision.sources.video_info`**（其对非法 fps 回退 25.0、`base_ts` 锚当前整秒）；`FRAME_COUNT==0` 时逐帧计数；`duration_ms = round(frames*1000/fps)` 且与 `frames/fps` 误差 ≤40 ms。
  - [ ] **T-UP-003-S5｜边界拒绝（4.3 表）**：`cv2` 缺失/打不开/`frames==0`/`fps∉[1,60]`/`width>1920`/`height>1080` → `422 UP_NOT_DECODABLE`；`duration<4 s` → `422 UP_TOO_SHORT`；`>600 s` → `422 UP_TOO_LONG`；**全部删目录**。
  - [ ] **T-UP-003-S6｜门区越界二次校验**：头解析只验「4 元 + 顺序 + 非负」；`probe_video` 拿到宽高后验 `x2≤width`、`y2≤height` → 否则 `400 UP_BAD_ROI` 并删目录。
  - [ ] **T-UP-003-S7｜唯一时间公式（7.1）**：`ts_of_frame(start_ts_ms, fps, frame) = start_ts_ms + round(frame*1000.0/fps)`；`frame_of_ts = round((ts-start_ts_ms)*fps/1000.0)`（**浮点 fps**，兼容 29.97/59.94，R3-UP-17）；`ts` 一律 13 位。
  - [ ] **T-UP-003-S8｜`validate_time_window`**：`d.ts < start_ts_ms - 1000` 或 `> start_ts_ms + duration_ms + 1000` → 抛 `UploadError(500, "UP_TIME_BASE_INVALID", "时间基准异常，已阻断上报")`；历史 `start_ts_ms` 属**合法输入**，不得拒绝、不得平移。
  - [ ] **T-UP-003-S9｜`X-Upload-Rebase-Ts` 反向断言**：请求出现该头 → `400 UP_BAD_REQUEST`；`upload.json` 字段集合中无任何 offset/rebase 字段；代码搜索 `UPLOAD_REBASE` 零命中。
  - [ ] **T-UP-003-S10｜单测**：首帧 `ts == start_ts_ms`；末帧 `ts` 与 `duration_ms` 差 ≤1 帧；25.0/29.97/59.94 三档正反算互逆（0 帧误差）；窗口上下界 ±1001 ms 各一例。

- **验收命令**：

```bash
python scripts/check_upload.py --probe
python -m unittest tests.test_upload_time -v
python -c "import service.upload"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | `parse_ts13`、来源优先级、`duration/fps` 边界、正反算、窗口阻断、历史不平移 | `--probe` + 单测（含 4/12 s 现生成 fixture） |
| **需端口（换环境）** | 真实 400/422 响应、真实 MP4（H.264）探测 | `curl`（CMD-UP-18） |
| **需人工** | 跨时区/时钟异常的录制时间是否合理 | 人工核对 `start_ts_ms` 与手机拍摄时间 |

- **完成判据（DoD）**：
  1. 带头部 13 位时间 → 响应 `start_ts_ms` 与头部一致、`start_ts_source=="client"`；`upload.json` 同值。
  2. 不带头部且无侧车 → `400 UP_BAD_TIMESTAMP`，`data/raw/uploads/` **不新增目录**；10 位/小数/负数同样 400。
  3. `vision.json[0].ts == start_ts_ms`（T-UP-004 落地后由 `--heuristic` 复核）。
  4. 25/29.97/59.94 fps 下 `frame_of_ts(ts_of_frame(f)) == f`（全帧遍历，0 误差）。
  5. `probe_video` 输出 `fps/frames/width/height/duration_ms` 与 `cv2` 原始属性一致；**无** `video_info` 的 25.0 回退痕迹。
  6. 3 s → `UP_TOO_SHORT`；601 s → `UP_TOO_LONG`；fps=0/61、frames=0、1921×1080、1280×1081 → `UP_NOT_DECODABLE`；以上均无残留目录。
  7. 门区 `x2>width` → `400 UP_BAD_ROI` 且无残留。
  8. `validate_time_window` 对窗口外一条 → 抛 `UP_TIME_BASE_INVALID`；历史 `start_ts_ms`（如 2026-01-01）不抛、不修改 `ts`。
  9. `X-Upload-Rebase-Ts: 1` → 400；全仓 `UPLOAD_REBASE|X-Upload-Rebase` 只在拒绝分支出现。
  10. `test_upload_time` 全绿，既有 192 项不受影响。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.3 冻结集合。
- **前置确认**：**PC-UP-01**（组合摘要影响幂等，与本任务的历史时间重试语义联动）。
- **回归风险**：① 复用 `video_info` → 非法 fps 被 25.0 放行、`base_ts` 锚当前秒（REQ-UP-012 直接红）；② `ts` 用整数 `frame_ms` 步长反算 → 29.97 fps 10 分钟漂移数秒；③ 历史时间被「修正」到今日 → `DEC-UP-01/02` 双红；④ 窗口校验写成 `>=`/`>` 边界错 → 合法末帧被判错；⑤ 探测失败未删目录 → 残留空目录占用配额。
- **预估工作量**：一天。

---

## 3. 里程碑 M2 · 可产出六字段

### T-UP-004 · L2 启发式特征后端（唯一长杆）

- **目标**：在「有 `cv2`、无任何姿态模型与权重」的现状下，用**可解释的画面统计量**产出与 demo 同构的 `List[VisionOut]` 六字段：`person_count` 由前景连通域估、`door_approach` 由门区前景占比 + 门区带内人数估、`crowded` 由全画面前景占比估；`fall_prob` **唯一赋值点恒为 0.0**、`keypoints_ok` 恒 `False`。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload_features.py` | **新增** | `FeatureBackend`（`Protocol`）、`HeuristicBackend`、`FeatureResult`、`resolve_door_roi`、`estimate_person_count`、`estimate_door_approach`、`estimate_crowded`、`write_vision_json`、`validate_vision_shape`、`validate_heuristic_vision` |
| `tests/test_upload_heuristic.py` | 新增 | 合成帧/全黑/跳帧复制/条数/六字段/fall_prob/ROI |
| `scripts/check_upload.py` | 追加 `--heuristic` | 六字段 6/6、`fall_prob` 全 0、全黑不告警、条数=frames |

> **交付物位置说明**：`upload_video.md` 6.1 建议 `vision/heuristic.py`，按设计 2.4 第 1 条改落 `service/upload_features.py`（A 侧 `vision/` 为冻结区，且上传特征与 `upload.json` 的 `feature_mode`/`roi` 强耦合）。本任务**不改** `vision/` 任何文件。

- **依赖**：T-UP-003。
- **实现要点**：
  - [ ] **T-UP-004-S1｜协议与结果**：`FeatureBackend` 仅 `mode: str` + `extract(video_path, start_ts_ms, door_roi, step, progress, deadline) -> FeatureResult`；`FeatureResult(frozen=True)` 含 `rows: List[VisionOut]`、`roi_source`、`low_conf_count`、`mode`；`progress(frames_done, frames_total)` 由 worker 注入。
  - [ ] **T-UP-004-S2｜cv2 稳定子集（R-UP-06）**：只用 `VideoCapture`/`CAP_PROP_*`/`read`/`resize`/`cvtColor`/`absdiff`/`threshold`/`medianBlur`/`findContours`/`createBackgroundSubtractorMOG2`；`MOG2` 构造或 `apply` 异常 → **回退 `absdiff + threshold(25,255)`**；`findContours` 用 `contours, _ =` 兼容 4.x/5.x；**禁止** 5.x 独有 API。
  - [ ] **T-UP-004-S3｜门区三级（D-UP-10 语义）**：`client` → manifest `door_roi`；`sidecar` → 侧车 `door_roi`；`default` → `x1=0,x2=width,y1=round(height*2/3),y2=height`；返回 `roi_source`，供 T-UP-007 写成 `roi=client|sidecar|default`。
  - [ ] **T-UP-004-S4｜人数估计**：连通域过滤 `area < MIN_BLOB_AREA`；`fg_ratio < MIN_FG_RATIO` 或有效连通域 = 0 → `person_count=0` + `low_conf=True`（宁可漏报）；否则 `min(8, 有效数)`。
  - [ ] **T-UP-004-S5｜门区接近度**：`door_fg_ratio = countNonZero(door_mask)/door_area`；`person_in_door_ratio = 底边落门区带的连通域数/max(1, person_count)`；`door_approach = clamp(0.6*door_fg_ratio/DOOR_FG_FULL + 0.4*person_in_door_ratio, 0, 1)`；**不得**因无门区恒 0。
  - [ ] **T-UP-004-S6｜拥挤度**：`crowded = clamp(fg_ratio / CROWDED_FG_FULL, 0, 1)`；参数为模块常量 + 环境变量覆盖（`MIN_FG_RATIO=0.005`、`MIN_BLOB_AREA=0.002*w*h`、`DOOR_FG_FULL=0.35`、`CROWDED_FG_FULL=0.45`、`UPLOAD_FRAME_STEP=2`）。
  - [ ] **T-UP-004-S7｜L2 红线（三层）**：产出层 `to_vision_out` 是 `fall_prob=0.0`/`keypoints_ok=False` 的**唯一赋值点**；校验层 `validate_heuristic_vision` 断言全帧 `fall_prob==0.0` 且 `keypoints_ok is False`，违反 → `UP_INTERNAL`；判决层由既有 `model/signals.py` 二次归零兜底。
  - [ ] **T-UP-004-S8｜逐帧输出（D-UP-02）**：`step` 只减少计算量，**中间帧复制上一帧统计量**；`vision.json` 条数恒 = `frames`；`ts` 严格递增、首条 = `start_ts_ms`；保证 `KptStore` 按帧号索引不错位。
  - [ ] **T-UP-004-S9｜写出与校验**：`write_vision_json` 写 `.tmp` 再 `os.replace`，UTF-8 无 BOM，字段集合恰为 `VISION_FIELDS` 六项；写完**直接调用** `model.eval.load_vision_sequence(Path)`（不复制规则）+ `validate_vision_shape`；每 256 帧 flush `vision.part.jsonl`。
  - [ ] **T-UP-004-S10｜性能与截止**：每帧检查 `time.monotonic() > deadline` → 抛超时；10 min @25 fps、step=2 目标 < 75 s（为 600 s 超时留余量）。
  - [ ] **T-UP-004-S11｜单测**：全黑 → 全 0 + low_conf；已知 mask → 人数/门区/拥挤单调；`fall_prob==0`/`keypoints_ok is False` 全帧；跳帧复制；`len(rows)==frames`；`load_vision_sequence` 可读。

- **验收命令**：

```bash
python scripts/check_upload.py --heuristic
python -m unittest tests.test_upload_heuristic -v
python -c "import service.upload_features"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 六字段集合/类型/条数、`ts` 递增、`fall_prob` 全 0、全黑不告警、ROI 三级、`load_vision_sequence` 复用校验 | `--heuristic`（4/12 s 现生成 fixture + 全黑合成帧） |
| **需端口（换环境）** | 真实 10 min 视频耗时与内存 | 起服务后真实上传计时（MAN-UP-03） |
| **需人工** | 真实拍摄视频的困人判读与人数稳定性 | **MAN-UP-01/02（唯一不可脚本替代）** |

- **完成判据（DoD）**：
  1. `vision.json` 存在，条数 = `frames`（逐帧），每条键集合恰 6 个、类型正确。
  2. `rows[0].ts == start_ts_ms`，`ts` 严格递增，全部 13 位。
  3. 全帧 `fall_prob == 0.0` 且 `keypoints_ok is False`（`--heuristic` 与单测双断言）。
  4. 代码中 `fall_prob` 赋值点只有一处且为 `0.0`（`grep -n "fall_prob" service/upload_features.py` 审查）。
  5. 全黑视频 → 全部 `person_count == 0`，无 `trapped=true` 判决（与 T-UP-007 联验）。
  6. 不带头部门区时 `door_roi is None`、`roi_source=="default"`；「有人贴近画面下部」样本 `door_approach > 0.6`。
  7. `model.eval.load_vision_sequence(Path(vision.json))` 直接通过（5 类非法输入仍被拒）。
  8. `service/upload_features.py` 顶层无 `fastapi`/`model.fuse` import；`cv2` 只在函数体内 import。
  9. `vision/`、`common/demo_data/`、`results/` 零改动（`git status --short` 集合不变）。
  10. `--heuristic` 全绿、`test_upload_heuristic` 全绿。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.3 冻结集合 + `python -c "import service.upload_features"`。
- **前置确认**：**PC-UP-02**（`reason` 三键的追加位置，本任务只产出 `roi_source`/`low_conf` 元数据，写入在 T-UP-007）；**PC-UP-04**（`cv2` 双版本）；**PC-UP-05**（启发式误报阈值与话术）。
- **回归风险**：① 用帧差/方差伪造 `fall_prob` → REQ-UP-009 红线（三层断言会拦，但必须在产出层就写死 0）；② 跳帧输出 `ceil(frames/step)` → `KptStore` 帧索引错位（R3-UP-13）；③ `MOG2` 在 5.x 签名差异未兜底 → 换机崩溃；④ 无门区时 `door_approach` 恒 0 → `pry` 永远判不出；⑤ 低置信仍给 `person_count≥1` → 空梯复位失效、误报不断；⑥ 把整段 `rows` 常驻内存的同时又重读一遍 JSON → 10 min 视频内存翻倍。
- **预估工作量**：两天半（含真视频调参）。

### T-UP-005 · L1 关键点接线（P1，可选并行）

- **目标**：让「有姿态模型」的环境自动升级为 `feature_mode=pose`，并允许合法 `kpts.jsonl` 侧车把上传件标成真关键点；**依赖缺失/权重不可加载一律安静降级 L2**，不得崩服务、不得假装 `pose`。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload_features.py` | 追加 | `detect_feature_mode`、`PoseFeatureBackend`、`load_kpts_sidecar`、`FeatureMode` 探测缓存 |
| `tests/test_upload_pose.py` | 新增 | 空权重/依赖缺失/侧车 16 点/非单调/合法侧车 |
| `scripts/check_upload.py` | 追加 `--pose` | 依赖缺失 SKIP（不失败）、空权重仍 heuristic、合法侧车 pose |

- **依赖**：T-UP-004（可插空；不阻塞 M2 出口）。
- **实现要点**：
  - [ ] **T-UP-005-S1｜档位探测**：`importlib.util.find_spec("ultralytics")` + `models/yolov8n-pose.pt` 可被 `YOLO(path)` 构造 → `pose`；`find_spec("onnxruntime")` + `models/yolov8n-pose.onnx` 可被 `InferenceSession` 构造 → `pose`；仅 `cv2` → `heuristic`；`cv2` 也无 → 拒绝上传（`422`）。
  - [ ] **T-UP-005-S2｜空权重不得升档**：构造异常一律捕获并降级 L2，**不抛到进程**（REQ-UP-008 验收）。
  - [ ] **T-UP-005-S3｜L1 复用**：`vision.sources.iter_frames` + `vision.rules.VisionEngine.process`，`cfg.runtime.base_ts = start_ts_ms`、`frame_skip=step`；**不另写关键点/倒地规则**，不改 `vision/` 任何文件。
  - [ ] **T-UP-005-S4｜侧车覆盖**：`source_kpts.jsonl`（兼容 `kpts.jsonl`）每行 `frame/ts/persons[17×[x,y,conf]]`、`ts` 单调；合法 → `pose` + `keypoints_ok=true`；16 点/非单调/字段缺失 → `422 UP_NOT_DECODABLE`，**禁止**静默回落却仍称 `pose`。
  - [ ] **T-UP-005-S5｜单件降级**：L1 运行期异常只降该上传件为 `heuristic`（`degraded=true`），不崩服务、不影响其他件。
  - [ ] **T-UP-005-S6｜`--pose` 的 SKIP 语义**：本机无依赖 → 打印 `SKIP` 且退出码 0（不得判失败），同时断言 `feature_mode=="heuristic"`；有依赖环境才跑真档位断言。

- **验收命令**：

```bash
python scripts/check_upload.py --pose
python -m unittest tests.test_upload_pose -v
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 依赖缺失 → SKIP/heuristic、空权重不升档、侧车合法/非法分支 | `--pose`（`sys.modules` 屏蔽 + 临时空权重文件） |
| **需端口（换环境）** | 有依赖环境下真 L1 端到端 | 换环境装包后重跑 `--pose` |
| **需人工** | 看板显示骨架（依赖 M4 的 `keypoints` 能力位） | 人工（P1，不阻断） |

- **完成判据（DoD）**：
  1. 本机（无 `ultralytics`/`onnxruntime`/`models/`）`--pose` SKIP、退出码 0，且上传件 `feature_mode=="heuristic"`。
  2. 伪造空 `models/yolov8n-pose.pt` → 档位**仍** `heuristic`（构造异常被捕获）。
  3. 合法侧车 → `feature_mode=="pose"`、`keypoints_ok==true`；16 点/非单调 → 422；无侧车 → `heuristic`。
  4. `vision/` 零改动；`service/upload_features.py` 顶层无 `numpy`/`vision.*` import。
  5. `--heuristic`（T-UP-004）在新增探测逻辑后仍全绿（不得因档位探测误改 L2 路径）。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.3 冻结集合。
- **前置确认**：无新增；沿用 **PC-UP-04**（如换 lock 版本会改变 L1 依赖可用性）。
- **回归风险**：① 空权重被判 `pose` → REQ-UP-008 验收红；② 侧车非法时静默回落但仍报 `pose` → 六字段语义失真；③ 顶层 import `ultralytics` → 无包环境 import 失败、连带 L2 不可用。
- **预估工作量**：一天（P1，可延后）。

---

## 4. 里程碑 M3 · 可判决上报

### T-UP-006 · 异步任务与进度

- **目标**：把「上传即返回 202」的后台处理做成可查询、可恢复、可超时的单并发队列：并发 1、排队 3（总容量 4）、第 5 个 `503 UP_BUSY`；`GET /tasks/{id}` 只读无副作用；重启把未完成件标 `failed`。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload_worker.py` | **新增** | `UploadTaskManager`、`enqueue`、`_worker_loop`、`run_task`、`recover_on_startup`、`task_payload`、`progress_of`、`Accepted` |
| `service/main.py` | **纯追加/插入** | `GET /api/v1/upload/tasks/{task_id}`；`lifespan` 内 `yield` 前插入 `recover_on_startup()` + `start(db, cache)` 两行、`yield` 后插入 `stop()`（**不改既有行**） |
| `tests/test_upload_task.py` | 新增 | 容量/进度/超时/恢复/无副作用 |
| `scripts/check_upload.py` | 追加 `--task` | 4 活动 / 第 5 个 503、progress 单调、超时、恢复 |

- **依赖**：T-UP-003（设计允许 003 后与 T-UP-004 并行；不同文件）。
- **实现要点**：
  - [ ] **T-UP-006-S1｜队列载体**：`queue.Queue(maxsize=3)` + `threading.Thread(daemon=True)`；`enqueue` 用 `put_nowait`，`queue.Full` → `503` 且**先回滚新建目录**；活动任务计数（运行 1 + 排队 3 = 4）用于 D-UP-07 的 5 个断言。
  - [ ] **T-UP-006-S2｜依赖注入**：`start(db, cache, base_dir)` 由 `lifespan` 在 `_load_db/_load_cache` 之后调用；worker **不 import** `service.main`（避免 `fastapi` 耦合，L2 层规则）。
  - [ ] **T-UP-006-S3｜任务字典与更新**：`Dict[str, UploadTask]` + `threading.Lock`；一律 `dataclasses.replace` 生成新实例；`updated_ms` 每 ≥1 s 更新，`frames_done` 每 64 帧更新。
  - [ ] **T-UP-006-S4｜`task_id` 可重建（D-UP-10）**：`<upload_id>-<received_ms>`；`parse_task_id` 往返；未知/过期 → `404 UP_TASK_NOT_FOUND`。
  - [ ] **T-UP-006-S5｜阶段与进度**：`received(0) → probe(5) → extract(10→90) → judge(92) → report(97) → done(100)`；`extract_progress = 10 + round(80*frames_done/max(1,frames_total))`；`eta_sec` 按 observed_fps；**progress 只增不减**。
  - [ ] **T-UP-006-S6｜超时（600 s）**：`time.monotonic()` 起点为 worker 取出任务；每帧/每 judge 批次/每次上报前检查；超时 → `vision.part.jsonl → vision.json`（合法数组）、`state=failed`、`stage=extract`、`error={code:"UP_TIMEOUT"}`、**不生成 decisions、不写告警**。
  - [ ] **T-UP-006-S7｜重启恢复（设计 4.4 五行）**：`reported→done(100)`；`failed→failed`；`received/extracted/judged→failed(UP_INTERNAL)` 并带「上次处理被中断，请重新上传」；`frames_done` 由 `vision.json` 条数推导。
  - [ ] **T-UP-006-S8｜查询无副作用**：`task_payload` 只读内存 + `upload.json`；连续两次查询不改变任何任务字段、不触发重跑（用 `started_ms`/`progress`/`updated_ms` 快照比对）。
  - [ ] **T-UP-006-S9｜`lifespan` 零删除**：只插入新行，不重排/不改既有行；`git diff` 中 `service/main.py` 无 `-` 行；`app.mount("/")` 仍在最后。
  - [ ] **T-UP-006-S10｜单测**：容量 4/第 5 个 503、进度单调、stage 顺序、超时、重启恢复、无副作用；DB 用临时 SQLite。

- **验收命令**：

```bash
python scripts/check_upload.py --task
python -m unittest tests.test_upload_task -v
python -c "import service.upload_worker"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 容量/503、进度单调、stage、超时、恢复表、无副作用 | `--task`（进程内 `enqueue` + 直调 `task_payload`） |
| **需端口（换环境）** | 真实 202→done 轮询、真实重启 | `python -m service.main` + `curl`（CMD-UP-18） |
| **需人工** | 进度条体验（P2 上传页才涉及） | 人工（本期不阻断） |

- **完成判据（DoD）**：
  1. 同时提交 4 个 → 4 个接受；第 5 个 → `503 UP_BUSY`，且该次未留下目录。
  2. 任意时刻运行任务数 ≤1（计数器断言）。
  3. `progress` 单调不减；`stage` 顺序符合 4.2；`done` 时 `progress==100`。
  4. `GET /tasks/{id}` 字段集合 = 12 项；`failed` 时 `error.code` 命中 4.3 表、`progress` 停在失败点；已知任务查询无副作用。
  5. 强制超时 → `UP_TIMEOUT`，`vision.json` 合法保留、`decisions.json` 不存在、`/alerts` 无新增。
  6. 重启后 `received/extracted/judged` 全部 `failed(UP_INTERNAL)`，`reported` 为 `done`；`/upload/items` 可查。
  7. `service/main.py` 删除列 = 0（工作树与暂存区各验一次）；新路由 `grep -n` 行号 < 305。
  8. `test_upload_task` 全绿；既有 192 项不受影响。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.2「改 `service/main.py`」项 + 第 7.3 冻结集合。
- **前置确认**：无新增（容量口径沿用 **D-UP-07**，如需改为总容量 3 由项目负责人确认）。
- **回归风险**：① 在 `lifespan` 里改写既有行 → 删除列 > 0，Gate 直接红；② `enqueue` 队列满未回滚目录 → 残留目录占用配额；③ `task_payload` 读时触发 `run_task` → 查询有副作用，REQ-UP-016 红；④ 进度用浮点回写导致回退（如超时后置 0）→ REQ-UP-016「不回退」红；⑤ worker 线程 import `service.main` → 循环依赖 + 沙箱起不来。
- **预估工作量**：一天半。

### T-UP-007 · 判决与上报

- **目标**：用**既有** `FusionRunner.run(vision, [])` 完成「六字段 → 九字段」，按 `trapped` **边沿**上报一次，落 `decisions.json` 并回填 `upload.json`；时基越界一律阻断入库。**不另写状态机、不走 `/edge/report` 的 30 s 去重**。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/upload_worker.py` | 追加 | `run_judge`、`edge_alerts`、`decorate_reason`、`report_alert`、`write_decisions`、`build_runtime_config` |
| `service/main.py` | 视需要追加 `--e2e` 所需读接口（不新增写接口） | — |
| `tests/test_upload_judge.py` | 新增 | `urgency≤2`/边沿 1·2 条/reason 三键/窗口阻断/今日口径 |
| `scripts/check_upload.py` | 追加 `--judge`、`--e2e` | 判决 + 12 s 全链路 |

- **依赖**：T-UP-004、T-UP-006。
- **实现要点**：
  - [ ] **T-UP-007-S1｜判决入口（C-UP-07）**：只调 `FusionRunner.run(vision, [])`；`RuntimeConfig` 用 `dataclasses.replace(PRESET_DEMO, device_id=..., location=...)` 注入；`UPLOAD_DWELL_SECONDS` 覆盖 dwell（默认 8.0，D-UP-08）；**禁止**读任何 `_imu.npy`。
  - [ ] **T-UP-007-S2｜九字段校验**：`decisions.json` 每条键集合严格 = `Decision` 九字段；`urgency ≤ 2`；`ts` 等于驱动帧的 `VisionOut.ts`（不做二次平移）；困人样本 `trapped=true`、`urgency==2`、`event_type∈{trapped,pry}`、`confidence>0`。
  - [ ] **T-UP-007-S3｜时基窗口（7.4）**：写 `decisions.json` **之前**调 `validate_time_window`；越界 → 任务 `failed` + `UP_TIME_BASE_INVALID` + **不写告警表**；历史时间合法、原样入库、不平移。
  - [ ] **T-UP-007-S4｜边沿算法（9.2）**：`prev=False`；`trapped && !prev` → 上报并 `edge_seq += 1`；`!trapped` 重置 `prev`；`urgency==0` 只刷 `cache.set_state`（心跳）；`edge_seq` 重试时从 `len(reported_alert_ids)` 继续。
  - [ ] **T-UP-007-S5｜绕开 30 s 去重（D-UP-04）**：**不调用** `service.main.report()`；直接 `db.insert_alert(payload)` + `db.upsert_device(device_id, received_ms)` + `cache.set_state(...)`；幂等键 `upload:{upload_id}:{edge_seq}`（`cache.setnx`，TTL 24 h）。
  - [ ] **T-UP-007-S6｜reason 追加（D-UP-03）**：仅 `trapped=true` 帧追加 `feature_mode=heuristic|pose`、`roi=client|sidecar|default`、低置信时 `count_low_conf=true`；**不得**回灌 `ReasonBuilder._self_check`；`model/reason.py` 零改动。
  - [ ] **T-UP-007-S7｜产物与回填**：`write_decisions`（原子写、九字段、UTF-8 无 BOM）；`upload.json` 回填 `reported_alert_ids`/`reported_alert_ts`/`final_event_type`（D-UP-06）；`state=reported`。
  - [ ] **T-UP-007-S8｜回滚开关**：`UPLOAD_REPORT=0` → 只写 `vision.json`/`decisions.json`，不写 DB、不回填 `reported_alert_ids`。
  - [ ] **T-UP-007-S9｜统计口径**：录制时间今天 → `stats.today_alerts +1`；历史 → `+0` 且告警仍入库；`upsert_device` 用 `received_ms`（D-UP-13）。
  - [ ] **T-UP-007-S10｜`--e2e`（12 s fixture）**：`--make-fixture --seconds 12` → 上传 → 等到 `done` → 断言 `vision.json`/`decisions.json` 存在、`/alerts` 含该 `device_id` 且 `event_type=="trapped"`/`urgency==2`、`stats.today_alerts` 增量 = 1。

- **验收命令**：

```bash
python scripts/check_upload.py --judge
python scripts/check_upload.py --e2e          # 内置 12 s fixture，无需外部素材
python -m unittest tests.test_upload_judge -v
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | `urgency≤2`、边沿 1/2 条、九字段、reason 三键、窗口阻断、今日/历史口径 | `--judge` + 独立 SQLite + `--e2e`（12 s fixture，`start_ts_ms=now_ms()`） |
| **需端口（换环境）** | 真实 202→done→`/alerts` 可见、30 s 去重不被上传件占用 | `curl`（CMD-UP-18） |
| **需人工** | 真实拍摄视频判出困人、看板告警卡内容 | **MAN-UP-01**、MAN-UP-04 |

- **完成判据（DoD）**：
  1. 12 s fixture → 全链路 `done`，`/alerts` 新增 1 条该设备 `trapped=true`/`urgency==2`；`stats.today_alerts` 增量 1。
  2. 10 min 连续滞留（`decisions.json` 中 `trapped=true` >100 帧）→ 新增告警数 == 1。
  3. 「困人→脱困→再困人」→ 新增 2 条；上报次数 ≤ 边沿数 + 1。
  4. 全部判决 `urgency ≤ 2`；`event_type` 与 `confidence` 合法；`ts` 逐条落在窗口内。
  5. 注入窗口外 `ts` → 任务 `failed(UP_TIME_BASE_INVALID)`、`/alerts` 无新增、`source.mp4`/`vision.json` 保留。
  6. `feature_mode=heuristic` 的上传件首条告警 `reason` 含三键；`model/reason.py` 零改动。
  7. 代码无 `_imu.npy` 引用；无 `service.main.report` 调用；`grep -c "StateMachine"` 只出现在 import。
  8. `UPLOAD_REPORT=0` → 无新增告警、`reported_alert_ids=[]`。
  9. `/alerts`、`/stats`、`/devices` 字段集合不变（只多数据行）。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.2「改 `service/main.py`」项 + 第 7.3 冻结集合 + `passthrough_test.py` 的 `ack` 404 字符串 `detail` 断言。
- **前置确认**：**PC-UP-02**（`reason` 白名单：只在上传层追加，不回灌 B）；**PC-UP-05**（误报策略：低置信即 0 人，宁可漏报）。
- **回归风险**：① 调用 `report()` → 两轮困人在同秒被 30 s 去重吞掉（REQ-UP-015 红）；② `reason` 回灌 `_self_check` → 断言异常（R3-UP-07）；③ 边沿用 `ts` 去重而非状态机 → 连续滞留逐帧刷屏；④ 窗口校验放在入库之后 → 越界判决已进 `/alerts`（REQ-UP-013 红）；⑤ `upsert_device` 用 `Decision.ts` → 历史视频设备立即「离线」（D-UP-13）；⑥ 用 4 s fixture 跑 e2e → 永远等不到 8 s dwell，误判链路不通（D-UP-08）。
- **预估工作量**：一天半。

---

## 5. 里程碑 M4 · 可看板可见

### T-UP-008 · 画面源与隐私

- **目标**：让上传件成为 `/api/v1/camera/sources` 里的**一类源**（`up_<16 hex>`、`anchor_ts_ms=start_ts_ms`、`video=true`、`vision=true`、`keypoints` 由档位决定），复用既有视频分发与关键点窗口；**demo 5 条的字段值与排序逐字节不变**；上传源水印为 `用户上传 · 非取证`。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `service/camera.py` | **只追加** | `scan_uploads(base_dir)`、`invalidate_sources()`、`_UPLOAD_CACHE` + `Lock`、`upload_source_from_manifest`、`load_sources` 合并、`alignment` 上传分支（`demo=False` → `aligned=true/offset=0`）、上传源 item 级 `watermark` |
| `service/upload.py` | 追加 | `final_event_type` 摘要读写、`delete_item` 调 `invalidate_sources()` |
| `tests/test_upload_sources.py` | 新增 | 合并/排序/demo 不变/watermark/kpts 空/删除消失 |
| `scripts/check_upload.py` | 追加 `--sources` | `up_*` 可查、demo 5 条不变、视频 200、删除后消失 |

- **依赖**：T-UP-007。
- **实现要点**：
  - [ ] **T-UP-008-S1｜`scan_uploads`**：扫 `data/raw/uploads/*/upload.json`；**只登记 `source.mp4` 存在**、未过期、`duration_ms` 与 `frames/fps` 误差 ≤40 ms 的件（D-UP-12）；非法侧车跳过 + `WARN upload_source_skipped`，不整服务崩。
  - [ ] **T-UP-008-S2｜合并与排序**：`load_sources()` = demo 源 + 上传源；按 `source_id` 字典序（`demo_*` 在前、`up_*` 在后）；**`scan_sources()` 与 demo 分支一行不改**（`service/camera.py:382-398`）。
  - [ ] **T-UP-008-S3｜上传源字段（8.7）**：14 项 + `demo=false`；`label` ≤24（`上传 · <location>`，`?` → `上传 · 未知楼层`，未完成加 `· 处理中`/`· 处理失败`）；`event_type=final_event_type`（未完成 `normal`）；`transport=["video"]` + 条件追加 `keypoints`/`frames`。
  - [ ] **T-UP-008-S4｜水印（D-UP-11）**：顶层 `watermark` 仍为 `"演示数据 · 非取证"`；**仅上传源 item** 额外输出 `"watermark": "用户上传 · 非取证"`；demo 5 条 item **不输出**该字段（逐字节不变）。
  - [ ] **T-UP-008-S5｜视频分发零改动复用**：`video_payload`/`iter_video_chunks`（`service/camera.py:682-705`）原样服务上传源；`/camera/video?source_id=up_*` → 200 + `Content-Length` = 文件大小；Range/206/416 行为由既有代码保证。
  - [ ] **T-UP-008-S6｜关键点降级**：`keypoints=false` → `/camera/kpts` 返回 `persons=[]` 且 **不 500**；`vision` 六字段按帧直通（依赖 D-UP-02 逐帧输出）。
  - [ ] **T-UP-008-S7｜对齐分支（D-UP-05/R3-UP-17）**：上传源（`demo=False`）无会话时 `aligned=true, offset_ms=0, drift_ms=0`；有会话仍按会话；`frame(alert)` 用**浮点 fps** 反算 `round((ts-anchor)*fps/1000.0)`，`KptFrame.ts` 用 `round(anchor+frame*1000.0/fps)`；**demo 分支不变**。
  - [ ] **T-UP-008-S8｜缓存失效（R3-UP-06）**：`_UPLOAD_CACHE` + `threading.Lock`；`invalidate_sources()` 清上传缓存（不动 `_BOOTSTRAPPED`）；T-UP-007 完成与 `DELETE` 成功后调用；并发读不得读到半截 JSON（依赖原子写）。
  - [ ] **T-UP-008-S9｜删除/过期可见性**：`source.mp4` 删除或过期后 `sources` 不再含该 `source_id`；`/upload/items` 仍列出（`video=false`）；一次请求与并发请求结果不出现「删了还在」。
  - [ ] **T-UP-008-S10｜隐私红线**：`service/` 不新增任何人脸库/`imwrite`/截图导出；日志不含坐标数组；`grep -rnE "face_recognition|insightface|dlib|facenet|MediaRecorder|toDataURL" service/ vision/ dashboard/` 零命中。
  - [ ] **T-UP-008-S11｜`--sources`**：断言 `up_*` 出现、`anchor_ts_ms==start_ts_ms`、`video=true`、`vision=true`、`keypoints=false`、`watermark` 正确、demo 5 条字段与排序逐项相等、删除后消失、`/camera/kpts` 空 persons。

- **验收命令**：

```bash
python scripts/check_upload.py --sources
python -m unittest tests.test_upload_sources -v
python scripts/check_camera.py --strict          # demo 零回归
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | 源合并/排序/demo 不变/watermark/kpts 空/删除消失/对齐分支 | `--sources`（直调 `sources_payload`/`kpts_payload`/`alignment`）+ 单测 |
| **需端口（换环境）** | 真实 `200 video/mp4`、Range 206、缓存失效时序 | `curl`（CMD-UP-18 + CMD-CAM-14 口径） |
| **需人工** | 浏览器播放上传视频、水印可读、骨架不显示 | MAN-UP-04/05/08 |

- **完成判据（DoD）**：
  1. 上传完成后 `/camera/sources` 含该 `up_*`；`anchor_ts_ms == start_ts_ms`；`video=true`、`vision=true`、`keypoints=false`、`demo=false`。
  2. demo 5 条源的 `source_id`/`anchor_ts_ms`/`duration_ms`/`fps`/`frames`/`width`/`height` 与 `common/demo_data/*_meta.json` 逐项相等，顺序不变；**demo item 无 `watermark` 键**。
  3. 上传源 item `watermark == "用户上传 · 非取证"`；顶层 `watermark` 仍为 demo 常量。
  4. 两次 `sources` 请求 `items` 数组逐字节一致（`server_time_ms` 除外）。
  5. `/camera/video?source_id=up_*` → 200 + 正确 `Content-Length`；`/camera/kpts?source_id=up_*` → `persons=[]`、`vision` 六字段、无 500。
  6. 删除上传件后 `sources` 不含该源；`/upload/items` 仍列出且 `video=false`。
  7. `python scripts/check_camera.py --strict` 仍 14/14；`test_upload_sources` 全绿。
  8. `service/camera.py` 的 demo 分支无删除行；`_BOOTSTRAPPED` 语义未变（demo 仍进程内缓存）。
  9. 隐私扫描零命中；`service/` 无 `imwrite`。

- **本任务后必跑的回归闸门**：第 7.1 全部四项 + 第 7.2「改 `service/camera.py`」项 + 第 7.3 冻结集合。
- **前置确认**：**PC-UP-03**（camera 源缓存竞态：`_UPLOAD_CACHE` + `Lock` + 失效点，需 C 侧 camera 实现负责人确认）。
- **回归风险**：① 改 `scan_sources()` 而非新增 `scan_uploads()` → demo 排序/字段漂移，REQ-UP-019 红；② 上传源 item 也给 demo 输出 `watermark` → demo 逐字节断言红；③ 复用 `frame_ms_of()` 整数步长处理上传源 → 29.97 fps 定位漂移（R3-UP-17）；④ 完成/删除后未失效缓存 → 新源不可见/已删仍可见（R-UP-10）；⑤ `alignment` 对上传源返回 `aligned=false` → 点告警「画面未对齐」；⑥ 过期件仍登记 → `/camera/video` 指向已删文件。
- **预估工作量**：一天半。

---

## 6. 里程碑 M5 · 闸门全绿

### T-UP-009 · 验收脚本与收口

- **目标**：把「能不能交付」变成一条可复现命令：`check_upload.py --strict` 一次判死错误码集合、零新增依赖、`main.py` 删除行、`source_id` 长度、日志行数、看板单文件；并把实测回填到本文档第 12 章（未验项写「未验」）。
- **交付文件**：

| 文件 | 状态 | 内容 |
|---|---|---|
| `scripts/check_upload.py` | 收口 | `--strict/--accept/--probe/--heuristic/--pose/--task/--judge/--sources/--e2e/--make-fixture` 全实现；未实现分支退出 2 |
| `tests/test_upload_*.py` | 收口 | 7 个文件全绿；`test_upload_accept/record/time/heuristic/pose/task/judge/sources` |
| `docs/spec/upload_video_tasks.md` | 追加第 12 章 | 实测回填（由实现轮填写；**不改上游 `upload_video.md`/`upload_video_design.md`**） |

- **依赖**：T-UP-001~008。
- **实现要点**：
  - [ ] **T-UP-009-S1｜`--strict` 七类断言**：`set(ERROR_CODES) == set(4.3 表 19 条)`（不是数量 20，D-UP-15）；`requirements*.txt` 零改动；`service/main.py` 删除列 = 0（工作树 + 暂存区）；`source_id` ≤20；单件日志 ≤60 行；`dashboard` 单文件且零外链；4 个新文件 PEP 604 扫描为空。
  - [ ] **T-UP-009-S2｜错误注入全覆盖**：19 个 `code` 各注入一次，`detail.code` 命中且 `message` 为中文、不含英文堆栈/绝对路径；既有 `POST /ack` 404 仍为字符串 `detail`。
  - [ ] **T-UP-009-S3｜脚本纪律**：只调 `service.upload*` 的真实服务函数/纯函数，**不复制**时间公式、`vision.json` 校验、配额公式（同 `scripts/check_camera.py:16-18`）；静态断言只读文件。
  - [ ] **T-UP-009-S4｜三档验收汇总**：机器（沙箱）项必须全绿；需端口项列入「换环境待验」；需人工项列入附录 C，**不得用进程内结果冒充**。
  - [ ] **T-UP-009-S5｜实测回填**：新增第 12 章，按 CMD-UP-01~23 与 MAN-UP-01~08 回填真实值；未执行项写「未验」+ 替代手段；登记残余风险。
  - [ ] **T-UP-009-S6｜回滚开关验证**：`UPLOAD_ENABLE=0` → 上传路由返回 `503`/`404`（保留代码）；`UPLOAD_REPORT=0` → 只判不发；两种状态下四道回归闸门仍全绿。

- **验收命令**：

```bash
python scripts/check_upload.py --strict
python scripts/check_upload.py --accept --probe --heuristic --task --judge --sources --e2e
python scripts/passthrough_test.py
python scripts/check_camera.py --strict
python scripts/check_frontend.py --strict
python -m unittest discover -s tests -t . -p "test_*.py"
```

- **沙箱可验性**：

| 档位 | 覆盖 | 手段 |
|---|---|---|
| **机器（沙箱可验）** | `--strict` 七类、错误码集合、PEP 604、冻结集合、四道闸门 | 全部脚本/单测 |
| **需端口（换环境）** | 真实 HTTP 全链路、200 MiB/10 min 耗时 | `python -m service.main` + `curl`（CMD-UP-18） |
| **需人工** | 真视频判困人、水印可读、浏览器播放、24 h 清理长跑 | 附录 C `MAN-UP-01~08` |

- **完成判据（DoD）**：
  1. `--strict` 退出码 0，且七类断言逐项打印通过。
  2. 9 个子命令全部实现（无「未实现」分支被触发）；`--pose` 在无依赖环境为 SKIP 且不判失败。
  3. `ERROR_CODES` 集合与 4.3 表 **19** 条集合相等（不是 20）。
  4. `python -c "import service.upload"`、`import service.upload_features`、`import service.upload_worker` 均退出码 0（3.9.12）。
  5. 4 个新文件 PEP 604 扫描无输出；`service/main.py` 删除列 = 0；`requirements*.txt` 与起点一致。
  6. `git status --short common results data/labels` 与 `data/raw/.upload_baseline.txt` **逐行相等**（25 条）。
  7. 四道回归闸门：`passthrough` 11/11、`check_camera --strict` 14/14、`check_frontend --strict` 29/29、`unittest` ≥192 + 新增全绿。
  8. `ls dashboard` 仍仅 `index.html`；`grep` 零外链。
  9. 第 12 章实测回填完成，未验项显式标注；残余风险 ≤5 条且有缓解。
  10. 既有文档（`upload_video.md`、`upload_video_design.md`、`tasks.md`、`camera_tasks.md`、`camera.md`、`integration.md`、`frontend.md`、`supplement_ac.md`、`requirement.md`、`design.md`）`git status` 无改动。

- **本任务后必跑的回归闸门**：第 7 章全部。
- **前置确认**：无新增；须汇总 PC-UP-01~05 的确认结论并在第 12 章登记。
- **回归风险**：① 为过 `--strict` 删既有断言/放宽阈值 → 「假通过」；② 把「需端口/需人工」项写成「已验证」→ 违反 0.4 事实口径；③ `--strict` 里复制服务端公式 → 脚本随时间漂移（R6-UP-03）；④ 回填时改上游文档 → 违反「只追加式引用」；⑤ `UPLOAD_ENABLE=0` 分支未验 → 回滚时发现路由仍可用。
- **预估工作量**：一天。

---

## 7. 回归闸门与冻结区检查

### 7.1 每个任务合入前必跑（最小集，四道）

```bash
python scripts/passthrough_test.py                                   # 期望 11/11，退出码 0
python -m unittest discover -s tests -t . -p "test_*.py"             # 期望 ≥192 项 OK + 本任务新增
git status --short -- requirements.txt requirements.lock.txt requirements-c39.txt
# 期望与起点一致：仅 `A  requirements-c39.txt`（既有 staged），本轮不得新增/修改
python -c "import service.upload"                                    # T-UP-001 起生效；退出码 0（3.9.12）
```

> `check_frontend.py --strict`（29/29）与 `check_camera.py --strict`（14/14）按 7.2 的改动面追加；因看板与画面零改动的红线贯穿全程，建议**每任务都跑**（两条命令各 < 2 s）。

### 7.2 按改动面追加必跑

| 触发条件 | 涉及任务 | 追加命令 | 通过条件 |
|---|---|---|---|
| 改 `service/main.py`（新增路由或 lifespan 插入行） | 001、006、007（如需） | `git diff --numstat -- service/main.py` + `git diff --cached --numstat -- service/main.py` | 两处**删除列之和 = 0**；新增路由 `grep -n` 行号 < 305 |
| 改 `service/camera.py`（只追加分支） | 008 | `python scripts/check_camera.py --strict` | 14/14；demo 5 条字段/排序断言通过 |
| 任何任务（看板零改动红线） | 全部 | `python scripts/check_frontend.py --strict` | 29/29，断言数不下降 |
| 有磁盘写入（`data/raw/`） | 001、006、007、008、009 | `python scripts/check_upload.py --strict` 的冻结段 + 7.3 | 冻结集合相等；`git status` 不含 `data/raw/` |
| 产出六字段/判决产物 | 004、007 | `python scripts/check_upload.py --heuristic` / `--judge` | 六字段 6/6；`urgency≤2`；边沿条数正确 |
| 新增/改动 `service/upload*.py` | 001~008 | PEP 604 扫描 + 顶层 import 审查 | 无 `X \| Y`；L0 层无 `cv2`/`fastapi`/`numpy` 顶层 import |

### 7.3 冻结区与依赖面检查（三条红线）

| 编号 | 判据 | 命令 | 通过条件 |
|---|---|---|---|
| **F-UP-1** | 冻结区条目**集合相等**（不是「为空」） | `git status --short common results data/labels \| sort \| diff data/raw/.upload_baseline.txt -` | 无输出；基线 25 条 |
| **F-UP-2** | `service/main.py` 既有行零删除 | `git diff --numstat -- service/main.py`（+ `--cached`） | 删除列 = 0；新增只允许 import/路由/lifespan 插入行 |
| **F-UP-3** | `requirements*.txt` 零改动 | `git status --short -- requirements*.txt` | 仅 `A  requirements-c39.txt`（起点既有），无 `M`/新 `A` |

> 基线文件生成（实现轮第一件事，落在已被忽略的 `data/raw/`，不污染仓库）：
> ```bash
> git status --short common results data/labels | sort > data/raw/.upload_baseline.txt
> ```

**失败处置**：F-UP-1 出现新增改动 → 判断是否为本轮误写（本轮对 A/B 产物**只读**），回退误写并记录；F-UP-2 若出现删除行 → 立刻 `git checkout -- service/main.py` 重做纯追加；F-UP-3 任何 `requirements*` 改动一律回退。

---

## 8. 里程碑最小可验收集合

| 里程碑 | 最小可验收集合（全部通过才算出口） | 出口判据 |
|---|---|---|
| **M1** | `python scripts/check_upload.py --accept`、`--probe`；`python -m unittest tests.test_upload_accept -v`、`tests.test_upload_record -v`、`tests.test_upload_time -v`；`python -c "import service.upload"`；7.1 四项；7.3 三条 | 合法视频 202 + sha256 一致；记录/幂等/排序/删除可验；`start_ts_ms` 必填且历史不平移；`main.py` 删除 0 |
| **M2** | `python scripts/check_upload.py --heuristic`；`--pose`（SKIP 合法）；`python -m unittest tests.test_upload_heuristic -v`（+ `tests.test_upload_pose -v`）；`python -c "import service.upload_features"`；7.1 四项 | `vision.json` 条数 = `frames`、六字段 6/6、`fall_prob` 全 0、`keypoints_ok is False`、全黑不告警 |
| **M3** | `python scripts/check_upload.py --task`、`--judge`、`--e2e`；`python -m unittest tests.test_upload_task -v`、`tests.test_upload_judge -v`；7.1 四项 + 7.2 `main.py` 项 | 12 s fixture → `/alerts` 1 条 `trapped/urgency=2`；边沿 1/2 条；窗口越界不入库；任务容量/恢复/超时可验 |
| **M4** | `python scripts/check_upload.py --sources`；`python -m unittest tests.test_upload_sources -v`；`python scripts/check_camera.py --strict`；7.1 四项 + 7.2 `camera.py` 项 | `up_*` 可见可播可追溯；demo 5 条逐字节不变；删除后消失；水印正确；`/camera/kpts` 不 500 |
| **M5** | `python scripts/check_upload.py --strict` + 全部子命令；`python scripts/passthrough_test.py`；`python scripts/check_camera.py --strict`；`python scripts/check_frontend.py --strict`；`python -m unittest discover -s tests -t . -p "test_*.py"`；7.3 三条 | 七类断言全绿；19 码集合相等；四道闸门全绿；冻结集合相等；实测回填完成（未验项显式标注） |

> **宣布口径**：M5 全绿 + `MAN-UP-01`（真实拍摄视频判出困人）通过，才可宣布「真实视频困人告警可用」。`MAN-UP-01` 是唯一不能由脚本替代的项。

---

## 9. 前置确认项（5 条开放风险 → 任务 → 需谁确认）

> 以下 5 项在设计文档中以「开放风险」形式登记（`R3-UP-01/02/06/07/08`、`Q-UP-D01`）。**未确认即开工的任务，其验收判据无法定稿**，因此写进对应任务的前置确认。

| 编号 | 事项 | 设计裁定（现口径） | 落在任务 | 需谁确认 | 不确认的后果 | 卡点等级 |
|---|---|---|---|---|---|---|
| **PC-UP-01** | `upload_id` 组合摘要：`sha256(tenant=device_id + "\x00" + file_sha256)[:32]`，即「同文件不同设备 → 不同 `upload_id`」 | D-UP-01（`R3-UP-08` / `Q-UP-D01`） | T-UP-001、T-UP-002 | **项目负责人（需求侧）** | 幂等键与目录命名返工；跨设备同文件串号，REQ-UP-006 验收口径需重写 | 🟠 中（开工即需） |
| **PC-UP-02** | `reason` 白名单：上传层在 B 生成 `Decision` 后**追加** `feature_mode`/`roi`/`count_low_conf` 三键，**不回灌** `ReasonBuilder._self_check`；`model/reason.py` 零改动 | D-UP-03（`R3-UP-07`） | T-UP-004（产出元数据）、T-UP-007（追加） | **B 侧负责人** | 若要求进白名单 → 必须改 B 冻结文件并重跑 B 回归；否则 `_self_check` 断言失败 | 🟡 低（有绕行） |
| **PC-UP-03** | camera 源缓存竞态：新增 `_UPLOAD_CACHE` + `threading.Lock`，完成/删除后 `invalidate_sources()`；`_BOOTSTRAPPED` 只控制 demo | R3-UP-06 | T-UP-008 | **C 侧 camera 实现负责人**（与 `camera_tasks` T-CAM-001 同文件） | 上传源短时不可见 / 删除后残留 → REQ-UP-019/020 机器断言红；并发读半截 JSON | 🟠 中（并发下必现） |
| **PC-UP-04** | `cv2` 版本：本机 5.1.0 vs lock 4.10.0.84，只允许稳定子集，`MOG2` 异常回退 `absdiff`，`findContours` 用 `contours, _ =` | R3-UP-01（`R-UP-06`） | T-UP-004（主）、T-UP-005 | **环境/运维负责人**（是否改 lock）+ **实现者**（双版本实测） | 换机崩溃；或为「修版本」改 `requirements*` → 违反 C-UP-03 直接红 | 🔴 **高（唯一长杆的运行时风险）** |
| **PC-UP-05** | 启发式误报策略：低置信（`fg_ratio` 不足 / 无有效连通域）→ `person_count=0` + `count_low_conf`，**宁可漏报不误报**；阈值外置可调，对外只承诺「长时间滞留 + 门未开 + 有人」 | R3-UP-02（`R-UP-01/02`） | T-UP-004（阈值）、T-UP-007（判决） | **项目负责人（对外话术）** + **验收人（MAN-UP-01/02 判读）** | 误报/漏报无判定基准，`MAN-UP-01` 无法给出结论；可能被要求「提高检出」而引入伪造 `fall_prob` 风险 | 🔴 **高（决定验收能否收口）** |

> 另 4 条设计开放问题（`Q-UP-D02/D03/D04/D05` 等）不影响任务拆解口径：D-UP-15（19 码）、D-UP-07（容量 4）、D-UP-08（12 s fixture）、D-UP-09（e2e 用 `now_ms()`）已在任务判据中固化；如需调整只改对应 DoD 数字，不改任务边界。

**最可能卡住实现的前 3 项**：**PC-UP-04（cv2 双版本）**、**PC-UP-05（启发式误报判读基准）**、**PC-UP-01（`upload_id`/幂等语义）**。

---

## 10. 回滚与降级（按任务）

| 触发条件 | 回滚动作 | 验证 |
|---|---|---|
| T-UP-001/002/003 任一自测红 | 删除 `service/upload.py`、移除 `service/main.py` 的 upload 路由块与 import 行、删 `service/config.py` 的 `UPLOAD_*` 常量、删 `scripts/check_upload.py` 与 `tests/test_upload_*` | `passthrough_test.py` 11/11；`main.py` 与 HEAD 一致（删除列 0）；192 项全绿 |
| T-UP-004 启发式崩/超时 | 保留 `service/upload_features.py`，把 `UPLOAD_ENABLE=0`（路由 503）或 `UPLOAD_FRAME_STEP` 调大；不删算法 | `--heuristic` 可重跑；既有 4 接口不变 |
| T-UP-005 L1 不可用 | 删 `detect_feature_mode` 的 L1 分支（保留 L2），`feature_mode` 恒 `heuristic` | `--heuristic` 全绿；`--pose` SKIP |
| T-UP-006 队列/恢复异常 | 停 worker（`lifespan` 插入行回退为注释），上传仍落盘但任务恒 `queued`；或 `UPLOAD_ENABLE=0` | `/upload/*` 返回 503；既有接口不变 |
| T-UP-007 误报严重 | `UPLOAD_REPORT=0`：只写 `vision.json`/`decisions.json`，不写 DB | `/alerts` 无新增；`--judge` 仍可核对 |
| T-UP-008 源/缓存污染 demo | 删除 `service/camera.py` 的上传分支（`scan_uploads`/`_UPLOAD_CACHE`/`alignment` 上传分支），`load_sources` 回 demo-only | `check_camera --strict` 14/14；`sources` 恰 5 条 |
| 磁盘/隐私事故 | 删除 `data/raw/uploads/*` 全部内容并重建空目录 | `ls data/raw/uploads` 空；`/upload/items` 空清单 |
| 演示前 30 分钟不稳定 | `UPLOAD_ENABLE=0` + 用既有 demo 链路答辩 | 基线命令（附录 A 第 1 组）逐条复现 |

**回滚红线**：不得为「让上传好看」改 `common/demo_data/`、`results/`、`data/labels/`、`common/schema.py`；不得改 `requirements*` 来「解决」依赖问题；不得删既有测试断言来「让闸门变绿」。

---

## 11. 与既有任务文档的关系（不改既有条目）

1. `docs/spec/tasks.md` 的 `T-B-*`、`T-INT-*`、`T-AC-*`、`T-UI-*`，`camera_tasks.md` 的 `T-CAM-001~008`，**一条不改**；本文档**不追加**任何指针行（本轮「不改任何既有文档」），指针与实测回填留待 T-UP-009 在本文档第 12 章完成。
2. 本文档任务与 `upload_video.md` 6.1、设计第 12 章**双向可追溯**：编号、标题、依赖图、关键路径、阻断点逐项对齐；未新增/合并任务编号。
3. `upload_video.md` 第 8 章的 `CMD-UP-01~18` 全部继承；本文档新增 `CMD-UP-19~23`（附录 A），只用于本文档的任务验收，不回写上游。
4. 设计裁定 `D-UP-01~15` 在本文档中按「实现期必须遵守」直接引用（附录 D），不改写、不新增裁定。

---

## 附录 A · 验收命令清单

> 第 1 组现状即可执行（只读，本轮已实测）；第 2 组在 `scripts/check_upload.py` 与 `tests/test_upload_*.py` 创建后生效；第 3 组需换环境（真实端口/浏览器/真实素材）。
> **脚本唯一来源**：`scripts/check_upload.py` 由 T-UP-001 创建，子命令集合以下表为准，未列出的子命令不存在。

```bash
# ── 第 1 组：现状基线（规划阶段，已实测）────────────────────────────
python -V                                                        # Python 3.9.12（CMD-UP-01）
python -c "import multipart"                                     # 仍失败（CMD-UP-03）
python -c "import ultralytics"                                   # 仍失败（CMD-UP-04）
python -c "import cv2;print(cv2.__version__)"                    # 5.1.0（CMD-UP-06）
ls models                                                        # 不存在（CMD-UP-05）
ls data/raw                                                      # 空（CMD-UP-08）
python scripts/passthrough_test.py                               # 11/11
python scripts/check_frontend.py --strict                        # 29/29
python scripts/check_camera.py --strict                          # 14/14
python -m unittest discover -s tests -t . -p "test_*.py"         # 192 项 OK
git diff --numstat -- service/main.py                            # 103  0（删除列 0）
git status --short -- requirements*.txt                          # 仅 A  requirements-c39.txt
git status --short common results data/labels | wc -l            # 25

# ── 第 2 组：实现轮（脚本创建后；项目根执行）──────────────────────────
python scripts/check_upload.py --make-fixture --seconds 4  --out data/raw/.fixtures/u4.mp4
python scripts/check_upload.py --make-fixture --seconds 12 --out data/raw/.fixtures/u12.mp4
python scripts/check_upload.py --accept                          # CMD-UP-12（T-UP-001）
python scripts/check_upload.py --probe                           # CMD-UP-13（T-UP-003）
python scripts/check_upload.py --heuristic                       # CMD-UP-14（T-UP-004）
python scripts/check_upload.py --pose                            # T-UP-005（无依赖 SKIP）
python scripts/check_upload.py --task                            # CMD-UP-15（T-UP-006）
python scripts/check_upload.py --judge                           # CMD-UP-16（T-UP-007）
python scripts/check_upload.py --sources                         # CMD-UP-17（T-UP-008）
python scripts/check_upload.py --e2e                             # 12 s fixture 全链路（T-UP-007/009）
python scripts/check_upload.py --strict                          # CMD-UP-11（T-UP-009）
python -m unittest tests.test_upload_accept -v
python -m unittest tests.test_upload_record -v
python -m unittest tests.test_upload_time -v
python -m unittest tests.test_upload_heuristic -v
python -m unittest tests.test_upload_pose -v
python -m unittest tests.test_upload_task -v
python -m unittest tests.test_upload_judge -v
python -m unittest tests.test_upload_sources -v
python -c "import service.upload"                                # T-UP-001/009
python -c "import service.upload_features"                       # T-UP-004
python -c "import service.upload_worker"                         # T-UP-006
python scripts/passthrough_test.py                               # 11/11
python scripts/check_camera.py --strict                          # 14/14
python scripts/check_frontend.py --strict                        # 29/29
python -m unittest discover -s tests -t . -p "test_*.py"         # ≥192 + 新增全绿

# ── 第 3 组：换环境（真实端口 + 真实素材 + 浏览器）────────────────────
python -m service.main                                           # 127.0.0.1:18080
curl -s -o /dev/null -w "%{http_code}" \
  -H "Content-Type: video/mp4" -H "Content-Length: <n>" \
  -H "X-Upload-Device-Id: ELEV-3F-001" \
  -H "X-Upload-Start-Ts-Ms: 1763300180000" \
  --data-binary @v.mp4 "http://127.0.0.1:18080/api/v1/upload/video"     # CMD-UP-18 → 202
curl -s "http://127.0.0.1:18080/api/v1/upload/tasks/<task_id>"           # → done / progress 100
curl -s "http://127.0.0.1:18080/api/v1/alerts?device_id=ELEV-3F-001"     # 含 trapped/urgency=2
curl -s "http://127.0.0.1:18080/api/v1/camera/sources" | grep "up_"      # M4 可见
```

### A.1 CMD-UP 编号对照

| 编号 | 命令 | 承载任务 | 状态 |
|---|---|---|---|
| CMD-UP-01~08 | `python -V`、`import service.upload`、`import multipart`、依赖探测、`ls models`、`cv2` 版本、全仓搜索、`ls data/raw` | 现状基线 | 现状已实测 |
| CMD-UP-09~10 | `git status --short requirements*`、`git diff --numstat service/main.py` | 依赖/删除行 | 现状已实测 |
| CMD-UP-11 | `python scripts/check_upload.py --strict` | T-UP-009 | 脚本待建 |
| CMD-UP-12 | `python scripts/check_upload.py --accept` | T-UP-001 | 脚本待建 |
| CMD-UP-13 | `python scripts/check_upload.py --probe` | T-UP-003 | 脚本待建 |
| CMD-UP-14 | `python scripts/check_upload.py --heuristic` | T-UP-004 | 脚本待建 |
| CMD-UP-15 | `python scripts/check_upload.py --task` | T-UP-006 | 脚本待建 |
| CMD-UP-16 | `python scripts/check_upload.py --judge` | T-UP-007 | 脚本待建 |
| CMD-UP-17 | `python scripts/check_upload.py --sources` | T-UP-008 | 脚本待建 |
| CMD-UP-18 | 真实端口 `curl` 上传 + tasks + alerts | T-UP-001/007 | 换环境 |
| **CMD-UP-19** | `git status --short common results data/labels \| sort \| diff data/raw/.upload_baseline.txt -`（本文档新增） | 全部 | 命令组合，无新脚本 |
| **CMD-UP-20** | `git diff --numstat -- service/main.py` + `--cached`（删除列 0） | 001/006/007 | 命令组合 |
| **CMD-UP-21** | `python scripts/check_upload.py --e2e`（12 s 全链路）（本文档新增编号，复用既有子命令） | 007/009 | 脚本待建 |
| **CMD-UP-22** | PEP 604 扫描：`grep -nE ": *[A-Za-z0-9_.\\[\\]]+ *\\|" service/upload.py service/upload_features.py service/upload_worker.py scripts/check_upload.py` | 001~009 | 命令组合 |
| **CMD-UP-23** | 依赖面三重：`git status --short -- requirements*.txt` + `python -c "import multipart"`（仍失败）+ `python -c "import service.upload"`（成功） | 001/009 | 命令组合 |

### A.2 子命令职责冻结（`check_upload.py`）

| 子命令 | 断言范围 | 首次落地任务 |
|---|---|---|
| `--make-fixture --seconds 4\|12 --out <path>` | 用 `cv2.VideoWriter` 现生成 mp4；打印 `fps/frames/size` | T-UP-001 |
| `--accept` | 合法 → 202；411/413/415/400；半成品清理；sha256 一致；幂等 200 且目录数不变；配额 507 | T-UP-001 |
| `--probe` | 来源优先级；10 位拒绝；时长/帧率边界；窗口阻断；历史不平移 | T-UP-003 |
| `--heuristic` | 六字段 6/6；`fall_prob` 全 0；`keypoints_ok` 全 False；全黑不告警；条数 = frames | T-UP-004 |
| `--pose` | 依赖缺失 SKIP；空权重仍 heuristic；合法侧车 → pose | T-UP-005 |
| `--task` | 4 活动 / 第 5 个 503；progress 单调；超时；重启恢复 | T-UP-006 |
| `--judge` | `urgency≤2`；`event_type=trapped`；边沿 1/2 条；窗口越界阻断 | T-UP-007 |
| `--sources` | `up_*` 可查；demo 5 条不变；视频 200；删除后消失 | T-UP-008 |
| `--e2e` | 12 s fixture 全链路：`vision.json`/`decisions.json`/`/alerts`/`/stats`/`sources` | T-UP-007（T-UP-009 收口） |
| `--strict` | 19 码集合；零新增依赖；`main.py` 删除 0；`source_id`≤20；日志≤60；`dashboard` 单文件 | T-UP-009 |

---

## 附录 B · 需求覆盖自检表（REQ-UP-001~027 → 任务）

| 需求 | 优先级 | 覆盖任务 | 机器判据落在哪 |
|---|---|---|---|
| REQ-UP-001（真实字节，不得复用 demo） | P0 | T-UP-001、T-UP-002 | `--accept` sha256 一致；`source_id` 以 `up_` 开头；`grep demo_data` 无上传源路径 |
| REQ-UP-002（raw body 流式，禁 multipart） | P0 | T-UP-001 | `--accept` 缺 `Content-Length` → 411；`import multipart` 仍失败；tracemalloc ≤64 MiB |
| REQ-UP-003（格式/尺寸边界显式拒绝） | P0 | T-UP-001、T-UP-003 | `--accept`/`--probe` 的 415/422/413/400 + 无残留 |
| REQ-UP-004（落盘位置/配额/清理） | P0 | T-UP-001 | `--accept` 四件产物、507、清理只删视频 |
| REQ-UP-005（目录投放/命令行同构，P1） | P1 | T-UP-002（记录同构） | 键集合相等断言；不进主线（P1） |
| REQ-UP-006（重复上传幂等） | P1 | T-UP-002 | `test_upload_record` 幂等 6 行 + 目录数不变 |
| REQ-UP-007（六字段同构） | P0 | T-UP-004 | `--heuristic` 条数/键集合/`ts` 递增/`load_vision_sequence` |
| REQ-UP-008（档位可探测可见） | P0 | T-UP-004、T-UP-005 | `--heuristic` `degraded=true`；`--pose` 空权重不升档 |
| REQ-UP-009（L2 语义边界，禁伪造 fall_prob） | P0 | T-UP-004 | 全帧 `fall_prob==0`/`keypoints_ok is False`；全黑不告警；唯一赋值点 |
| REQ-UP-010（门区兜底必须标注） | P1 | T-UP-004、T-UP-007 | `roi=client/sidecar/default`；兜底 `door_approach>0.6` |
| REQ-UP-011（侧车覆盖为真关键点，P1） | P1 | T-UP-005 | `--pose` 合法侧车 → pose；非法 → 422 |
| REQ-UP-012（`start_ts_ms` 必填且录制时间为准） | P0 | T-UP-003 | `--probe` 三源优先级；缺失 → 400 且无目录；`vision[0].ts==start_ts_ms` |
| REQ-UP-013（时基越界阻断，历史不平移） | P0 | T-UP-003、T-UP-007 | `--probe` 窗口函数；`--judge` 越界 → `UP_TIME_BASE_INVALID` 且不入库；历史时间 +0 |
| REQ-UP-014（端到端脚本可复现） | P0 | T-UP-007、T-UP-009 | `--e2e` 退出码 0 + `/alerts`/`/stats` 断言 |
| REQ-UP-015（边沿触发，禁逐帧刷屏） | P0 | T-UP-007 | `--judge` 10 min → 1 条；两轮 → 2 条；上报 ≤ 边沿+1 |
| REQ-UP-016（失败可查可重试） | P1 | T-UP-006 | `--task` 超时/恢复/进度不回退；重试日志合并 |
| REQ-UP-017（无 IMU 最高 U2） | P0 | T-UP-007 | 全部 `urgency≤2`；`trapped/urgency=2`；无 `_imu.npy` |
| REQ-UP-018（复用既有状态机） | P0 | T-UP-007 | `FusionRunner.run(vision, [])`；无第二套 dwell；九字段集合 |
| REQ-UP-019（上传源可见可播） | P0 | T-UP-008 | `--sources` `up_*`/anchor/video/vision；delete 后消失 |
| REQ-UP-020（设备归属唯一） | P0 | T-UP-002、T-UP-007、T-UP-008 | `ELEV-DEMO-001` 拒绝；`/devices` 聚合；`upload.json.device_id == decisions[].device_id` |
| REQ-UP-021（不进冻结区 + 24 h 删除） | P0 | T-UP-001、T-UP-009 | F-UP-1 集合相等；清理后仅视频消失；`data/raw/uploads` 写入点唯一 |
| REQ-UP-022（无人脸识别/非取证） | P0 | T-UP-008 | 隐私扫描零命中；上传源 item `watermark` |
| REQ-UP-023（降级矩阵可感知） | P0 | T-UP-009 | 7 种降级逐个注入；4 个既有接口 200 且字段不变 |
| REQ-UP-024（错误码一一映射） | P0 | T-UP-009 | 19 码集合相等；中文文案；`ack` 404 仍字符串 detail |
| REQ-UP-025（3.9 兼容 + 既有链零破坏） | P0 | T-UP-009 | 三条 import 退出 0；PEP 604 空；`main.py` 删除 0；四道闸门 |
| REQ-UP-026（看板上传页，P2） | P2 | **不派任务**（本期不做） | 反向断言：`dashboard` 单文件、零外链、轮询未变 |
| REQ-UP-027（批量与并发上限，P2） | P2 | T-UP-006（容量部分兜底） | `--task` 4 活动 / 第 5 个 503；运行任务 ≤1 |

> 未覆盖说明：`REQ-UP-026` 属 P2（上传页），按设计 0.3 与 `frontend.md` 本期不做，仅在 T-UP-009 做反向断言；`REQ-UP-027` 的「批量 UI」不派任务，只落地其后端容量约束（并发 1 / 排队 3 / 503）。

---

## 附录 C · 沙箱不可验项与人工清单

### C.1 沙箱能力边界（不得「假通过」）

| 能力 | 沙箱可验（本机） | 沙箱不可验 | 替代手段 |
|---|---|---|---|
| 头解析/411/413/415/400 | ✅ 进程内直调 + 模拟 chunk | ✅ 真实响应语义需端口 | 换环境 `curl`（CMD-UP-18） |
| 流式落盘/sha256/幂等/配额 | ✅ `UploadSink` + 临时目录 | ✅ 真实 200 MiB / 2 GiB 配额 | 换环境计时 + 手工清目录 |
| 探测/时长/分辨率 | ✅ 现生成 4/12 s fixture | ✅ 手机 H.264/HEVC 变体 | 换环境真实素材 |
| L2 六字段 | ✅ 合成帧 + 全黑 | ❌ 真实视频人数稳定性 | **MAN-UP-01/02（人工）** |
| 判决/边沿/窗口 | ✅ 直调 `FusionRunner` + 独立 SQLite | ✅ 真实 HTTP 时序 | 换环境 |
| `sources` 可见/可播 | ✅ 直调 `sources_payload`/`kpts_payload` | ❌ 浏览器解码/Range | 换环境 + 人工播放 |
| 24 h 清理 | ✅ 伪造 `expire_ms` | ✅ 真实长跑 | MAN-UP-06 |
| 200 MiB / 10 min 耗时 | ❌ | ✅ | MAN-UP-03 |
| **真实视频判困人** | ❌ | ❌ | **MAN-UP-01（唯一不可替代）** |

### C.2 必须人工 / 换环境确认（`MAN-UP-01~08`，继承需求 8.3）

| 编号 | 项 | 为什么本机不行 | 责任 | 关联任务 |
|---|---|---|---|---|
| **MAN-UP-01** | 用一段**真实拍摄**电梯视频（非 demo）判出困人并告警 | 需真实素材 + 主观判读 | 项目负责人 | T-UP-004/007 |
| MAN-UP-02 | 真实视频启发式人数在无遮挡/有遮挡两份样本下是否稳定 | 需真实画面 | 人工 | T-UP-004 |
| MAN-UP-03 | 200 MiB / 10 min 上传与处理耗时可接受 | 需真实端口与文件 | 换环境计时 | T-UP-001/006 |
| MAN-UP-04 | 看板告警卡正确（楼层/人数/等级），播放/进度（若 P2） | 需浏览器 | 人工 | T-UP-007/008 |
| MAN-UP-05 | 水印「用户上传 · 非取证」在缩放/投影下可读（≥4.5:1） | 需去色与投影设备 | 人工 | T-UP-008 |
| MAN-UP-06 | 24 h 清理真实生效（可伪造 `expire_ms` 提前触发） | 需长跑或改时钟 | 人工 | T-UP-001 |
| MAN-UP-07 | 多设备上传后 `/devices` 多行、`last_seen` 正确 | 需真实上报时序 | 换环境 | T-UP-007 |
| MAN-UP-08 | 撤销上传后看板不再显示该源、告警与派生仍在 | 需浏览器 + 接口联看 | 人工 | T-UP-008 |

---

## 附录 D · 设计裁定索引（实现期必须遵守 `D-UP-01~15`）

| 裁定 | 一句话 | 落点任务 |
|---|---|---|
| D-UP-01 | `upload_id = sha256(device_id+"\x00"+file_sha256)[:32]` | T-UP-002 |
| D-UP-02 | `vision.json` 始终逐帧（`step` 只减计算量） | T-UP-004 |
| D-UP-03 | `reason` 三键只在上传层追加，不回灌 `_self_check`，不改 `model/reason.py` | T-UP-004/007 |
| D-UP-04 | 上传件不调 `report()`，直写 `db.insert_alert` + `upsert_device` + `set_state` | T-UP-007 |
| D-UP-05 | 上传源无会话时 `aligned=true/offset=0`；demo 行为不变 | T-UP-008 |
| D-UP-06 | `upload.json` 增 `final_event_type`/`reported_alert_ts` 摘要字段 | T-UP-002/007 |
| D-UP-07 | 队列容量 4（运行 1 + 排队 3），第 5 个 503 | T-UP-006 |
| D-UP-08 | 默认 dwell 8 s；e2e 用 12 s fixture | T-UP-004/007 |
| D-UP-09 | e2e 默认 `start_ts_ms=now_ms()`；历史时间另测 | T-UP-007 |
| D-UP-10 | `task_id = <upload_id>-<received_ms>`，可重建 | T-UP-002/006 |
| D-UP-11 | 顶层 `watermark` 不变；仅上传源 item 增 `watermark` | T-UP-008 |
| D-UP-12 | `scan_uploads` 只登记 `source.mp4` 存在；items 仍列 `video=false` | T-UP-008 |
| D-UP-13 | `upsert_device` 用 `received_ms` 作 `last_seen` | T-UP-007 |
| D-UP-14 | HTTP 路径只认 `X-Upload-Start-Ts-Ms`；sidecar 只用于非 HTTP 入口 | T-UP-003 |
| D-UP-15 | `ERROR_CODES` 严格 19 条；`--strict` 断言集合相等 | T-UP-009 |

---

## 12. 实测回填（2026-11-25）

| 项 | 结果 |
|---|---|
| 新增单测 | `tests/test_upload_*.py` 7 文件 + `upload_helpers.py`：54 例全绿 |
| 全量单测 | `python -m unittest discover -s tests -t . -p "test_*.py"`：**246/246** |
| 验收脚本 | `scripts/check_upload.py` 8 子命令 + `--strict`：**78/78** |
| 既有闸门 | `passthrough_test.py` 11/11；`check_frontend.py --strict` 29/29；`check_camera.py --strict` 14/14 |
| 判困链路 | 12 s fixture：`trapped` 21 帧、1 条告警、`final_event_type=trapped`、`state=reported` |
| 依赖面 | 零新增（仅用已装 `cv2`/`numpy`）；`python -c "import service.upload"` 在 3.9 最小环境退出码 0 |

**实现取舍（与设计字面不同之处）**

1. L2 启发式在 `MOG2` 之外并联 `absdiff`，并对人数做「只增不减 + 连续 25 个计算帧为空才归零（约 2 s）」的稳定化：残影造成的 1↔2 抖动会被 B 的 `motion_level` 代理判为「有人走动」，反复重置 dwell；红线 `fall_prob=0` / `keypoints_ok=False` 不变。
2. 首帧不产生前景（只用于初始化背景模型）：否则全黑视频也会报「有人」，破坏 `person_count=0` 的验收。
3. `upload.json` 的摘要字段除设计的 5 项外增加 `roi_source`（承载 `reason` 的 `roi=client|sidecar|default`），不进入九字段/六字段契约。

**残余风险**

- L1（`ultralytics`/`onnxruntime` + 权重）未安装：`--pose` 走 SKIP，真实上传件默认 `feature_mode=heuristic`、`degraded=true`。
- 看板上传页（P2）未做：上传只提供 API / `scripts/upload_video.py`，播放复用 `/camera/*`。
- 真实端口（uvicorn）与真实拍摄视频受沙箱限制未验；`MAN-UP-01/03/04/05/08` 待人工。

---

**文档状态**：T-UP-001~009 已落地并通过上述闸门；第 12 章为实测记录，`MAN-UP-*` 未验项由人工/换环境补齐。