"""Поиск пауз (тишины) и расчёт кусков, которые оставляем."""

from __future__ import annotations

import re
from pathlib import Path

from .ffmpeg import run

Segment = tuple[float, float]


def detect_silences(path: Path, noise_db: float, min_duration: float, total: float) -> list[Segment]:
    err = run(
        ["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}", "-f", "null", "-"]
    )
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    silences: list[Segment] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else total  # тишина до самого конца файла
        silences.append((max(0.0, start), min(end, total)))
    return silences


def keep_segments(
    silences: list[Segment], total: float, pad: float, min_keep: float = 0.25
) -> list[Segment]:
    """Всё, что не тишина, плюс небольшой запас `pad` по краям, чтобы не обрезать слова."""
    segments: list[Segment] = []
    cursor = 0.0
    for start, end in silences:
        seg_end = min(start + pad, total)
        if seg_end - cursor >= min_keep:
            segments.append((cursor, seg_end))
        cursor = max(end - pad, cursor)
    if total - cursor >= min_keep:
        segments.append((cursor, total))

    merged: list[Segment] = []
    for a, b in segments:
        if merged and a - merged[-1][1] < 0.05:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged or [(0.0, total)]
