import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional


def _sh(cmd: List[str]) -> None:
    subprocess.check_call(cmd)


def _escape_drawtext_literal(s: str) -> str:
    """
    Escape a literal string for ffmpeg drawtext text=...
    drawtext uses ':' and ',' as separators, and supports escaping via backslash.
    """
    s = s or ""
    # Order matters: escape backslash first
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace(",", "\\,")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _timer_expr(start_sec: int) -> str:
    """
    Build timer expression for drawtext where:
      MM:SS relative to start_sec
    Critical: escape ':' between MM and SS as '\:' and escape comma in mod( , ) as '\,'.
    """
    s = int(start_sec)
    # NOTE: We must escape ':' inside the eif formats and also escape the visible ":" between MM and SS.
    # Also escape comma inside mod().
    return f"%{{eif\\:(t-{s})/60\\:d2}}\\:%{{eif\\:mod(t-{s}\\,60)\\:d2}}"


def _between(t0: int, t1: int) -> str:
    # inclusive range used in your logs
    return f"between(t\\,{int(t0)}\\,{int(t1)})"


def render_waveform_video(
    bg_concat_txt: str,
    audio_path: str,
    ffmeta_path: str,
    out_mp4: str,
    segments: List[Dict[str, Any]],
    overlay_cfg: Dict[str, Any],
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 25,
    crf: int = 21,                 # a touch faster than 20
    preset: str = "veryfast",
) -> str:
    """
    Renders a background slideshow + blur + dark overlay + per-segment title + timer.
    (No waveform visual.)
    Inputs:
      - bg_concat_txt: ffmpeg concat demuxer file listing background images/videos
      - audio_path: mp3
      - ffmeta_path: ffmetadata file (chapters)
      - segments: [{"title": "...", "start": 0, "end": 119}, ...] seconds
      - overlay_cfg: from topic json video_overlay
    """

    bg_concat_txt = str(bg_concat_txt)
    audio_path = str(audio_path)
    ffmeta_path = str(ffmeta_path)
    out_mp4 = str(out_mp4)

    Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)

    # Overlay config defaults
    intro_seconds = int(overlay_cfg.get("intro_seconds", 10))
    outro_seconds = int(overlay_cfg.get("outro_seconds", 12))

    title_fontsize = int(overlay_cfg.get("title_fontsize", 42))
    timer_fontsize = int(overlay_cfg.get("timer_fontsize", 34))

    title_x = str(overlay_cfg.get("title_x", "(w-text_w)/2"))
    title_y = str(overlay_cfg.get("title_y", "h-220"))

    timer_x = str(overlay_cfg.get("timer_x", "w-tw-60"))
    timer_y = str(overlay_cfg.get("timer_y", "h-305"))

    intro_x = str(overlay_cfg.get("intro_x", "60"))
    intro_y = str(overlay_cfg.get("intro_y", "h-220"))
    outro_x = str(overlay_cfg.get("outro_x", "60"))
    outro_y = str(overlay_cfg.get("outro_y", "h-220"))

    boxcolor = str(overlay_cfg.get("boxcolor", "black@0.55"))
    timer_boxcolor = str(overlay_cfg.get("timer_boxcolor", "black@0.40"))
    boxborderw = int(overlay_cfg.get("boxborderw", 18))
    timer_boxborderw = int(overlay_cfg.get("timer_boxborderw", 14))

    kenburns_enabled = bool(overlay_cfg.get("kenburns_enabled", True))
    kenburns_zoom_end = float(overlay_cfg.get("kenburns_zoom_end", 1.08))
    kenburns_seconds = int(overlay_cfg.get("kenburns_seconds", 18))
    kenburns_direction = str(overlay_cfg.get("kenburns_direction", "diag"))

    bg_blur_sigma = int(overlay_cfg.get("bg_blur_sigma", 18))
    bg_dark_overlay = float(overlay_cfg.get("bg_dark_overlay", 0.38))

    # SPEED OPTIMIZATION:
    # - Use fps=25 but zoompan d=1:fps=25
    # - Shorter kenburns window reduces compute
    kb_frames = max(1, int(kenburns_seconds * fps))

    # Ken Burns pan expressions
    # Keep simple & cheap. (Avoid heavy per-frame math.)
    if kenburns_direction == "h":
        pan_x = f"(iw-ow)*on/{kb_frames}"
        pan_y = "0"
    elif kenburns_direction == "v":
        pan_x = "0"
        pan_y = f"(ih-oh)*on/{kb_frames}"
    else:
        pan_x = f"(iw-ow)*on/{kb_frames}"
        pan_y = f"(ih-oh)*on/{kb_frames}"

    if kenburns_enabled:
        zoompan = (
            f"zoompan="
            f"z='min(1+({kenburns_zoom_end}-1)*on/{kb_frames},{kenburns_zoom_end})':"
            f"x='{pan_x}':y='{pan_y}':"
            f"d=1:fps={fps}"
        )
    else:
        zoompan = None

    # Build drawtext chain
    # NOTE: avoid fancy quotes; keep font generic.
    filters = []

    base = f"[0:v]scale={width}:{height},format=yuv420p"
    if zoompan:
        base += f",{zoompan}"
    if bg_blur_sigma > 0:
        base += f",gblur=sigma={bg_blur_sigma}"
    if bg_dark_overlay > 0:
        base += f",drawbox=x=0:y=0:w=iw:h=ih:color=black@{bg_dark_overlay}:t=fill"
    base += "[v0]"
    filters.append(base)

    # Intro / Outro text (optional)
    intro_text = _escape_drawtext_literal(str(overlay_cfg.get("intro_text", "AGENDA • Deep Dive Overview")))
    outro_text = _escape_drawtext_literal(str(overlay_cfg.get("outro_text", "Full sources in description • Subscribe for daily briefings")))

    if intro_seconds > 0 and intro_text:
        filters.append(
            "[v0]"
            f"drawtext=font='Sans':text='{intro_text}':"
            f"x={intro_x}:y={intro_y}:fontsize=40:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='{_between(0, max(0, intro_seconds))}'"
            "[v1]"
        )
        cur = "v1"
    else:
        cur = "v0"

    # Segment overlays
    # segments: list of dicts with start/end/title
    for seg in segments or []:
        try:
            st = int(seg.get("start", 0))
            en = int(seg.get("end", st + 1))
            ttl = _escape_drawtext_literal(str(seg.get("title", "") or ""))
        except Exception:
            continue
        if not ttl:
            continue
        if en <= st:
            en = st + 1

        # Title
        filters.append(
            f"[{cur}]"
            f"drawtext=font='Sans':text='{ttl}':"
            f"x={title_x}:y={title_y}:fontsize={title_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='{_between(st, en)}'"
            f"[{cur}t]"
        )
        cur = f"{cur}t"

        # Timer (THIS IS WHERE YOUR ERROR WAS)
        timer = _timer_expr(st)
        filters.append(
            f"[{cur}]"
            f"drawtext=font='Sans':text='{timer}':"
            f"x={timer_x}:y={timer_y}:fontsize={timer_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={timer_boxcolor}:boxborderw={timer_boxborderw}:"
            f"enable='{_between(st, en)}'"
            f"[{cur}m]"
        )
        cur = f"{cur}m"

    # Outro
    # Place outro at end range if duration known via last segment end; else do short tail
    if outro_seconds > 0 and outro_text:
        tail_start = 0
        tail_end = 0
        if segments:
            try:
                tail_end = int(max(s.get("end", 0) for s in segments if isinstance(s, dict)))
            except Exception:
                tail_end = 0
        tail_start = max(0, tail_end - outro_seconds)

        filters.append(
            f"[{cur}]"
            f"drawtext=font='Sans':text='{outro_text}':"
            f"x={outro_x}:y={outro_y}:fontsize=40:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='{_between(tail_start, max(tail_start + 1, tail_end))}'"
            f"[v]"
        )
        out_label = "[v]"
    else:
        filters.append(f"[{cur}]copy[v]")
        out_label = "[v]"

    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", bg_concat_txt,
        "-i", audio_path,
        "-i", ffmeta_path,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "1:a",
        "-map_metadata", "2",
        "-shortest",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        out_mp4,
    ]

    _sh(cmd)
    return out_mp4        last = ch["start_sec"]

    return out if out else [{"start_sec": 0, "title": "Overview"}]


