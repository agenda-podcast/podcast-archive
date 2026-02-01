from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

from .util import ffprobe_duration_sec


def _run_ffmpeg(cmd: List[str]) -> None:
    """Run ffmpeg while emitting progress to stdout.

    GitHub Actions sometimes looks "stuck" when tools are quiet. We always add
    a machine-readable progress channel.
    """
    cmd2 = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
    ] + cmd
    print("[ffmpeg] " + " ".join(cmd2), flush=True)
    p = subprocess.Popen(
        cmd2,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert p.stdout is not None
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        # Keep log volume modest; only show key progress lines.
        if line.startswith("out_time=") or line.startswith("speed=") or line.startswith("progress="):
            print("[ffmpeg] " + line, flush=True)
    rc = p.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed rc={rc}")


def ffmpeg_prepare_segment_no_audio(
    *,
    src_mp4: Path,
    dst_mp4: Path,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Scale/pad a segment to the canonical video settings.

    Output is video-only (no audio) so it can be concatenated later.
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}"
    )
    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(src_mp4),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-r",
            str(fps),
            "-x264-params",
            f"keyint={fps*2}:min-keyint={fps*2}:scenecut=0",
            "-movflags",
            "+faststart",
            str(dst_mp4),
        ]
    )


def ffmpeg_make_clip_with_frame(
    *,
    src_mp4: Path,
    dst_mp4: Path,
    start_sec: float,
    duration_sec: float,
    frame_png: Path,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Cut a clip and burn-in the frame overlay.

    Output is video-only (no audio) and encoded with canonical settings.
    """
    # The frame is scaled by height to match the main video and overlaid centered.
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}[base];"
        f"[1:v]format=rgba[frame];"
        f"[frame][base]scale2ref=w=-1:h=ih[frame_s][base_r];"
        f"[base_r][frame_s]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[v]"
    )
    _run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-i",
            str(src_mp4),
            "-t",
            f"{duration_sec:.3f}",
            "-loop",
            "1",
            "-i",
            str(frame_png),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-r",
            str(fps),
            "-x264-params",
            f"keyint={fps*2}:min-keyint={fps*2}:scenecut=0",
            "-movflags",
            "+faststart",
            str(dst_mp4),
        ]
    )


def ffmpeg_make_clip(
    src_path: Path,
    dst_mp4: Path,
    start_sec: float,
    duration_sec: float,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> None:
    """Create a plain video-only clip, encoded for stream-copy concat."""
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    start = f"{float(start_sec):.3f}"
    dur = f"{float(duration_sec):.3f}"
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}"
    )
    _run_ffmpeg(
        [
            "-y",
            "-ss",
            start,
            "-i",
            str(src_path),
            "-t",
            dur,
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-x264-params",
            f"keyint={fps*2}:min-keyint={fps*2}:scenecut=0",
            "-movflags",
            "+faststart",
            str(dst_mp4),
        ]
    )


def ffmpeg_concat_video_streamcopy(*, concat_list_txt: Path, dst_mp4: Path) -> None:
    """Concatenate already-encoded segments using concat demuxer.

    Requires all segments to have matching codec/params. We enforce this by
    encoding intro/outro and clips using the same settings.
    """
    _run_ffmpeg(
        [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_txt),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dst_mp4),
        ]
    )


def ffmpeg_build_audio_track_aac(
    *,
    main_mp3: Path,
    intro_silence_sec: float,
    outro_silence_sec: float,
    dst_m4a: Path,
) -> None:
    """Create one AAC audio track: intro silence + main mp3 + outro silence."""
    fc = (
        "[0:a]asetpts=PTS-STARTPTS[a0];"
        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asetpts=PTS-STARTPTS[a1];"
        "[2:a]asetpts=PTS-STARTPTS[a2];"
        "[a0][a1][a2]concat=n=3:v=0:a=1[a]"
    )
    _run_ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{intro_silence_sec:.3f}",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-i",
            str(main_mp3),
            "-f",
            "lavfi",
            "-t",
            f"{outro_silence_sec:.3f}",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            fc,
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dst_m4a),
        ]
    )


def ffmpeg_mux_av_streamcopy(*, video_mp4: Path, audio_m4a: Path, dst_mp4: Path) -> None:
    """Mux audio+video without re-encoding."""
    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(video_mp4),
            "-i",
            str(audio_m4a),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dst_mp4),
        ]
    )


def sum_durations_sec(paths: List[Path]) -> float:
    total = 0.0
    for p in paths:
        total += ffprobe_duration_sec(p)
    return total
