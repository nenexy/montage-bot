"""Весь монтаж: паузы → зум на склейках → субтитры → фото-вставки → музыка и звуки переходов."""

from __future__ import annotations

import os
import random
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import subtitles
from .ffmpeg import MediaInfo, probe, run
from .silence import detect_silences, keep_segments

ASSETS = Path(__file__).resolve().parent.parent / "assets"
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}
MAC_FONTS = Path("/System/Library/Fonts/Supplemental")

StageCallback = Callable[[str], None]


@dataclass
class Settings:
    cut_pauses: bool = True
    zoom_cuts: bool = True
    subtitles: bool = True
    music: bool = True
    sfx: bool = True
    photos: bool = True
    # паузы
    silence_db: float = -35.0
    min_silence: float = 0.45
    pad: float = 0.12
    # картинка
    zoom: float = 1.08
    max_side: int = 1920
    photo_duration: float = 2.5
    # звук
    music_volume: float = 0.18
    sfx_volume: float = 0.5
    sfx_min_gap: float = 2.0
    # субтитры
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    language: str | None = "ru"
    font: str = "Arial"
    fonts_dir: str | None = None
    # кодирование
    crf: int = 22
    preset: str = "medium"

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        s = cls(
            whisper_model=os.getenv("WHISPER_MODEL", "small"),
            whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
            language=os.getenv("LANGUAGE", "ru") or None,
            font=os.getenv("SUB_FONT", "Arial"),
            fonts_dir=os.getenv("SUB_FONTS_DIR") or None,
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        return s


@dataclass
class Result:
    output: Path
    duration_in: float
    duration_out: float
    cuts: int
    words: int


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def output_size(info: MediaInfo, max_side: int) -> tuple[int, int]:
    k = min(1.0, max_side / max(info.width, info.height))
    return _even(info.width * k), _even(info.height * k)


def _fonts_dir(s: Settings) -> str | None:
    if s.fonts_dir:
        return s.fonts_dir
    own = ASSETS / "fonts"
    if own.is_dir() and any(own.glob("*.[ot]tf")):
        return str(own)
    if MAC_FONTS.is_dir():
        return str(MAC_FONTS)
    return None


def _q(value: str) -> str:
    """Экранирование значения опции внутри filtergraph."""
    return "'" + value.replace("'", r"'\''") + "'"


def pick_music() -> Path | None:
    folder = ASSETS / "music"
    tracks = [p for p in folder.glob("*") if p.suffix.lower() in AUDIO_EXT] if folder.is_dir() else []
    return random.choice(tracks) if tracks else None


# ---------- шаг 1: вырезаем паузы ----------

def cut_pauses(src: Path, dst: Path, info: MediaInfo, s: Settings, workdir: Path) -> list[float]:
    """Режет паузы и склеивает куски. Возвращает моменты склеек на новой шкале времени."""
    W, H = output_size(info, s.max_side)
    if s.cut_pauses and info.has_audio:
        silences = detect_silences(src, s.silence_db, s.min_silence, info.duration)
        segments = keep_segments(silences, info.duration, s.pad)
    else:
        segments = [(0.0, info.duration)]

    inputs = ["-i", str(src)]
    audio_in = "0:a"
    if not info.has_audio:
        inputs += ["-f", "lavfi", "-t", f"{info.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_in = "1:a"

    ZW, ZH = _even(W * s.zoom), _even(H * s.zoom)
    graph: list[str] = []
    pads: list[str] = []
    for i, (a, b) in enumerate(segments):
        zoomed = s.zoom_cuts and len(segments) > 1 and i % 2 == 1  # «панч-ин» на каждой второй склейке
        vf = f"scale={ZW}:{ZH},crop={W}:{H}" if zoomed else f"scale={W}:{H}"
        graph.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,{vf},setsar=1[v{i}]")
        length = b - a
        f = min(0.015, length / 4)
        graph.append(
            f"[{audio_in}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=in:d={f:.3f},afade=t=out:st={length - f:.3f}:d={f:.3f}[a{i}]"
        )
        pads.append(f"[v{i}][a{i}]")
    graph.append("".join(pads) + f"concat=n={len(segments)}:v=1:a=1[v][a]")

    (workdir / "cut.graph").write_text(";\n".join(graph), encoding="utf-8")
    run(
        [*inputs, "-/filter_complex", "cut.graph", "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", str(dst)],
        cwd=workdir,
    )

    cuts: list[float] = []
    t = 0.0
    for a, b in segments[:-1]:
        t += b - a
        cuts.append(t)
    return cuts


# ---------- шаг 2: финальная сборка ----------

def photo_schedule(photos: Sequence[Path], duration: float, length: float) -> list[tuple[Path, float]]:
    """Равномерно раскидывает фото по ролику."""
    if not photos or duration < length + 1.0:
        return []
    n = len(photos)
    out = []
    for k, p in enumerate(photos):
        t = duration * (k + 1) / (n + 1) - length / 2
        out.append((p, min(max(0.5, t), duration - length - 0.2)))
    return out


def sfx_times(cuts: list[float], duration: float, min_gap: float) -> list[float]:
    times: list[float] = []
    for t in cuts:
        if 0.5 < t < duration - 0.3 and (not times or t - times[-1] >= min_gap):
            times.append(t)
    return times[:60]


def render(
    cut_path: Path,
    dst: Path,
    s: Settings,
    workdir: Path,
    width: int,
    height: int,
    duration: float,
    cuts: list[float],
    photos: Sequence[Path],
    music: Path | None,
    ass_name: str | None,
) -> None:
    W, H = width, height
    inputs = ["-i", str(cut_path)]
    graph: list[str] = []
    idx = 1

    # --- видео: фото-вставки с появлением и «выездом» снизу ---
    v = "[0:v]"
    D = s.photo_duration
    PW, PH = _even(W * 0.72), _even(H * 0.5)
    y0, slide = round(H * 0.14), round(H * 0.25)
    for k, (photo, t) in enumerate(photo_schedule(photos if s.photos else [], duration, D)):
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{D:.2f}", "-i", str(photo)]
        graph.append(
            f"[{idx}:v]scale={PW}:{PH}:force_original_aspect_ratio=decrease,format=rgba,"
            f"fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st={D - 0.25:.2f}:d=0.25:alpha=1,"
            f"setpts=PTS-STARTPTS+{t:.3f}/TB[p{k}]"
        )
        graph.append(
            f"{v}[p{k}]overlay=x=(W-w)/2:y='{y0}+max(0,0.3-(t-{t:.3f}))*{slide}':"
            f"enable='between(t,{t:.3f},{t + D:.3f})':eof_action=pass[vp{k}]"
        )
        v = f"[vp{k}]"
        idx += 1

    if ass_name:
        fonts = _fonts_dir(s)
        opt = f"ass=filename={_q(ass_name)}" + (f":fontsdir={_q(fonts)}" if fonts else "")
        graph.append(f"{v}{opt}[vout]")
    else:
        graph.append(f"{v}null[vout]")

    # --- звук: голос + музыка с «приглушением» под голос + вжухи на склейках ---
    norm = "aformat=sample_rates=48000:channel_layouts=stereo"
    mix = ["[voice]"]
    if music:
        graph.append(f"[0:a]{norm},asplit=2[voice][vsc]")
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        fo = min(1.5, duration / 3)
        graph.append(
            f"[{idx}:a]{norm},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={s.music_volume},"
            f"afade=t=in:d=0.8,afade=t=out:st={duration - fo:.3f}:d={fo:.3f}[mus]"
        )
        graph.append("[mus][vsc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[duck]")
        mix.append("[duck]")
        idx += 1
    else:
        graph.append(f"[0:a]{norm}[voice]")

    whoosh = ASSETS / "sfx" / "whoosh.wav"
    times = sfx_times(cuts, duration, s.sfx_min_gap) if s.sfx and whoosh.exists() else []
    if times:
        inputs += ["-i", str(whoosh)]
        outs = "".join(f"[w{j}]" for j in range(len(times)))
        graph.append(f"[{idx}:a]{norm},volume={s.sfx_volume},asplit={len(times)}{outs}")
        for j, t in enumerate(times):
            ms = int(max(0.0, t - 0.18) * 1000)  # пик вжуха приходится ровно на склейку
            graph.append(f"[w{j}]adelay={ms}:all=1[wd{j}]")
            mix.append(f"[wd{j}]")
        idx += 1

    if len(mix) > 1:
        graph.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    else:
        graph.append("[voice]anull[aout]")

    (workdir / "final.graph").write_text(";\n".join(graph), encoding="utf-8")
    run(
        [*inputs, "-/filter_complex", "final.graph", "-map", "[vout]", "-map", "[aout]",
         "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", s.preset, "-crf", str(s.crf),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)],
        cwd=workdir,
    )


def fit_to_size(path: Path, max_bytes: int, duration: float) -> Path:
    """Пережимает ролик, если он не влезает в лимит Telegram."""
    if path.stat().st_size <= max_bytes or duration <= 0:
        return path
    total_kbps = max_bytes * 8 / 1000 / duration * 0.92
    v_kbps = max(250, int(total_kbps - 128))
    out = path.with_name(path.stem + "_small.mp4")
    run(["-i", str(path), "-c:v", "libx264", "-preset", "medium", "-b:v", f"{v_kbps}k",
         "-maxrate", f"{int(v_kbps * 1.3)}k", "-bufsize", f"{v_kbps * 2}k", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    return out


# ---------- всё вместе ----------

def process(
    src: Path,
    dst: Path,
    settings: Settings | None = None,
    photos: Sequence[Path] = (),
    music: Path | None = None,
    on_stage: StageCallback | None = None,
) -> Result:
    s = settings or Settings()
    stage = on_stage or (lambda _msg: None)
    src, dst = Path(src).resolve(), Path(dst).resolve()
    photos = [Path(p).resolve() for p in photos]

    info = probe(src)
    if not info.has_video:
        raise ValueError("В файле нет видеодорожки")
    if info.duration <= 0:
        raise ValueError("Не удалось определить длительность видео")

    work = Path(tempfile.mkdtemp(prefix="montage_"))
    try:
        stage("✂️ Ищу и вырезаю паузы…")
        cut_path = work / "cut.mp4"
        cuts = cut_pauses(src, cut_path, info, s, work)
        cut_info = probe(cut_path)
        W, H = cut_info.width, cut_info.height

        ass_name = None
        words: list[subtitles.Word] = []
        if s.subtitles and info.has_audio:
            stage("📝 Распознаю речь для субтитров…")
            words = subtitles.transcribe(cut_path, s.whisper_model, s.language, s.whisper_device)
            if words:
                subtitles.write_ass(words, work / "subs.ass", W, H, s.font)
                ass_name = "subs.ass"

        track = (Path(music).resolve() if music else pick_music()) if s.music else None

        stage("🎬 Собираю финальный ролик…")
        render(cut_path, dst, s, work, W, H, cut_info.duration, cuts, photos, track, ass_name)
        return Result(dst, info.duration, cut_info.duration, len(cuts), len(words))
    finally:
        shutil.rmtree(work, ignore_errors=True)
