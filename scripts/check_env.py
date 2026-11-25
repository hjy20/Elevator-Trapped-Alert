"""
环境自检：python scripts/check_env.py

检查 4 项：
  ① Python 版本（契约要求 3.10/3.11）
  ② 关键依赖是否可 import（RDS/Redis 驱动缺失只警告，因为本地模式用不到）
  ③ 18080 端口状态（服务没启动只警告）
  ④ common/demo_data 演示数据（A 的产出，D7 前缺失属正常，只警告）

只有 ① 严重不符或 ② 核心依赖缺失时才以退出码 1 结束。
"""
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

OK, WARN, FAIL = "[OK]  ", "[WARN]", "[FAIL]"
problems = 0


def line(flag: str, msg: str) -> None:
    print(f"{flag} {msg}")


def check_python() -> None:
    global problems
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) in ((3, 10), (3, 11)):
        line(OK, f"Python {ver}（与契约 §7.4 一致）")
    elif (v.major, v.minor) == (3, 9):
        line(WARN, f"Python {ver}：低于契约 3.10/3.11，但本地兜底依赖已兼容，服务端功能可跑")
    elif (v.major, v.minor) == (3, 12):
        line(WARN, f"Python {ver} 可用，契约要求 3.10/3.11，提交前建议统一")
    elif (v.major, v.minor) >= (3, 13):
        line(WARN, f"Python {ver}：团队 lock 无 3.13+ 预编译包，兼容装法见 docs/C_SERVICE_SETUP.md")
        line("      ", "服务端功能不受影响；A 的视觉脚本建议改用 Python 3.11 运行")
    else:
        line(FAIL, f"Python {ver} 过低，请安装 3.10 或 3.11")
        problems += 1


def check_deps() -> None:
    global problems
    core = ["fastapi", "uvicorn", "pydantic"]
    missing_core = []
    for name in core:
        try:
            __import__(name)
        except ImportError:
            missing_core.append(name)
    if missing_core:
        line(FAIL, "核心依赖缺失：" + ", ".join(missing_core))
        line("      ", "请先激活虚拟环境，再 pip install -r requirements.lock.txt")
        problems += 1
    else:
        line(OK, "核心依赖：fastapi / uvicorn / pydantic")

    optional = {"mysql.connector": "mysql-connector-python", "redis": "redis", "cv2": "opencv-python"}
    for module, pkg in optional.items():
        try:
            __import__(module)
        except ImportError:
            line(WARN, f"{pkg} 未安装（仅 DB_MODE=rds / CACHE_MODE=redis / 回放视频时需要）")


def check_port(port: int = 18080) -> None:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        line(WARN, f"端口 {port} 已被占用（服务可能已在运行，属正常）")
    except OSError:
        line(OK, f"端口 {port} 可用")
    finally:
        s.close()


def check_demo_data() -> None:
    demo_dir = BASE_DIR / "common" / "demo_data"
    videos = list(demo_dir.glob("*.mp4")) if demo_dir.exists() else []
    if videos:
        line(OK, f"演示数据：{len(videos)} 个 mp4")
    else:
        line(WARN, "common/demo_data 下暂无演示数据（A 的产出，D7 前缺失属正常）")


def check_modes() -> None:
    try:
        from service import config as cfg
    except Exception as exc:  # noqa: BLE001
        line(WARN, f"读取 .env 失败（不影响启动，将使用默认值）：{exc}")
        return
    line(OK, f"运行模式：DB_MODE={cfg.DB_MODE}  CACHE_MODE={cfg.CACHE_MODE}  PORT={cfg.PORT}")
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        line(WARN, ".env 不存在（将使用默认 sqlite + memory，可复制 .env.example 生成）")


def main() -> int:
    print("=" * 62)
    print("  电梯困人主动预警 · 环境自检")
    print(f"  项目目录：{BASE_DIR}")
    print("=" * 62)
    check_python()
    check_deps()
    check_port()
    check_demo_data()
    check_modes()
    print("-" * 62)
    if problems:
        print(f"发现 {problems} 个必须解决的问题，见上方 [FAIL]。")
        return 1
    print("自检通过，可以启动：python -m service.main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
