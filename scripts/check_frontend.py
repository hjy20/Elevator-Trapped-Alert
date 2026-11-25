"""前端静态验收（只读，不修改 dashboard/）。

用法：
  python scripts/check_frontend.py --baseline   # 打印当前快照（行数/字节/关键计数）
  python scripts/check_frontend.py --strict     # 全部断言，任一失败退出 1

对应 docs/spec/frontend.md 第 5、7 章的 CMD-UI 断言。
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
PAGE = DASH / "index.html"

MAX_BYTES = 48 * 1024
API_ALLOWED = ("/api/v1/alerts", "/api/v1/stats", "/api/v1/devices", "/api/v1/camera/")


def _read() -> str:
    return PAGE.read_text(encoding="utf-8")


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def _color(text: str, name: str) -> str:
    match = re.search(r"--%s:\s*(#[0-9a-fA-F]{6})" % re.escape(name), text)
    if not match:
        raise SystemExit(f"缺少颜色令牌 --{name}")
    return match.group(1).lower()


def _rgb(hex_color: str):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex_color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    high, low = max(la, lb), min(la, lb)
    return round((high + 0.05) / (low + 0.05), 2)


def baseline(text: str) -> None:
    counts = {
        "行数": len(text.splitlines()),
        "字节": len(text.encode("utf-8")),
        "外链": _count(r"https?://|<link|<img", text),
        "@media": _count(r"@media", text),
        "aria/role": _count(r"aria-|role=", text),
        "/api/v1": _count(r"/api/v1", text),
        "fetch": _count(r"fetch\(", text),
        "innerHTML": _count(r"innerHTML", text),
    }
    print("基线快照")
    for name, value in counts.items():
        print(f"  {name:<10} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="前端静态验收（只读）")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if not PAGE.exists():
        print(f"[FAIL] 找不到 {PAGE}")
        return 1
    text = _read()

    if args.baseline or not args.strict:
        baseline(text)
        if not args.strict:
            return 0

    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))

    print("严格断言")
    check("零外链（无 http(s)/link/img）", _count(r"https?://|<link|<img", text) == 0)
    check("体积 ≤ 48 KB", len(text.encode("utf-8")) <= MAX_BYTES, f"{len(text.encode('utf-8'))} B")
    check("dashboard 仅静态页、无 api/docs 子目录",
          len([p for p in DASH.iterdir() if p.is_file()]) <= 3
          and not (DASH / "api").exists() and not (DASH / "docs").exists())
    found = set(re.findall(r'"(/api/v1/[a-z/]+)', text))
    check("接口路径在白名单内",
          all(any(path.startswith(prefix) for prefix in API_ALLOWED) for path in found),
          str(sorted(found)))
    check("两档断点 @media ≥ 2", _count(r"@media", text) >= 2, f"{_count(r'@media', text)}")
    check("reduced-motion", _count(r"prefers-reduced-motion", text) >= 1)
    check("aria/role ≥ 6", _count(r"aria-|role=", text) >= 6, f"{_count(r'aria-|role=', text)}")
    check("aria-live", _count(r"aria-live", text) >= 1)
    check("focus-visible", _count(r"focus-visible", text) >= 1)
    check("AbortController", _count(r"AbortController", text) >= 1)
    check("visibilitychange", _count(r"visibilitychange", text) >= 1)
    check("innerHTML ≤ 1", _count(r"innerHTML", text) <= 1, f"{_count(r'innerHTML', text)}")
    check("lastIds 读取（has/delete）", _count(r"lastIds\.(has|delete)", text) >= 1)
    check("requestFullscreen", _count(r"requestFullscreen", text) >= 1)
    check("告警字段 device_id", _count(r"device_id", text) >= 1)
    check("告警字段 trapped", _count(r"trapped", text) >= 1)
    check("缺省值 位置未知", _count(r"位置未知", text) >= 1)
    check("过期提示 数据已过期", _count(r"数据已过期", text) >= 1)
    check("降级文案 兜底 ≥ 2", _count(r"兜底", text) >= 2, f"{_count(r'兜底', text)}")
    check("二次确认 再点一次确认", _count(r"再点一次确认", text) >= 1)
    check("404 文案 该告警已被处置", _count(r"该告警已被处置", text) >= 1)
    check("画面 <video> 恰 1 个", _count(r"<video", text) == 1, f"{_count(r'<video', text)}")
    check("画面叠加层 <canvas>", _count(r"<canvas", text) >= 1, f"{_count(r'<canvas', text)}")
    check("画面水印 演示数据 · 非取证", "演示数据 · 非取证" in text and 'class="cam-wm"' in text)
    check("媒体同源前缀 /api/v1/camera/", "/api/v1/camera/" in text)

    try:
        bg, panel = _color(text, "bg"), _color(text, "panel")
        text_c, muted = _color(text, "text"), _color(text, "muted")
        checks = {
            "正文/面板": contrast(text_c, panel),
            "正文/背景": contrast(text_c, bg),
            "辅助/面板": contrast(muted, panel),
            "辅助/背景": contrast(muted, bg),
        }
        for label, ratio in checks.items():
            check(f"对比度 {label} ≥ 4.5", ratio >= 4.5, f"{ratio}:1")
    except SystemExit as exc:
        check("颜色令牌可解析", False, str(exc))

    passed, total = sum(results), len(results)
    print(f"结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())