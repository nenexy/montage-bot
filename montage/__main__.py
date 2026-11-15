"""Монтаж из командной строки, без Telegram:

    python -m montage input.mp4 -o out.mp4 --photo a.jpg --photo b.jpg --music track.mp3
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

from .pipeline import Settings, process


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="montage", description="Автомонтаж видео")
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--photo", type=Path, action="append", default=[], help="фото-вставка (можно несколько)")
    p.add_argument("--music", type=Path, help="фоновая музыка (по умолчанию — случайная из assets/music)")
    p.add_argument("--model", help="модель Whisper: tiny, base, small, medium, large-v3")
    for flag in ("cut", "zoom", "subs", "music", "sfx"):
        p.add_argument(f"--no-{flag}", action="store_true")
    a = p.parse_args()

    s = Settings.from_env()
    s.cut_pauses, s.zoom_cuts, s.subtitles = not a.no_cut, not a.no_zoom, not a.no_subs
    s.music, s.sfx = not a.no_music, not a.no_sfx
    if a.model:
        s.whisper_model = a.model

    out = a.output or a.input.with_name(a.input.stem + "_montage.mp4")
    t0 = time.time()
    r = process(a.input, out, s, a.photo, a.music, on_stage=print)
    print(
        f"Готово за {time.time() - t0:.0f} с → {r.output}\n"
        f"Было {r.duration_in:.1f} с, стало {r.duration_out:.1f} с, склеек {r.cuts}, слов в субтитрах {r.words}"
    )


if __name__ == "__main__":
    main()
