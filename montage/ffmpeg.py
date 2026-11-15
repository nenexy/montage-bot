"""Тонкая обёртка над ffmpeg: запуск и разбор информации о файле."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

FFMPEG = os.environ.get("FFMPEG_BINARY") or imageio_ffmpeg.get_ffmpeg_exe()


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Запускает ffmpeg и возвращает его stderr (там весь полезный вывод)."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        errors="replace",
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        raise FFmpegError(proc.stderr[-4000:])
    return proc.stderr


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool

    @property
    def has_video(self) -> bool:
        return self.width > 0 and self.height > 0


def probe(path: Path) -> MediaInfo:
    # showinfo печатает размер кадра уже после автоповорота (важно для видео с телефона)
    err = run(
        ["-i", str(path), "-map", "0:v:0?", "-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"],
        check=False,
    )
    duration = 0.0
    if m := re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err):
        h, mnt, sec = m.groups()
        duration = int(h) * 3600 + int(mnt) * 60 + float(sec)
    width = height = 0
    if m := re.search(r"\bs:(\d+)x(\d+)", err):
        width, height = int(m.group(1)), int(m.group(2))
    elif m := re.search(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})", err):
        width, height = int(m.group(1)), int(m.group(2))
    has_audio = re.search(r"Stream #\d+:\d+.*?: Audio:", err) is not None
    return MediaInfo(duration=duration, width=width, height=height, has_audio=has_audio)
