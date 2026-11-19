"""
内存缓存实现（CACHE_MODE=memory）—— 本地开发与断网演示兜底。

与 cache_redis.py 保持同样 4 个函数：
    init() / setnx() / set_state() / get_state()
"""
import time
from typing import Any, Dict, Optional

_EXP: Dict[str, float] = {}
_DATA: Dict[str, Any] = {}


def init() -> None:
    _EXP.clear()
    _DATA.clear()


def setnx(key: str, ttl: int) -> bool:
    """key 不存在则占用并设过期时间，返回 True（=首次上报，应入库）。
    已存在且未过期返回 False（=重复上报，去重丢弃）。"""
    now = time.time()
    expire_at = _EXP.get(key)
    if expire_at is not None and expire_at > now:
        return False
    _EXP[key] = now + ttl
    _DATA[key] = 1
    return True


def set_state(device_id: str, payload: Dict[str, Any], ttl: int = 60) -> None:
    _set_json(f"state:{device_id}", payload, ttl)


def get_state(device_id: str) -> Optional[Dict[str, Any]]:
    return _get_json(f"state:{device_id}")


def _set_json(key: str, value: Any, ttl: int) -> None:
    _DATA[key] = value
    _EXP[key] = time.time() + ttl


def _get_json(key: str) -> Optional[Any]:
    if _EXP.get(key, 0) < time.time():
        _DATA.pop(key, None)
        _EXP.pop(key, None)
        return None
    return _DATA.get(key)
