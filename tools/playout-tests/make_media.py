"""Generates the test media for the PlayOut fork harness.

Every clip is 320x180 at 50p on a mid-grey background:
- seven 20x20 blocks at y 80..99, x 20..159 hold the source frame index in binary (white = 1);
- a 100x40 block at x 200..299, y 20..59 holds the clip identity as a chroma pair at mid luma.
Both survive the YUV -> RGB -> YUV round trip of the mixer and the recorder without clipping.
"""
import os
import shutil
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(ROOT, "media")

CLIPS = {
    # name: (frames, U, V)
    "clipa": (100, 98, 158),
    "clipb": (100, 158, 98),
    "clipc": (100, 98, 98),
    "clipd": (100, 158, 158),
    "slate": (50, 128, 128),
}

BITS = "if(between(Y,80,99)*between(X,20,159),if(mod(floor(N/pow(2,floor((X-20)/20))),2),235,16),128)"


def chroma(value):
    # Chroma planes are half size in yuv420p.
    return f"if(between(Y,10,29)*between(X,100,149),{value},128)"


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
        raise SystemExit(f"ffmpeg failed: {' '.join(args[:6])}")


def make_clip(name, frames, u, v):
    seconds = frames / 50
    lum = "128" if name == "slate" else BITS
    run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"nullsrc=s=320x180:r=50:d={seconds},format=yuv420p,"
                             f"geq=lum='{lum}':cb='{chroma(u)}':cr='{chroma(v)}'",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "mpeg2video", "-g", "1", "-q:v", "1", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", "-ac", "2",
        os.path.join(MEDIA, f"{name}.mov"),
    ])


def make_badge(name, x, y):
    # A transparent 320x180 frame with an opaque white 40x40 square at (x, y).
    run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black@0.0:s=320x180,format=rgba",
        "-f", "lavfi", "-i", "color=c=white:s=40x40,format=rgba",
        "-filter_complex", f"[0][1]overlay={x}:{y}:format=auto,format=rgba",
        "-frames:v", "1",
        os.path.join(MEDIA, f"{name}.png"),
    ])


def main():
    os.makedirs(MEDIA, exist_ok=True)
    for name, (frames, u, v) in CLIPS.items():
        make_clip(name, frames, u, v)
    make_badge("badge_tl", 0, 0)
    make_badge("badge_br", 280, 140)
    for entry in sorted(os.listdir(MEDIA)):
        print(entry, os.path.getsize(os.path.join(MEDIA, entry)))


if __name__ == "__main__":
    main()
