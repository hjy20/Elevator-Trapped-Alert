"""
端口检查：python scripts/check_ports.py
退出码 0 = 18080 可用；1 = 被占用（附占用进程提示）。
"""
import socket
import sys

PORT = 18080


def is_free(port: int = PORT) -> bool:
    s = socket.socket()
    s.settimeout(0.8)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main() -> int:
    if is_free():
        print(f"[OK]   {PORT} 可用，可以启动服务")
        return 0
    print(f"[FAIL] {PORT} 已被占用，服务无法启动")
    print("       处理办法（任选其一）：")
    print("       1) 之前启动的服务没关 → 找到那个窗口按 Ctrl+C")
    print(f"       2) Windows 查占用进程：netstat -ano | findstr :{PORT}  然后 taskkill /PID <PID> /F")
    print("       3) Git Bash：netstat -ano | grep 18080")
    return 1


if __name__ == "__main__":
    sys.exit(main())
