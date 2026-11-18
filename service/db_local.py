"""
SQLite 实现（本地开发 / 答辩断网兜底）。

★ 与 db_rds.py 必须保持完全相同的 5 个函数签名，main.py 才能靠 DB_MODE 一行切换：
    init_db() / insert_alert() / list_alerts() / ack_alert() / upsert_device()
   以及看板要用的 stats() / list_devices()
"""
import json
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config

DB_PATH = config.DB_SQLITE_PATH

# 字段语义与 db_rds.py 的 DDL_MYSQL 一一对应（方言不同，字段名和顺序相同）
DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS alerts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  device_id    TEXT    NOT NULL,
  trapped      INTEGER NOT NULL DEFAULT 0,
  urgency      INTEGER NOT NULL DEFAULT 0,
  location     TEXT,
  people_count INTEGER DEFAULT 0,
  event_type   TEXT,
  confidence   REAL    DEFAULT 0,
  reason       TEXT,
  status       TEXT    DEFAULT 'active',
  ack_ts       INTEGER
);

CREATE TABLE IF NOT EXISTS devices (
  device_id     TEXT PRIMARY KEY,
  last_seen     INTEGER,
  model_version TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_status_ts ON alerts(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_device    ON alerts(device_id, ts DESC);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _today_start_ms() -> int:
    n = datetime.now()
    return int(datetime(n.year, n.month, n.day).timestamp() * 1000)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(DDL_SQLITE)


def insert_alert(d: Dict[str, Any]) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO alerts
               (ts, device_id, trapped, urgency, location, people_count,
                event_type, confidence, reason, status)
               VALUES (?,?,?,?,?,?,?,?,?, 'active')""",
            (
                int(d["ts"]),
                d["device_id"],
                int(bool(d.get("trapped", False))),
                int(d.get("urgency", 0)),
                d.get("location") or "?",
                int(d.get("people_count", 0)),
                d.get("event_type", "normal"),
                float(d.get("confidence", 0.0)),
                json.dumps(d.get("reason") or [], ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["reason"] = json.loads(item.get("reason") or "[]")
    except (json.JSONDecodeError, TypeError):
        item["reason"] = []
    item["trapped"] = bool(item.get("trapped"))
    return item


def list_alerts(status: str = "active", limit: int = 100) -> List[Dict[str, Any]]:
    if status == "all":
        sql, args = "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
    else:
        sql, args = "SELECT * FROM alerts WHERE status = ? ORDER BY ts DESC LIMIT ?", (status, limit)
    with _conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def ack_alert(alert_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE alerts SET status='acked', ack_ts=? WHERE id=? AND status='active'",
            (now_ms(), alert_id),
        )
        return cur.rowcount > 0


def upsert_device(device_id: str, ts: int, model_version: Optional[str] = None) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO devices (device_id, last_seen, model_version)
               VALUES (?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET last_seen = excluded.last_seen""",
            (device_id, int(ts), model_version),
        )


def list_devices() -> List[Dict[str, Any]]:
    now = now_ms()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["online"] = (now - int(item.get("last_seen") or 0)) < config.DEVICE_OFFLINE_MS
        out.append(item)
    return out


def stats() -> Dict[str, Any]:
    """看板顶部三个数字卡 + 设备在线情况。"""
    today = _today_start_ms()
    with _conn() as conn:
        today_total = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts >= ?", (today,)
        ).fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='active'"
        ).fetchone()[0]
        avg_row = conn.execute(
            """SELECT AVG((ack_ts - ts) / 1000.0) FROM alerts
               WHERE ack_ts IS NOT NULL
                 AND ack_ts >= ts
                 AND (ack_ts - ts) <= 86400000"""
        ).fetchone()[0]
        devices_total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        devices_online = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE last_seen >= ?",
            (now_ms() - config.DEVICE_OFFLINE_MS,),
        ).fetchone()[0]
    return {
        "today_total": int(today_total),
        "active_count": int(active_count),
        "avg_ack_seconds": round(float(avg_row or 0.0), 1),
        "devices_total": int(devices_total),
        "devices_online": int(devices_online),
    }


if __name__ == "__main__":
    # 单独自测：python -m service.db_local
    init_db()
    demo = {
        "ts": now_ms(),
        "device_id": "ELEV-DEMO-001",
        "trapped": True,
        "urgency": 3,
        "location": "3F",
        "people_count": 2,
        "event_type": "faint",
        "confidence": 0.91,
        "reason": ["self-test"],
    }
    new_id = insert_alert(demo)
    upsert_device(demo["device_id"], demo["ts"])
    print(f"inserted id={new_id}")
    print("active:", len(list_alerts("active")))
    print("stats:", stats())
