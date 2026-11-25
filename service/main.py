"""
电梯困人主动预警 · 服务端（C 负责）

启动：python -m service.main      → http://127.0.0.1:18080/docs
双模式：
  DB_MODE=rds|sqlite      云端用 rds，本地/断网用 sqlite（连不上 RDS 自动降级）
  CACHE_MODE=redis|memory 云端用 redis，本地/断网用 memory（连不上 Redis 自动降级）

接口（契约 §7.2）：
  GET  /api/v1/health
  POST /api/v1/edge/report
  GET  /api/v1/alerts?status=active
  POST /api/v1/alerts/{id}/ack
看板辅助接口：
  GET  /api/v1/stats
  GET  /api/v1/devices
"""
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional

from . import config
from .db_local import now_ms
from .models import DecisionIn
from . import camera as camera_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("elevator")

# 运行时状态（避免在函数里用 global 满天飞）
STATE: Dict[str, Any] = {
    "db": None,
    "cache": None,
    "db_mode": "none",
    "cache_mode": "none",
    "db_error": "",
    "cache_error": "",
}


def _load_db() -> None:
    """优先 RDS，失败自动降级 SQLite —— 云端断库也能继续演示。"""
    if config.DB_MODE == "rds":
        try:
            from . import db_rds as mod

            mod.init_db()
            STATE.update(db=mod, db_mode="rds", db_error="")
            log.info("数据层：华为云 RDS (MySQL) 已连接")
            return
        except Exception as exc:  # noqa: BLE001 连接/建表任何异常都降级
            STATE["db_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("RDS 连接失败，自动降级到 SQLite：%s", STATE["db_error"])
    from . import db_local as mod

    mod.init_db()
    STATE.update(db=mod, db_mode="sqlite")
    log.info("数据层：SQLite（本地兜底）%s", config.DB_SQLITE_PATH.name)


def _load_cache() -> None:
    if config.CACHE_MODE == "redis":
        try:
            from . import cache_redis as mod

            mod.init()
            STATE.update(cache=mod, cache_mode="redis", cache_error="")
            log.info("缓存层：华为云 Redis 已连接")
            return
        except Exception as exc:  # noqa: BLE001
            STATE["cache_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("Redis 连接失败，自动降级到内存：%s", STATE["cache_error"])
    from . import cache_memory as mod

    mod.init()
    STATE.update(cache=mod, cache_mode="memory")
    log.info("缓存层：内存（本地兜底）")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("启动中… DB_MODE=%s CACHE_MODE=%s", config.DB_MODE, config.CACHE_MODE)
    _load_db()
    _load_cache()
    camera_svc.bootstrap()  # 画面源预载（幂等，每次启动只记一次「画面源载入」）
    log.info("就绪：http://127.0.0.1:%s/docs", config.PORT)
    yield
    log.info("已停止")


app = FastAPI(
    title="电梯困人主动预警 · 服务端",
    description="采集「是否有人被困」的可解释告警：楼层 / 人数 / 紧急度 / 判定理由",
    version="1.0.0",
    lifespan=lifespan,
)


def _db():
    if STATE["db"] is None:
        raise HTTPException(status_code=503, detail="数据层未就绪")
    return STATE["db"]


def _cache():
    if STATE["cache"] is None:
        raise HTTPException(status_code=503, detail="缓存层未就绪")
    return STATE["cache"]


# ---------------------------------------------------------------- 接口


@app.get("/api/v1/health", summary="健康检查")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "time": now_ms(),
        "db_mode": STATE["db_mode"],
        "cache_mode": STATE["cache_mode"],
        "degraded": bool(STATE["db_error"] or STATE["cache_error"]),
    }


@app.post("/api/v1/edge/report", summary="接收边缘端 Decision 上报")
def report(d: DecisionIn) -> Dict[str, Any]:
    db, cache = _db(), _cache()
    payload = d.model_dump()
    ts = payload["ts"] or now_ms()
    payload["ts"] = ts

    # 心跳与最新状态：无论是否告警都要更新
    db.upsert_device(payload["device_id"], ts)
    cache.set_state(payload["device_id"], payload, config.STATE_TTL_SEC)

    # urgency=0 且未困人 → 只刷新心跳，不产生告警记录（看板保持干净）
    if payload["urgency"] == 0 and not payload["trapped"]:
        return {"ok": True, "alert": False, "dedup": False, "id": None}

    # 去重：同设备同紧急度 30s 内只留一条
    is_first = cache.setnx(
        f"alert:{payload['device_id']}:{payload['urgency']}", config.DEDUP_WINDOW_SEC
    )
    if not is_first:
        log.info(
            "去重丢弃 device=%s urgency=%s", payload["device_id"], payload["urgency"]
        )
        return {"ok": True, "alert": True, "dedup": True, "id": None}

    alert_id = db.insert_alert(payload)
    log.info(
        "入库 #%s device=%s urgency=%s location=%s people=%s event=%s",
        alert_id,
        payload["device_id"],
        payload["urgency"],
        payload["location"],
        payload["people_count"],
        payload["event_type"],
    )
    return {"ok": True, "alert": True, "dedup": False, "id": alert_id}


@app.get("/api/v1/alerts", summary="查询告警列表")
def alerts(
    status: str = Query("active", description="active | acked | all"),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    return {"items": _db().list_alerts(status, limit)}


@app.post("/api/v1/alerts/{alert_id}/ack", summary="确认处置")
def ack(alert_id: int) -> Dict[str, Any]:
    if not _db().ack_alert(alert_id):
        raise HTTPException(status_code=404, detail=f"告警 {alert_id} 不存在或已处置")
    log.info("已确认处置 #%s", alert_id)
    return {"ok": True, "id": alert_id}


@app.get("/api/v1/stats", summary="看板统计")
def stats() -> Dict[str, Any]:
    data = _db().stats()
    data["db_mode"] = STATE["db_mode"]
    data["cache_mode"] = STATE["cache_mode"]
    return data


@app.get("/api/v1/devices", summary="设备在线状态")
def devices() -> Dict[str, Any]:
    return {"items": _db().list_devices()}


# ---------------------------------------------------------------- 现场画面（C · camera）
# 纯追加块：既有 6 接口零变化（camera.md C-CAM-04 / AMEND-CAM-05）。
# 必须位于 app.mount("/") 之前，否则 /camera/* 会被静态页兜住（camera.md 4.1 判据 2）。


def _camera_http_error(exc: "camera_svc.CameraError") -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


@app.get("/api/v1/camera/sources", summary="画面源清单")
def camera_sources() -> Dict[str, Any]:
    try:
        return camera_svc.sources_payload()
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)


@app.get("/api/v1/camera/video", summary="视频分发（支持 Range）")
def camera_video(request: Request, source_id: str = Query("", description="画面源 ID")):
    range_header = request.headers.get("range")
    try:
        status, headers, path, start, length = camera_svc.video_payload(source_id, range_header)
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)
    if status == 416:
        detail = camera_svc.error_detail(
            "CAM_RANGE_INVALID", "画面片段不可用", source_id=source_id
        )
        return JSONResponse(status_code=416, content={"detail": detail}, headers=headers)
    return StreamingResponse(
        camera_svc.iter_video_chunks(path, start, length),
        status_code=status,
        headers=headers,
    )


