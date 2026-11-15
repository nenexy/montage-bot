"""Распознавание речи (faster-whisper) и генерация «тиктоковых» субтитров в формате ASS."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffmpeg import FFMPEG


@dataclass
class Word:
    start: float
    end: float
    text: str


_models: dict[tuple[str, str], Any] = {}


def _load_audio(path: Path) -> Any:
    """16 кГц моно float32 через наш ffmpeg (без PyAV — у него ломается совместимость версий)."""
    import numpy as np

    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def transcribe(path: Path, model_name: str, language: str | None, device: str = "cpu") -> list[Word]:
    from faster_whisper import WhisperModel  # импорт тяжёлый, грузим только когда нужно

    key = (model_name, device)
    if key not in _models:
        _models[key] = WhisperModel(model_name, device=device, compute_type="int8")
    segments, _ = _models[key].transcribe(
        _load_audio(path), language=language or None, word_timestamps=True, beam_size=5
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(float(w.start), float(w.end), text))
    return words


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 18, max_gap: float = 0.6) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        too_long = len(" ".join(x.text for x in [*cur, w])) > max_chars
        if cur and (len(cur) >= max_words or too_long or w.start - cur[-1].end > max_gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w.text[-1] in ".!?…":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return text.rstrip(",.;:").upper()


HIGHLIGHT = "&H0000E1FF&"  # жёлтый (в ASS цвета записываются как BGR)


def write_ass(words: list[Word], path: Path, width: int, height: int, font: str = "Arial") -> None:
    portrait = height >= width
    base = min(width, height)
    size = round(base * (0.09 if portrait else 0.075))
    outline = max(2, round(size * 0.09))
    margin_v = round(height * (0.22 if portrait else 0.08))
    margin_h = round(width * 0.06)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Main,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,"
        f"{outline},{round(outline * 0.6)},2,{margin_h},{margin_h},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    chunks = chunk_words(words)
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0].start if ci + 1 < len(chunks) else chunk[-1].end + 1.0
        chunk_end = min(chunk[-1].end + 0.25, next_start)
        texts = [_clean(w.text) for w in chunk]
        for i, w in enumerate(chunk):
            start = chunk[0].start if i == 0 else w.start
            end = chunk[i + 1].start if i + 1 < len(chunk) else chunk_end
            if end - start < 0.02:
                continue
            parts = [f"{{\\c{HIGHLIGHT}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t for j, t in enumerate(texts)]
            pop = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Main,,0,0,0,,{pop}{' '.join(parts)}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
