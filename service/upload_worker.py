"""用户上传真实视频 · 任务编排与判决上报（C · T-UP-006/007）。

职责：
  1. 单进程队列（并发 1 + 排队 3，总容量 4）与任务状态机；
  2. 600 s 超时、阶段进度、重启恢复；
  3. `probe → extract → judge → report` 四阶段推进；
  4. 仅 `trapped` 上升沿入库，绕过实时 `/edge/report` 的 30 s 去重（D-UP-04）；
  5. 写 `vision.json` / `decisions.json` / `upload.json`，并失效画面源缓存。

依赖由 `start(db, cache)` 注入，不 import `service.main`、不 import `fastapi`。
"""
import dataclasses
import json
import logging
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from model.config import PRESET_DEMO, RuntimeConfig
from model.eval import load_vision_sequence
from model.fuse import FusionRunner

from common.schema import Decision, VisionOut

from . import config
from .upload import (
    DECISIONS_NAME,
    SOURCE_NAME,
    VISION_NAME,
    VISION_PART_NAME,
    Accepted,
    UploadError,
    UploadRecord,
    UploadTask,
    UploadTimeout,
    _atomic_write_text,
    accept_body,
    item_payload,
    manifest_payload,
    now_ms,
    parse_task_id,
    read_manifest,
    record_from_manifest,
    record_from_manifest as _record_from_manifest,
    scan_manifests,
    source_id_of,
    task_id_of,
    ttl_ms,
    upload_dir,
    validate_time_window,
    write_manifest,
)
from .upload_features import (
    FeatureResult,
    PoseUnavailable,
    detect_feature_mode,
    extract_rows,
    load_kpts_sidecar,
    validate_heuristic_vision,
    validate_vision_shape,
    write_vision_json,
)

log = logging.getLogger("elevator.upload.worker")

ACTIVE_LIMIT = 4
_POISON = "__stop__"


def _queue_size() -> int:
    try:
        return max(1, int(getattr(config, "UPLOAD_QUEUE_SIZE", 3)))
    except (TypeError, ValueError):
        return 3


def _timeout_sec() -> float:
    try:
        return max(1.0, float(getattr(config, "UPLOAD_TASK_TIMEOUT_SEC", 600)))
    except (TypeError, ValueError):
        return 600.0


def _report_enabled() -> bool:
    try:
        return int(getattr(config, "UPLOAD_REPORT", 1)) != 0
    except (TypeError, ValueError):
        return True


def build_runtime_config(record: UploadRecord) -> RuntimeConfig:
    """上传件默认 demo 预设（dwell=8 s），设备/楼层从记录注入。"""
    cfg = dataclasses.replace(
        PRESET_DEMO, device_id=record.device_id, location=record.location
    )
    try:
        dwell = float(getattr(config, "UPLOAD_DWELL_SECONDS", 8.0))
    except (TypeError, ValueError):
        dwell = 8.0
    if dwell != cfg.thresholds.dwell_threshold:
        cfg = dataclasses.replace(
            cfg,
            thresholds=dataclasses.replace(cfg.thresholds, dwell_threshold=dwell),
        )
    return cfg


def edge_alerts(decisions: List[Decision]) -> List[Tuple[int, Decision]]:
    """只取 `trapped` 的上升沿（脱困后再次困人算新边沿）。"""
    edges: List[Tuple[int, Decision]] = []
    previous = False
    sequence = 0
    for decision in decisions:
        if bool(decision.trapped) and not previous:
            edges.append((sequence, decision))
            sequence += 1
        previous = bool(decision.trapped)
    return edges


def decorate_reason(
    decision: Decision, feature_mode: str, roi_source: str
) -> Decision:
    """D-UP-03：上传件追加 3 类扩展条目，不回灌 B 的 reason 白名单。"""
    if not decision.trapped:
        return decision
    extra = [
        "feature_mode=%s" % str(feature_mode),
        "roi=%s" % str(roi_source),
    ]
    if int(decision.people_count) == 0:
        extra.append("count_low_conf=true")
    return dataclasses.replace(
        decision,
        reason=list(decision.reason) + extra,
    )


def write_decisions(path: Path, decisions: List[Decision]) -> None:
    _atomic_write_text(
        path, json.dumps([asdict(item) for item in decisions], ensure_ascii=False)
    )


