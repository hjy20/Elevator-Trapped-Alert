"""H.264 视频写出与转码（L1 契约要求 .mp4 / H.264）。

opencv-python 的 wheel 里没有 x264 编码器（授权原因），直接 ``VideoWriter('avc1')``
经常会静默失败或报 openh264 缺失。所以这里优先用 ``imageio-ffmpeg`` 自带的
ffmpeg 二进制，把 BGR 原始帧直接灌进 stdin 编码；没有 imageio-ffmpeg 时才退回
opencv 的 mp4v（画面能播，但不是 H.264，脚本会明确告警）。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

__all__ = ["H264Writer", "ffmpeg_exe", "transcode"]


def ffmpeg_exe() -> Optional[str]:
    """找 ffmpeg：imageio-ffmpeg 自带 > PATH 里的系统 ffmpeg。"""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


class H264Writer:
    """逐帧写 H.264 mp4；用法同 ``cv2.VideoWriter``。"""

    def __init__(
        self,
        path: "str | Path",
        width: int,
        height: int,
        fps: float = 25.0,
        crf: int = 23,
        preset: str = "medium",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.width = int(width) - (int(width) % 2)      # yuv420p 要求偶数
        self.height = int(height) - (int(height) % 2)
        self.fps = float(fps)
        self._proc: Optional[subprocess.Popen] = None
        self._cv_writer = None
        self.codec = "h264"

        exe = ffmpeg_exe()
        if exe:
            cmd = [
                exe, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{self.width}x{self.height}",
                "-r", f"{self.fps}",
                "-i", "-",
                "-an",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(self.path),
            ]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            import cv2

            self.codec = "mp4v"
            self._cv_writer = cv2.VideoWriter(
                str(self.path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.width, self.height)
            )
            print(
                "[warn] 没有 ffmpeg（pip install imageio-ffmpeg），"
                "退回 mp4v 编码：能播但不是 H.264，不符合 L1 契约。"
            )

    def write(self, frame: np.ndarray) -> None:
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            import cv2

            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
        elif self._cv_writer is not None:
            self._cv_writer.write(frame)

    def close(self) -> None:
        if self._proc is not None:
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            stderr = b""
            if self._proc.stderr is not None:
                stderr = self._proc.stderr.read() or b""
            code = self._proc.wait()
            if code != 0:
                raise RuntimeError(f"ffmpeg 编码失败（exit={code}）：{stderr.decode('utf-8', 'ignore')[:400]}")
            self._proc = None
        if self._cv_writer is not None:
            self._cv_writer.release()
            self._cv_writer = None

    def __enter__(self) -> "H264Writer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def transcode(src: "str | Path", dst: "str | Path", *, fps: float = 25.0, size: "tuple[int,int] | None" = (1280, 720), crf: int = 23) -> Path:
    """把任意视频（手机 .mov 等）转成契约要求的 1280x720 / 25fps / H.264。"""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("需要 ffmpeg：pip install imageio-ffmpeg")
    src_path, dst_path = Path(src), Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    vf = []
    if size:
        vf.append(f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease")
        vf.append(f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2:color=black")
    vf.append(f"fps={fps}")
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src_path),
        "-vf", ",".join(vf),
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dst_path),
    ]
    subprocess.run(cmd, check=True)
    return dst_path