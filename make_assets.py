"""Генерирует заглушки звуков: «вжух» для склеек и простую фоновую музыку.

Ничего не скачивает — всё синтезируется ffmpeg. Свои треки клади в assets/music/,
свой звук перехода — в assets/sfx/whoosh.wav.
"""

from pathlib import Path

from montage.ffmpeg import run

ROOT = Path(__file__).resolve().parent / "assets"


def main() -> None:
    (ROOT / "sfx").mkdir(parents=True, exist_ok=True)
    (ROOT / "music").mkdir(parents=True, exist_ok=True)

    run([
        "-f", "lavfi", "-i", "anoisesrc=color=pink:d=0.6:a=0.7:r=48000",
        "-af", "highpass=f=350,lowpass=f=5000,afade=t=in:d=0.35:curve=exp,"
               "afade=t=out:st=0.35:d=0.25,aformat=channel_layouts=stereo",
        str(ROOT / "sfx" / "whoosh.wav"),
    ])

    am = "sin(2*PI*220*t)+sin(2*PI*261.63*t)+sin(2*PI*329.63*t)"
    f = "sin(2*PI*174.61*t)+sin(2*PI*220*t)+sin(2*PI*261.63*t)"
    expr = f"0.06*if(lt(mod(t,8),4),{am},{f})*(0.8+0.2*sin(2*PI*0.25*t))"
    run([
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d=32",
        "-af", "lowpass=f=1800,aecho=0.8:0.7:120:0.3,afade=t=in:d=1,afade=t=out:st=30:d=2,"
               "aformat=channel_layouts=stereo",
        "-b:a", "160k", str(ROOT / "music" / "demo_pad.mp3"),
    ])
    print("Готово:", *sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.*")), sep="\n  ")


if __name__ == "__main__":
    main()
