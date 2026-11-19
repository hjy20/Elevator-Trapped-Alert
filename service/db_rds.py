"""
华为云 RDS for MySQL 适配层（D4-D6 开通后启用：DB_MODE=rds）。

★ 与 db_local.py 函数签名完全一致，main.py 只靠环境变量切换。
★ 任何连接异常都向上抛，由 main.py 捕获后自动降级到 SQLite 并打日志——
   这是 D8-D10「RDS 连不上也能演示」的关键设计，不要在这里 try/except 吞掉异常。
"""
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from . import config

# 字段名与顺序和 DDL_SQLITE 一一对应，只有方言不同：
#   SQLite: INTEGER PRIMARY KEY AUTOINCREMENT / REAL
#   MySQL : BIGINT AUTO_INCREMENT PRIMARY KEY / FLOAT
DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS alerts (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts           BIGINT  NOT NULL,
  device_id    VARCHAR(64)  NOT NULL,
  trapped      TINYINT(1)   NOT NULL DEFAULT 0,
  urgency      INT         NOT NULL DEFAULT 0,
  location     VARCHAR(16),
  people_count INT          DEFAULT 0,
  event_type   VARCHAR(16),
  confidence   FLOAT        DEFAULT 0,
  reason       TEXT,
  status       VARCHAR(16)  DEFAULT 'active',
  ack_ts       BIGINT      NULL,
  INDEX idx_status_ts (status, ts),
  INDEX idx_device (device_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS devices (
  device_id     VARCHAR(64) PRIMARY KEY,
  last_seen     BIGINT,
  model_version VARCHAR(32)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _today_start_ms() -> int:
    n = datetime.now()
    return int(datetime(n.year, n.month, n.day).timestamp() * 1000)


def _conn():
    return mysql.connector.connect(
        host=config.RDS_HOST,
        port=config.RDS_PORT,
        user=config.RDS_USER,
        password=config.RDS_PASSWORD,
        database=config.RDS_DB,
        connection_timeout=5,
        autocommit=True,
        charset="utf8mb4",
    )


def init_db() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        for statement in filter(None, (s.strip() for s in DDL_MYSQL.split(";"))):
            cur.execute(statement)
        cur.close()
    finally:
        conn.close()


def insert_alert(d: Dict[str, Any]) -> int:
    sql = """INSERT INTO alerts
             (ts, device_id, trapped, urgency, location, people_count,
              event_type, confidence, reason, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')"""
    args = (
        int(d["ts"]),
        d["device_id"],
        int(bool(d.get("trapped", False))),
        int(d.get("urgency", 0)),
        d.get("location") or "?",
        int(d.get("people_count", 0)),
        d.get("event_type", "normal"),
        float(d.get("confidence", 0.0)),
        json.dumps(d.get("reason") or [], ensure_ascii=False),
    )
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        new_id = int(cur.lastrowid)
        cur.close()
        return new_id
    finally:
        conn.close()


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    raw = item.get("reason")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    try:
        item["reason"] = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        item["reason"] = []
    item["trapped"] = bool(item.get("trapped"))
    return item


def list_alerts(status: str = "active", limit: int = 100) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        if status == "all":
            cur.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT %s", (limit,))
        else:
            cur.execute(
                "SELECT * FROM alerts WHERE status=%s ORDER BY ts DESC LIMIT %s",
                (status, limit),
            )
        rows = cur.fetchall()
        cur.close()
        return [_normalize(r) for r in rows]
    finally:
        conn.close()


def ack_alert(alert_id: int) -> bool:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE alerts SET status='acked', ack_ts=%s WHERE id=%s AND status='active'",
            (now_ms(), alert_id),
        )
        affected = cur.rowcount
        cur.close()
        return affected > 0
    finally:
        conn.close()


def upsert_device(device_id: str, ts: int, model_version: Optional[str] = None) -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO devices (device_id, last_seen, model_version)
               VALUES (%s,%s,%s)
               ON DUPLICATE KEY UPDATE last_seen = VALUES(last_seen)""",
            (device_id, int(ts), model_version),
        )
        cur.close()
    finally:
        conn.close()


def list_devices() -> List[Dict[str, Any]]:
    now = now_ms()
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM devices ORDER BY last_seen DESC")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    out = []
    for r in rows:
        item = dict(r)
        item["online"] = (now - int(item.get("last_seen") or 0)) < config.DEVICE_OFFLINE_MS
        out.append(item)
    return out


def stats() -> Dict[str, Any]:
    today = _today_start_ms()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM alerts WHERE ts >= %s", (today,))
        today_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM alerts WHERE status='active'")
        active_count = cur.fetchone()[0]
        cur.execute(
            """SELECT AVG((ack_ts - ts)/1000.0) FROM alerts
               WHERE ack_ts IS NOT NULL
                 AND ack_ts >= ts
                 AND (ack_ts - ts) <= 86400000"""
        )
        avg_row = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM devices")
        devices_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM devices WHERE last_seen >= %s", (now_ms() - config.DEVICE_OFFLINE_MS,))
        devices_online = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()
    return {
        "today_total": int(today_total or 0),
        "active_count": int(active_count or 0),
        "avg_ack_seconds": round(float(avg_row or 0.0), 1),
        "devices_total": int(devices_total or 0),
        "devices_online": int(devices_online or 0),
    }


__all__ = [
    "DDL_MYSQL",
    "MySQLError",
    "ack_alert",
    "init_db",
    "insert_alert",
    "list_alerts",
    "list_devices",
    "now_ms",
    "stats",
    "upsert_device",
]
