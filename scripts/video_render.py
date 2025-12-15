import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Union, Optional


def _run(cmd: List[str]) -> None:
    subprocess.check_call(cmd)


def _escape_drawtext_literal(s: str) -> str:
    """
    Escape literal text for ffmpeg drawtext.
    - ':' separates options => must be escaped in text
    - ',' is used inside functions (mod()) and can break parsing => escape
    - '\'' can break quoting => escape
    """
    s = s or ""
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace(",", "\\,")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _between(t0: int, t1: int) -> str:
    # drawtext enable uses comma-separated args; escape commas as '\,'
    return f"between(t\\,{int(t0)}\\,{int(t1)})"


def _timer_expr(start_sec: int) -> str:
    """
    Build MM:SS timer relative to start_sec.
    CRITICAL:
      - The visible ':' between MM and SS must be escaped as '\:'
      - The comma in mod(t,60) must be escaped as '\,'
    """
    s = int(start_sec)
    return f"%{{eif\\:(t-{s})/60\\:d2}}\\:%{{eif\\:mod(t-{s}\\,60)\\:d2}}"


def render_waveform_video(
    bg_concat_txt: Union[str, Path],
    audio_path: Union[str, Path],
    ffmeta_path: Union[str, Path],
    out_mp4: Union[str, Path],
    segments: List[Dict[str, Any]],
    overlay_cfg: Dict[str, Any],
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 25,
    crf: int = 21,
    preset: str = "veryfast",
) -> str:
    """
    Render a background slideshow video with:
      - blur + dark overlay
      - intro/outro lower-third text blocks
      - per-segment title and on-screen timer (MM:SS)
    No waveform visual.
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

    # Optional per-topic intro/outro text in overlay config
    intro_text = _escape_drawtext_literal(str(overlay_cfg.get("intro_text", "AGENDA • Deep Dive Overview")))
    outro_text = _escape_drawtext_literal(str(overlay_cfg.get("outro_text", "Full sources in description • Subscribe for daily briefings")))

    # Ken Burns setup (cheap math)
    kb_frames = max(1, int(kenburns_seconds * fps))
    if kenburns_direction == "h":
        pan_x = f"(iw-ow)*on/{kb_frames}"
        pan_y = "0"
    elif kenburns_direction == "v":
        pan_x = "0"
        pan_y = f"(ih-oh)*on/{kb_frames}"
    else:
        pan_x = f"(iw-ow)*on/{kb_frames}"
        pan_y = f"(ih-oh)*on/{kb_frames}"

    zoompan = None
    if kenburns_enabled:
        zoompan = (
            "zoompan="
            f"z='min(1+({kenburns_zoom_end}-1)*on/{kb_frames},{kenburns_zoom_end})':"
            f"x='{pan_x}':y='{pan_y}':"
            f"d=1:fps={fps}"
        )

    filters: List[str] = []

    # Base video processing
    base = f"[0:v]scale={width}:{height},format=yuv420p"
    if zoompan:
        base += f",{zoompan}"
    if bg_blur_sigma > 0:
        base += f",gblur=sigma={bg_blur_sigma}"
    if bg_dark_overlay > 0:
        base += f",drawbox=x=0:y=0:w=iw:h=ih:color=black@{bg_dark_overlay}:t=fill"
    base += "[v0]"
    filters.append(base)

    cur = "v0"

    # Intro block
    if intro_seconds > 0 and intro_text:
        filters.append(
            f"[{cur}]"
            f"drawtext=font='Sans':text='{intro_text}':"
            f"x={intro_x}:y={intro_y}:fontsize=40:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='{_between(0, max(1, intro_seconds))}'"
            "[v1]"
        )
        cur = "v1"

    # Segments overlays: title + timer
    for seg in (segments or []):
        if not isinstance(seg, dict):
            continue
        try:
            st = int(seg.get("start", 0))
            en = int(seg.get("end", st + 1))
        except Exception:
            continue
        if en <= st:
            en = st + 1

        ttl = _escape_drawtext_literal(str(seg.get("title", "") or ""))
        if ttl:
            filters.append(
                f"[{cur}]"
                f"drawtext=font='Sans':text='{ttl}':"
                f"x={title_x}:y={title_y}:fontsize={title_fontsize}:fontcolor=white:"
                f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
                f"enable='{_between(st, en)}'"
                f"[{cur}t]"
            )
            cur = f"{cur}t"

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

    # Outro block (place at the end based on last segment end)
    if outro_seconds > 0 and outro_text:
        last_end = 0
        for seg in (segments or []):
            if isinstance(seg, dict):
                try:
                    last_end = max(last_end, int(seg.get("end", 0)))
                except Exception:
                    pass
        tail_end = max(1, last_end)
        tail_start = max(0, tail_end - outro_seconds)

        filters.append(
            f"[{cur}]"
            f"drawtext=font='Sans':text='{outro_text}':"
            f"x={outro_x}:y={outro_y}:fontsize=40:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='{_between(tail_start, tail_end)}'"
            "[v]"
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

    _run(cmd)
    return out_mp4