@app.get("/api/v1/camera/kpts", summary="关键点窗口 + 视觉分数")
def camera_kpts(
    source_id: str = Query("", description="画面源 ID"),
    from_frame: Optional[str] = Query(None, description="窗口起始帧"),
    count: Optional[str] = Query(None, description="返回帧数 1~120"),
    ts: Optional[str] = Query(None, description="告警墙钟毫秒"),
) -> Dict[str, Any]:
    try:
        return camera_svc.kpts_payload(
            source_id, from_frame=from_frame, count=count, ts=ts
        )
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)


@app.get("/api/v1/camera/frame", summary="单帧 JPEG（降级通道）")
def camera_frame(
    source_id: str = Query("", description="画面源 ID"),
    frame: Optional[str] = Query(None, description="帧号"),
    ts: Optional[str] = Query(None, description="素材时间毫秒"),
) -> Response:
    try:
        data, index, ts_ms = camera_svc.frame_payload(source_id, frame=frame, ts=ts)
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)
    return Response(
        content=data,
        status_code=200,
        headers={
            "Content-Type": "image/jpeg",
            "Cache-Control": "no-store",
            "X-Camera-Frame": str(index),
            "X-Camera-Ts": str(ts_ms),
        },
    )


@app.get("/api/v1/camera/health", summary="画面子系统自检")
def camera_health() -> Dict[str, Any]:
    return camera_svc.health_payload()


@app.post("/api/v1/camera/session", summary="起播会话登记（start/stop/heartbeat）")
async def camera_session_post(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON 体统一按 CAM_BAD_REQUEST 处理
        body = None
    try:
        return camera_svc.session_action(body if isinstance(body, dict) else {})
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)


@app.get("/api/v1/camera/session", summary="画面对齐状态")
def camera_session_get(source_id: str = Query("", description="画面源 ID")) -> Dict[str, Any]:
    try:
        return camera_svc.session_payload(source_id)
    except camera_svc.CameraError as exc:
        raise _camera_http_error(exc)


# ---------------------------------------------------------------- 静态页面
# ★ 必须放在所有 API 路由之后：mount 到 "/" 会兜住所有未匹配路径，
#   写太早会把 /docs 和 /openapi.json 盖掉。
if config.DASHBOARD_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
