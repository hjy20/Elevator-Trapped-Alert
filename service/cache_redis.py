"""
华为云 Redis 适配层（CACHE_MODE=redis）。

两个用途（契约与任务卡 D4-D6 明确要求）：
1. 告警去重：key = alert:{device_id}:{urgency}，SETNX + 30s TTL
   → 同设备 30 秒内相同紧急度重复上报，只保留一条
2. 最新状态缓存：key = state:{device_id}，存最近一次 Decision，TTL 60s
   → 看板读缓存，不打数据库

★ 连接失败必须抛异常，由 main.py 降级到内存实现，不要吞。
"""
import json
from typing import Any, Dict, Optional

import redis

from . import config

_client: Optional[redis.Redis] = None


def init() -> None:
    global _client
    _client = redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        password=config.REDIS_PASSWORD or None,
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )
    _client.ping()  # 连不上会直接抛异常


def _c() -> redis.Redis:
    if _client is None:
        init()
    assert _client is not None
    return _client


def setnx(key: str, ttl: int) -> bool:
    """SET key 1 NX EX ttl → True 表示抢到了（首次），False 表示 30s 内重复。"""
    return bool(_c().set(key, "1", nx=True, ex=ttl))


def set_state(device_id: str, payload: Dict[str, Any], ttl: int = 60) -> None:
    _c().setex(f"state:{device_id}", ttl, json.dumps(payload, ensure_ascii=False))


def get_state(device_id: str) -> Optional[Dict[str, Any]]:
    raw = _c().get(f"state:{device_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