def read_decisions(path: Path) -> List[Decision]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise UploadError(500, "UP_INTERNAL", "判决产物格式异常")
    out: List[Decision] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                Decision(
                    ts=int(row["ts"]),
                    device_id=str(row["device_id"]),
                    trapped=bool(row["trapped"]),
                    urgency=int(row["urgency"]),
                    location=str(row.get("location") or "?"),
                    people_count=int(row.get("people_count") or 0),
                    event_type=str(row.get("event_type") or "normal"),
                    confidence=float(row.get("confidence") or 0.0),
                    reason=[str(item) for item in (row.get("reason") or [])],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _task_state_for(state: str) -> str:
    return {
        "received": "queued",
        "extracted": "running",
        "judged": "running",
        "reported": "done",
        "failed": "failed",
    }.get(str(state), "queued")


class UploadTaskManager:
    """单 worker 线程 + 内存任务表；状态更新用 `dataclasses.replace`（frozen）。"""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=max(1, _queue_size()) + 1)
        self._tasks: Dict[str, UploadTask] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active = 0
        self.db: Any = None
        self.cache: Any = None

    # -- 生命周期 -----------------------------------------------------

    def start(self, db: Any, cache: Any) -> None:
        self.db = db
        self.cache = cache
        self.recover_on_startup()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="upload-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(_POISON)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if task_id == _POISON:
                break
            try:
                self._run_task(task_id)
            except Exception as exc:  # noqa: BLE001 - 单任务异常不得杀线程
                log.error("upload_task_crashed task=%s err=%s", task_id, type(exc).__name__)
            finally:
                with self._lock:
                    self._active = max(0, self._active - 1)

    # -- 入队与查询 ---------------------------------------------------

    def enqueue(self, record: UploadRecord, created: bool = True) -> Accepted:
        task_id = task_id_of(record)
        with self._lock:
            existing = self._tasks.get(task_id)
            if not created or (existing is not None and existing.state in ("queued", "running", "done")):
                state = existing.state if existing is not None else _task_state_for(record.state)
                return Accepted(200, accept_body(record, state))
            if self._active >= ACTIVE_LIMIT:
                rollback = upload_dir(record.upload_id, self.base_dir)
                import shutil

                shutil.rmtree(str(rollback), ignore_errors=True)
                raise UploadError(503, "UP_BUSY", "当前有视频在处理，请稍后再传")
            task = UploadTask(
                task_id=task_id,
                upload_id=record.upload_id,
                device_id=record.device_id,
                state="queued",
                stage="received",
                progress=0,
                frames_total=int(record.frames),
                frames_done=0,
                feature_mode=record.feature_mode,
                error=None,
                updated_ms=now_ms(),
                started_ms=now_ms(),
                reported_alert_ts=[],
            )
            self._tasks[task_id] = task
            self._active += 1
            try:
                self._queue.put_nowait(task_id)
            except queue.Full:
                self._tasks.pop(task_id, None)
                self._active = max(0, self._active - 1)
                raise UploadError(503, "UP_BUSY", "当前有视频在处理，请稍后再传")
        return Accepted(202, accept_body(record, "queued"))

    def task_payload(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                task = self._rebuild_task(str(task_id))
            if task is None:
                raise UploadError(404, "UP_TASK_NOT_FOUND", "处理任务不存在", task_id=str(task_id))
            return self._task_public(task)

    def _rebuild_task(self, task_id: str) -> Optional[UploadTask]:
        try:
            upload_id, received = parse_task_id(task_id)
        except UploadError:
            return None
        manifest_path = upload_dir(upload_id, self.base_dir) / "upload.json"
        data = read_manifest(manifest_path)
        if data is None:
            return None
        current = now_ms()
        if int(data.get("expire_ms") or 0) < current:
            return None
        record = record_from_manifest(data)
        if record is None:
            return None
        reported = [int(value) for value in (data.get("reported_alert_ids") or [])]
        return UploadTask(
            task_id=str(task_id),
            upload_id=upload_id,
            device_id=record.device_id,
            state=_task_state_for(record.state),
            stage="done" if record.state == "reported" else record.state,
            progress=100 if record.state == "reported" else 0,
            frames_total=int(record.frames),
            frames_done=0,
            feature_mode=record.feature_mode,
            error=({"code": "UP_INTERNAL", "message": "上次处理被中断，请重新上传"} if record.state == "failed" else None),
            updated_ms=int(record.received_ms),
            started_ms=int(received),
            reported_alert_ts=reported,
        )

    def _task_public(self, task: UploadTask) -> Dict[str, Any]:
        return {
            "task_id": task.task_id,
            "upload_id": task.upload_id,
            "source_id": source_id_of(task.upload_id),
            "device_id": task.device_id,
            "state": task.state,
            "stage": task.stage,
            "progress": int(task.progress),
            "frames_total": int(task.frames_total),
            "frames_done": int(task.frames_done),
            "feature_mode": task.feature_mode,
            "eta_sec": 0 if task.state in ("done", "failed") else 0,
            "started_ms": int(task.started_ms),
            "updated_ms": int(task.updated_ms),
            "alerts": {
                "produced": len(task.reported_alert_ts),
                "first_alert_ts": task.reported_alert_ts[0] if task.reported_alert_ts else None,
            },
            "error": task.error,
        }

    # -- 恢复 ---------------------------------------------------------

    def recover_on_startup(self) -> None:
        """把中断任务置 failed / 已完成任务重建为 done；查询无副作用。"""
        current = now_ms()
        for manifest_path, data in scan_manifests(self.base_dir, current, include_expired=True):
            record = record_from_manifest(data)
            if record is None:
                continue
            state = str(data.get("state") or "")
            if state in ("received", "extracted", "judged"):
                failed = dataclasses.replace(record, state="failed")
                try:
                    write_manifest(
                        manifest_path,
                        failed,
                        reported_alert_ids=data.get("reported_alert_ids") or [],
                        reported_alert_ts=data.get("reported_alert_ts") or [],
                        final_event_type=data.get("final_event_type") or "normal",
                        roi_source=data.get("roi_source") or record.roi_source,
                    )
                except OSError:
                    continue
                log.info("upload_recovered upload=%s state=failed", record.upload_id)
                record = failed
            reported = [int(value) for value in (data.get("reported_alert_ids") or [])]
            task = UploadTask(
                task_id=task_id_of(record),
                upload_id=record.upload_id,
                device_id=record.device_id,
                state=_task_state_for(record.state),
                stage="done" if record.state == "reported" else record.state,
                progress=100 if record.state == "reported" else 0,
                frames_total=int(record.frames),
                frames_done=0,
                feature_mode=record.feature_mode,
                error=(
                    {"code": "UP_INTERNAL", "message": "上次处理被中断，请重新上传"}
                    if record.state == "failed"
                    else None
                ),
                updated_ms=int(record.received_ms),
                started_ms=int(record.received_ms),
                reported_alert_ts=reported,
            )
            with self._lock:
                self._tasks.setdefault(task.task_id, task)

    # -- 任务执行 -----------------------------------------------------

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return
        started = now_ms()
        task = dataclasses.replace(
            task, state="running", stage="probe", progress=5, started_ms=started, updated_ms=now_ms()
        )
        self._store(task)
        deadline = time.monotonic() + _timeout_sec()
        try:
            record = self._load_record(task.upload_id)
            record = self.run_extract(record, task_id, deadline)
            record, decisions = self.run_judge(record, task_id, deadline)
            self.run_report(record, decisions, task_id)
            with self._lock:
                task = self._tasks.get(task_id)
                if task is not None:
                    self._tasks[task_id] = dataclasses.replace(
                        task,
                        state="done",
                        stage="done",
                        progress=100,
                        frames_done=int(task.frames_total),
                        updated_ms=now_ms(),
                    )
        except UploadTimeout as exc:
            self._fail(task_id, "UP_TIMEOUT", "视频处理超时", stage="extract", rows=exc.rows)
        except UploadError as exc:
            self._fail(task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.error("upload_task_failed task=%s err=%s", task_id, type(exc).__name__)
            self._fail(task_id, "UP_INTERNAL", "处理异常，已保留现场")

    def _store(self, task: UploadTask) -> None:
        with self._lock:
            self._tasks[task.task_id] = task

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            changes.setdefault("updated_ms", now_ms())
            self._tasks[task_id] = dataclasses.replace(task, **changes)

    def _fail(
        self,
        task_id: str,
        code: str,
        message: str,
        stage: Optional[str] = None,
        rows: Optional[List[VisionOut]] = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return
        self._update_task(
            task_id,
            state="failed",
            stage=stage or task.stage,
            error={"code": str(code), "message": str(message)},
        )
        if rows:
            try:
                record = self._load_record(task.upload_id)
                write_vision_json(self._dir(record) / VISION_NAME, rows)
            except Exception:  # noqa: BLE001
                pass
        try:
            record = self._load_record(task.upload_id)
            self._write_state(record, "failed", task_id=task_id)
        except Exception:  # noqa: BLE001
            pass
        log.warning("upload_task_failed upload=%s code=%s", task.upload_id, code)

    def _dir(self, record: UploadRecord) -> Path:
        return upload_dir(record.upload_id, self.base_dir)

    def _load_record(self, upload_id: str) -> UploadRecord:
        manifest_path = upload_dir(upload_id, self.base_dir) / "upload.json"
        data = read_manifest(manifest_path)
        if data is None:
            raise UploadError(500, "UP_INTERNAL", "上传记录丢失")
        record = _record_from_manifest(data)
        if record is None:
            raise UploadError(500, "UP_INTERNAL", "上传记录损坏")
        return record

    def _write_state(
        self,
        record: UploadRecord,
        state: str,
        decisions: Optional[List[Decision]] = None,
        task_id: Optional[str] = None,
    ) -> UploadRecord:
        manifest_path = self._dir(record) / "upload.json"
        existing = read_manifest(manifest_path) or {}
        ids = [int(value) for value in (existing.get("reported_alert_ids") or [])]
        ts_list = [int(value) for value in (existing.get("reported_alert_ts") or [])]
        final = str(existing.get("final_event_type") or "normal")
        if decisions:
            final = str(decisions[-1].event_type) if decisions else "normal"
        updated = dataclasses.replace(record, state=state)
        write_manifest(
            manifest_path,
            updated,
            reported_alert_ids=ids,
            reported_alert_ts=ts_list,
            final_event_type=final,
            roi_source=updated.roi_source,
        )
        return updated

    # -- 阶段 1：特征提取 ---------------------------------------------

    def run_extract(self, record: UploadRecord, task_id: str, deadline: float) -> UploadRecord:
        self._update_task(task_id, stage="extract", progress=10)
        directory = self._dir(record)
        source = directory / SOURCE_NAME
        if not source.is_file():
            raise UploadError(500, "UP_INTERNAL", "源视频缺失")

        mode = record.feature_mode
        kpts_sidecar = directory / "source_kpts.jsonl"
        if kpts_sidecar.is_file():
            try:
                load_kpts_sidecar(kpts_sidecar, record.frames)
                mode = "pose"
            except (ValueError, PoseUnavailable) as exc:
                raise UploadError(
                    422, "UP_NOT_DECODABLE", "关键点侧车不合法", reason=type(exc).__name__
                )

        vision_path = directory / VISION_NAME
        part_path = directory / VISION_PART_NAME
        for leftover in (vision_path, part_path):
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass

        def progress(done: int, total: int) -> None:
            ratio = 10 + int(80.0 * float(done) / float(max(1, total)))
            self._update_task(
                task_id,
                frames_done=int(done),
                progress=max(10, min(90, ratio)),
            )

        try:
            result = extract_rows(
                mode=mode,
                video_path=source,
                start_ts_ms=record.start_ts_ms,
                door_roi=record.door_roi,
                fps=record.fps,
                frames_total=record.frames,
                deadline=deadline,
                progress=progress,
                part_path=part_path,
            )
        except UploadTimeout:
            raise
        except PoseUnavailable:
            result = extract_rows(
                mode="heuristic",
                video_path=source,
                start_ts_ms=record.start_ts_ms,
                door_roi=record.door_roi,
                fps=record.fps,
                frames_total=record.frames,
                deadline=deadline,
                progress=progress,
                part_path=part_path,
            )
        except UploadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UploadError(
                422, "UP_NOT_DECODABLE", "视频无法解码，请换 MP4(H.264)", reason=type(exc).__name__
            )

        rows = result.rows
        write_vision_json(vision_path, rows)
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        updated = dataclasses.replace(
            record,
            feature_mode=str(result.mode),
            keypoints_ok=(result.mode == "pose"),
            roi_source=str(result.roi_source),
        )
        updated = self._write_state(updated, "extracted")
        self._update_task(task_id, progress=90, frames_done=len(rows), updated_ms=now_ms())
        return updated

    # -- 阶段 2：判决 -------------------------------------------------

    def run_judge(
        self, record: UploadRecord, task_id: str, deadline: float
    ) -> Tuple[UploadRecord, List[Decision]]:
        self._update_task(task_id, stage="judge", progress=92)
        directory = self._dir(record)
        vision_path = directory / VISION_NAME
        try:
            rows = load_vision_sequence(vision_path)
            validate_vision_shape(rows, record.frames, record.start_ts_ms)
            if record.feature_mode == "heuristic":
                validate_heuristic_vision(rows)
        except Exception as exc:  # noqa: BLE001
            raise UploadError(500, "UP_INTERNAL", "视觉产物校验失败", reason=type(exc).__name__)

        cfg = build_runtime_config(record)
        decisions = FusionRunner(cfg).run(rows, [])
        if not decisions:
            raise UploadError(500, "UP_INTERNAL", "判决未产出")
        validate_time_window(decisions, record.start_ts_ms, record.duration_ms)
        decisions = [
            decorate_reason(item, record.feature_mode, record.roi_source)
            for item in decisions
        ]
        write_decisions(directory / DECISIONS_NAME, decisions)
        updated = self._write_state(record, "judged", decisions=decisions)
        return updated, decisions

    # -- 阶段 3：边沿上报 ---------------------------------------------

    def run_report(
        self, record: UploadRecord, decisions: List[Decision], task_id: str
    ) -> UploadRecord:
        self._update_task(task_id, stage="report", progress=97)
        directory = self._dir(record)
        manifest_path = directory / "upload.json"
        existing = read_manifest(manifest_path) or {}
        ids = [int(value) for value in (existing.get("reported_alert_ids") or [])]
        ts_list = [int(value) for value in (existing.get("reported_alert_ts") or [])]
        edges = edge_alerts(decisions)

        if _report_enabled() and self.db is not None and self.cache is not None:
            for sequence, decision in edges:
                if sequence < len(ids):
                    continue
                key = "upload:%s:%d" % (record.upload_id, sequence)
                if not self.cache.setnx(key, max(60, ttl_ms() // 1000)):
                    continue
                payload = asdict(decision)
                alert_id = int(self.db.insert_alert(payload))
                ids.append(alert_id)
                ts_list.append(int(decision.ts))
                self.db.upsert_device(record.device_id, int(record.received_ms))
                try:
                    self.cache.set_state(
                        record.device_id, payload, getattr(config, "STATE_TTL_SEC", 60)
                    )
                except Exception:  # noqa: BLE001
                    pass
                log.info(
                    "upload_alert upload=%s alert=%s urgency=%s event=%s",
                    record.upload_id,
                    alert_id,
                    decision.urgency,
                    decision.event_type,
                )

        final_event = str(decisions[-1].event_type) if decisions else "normal"
        updated = dataclasses.replace(record, state="reported")
        write_manifest(
            manifest_path,
            updated,
            reported_alert_ids=ids,
            reported_alert_ts=ts_list,
            final_event_type=final_event,
            roi_source=updated.roi_source,
        )
        if final_event != "normal" or ids:
            try:
                from . import camera as camera_svc

                camera_svc.invalidate_sources()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                self._tasks[task_id] = dataclasses.replace(
                    task,
                    reported_alert_ts=ts_list,
                    frames_total=int(task.frames_total),
                    updated_ms=now_ms(),
                )
        return updated


# ---------------------------------------------------------------- 进程级入口


_MANAGER: Optional[UploadTaskManager] = None
_MANAGER_LOCK = threading.Lock()


def start(db: Any, cache: Any, base_dir: Optional[Path] = None) -> UploadTaskManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = UploadTaskManager(base_dir)
        _MANAGER.start(db, cache)
        return _MANAGER


def stop() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        manager = _MANAGER
        _MANAGER = None
    if manager is not None:
        manager.stop()


def manager() -> UploadTaskManager:
    if _MANAGER is None:
        raise UploadError(503, "UP_BUSY", "上传服务尚未就绪")
    return _MANAGER


def enqueue(record: UploadRecord, created: bool = True) -> Accepted:
    return manager().enqueue(record, created=created)


def task_payload(task_id: str) -> Dict[str, Any]:
    return manager().task_payload(task_id)


def reset_state() -> None:
    """test-only：停 worker 并清空全局实例。"""
    stop()