def _write_ffmetadata(meta_path: Path, chapters: List[Dict[str, Any]], total_sec: int) -> None:
    lines = [";FFMETADATA1"]

    if len(chapters) == 1:
        title = chapters[0]["title"].replace("\n", " ").strip()
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1",
            "START=0",
            f"END={max(1, total_sec)}",
            f"title={title}",
        ]
        meta_path.write_text("\n".join(lines), encoding="utf-8")
        return

    for i, ch in enumerate(chapters):
        start = int(ch["start_sec"])
        if i + 1 < len(chapters):
            next_start = int(chapters[i + 1]["start_sec"])
            end = max(start + 1, min(total_sec, next_start - 1))
        else:
            end = max(start + 1, total_sec)

        title = ch["title"].replace("\n", " ").strip() or f"Chapter {i + 1}"
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1",
            f"START={start}",
            f"END={end}",
            f"title={title}",
        ]

    meta_path.write_text("\n".join(lines), encoding="utf-8")


def _escape_drawtext(s: str) -> str:
    s = (s or "")
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ")
    return s


def _cfg(topic_cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = topic_cfg.get("video_overlay") if isinstance(topic_cfg, dict) else None
    v = v if isinstance(v, dict) else {}
    merged = dict(DEFAULTS)
    for k, val in v.items():
        merged[k] = val
    return merged


def _chapter_windows(chapters: List[Dict[str, Any]], total_sec: int) -> List[Dict[str, Any]]:
    windows = []
    for i, ch in enumerate(chapters):
        start = int(ch["start_sec"])
        if i + 1 < len(chapters):
            end = max(start, int(chapters[i + 1]["start_sec"]) - 1)
        else:
            end = total_sec
        windows.append({"start": start, "end": end, "title": ch["title"]})
    return windows


def _build_filters_for_chapters(chapters: List[Dict[str, Any]], total_sec: int, cfg: Dict[str, Any]) -> str:
    windows = _chapter_windows(chapters, total_sec)
    filters = []

    font = str(cfg["font_family"])
    title_fs = int(cfg["title_fontsize"])
    timer_fs = int(cfg["timer_fontsize"])

    title_x = str(cfg["title_x"])
    title_y = str(cfg["title_y"])
    timer_x = str(cfg["timer_x"])
    timer_y = str(cfg["timer_y"])

    boxcolor = str(cfg["boxcolor"])
    timer_boxcolor = str(cfg["timer_boxcolor"])
    boxborderw = int(cfg["boxborderw"])
    timer_boxborderw = int(cfg["timer_boxborderw"])

    for w in windows:
        start = int(w["start"])
        end = int(w["end"])
        title = _escape_drawtext(w["title"])
        enable = f"between(t,{start},{end})"

        filters.append(
            "drawtext="
            f"font='{font}':"
            f"text='{title}':"
            f"x={title_x}:"
            f"y={title_y}:"
            f"fontsize={title_fs}:"
            "fontcolor=white:"
            "box=1:"
            f"boxcolor={boxcolor}:"
            f"boxborderw={boxborderw}:"
            f"enable='{enable}'"
        )

        timer_expr = (
            "%{eif\\:(t-" + str(start) + ")/60\\:d2}"
            ":%{eif\\:mod(t-" + str(start) + ",60)\\:d2}"
        )

        filters.append(
            "drawtext="
            f"font='{font}':"
            f"text='{timer_expr}':"
            f"x={timer_x}:"
            f"y={timer_y}:"
            f"fontsize={timer_fs}:"
            "fontcolor=white:"
            "box=1:"
            f"boxcolor={timer_boxcolor}:"
            f"boxborderw={timer_boxborderw}:"
            f"enable='{enable}'"
        )

    return ",".join(filters)


def _build_intro_outro_filters(total_sec: int, cfg: Dict[str, Any]) -> str:
    filters = []

    intro_text = _escape_drawtext(str(cfg.get("intro_text", "")))
    outro_text = _escape_drawtext(str(cfg.get("outro_text", "")))

    intro_seconds = int(cfg.get("intro_seconds", 10))
    outro_seconds = int(cfg.get("outro_seconds", 12))

    intro_end = max(1, min(intro_seconds, total_sec))
    outro_start = max(0, total_sec - max(1, outro_seconds))

    font = str(cfg["font_family"])
    boxcolor = str(cfg["boxcolor"])
    boxborderw = int(cfg["boxborderw"])

    intro_x = str(cfg["intro_x"])
    intro_y = str(cfg["intro_y"])
    outro_x = str(cfg["outro_x"])
    outro_y = str(cfg["outro_y"])

    if intro_text:
        filters.append(
            "drawtext="
            f"font='{font}':"
            f"text='{intro_text}':"
            f"x={intro_x}:"
            f"y={intro_y}:"
            "fontsize=40:"
            "fontcolor=white:"
            "box=1:"
            f"boxcolor={boxcolor}:"
            f"boxborderw={boxborderw}:"
            f"enable='between(t,0,{intro_end})'"
        )

    if outro_text:
        filters.append(
            "drawtext="
            f"font='{font}':"
            f"text='{outro_text}':"
            f"x={outro_x}:"
            f"y={outro_y}:"
            "fontsize=40:"
            "fontcolor=white:"
            "box=1:"
            f"boxcolor={boxcolor}:"
            f"boxborderw={boxborderw}:"
            f"enable='between(t,{outro_start},{total_sec})'"
        )

    return ",".join(filters)


def _make_concat_image_list(bg_images: List[Path], durations: List[int], list_path: Path) -> None:
    """
    FFmpeg concat demuxer list:
      file '/abs/path/image.jpg'
      duration 12
    Last file must be repeated without duration.
    """
    assert len(bg_images) == len(durations)
    lines = []
    for img, dur in zip(bg_images, durations):
        lines.append(f"file '{img.absolute()}'")
        lines.append(f"duration {max(1, int(dur))}")
    lines.append(f"file '{bg_images[-1].absolute()}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")


def _kenburns_zoompan(cfg: Dict[str, Any], fps: int = 25) -> str:
    """
    Adds a gentle Ken Burns effect using zoompan.
    Works on a continuous video stream (slideshow or static loop).
    """
    enabled = bool(cfg.get("kenburns_enabled", True))
    if not enabled:
        return ""

    zoom_end = float(cfg.get("kenburns_zoom_end", 1.10))
    zoom_end = max(1.0, min(1.25, zoom_end))

    seconds = int(cfg.get("kenburns_seconds", 18))
    seconds = max(6, min(60, seconds))
    frames = seconds * fps

    direction = str(cfg.get("kenburns_direction", "diag")).lower()

    # zoom expression: slowly approach zoom_end, then effectively stays near it
    # Using a linear-ish growth until frames, then clamps.
    z = f"min(1+({zoom_end}-1)*on/{frames}, {zoom_end})"

    # Pan expressions (x/y) depend on direction; 'iw'/'ih' available after scale.
    # NOTE: zoompan uses x/y in source coordinates after zoom.
    if direction == "left":
        x = "0"
        y = "(ih-oh)/2"
    elif direction == "right":
        x = "(iw-ow)"
        y = "(ih-oh)/2"
    elif direction == "up":
        x = "(iw-ow)/2"
        y = "0"
    elif direction == "down":
        x = "(iw-ow)/2"
        y = "(ih-oh)"
    elif direction == "center":
        x = "(iw-ow)/2"
        y = "(ih-oh)/2"
    else:  # diag (default)
        x = "(iw-ow)*on/{}".format(frames)
        y = "(ih-oh)*on/{}".format(frames)

    # We already scale to 1920x1080; keep output fixed.
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:fps={fps}"


def render_waveform_video(
    cover_png: Path,
    mp3_path: Path,
    mp4_path: Path,
    chapters: List[Dict[str, Any]],
    topic_cfg: Dict[str, Any] | None = None,
    bg_images: Optional[List[Path]] = None,
) -> None:
    """
    Video = slideshow (trusted source images) or static cover + overlays.
    Waveform removed. Adds optional Ken Burns effect.
    """
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    if not mp3_path.exists():
        raise RuntimeError(f"Audio file not found: {mp3_path}")
    if not cover_png.exists():
        raise RuntimeError(f"Cover image not found: {cover_png}")

    total_sec = _ffprobe_duration_sec(mp3_path)
    ch_clean = _clean_chapters(chapters, total_sec)

    meta_path = mp4_path.with_suffix(".ffmeta")
    _write_ffmetadata(meta_path, ch_clean, total_sec)

    cfg = _cfg(topic_cfg or {})
    chapter_draw = _build_filters_for_chapters(ch_clean, total_sec, cfg)
    intro_outro_draw = _build_intro_outro_filters(total_sec, cfg)

    blur_sigma = int(cfg.get("bg_blur_sigma", 18))
    dark_alpha = float(cfg.get("bg_dark_overlay", 0.38))
    dark_alpha = max(0.0, min(0.85, dark_alpha))

    inputs = []
    filter_parts = []

    fps = 25
    kb = _kenburns_zoompan(cfg, fps=fps)

    if bg_images and len(bg_images) >= 1:
        windows = _chapter_windows(ch_clean, total_sec)
        if len(bg_images) >= len(windows):
            use_images = bg_images[:len(windows)]
        else:
            use_images = [bg_images[i % len(bg_images)] for i in range(len(windows))]

        durations = []
        for w in windows:
            start, end = int(w["start"]), int(w["end"])
            durations.append(max(1, end - start))

        concat_list = mp4_path.with_suffix(".bg_concat.txt")
        _make_concat_image_list(use_images, durations, concat_list)

        inputs += ["-f", "concat", "-safe", "0", "-i", str(concat_list)]
        bg_input_index = 0
        concat_list_path = concat_list
    else:
        inputs += ["-loop", "1", "-i", str(cover_png)]
        bg_input_index = 0
        concat_list_path = None

    inputs += ["-i", str(mp3_path)]
    inputs += ["-i", str(meta_path)]

    # Background: scale -> (optional kenburns) -> blur -> dark overlay
    bg_chain = f"[{bg_input_index}:v]scale=1920:1080,format=yuv420p"
    if kb:
        bg_chain += f",{kb}"
    bg_chain += f",gblur=sigma={blur_sigma},drawbox=x=0:y=0:w=iw:h=ih:color=black@{dark_alpha}:t=fill[v0]"
    filter_parts.append(bg_chain)

    overlays = ",".join([x for x in [intro_outro_draw, chapter_draw] if x])
    if overlays:
        filter_complex = ";".join(filter_parts) + f";[v0]{overlays}[v]"
        video_map = "[v]"
    else:
        filter_complex = ";".join(filter_parts)
        video_map = "[v0]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", video_map,
        "-map", "1:a",
        "-map_metadata", "2",
        "-shortest",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-r", str(fps),
        "-c:a", "aac",
        "-b:a", "192k",
        str(mp4_path),
    ]
    subprocess.check_call(cmd)

    # cleanup
    try:
        meta_path.unlink()
    except Exception:
        pass

    if concat_list_path:
        try:
            concat_list_path.unlink()
        except Exception:
            pass
