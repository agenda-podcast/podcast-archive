# ASCII-only. No ellipses. Keep <= 500 lines.

from pathlib import Path
from typing import List

from .util import run

TARGET_W = 1920
TARGET_H = 1080
TARGET_FPS = 30


def ffmpeg_make_clip(src: Path, dst: Path, start_sec: float, dur_sec: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
        "fps=%d" % (TARGET_W, TARGET_H, TARGET_W, TARGET_H, TARGET_FPS)
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", "%.3f" % start_sec,
        "-t", "%.3f" % dur_sec,
        "-i", str(src),
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    run(cmd)


def ffmpeg_concat_and_encode(clips: List[Path], dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    lst = dst.parent / "concat_list.txt"
    lines = []
    for c in clips:
        lines.append("file '%s'" % c.as_posix())
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(lst),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", str(TARGET_FPS),
        str(dst),
    ]
    run(cmd)


def ffmpeg_mux_audio(video: Path, audio: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(dst),
    ]
    run(cmd)
