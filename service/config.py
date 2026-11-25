"""
配置读取：只用标准库手写解析 .env，不引入额外依赖。
优先级：真实环境变量 > .env 文件 > 代码默认值。
"""
import os
from pathlib import Path

# 项目根目录（service/ 的上一层），所有路径都基于它，禁止写绝对路径
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """把 .env 里的键值塞进 os.environ（不覆盖已存在的真实环境变量）。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file()


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return default


DB_MODE = _s("DB_MODE", "sqlite").lower() or "sqlite"        # rds | sqlite
CACHE_MODE = _s("CACHE_MODE", "memory").lower() or "memory"   # redis | memory
PORT = _i("PORT", 18080)

RDS_HOST = _s("RDS_HOST")
RDS_PORT = _i("RDS_PORT", 3306)
RDS_USER = _s("RDS_USER")
RDS_PASSWORD = _s("RDS_PASSWORD")
RDS_DB = _s("RDS_DB", "elevator")

REDIS_HOST = _s("REDIS_HOST")
REDIS_PORT = _i("REDIS_PORT", 6379)
REDIS_PASSWORD = _s("REDIS_PASSWORD")

# 业务常量（看板与统计共用）
DEVICE_OFFLINE_MS = 120_000      # 2 分钟没上报视为设备离线
DEDUP_WINDOW_SEC = 30            # 同设备同紧急度 30s 内去重
STATE_TTL_SEC = 60               # 最新状态缓存 60s

DASHBOARD_DIR = BASE_DIR / "dashboard"
DB_SQLITE_PATH = BASE_DIR / "elevator.db"

# 现场画面（摄像头视图）开关与源模式（camera.md 2.2 C-CAM-09）
CAMERA_ENABLE = _s("CAMERA_ENABLE", "1")                      # "0"/"false" 视为关闭
CAMERA_SOURCE_MODE = (_s("CAMERA_SOURCE_MODE", "demo").lower() or "demo")  # demo|file|usb|rtsp
