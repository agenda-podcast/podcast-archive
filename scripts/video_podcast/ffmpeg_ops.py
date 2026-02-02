# ASCII-only. No ellipses. Keep <= 500 lines.

from pathlib import Path
from typing import Dict, List, Tuple

from .util import ffprobe_duration_sec, run

TARGET_W = 1920
TARGET_H = 1080
TARGET_FPS = 30


def _verify_output_media(p: Path, min_bytes: int = 1024, min_dur_sec: float = 0.5) -> None:
    if not p.exists():
        raise FileNotFoundError("ffmpeg did not produce output: %s" % str(p))
    if p.stat().st_size < min_bytes:
        raise RuntimeError("ffmpeg output too small: %s" % str(p))
    try:
        d = ffprobe_duration_sec(p)
    except Exception as e:
        raise RuntimeError("ffmpeg output is not probeable: %s" % str(p)) from e
    if d < min_dur_sec:
        raise RuntimeError("ffmpeg output duration too short: %s" % str(p))


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
    # Stream output to avoid "looks stuck" runs on Actions.
    run(cmd, timeout_sec=900, stream=True)
    _verify_output_media(dst, min_bytes=50 * 1024, min_dur_sec=max(0.5, float(dur_sec) * 0.5))


def ffmpeg_normalize_video(src: Path, dst: Path) -> None:
    """Normalize a full video without trimming.

    This is used for Tier-1 assets where we want to keep long clips intact.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
        "fps=%d" % (TARGET_W, TARGET_H, TARGET_W, TARGET_H, TARGET_FPS)
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    run(cmd, timeout_sec=3600, stream=True)
    _verify_output_media(dst, min_bytes=200 * 1024, min_dur_sec=3.0)


def ffmpeg_normalize_audio(src: Path, dst: Path) -> None:
    """Normalize podcast audio for consistent loudness.

    Single-pass loudnorm is used for speed and predictable runtime.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100",
        "-ac", "2",
        "-c:a", "aac",
        "-b:a", "192k",
        str(dst),
    ]
    run(cmd, timeout_sec=1800, stream=True)
    _verify_output_media(dst, min_bytes=50 * 1024, min_dur_sec=5.0)


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
    run(cmd, timeout_sec=3600, stream=True)
    _verify_output_media(dst, min_bytes=200 * 1024, min_dur_sec=3.0)


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
    run(cmd, timeout_sec=3600, stream=True)
    _verify_output_media(dst, min_bytes=200 * 1024, min_dur_sec=3.0)


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
    run(cmd, timeout_sec=3600, stream=True)
    _verify_output_media(dst, min_bytes=200 * 1024, min_dur_sec=3.0)


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

    Video:
    - Intro video: scaled/padded
    - Main video: concat list plus frame overlay
    - Outro video: scaled/padded

    Audio:
    - Intro audio: silence
    - Main audio: podcast mp3
    - Outro audio: silence

    The frame PNG is scaled to match output height (no stretching), centered.
    Podcast audio starts after the intro and ends before the outro.
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

    intro_dur = ffprobe_duration_sec(intro_outro_mp4)
    if intro_dur <= 0.01:
        raise ValueError("intro/outro duration is invalid")

    main_dur = 0.0
    for c in clips:
        try:
            main_dur += ffprobe_duration_sec(c)
        except Exception:
            pass
    if main_dur <= 0.01:
        raise ValueError("main duration is invalid")

    intro_dur_s = "%.3f" % float(intro_dur)
    main_dur_s = "%.3f" % float(main_dur)

    # Inputs:
    # 0: intro/outro mp4 (video only)
    # 1: concat list (silent clips)
    # 2: podcast audio mp3
    # 3: frame png (looped)
    filt = (
        "[0:v]split=2[i0][o0];"
        "[i0]%s[introv];"
        "[o0]%s[outrov];"
        "[1:v]%s[main_pre];"
        "[3:v]format=rgba[frame];"
        "[frame][main_pre]scale2ref=w=-1:h=main_h[frame_m][main_ref];"
        "[main_ref][frame_m]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[mainv];"
        "anullsrc=r=44100:cl=stereo,atrim=0:%s,asetpts=N/SR/TB[introa];"
        "[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        "apad,atrim=0:%s,asetpts=N/SR/TB[maina];"
        "anullsrc=r=44100:cl=stereo,atrim=0:%s,asetpts=N/SR/TB[outroa];"
        "[introv][introa][mainv][maina][outrov][outroa]concat=n=3:v=1:a=1[v][a]"
    ) % (vf_base, vf_base, vf_base, intro_dur_s, main_dur_s, intro_dur_s)

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
        run(cmd, timeout_sec=7200, stream=True)
        _verify_output_media(dst, min_bytes=500 * 1024, min_dur_sec=5.0)
    finally:
        try:
            if lst.exists():
                lst.unlink()
        except Exception:
            pass


