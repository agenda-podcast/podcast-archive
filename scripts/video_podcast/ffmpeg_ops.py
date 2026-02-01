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


def ffmpeg_concat_with_audio(clips: List[Path], audio: Path, dst: Path) -> None:
    """Concatenate silent clips and mux external audio into a single output.

    This avoids writing an intermediate silent timeline file.
    """
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
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", str(TARGET_FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(dst),
    ]
    run(cmd)


def ffmpeg_concat_with_intro_outro_and_frame(
    clips: List[Path],
    podcast_audio: Path,
    intro_outro_mp4: Path,
    frame_png: Path,
    dst: Path,
) -> None:
    """Build the final video with:

    - Intro segment: intro_outro_mp4
    - Main segment: concatenated clips with podcast_audio
    - Outro segment: intro_outro_mp4

    A PNG frame is overlaid on top of all segments.
    The PNG is scaled to match output height (no stretching), centered.

    Notes:
    - Podcast audio starts after the intro and ends before the outro.
    - The intro/outro keep their own audio track (if present).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not intro_outro_mp4.exists():
        raise FileNotFoundError("intro/outro mp4 not found: %s" % str(intro_outro_mp4))
    if not frame_png.exists():
        raise FileNotFoundError("frame png not found: %s" % str(frame_png))
    if not podcast_audio.exists():
        raise FileNotFoundError("podcast audio not found: %s" % str(podcast_audio))
    if not clips:
        raise ValueError("no clips provided")

    lst = dst.parent / "concat_list.txt"
    lines = []
    for c in clips:
        lines.append("file '%s'" % c.as_posix())
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

    vf_base = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
        "fps=%d" % (TARGET_W, TARGET_H, TARGET_W, TARGET_H, TARGET_FPS)
    )

    # Inputs:
    # 0: intro/outro mp4
    # 1: concat list (silent)
    # 2: podcast audio
    # 3: frame png (looped)
    #
    # We split intro/outro so we can use the same file for both ends.
    filt = (
        "[0:v]split=2[i0][o0];"
        "[0:a]asplit=2[i0a][o0a];"
        "[i0]%s[intro_pre];"
        "[o0]%s[outro_pre];"
        "[1:v]%s[main_pre];"
        "[3:v]format=rgba[frame];"
        "[frame][intro_pre]scale2ref=w=-1:h=main_h[frame_i][intro_ref];"
        "[intro_ref][frame_i]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[introv];"
        "[frame][main_pre]scale2ref=w=-1:h=main_h[frame_m][main_ref];"
        "[main_ref][frame_m]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[mainv];"
        "[frame][outro_pre]scale2ref=w=-1:h=main_h[frame_o][outro_ref];"
        "[outro_ref][frame_o]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[outrov];"
        "[i0a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[introa];"
        "[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[maina];"
        "[o0a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[outroa];"
        "[introv][introa][mainv][maina][outrov][outroa]concat=n=3:v=1:a=1[v][a]"
    ) % (vf_base, vf_base, vf_base)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(intro_outro_mp4),
        "-f", "concat",
        "-safe", "0",
        "-i", str(lst),
        "-i", str(podcast_audio),
        "-loop", "1",
        "-i", str(frame_png),
        "-filter_complex", filt,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", str(TARGET_FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        run(cmd)
    finally:
        try:
            if lst.exists():
                lst.unlink()
        except Exception:
            pass
