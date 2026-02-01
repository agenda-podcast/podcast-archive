from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple
import math

from .util import ffprobe_duration_sec, log, run, run_stream


TARGET_W = 1920
TARGET_H = 1080
TARGET_FPS = 30

AAC_SR = 44100
AAC_CH = 2


def _vf_base() -> str:
    # Scale/pad to 1080p while preserving aspect, then normalize fps.
    return (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
        "fps=%d,format=yuv420p"
        % (TARGET_W, TARGET_H, TARGET_W, TARGET_H, TARGET_FPS)
    )


def _x264_args() -> List[str]:
    # Keep streams concat-friendly (stable GOP) so we can -c:v copy later.
    gop = int(TARGET_FPS * 2)
    x264_params = "keyint=%d:min-keyint=%d:scenecut=0" % (gop, gop)
    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-x264-params", x264_params,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]


def ffmpeg_make_clip(
    src_mp4: Path,
    start_sec: float,
    dur_sec: float,
    dst_mp4: Path,
    frame_png: Optional[Path] = None,
) -> None:
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.2, float(dur_sec))

    cmd: List[str] = [
        "ffmpeg",
        "-y",
        "-ss", "%.3f" % float(start_sec),
        "-t", "%.3f" % dur,
        "-i", str(src_mp4),
    ]

    if frame_png is not None:
        cmd += ["-loop", "1", "-t", "%.3f" % dur, "-i", str(frame_png)]
        # Frame overlay must be height-aligned to the main video and centered.
        # Do not stretch: scale2ref keeps aspect ratio.
        fc = (
            "[0:v]%s[base];"
            "[1:v]format=rgba[frame];"
            "[frame][base]scale2ref=w=-1:h=main_h[frame_s][base_s];"
            "[base_s][frame_s]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[v]"
            % _vf_base()
        )
        cmd += [
            "-filter_complex", fc,
            "-map", "[v]",
            "-an",
        ]
    else:
        cmd += [
            "-vf", _vf_base(),
            "-an",
        ]

    cmd += _x264_args()
    cmd += [str(dst_mp4)]
    run(cmd)


def ffmpeg_prepare_intro_outro(src_mp4: Path, dst_mp4: Path) -> float:
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src_mp4),
        "-an",
        "-vf", _vf_base(),
    ]
    cmd += _x264_args()
    cmd += [str(dst_mp4)]
    run_stream(cmd, prefix="intro_outro")
    return ffprobe_duration_sec(dst_mp4)


def ffmpeg_concat_video_streamcopy(segments: List[Path], dst_mp4: Path, work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    list_path = work_dir / "concat_list.txt"
    lines = []
    for p in segments:
        lines.append("file '%s'" % str(p))
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(dst_mp4),
    ]
    run_stream(cmd, prefix="concat_v")


def ffmpeg_build_audio_track_aac(
    podcast_mp3: Path,
    intro_sec: float,
    main_pad_sec: float,
    outro_sec: float,
    dst_aac: Path,
) -> None:
    dst_aac.parent.mkdir(parents=True, exist_ok=True)
    intro = max(0.0, float(intro_sec))
    pad = max(0.0, float(main_pad_sec))
    outro = max(0.0, float(outro_sec))

    inputs: List[str] = []
    # 0: intro silence
    inputs += ["-f", "lavfi", "-t", "%.3f" % intro, "-i", "anullsrc=r=%d:cl=stereo" % AAC_SR]
    # 1: mp3
    inputs += ["-i", str(podcast_mp3)]
    n = 3
    if pad > 0.05:
        # 2: pad silence after mp3
        inputs += ["-f", "lavfi", "-t", "%.3f" % pad, "-i", "anullsrc=r=%d:cl=stereo" % AAC_SR]
        n = 4
    # last: outro silence
    inputs += ["-f", "lavfi", "-t", "%.3f" % outro, "-i", "anullsrc=r=%d:cl=stereo" % AAC_SR]

    # Build concat filter. Inputs are 0..(n-1).
    parts: List[str] = []
    for i in range(n):
        parts.append("[%d:a]aformat=sample_fmts=fltp:sample_rates=%d:channel_layouts=stereo[a%d]" % (i, AAC_SR, i))
    concat_in = "".join(["[a%d]" % i for i in range(n)])
    parts.append("%sconcat=n=%d:v=0:a=1[a]" % (concat_in, n))
    fc = ";".join(parts)

    cmd = ["ffmpeg", "-y"]
    cmd += inputs
    cmd += [
        "-filter_complex", fc,
        "-map", "[a]",
        "-c:a", "aac",
        "-b:a", "192k",
        str(dst_aac),
    ]
    run_stream(cmd, prefix="audio")


def ffmpeg_mux_av_copy(video_mp4: Path, audio_aac: Path, dst_mp4: Path) -> None:
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_mp4),
        "-i", str(audio_aac),
        "-c:v", "copy",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dst_mp4),
    ]
    run_stream(cmd, prefix="mux")


def build_episode_video_streamcopy(
    *,
    clips: List[Path],
    podcast_mp3: Path,
    intro_outro_mp4: Path,
    out_mp4: Path,
    work_dir: Path,
) -> None:
    # 1) Prepare intro/outro as concat-friendly, video-only segment.
    io_pre = work_dir / "intro_outro_pre.mp4"
    io_dur = ffmpeg_prepare_intro_outro(intro_outro_mp4, io_pre)

    # 2) Concat intro + clips + outro using stream copy (no re-encode).
    segs = [io_pre] + clips + [io_pre]
    video_v = work_dir / "video_only.mp4"
    ffmpeg_concat_video_streamcopy(segs, video_v, work_dir=work_dir)

    # 3) Build audio: intro silence + mp3 (+ pad silence) + outro silence.
    audio_dur = ffprobe_duration_sec(podcast_mp3)
    clips_dur = 0.0
    for p in clips:
        clips_dur += ffprobe_duration_sec(p)
    pad = max(0.0, clips_dur - audio_dur)

    log("[render] io_sec=%.2f clips_sec=%.2f audio_sec=%.2f pad_sec=%.2f" % (io_dur, clips_dur, audio_dur, pad))

    audio_aac = work_dir / "audio_track.aac"
    ffmpeg_build_audio_track_aac(
        podcast_mp3=podcast_mp3,
        intro_sec=io_dur,
        main_pad_sec=pad,
        outro_sec=io_dur,
        dst_aac=audio_aac,
    )

    # 4) Mux video + audio (stream copy).
    ffmpeg_mux_av_copy(video_v, audio_aac, out_mp4)