def ffmpeg_render_one_pass_with_intro_outro_and_frame(
    segments: List[Dict[str, float]],
    podcast_audio: Path,
    intro_outro_mp4: Path,
    frame_png: Path,
    dst: Path,
    main_dur_sec: float,
    intro_silence_sec: float,
    outro_silence_sec: float,
) -> Tuple[List[str], float]:
    """One-pass final render.

    This function builds and runs a single ffmpeg command that:

    - Uses raw source clips (no per-clip encoding).
    - Trims the last clip as needed so total main video duration equals main_dur_sec.
    - Applies the existing frame overlay sizing logic (height-aligned, centered, AR preserved).
    - Produces intro and outro segments with silent audio.

    segments items are dicts with:
      - path: str
      - start_sec: float
      - dur_sec: float

    Returns (cmd, expected_total_sec).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not intro_outro_mp4.exists():
        raise FileNotFoundError("intro/outro mp4 not found: %s" % str(intro_outro_mp4))
    if not frame_png.exists():
        raise FileNotFoundError("frame png not found: %s" % str(frame_png))
    if not podcast_audio.exists():
        raise FileNotFoundError("podcast audio not found: %s" % str(podcast_audio))
    if not segments:
        raise ValueError("no segments provided")
    if main_dur_sec <= 0.01:
        raise ValueError("main duration is invalid")
    if intro_silence_sec < 0.0 or outro_silence_sec < 0.0:
        raise ValueError("intro/outro silence duration is invalid")

    # Keep the existing video normalization targets.
    # One-pass concat requires every segment to have matching geometry, fps, and pixel format.
    # Per-clip mode already outputs yuv420p, so we enforce the same here to avoid concat failures.
    # NOTE: Some providers ship MP4s with pathological sample-aspect-ratio (SAR) metadata.
    # The concat filter requires matching SAR across all inputs, so force SAR to 1:1
    # after scaling/padding (this does not change pixel dimensions; it only normalizes metadata).
    vf_base = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        "fps=%d,"
        "format=yuv420p" % (TARGET_W, TARGET_H, TARGET_W, TARGET_H, TARGET_FPS)
    )

    intro_dur_s = "%.3f" % float(intro_silence_sec)
    outro_dur_s = "%.3f" % float(outro_silence_sec)
    main_dur_s = "%.3f" % float(main_dur_sec)
    expected_total = float(intro_silence_sec) + float(main_dur_sec) + float(outro_silence_sec)

    # Inputs:
    # 0: intro/outro mp4 (video)
    # 1: podcast audio (original)
    # 2: frame png (looped)
    # 3..: raw source clips
    cmd: List[str] = [
        "ffmpeg", "-y",
        "-i", str(intro_outro_mp4),
        "-i", str(podcast_audio),
        "-loop", "1",
        "-i", str(frame_png),
    ]
    for seg in segments:
        p = str(seg.get("path") or "")
        if not p:
            raise ValueError("segment missing path")
        cmd += ["-i", p]

    # Build per-segment trim+normalize filters.
    # Concat uses v=1:a=0, and audio is provided separately.
    v_parts: List[str] = []
    v_labels: List[str] = []
    for i, seg in enumerate(segments):
        in_idx = 3 + i
        st = float(seg.get("start_sec") or 0.0)
        du = float(seg.get("dur_sec") or 0.0)
        if du <= 0.01:
            raise ValueError("segment duration too short")
        lab = "v%02d" % i
        v_labels.append("[%s]" % lab)
        v_parts.append(
            "[%d:v]trim=start=%.3f:duration=%.3f,setpts=PTS-STARTPTS,%s[%s]" % (in_idx, st, du, vf_base, lab)
        )

    # Concat main video from all segments.
    concat_main = "%sconcat=n=%d:v=1:a=0[main_pre]" % ("".join(v_labels), len(segments))

    # Apply existing frame overlay sizing logic.
    overlay = (
        "[2:v]format=rgba[frame];"
        "[frame][main_pre]scale2ref=w=-1:h=main_h[frame_m][main_ref];"
        "[main_ref][frame_m]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2,format=yuv420p[mainv]"
    )

    # Intro/outro video from the same asset, trimmed by duration and normalized.
    intro_outro = (
        "[0:v]split=2[i0][o0];"
        "[i0]trim=0:%s,setpts=PTS-STARTPTS,%s[introv];"
        "[o0]trim=0:%s,setpts=PTS-STARTPTS,%s[outrov]" % (intro_dur_s, vf_base, outro_dur_s, vf_base)
    )

    # Audio: silence intro/outro, original audio for main (trimmed), all concatenated.
    # Keep loudnorm settings consistent with the earlier approach, but apply in the final pass.
    audio = (
        "anullsrc=r=44100:cl=stereo,atrim=0:%s,asetpts=N/SR/TB[introa];"
        "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,atrim=0:%s,asetpts=N/SR/TB[maina];"
        "anullsrc=r=44100:cl=stereo,atrim=0:%s,asetpts=N/SR/TB[outroa]" % (intro_dur_s, main_dur_s, outro_dur_s)
    )

    # Final concat of (intro, main, outro) for both video and audio.
    tail = "[introv][introa][mainv][maina][outrov][outroa]concat=n=3:v=1:a=1[v][a]"

    filt = ";".join(v_parts + [concat_main, overlay, intro_outro, audio, tail])

    cmd += [
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

    # Print the final ffmpeg command before running so failures still show the exact invocation.
    print("[ffmpeg][one_pass] %s" % " ".join([str(x) for x in cmd]), flush=True)
    run(cmd, timeout_sec=7200, stream=True)
    _verify_output_media(dst, min_bytes=500 * 1024, min_dur_sec=5.0)
    return cmd, expected_total
