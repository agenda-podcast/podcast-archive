#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore


TRUST_TIERS = [
    "reuters.com",
    "bbc.co.uk",
    "bbc.com",
    "nytimes.com",
    "ft.com",
    "wsj.com",
    "apnews.com",
    "bloomberg.com",
    "theguardian.com",
    "economist.com",
    "axios.com",
    "npr.org",
    "propublica.org",
    "aljazeera.com",
]

DEFAULT_FONTFILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _run(cmd: List[str], timeout: int = 1800) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def _ffprobe_duration_seconds(media_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        media_path,
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "ignore").strip()
    try:
        return float(out)
    except Exception:
        return 0.0


def _domain(u: str) -> str:
    try:
        return (urlparse(u).netloc or "").lower()
    except Exception:
        return ""


def _trust_score(u: str) -> int:
    d = _domain(u)
    for i, dom in enumerate(TRUST_TIERS):
        if d.endswith(dom):
            return 100 - i
    return 10


def _pick_url(item: Dict[str, Any]) -> str:
    for k in ("url", "link", "source_url", "canonical_url"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return ""


def _fetch_html(url: str, timeout: int = 12) -> str:
    if requests is None:
        return ""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "agenda-video/1.0"})
        if r.status_code != 200:
            return ""
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" not in ct and "application/xhtml" not in ct:
            return ""
        return r.text[:500_000]
    except Exception:
        return ""


def _extract_og_image(html: str) -> str:
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _guess_ext_from_url(u: str) -> str:
    try:
        p = urlparse(u).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            if p.endswith(ext):
                return ext
    except Exception:
        pass
    return ""


def _download_image(url: str, out_path: Path, timeout: int = 20) -> bool:
    if requests is None:
        return False
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "agenda-video/1.0"})
        if r.status_code != 200:
            return False

        data = r.content
        if len(data) < 10_000:
            return False

        out_path.write_bytes(data)
        return True
    except Exception:
        return False


def select_and_download_backgrounds(
    sources: List[Dict[str, Any]],
    max_images: int,
    work_dir: Path,
) -> List[Path]:
    candidates: List[str] = []
    for s in sources:
        if isinstance(s, dict):
            u = _pick_url(s) or ""
            if u:
                candidates.append(u)

    candidates = sorted(set(candidates), key=_trust_score, reverse=True)

    out: List[Path] = []
    for u in candidates:
        if len(out) >= max_images:
            break

        html = _fetch_html(u)
        if not html:
            continue

        img = _extract_og_image(html)
        if not img or not img.startswith(("http://", "https://")):
            continue

        ext = _guess_ext_from_url(img) or ".jpg"
        fn = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", _domain(u) + "_" + str(len(out) + 1)) + ext
        p = work_dir / fn

        if _download_image(img, p):
            out.append(p)

    return out


def _escape_drawtext_text(s: str) -> str:
    # drawtext escaping: \, :, ', and (commas should be avoided in text)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ")
    return s


def _ffconcat_quote(path: Path) -> str:
    # ffconcat requires single-quoted paths; escape embedded single quotes safely
    return str(path).replace("'", "'\\''")


def render_background_video(
    *,
    topic_id: str,
    topic: Dict[str, Any],
    mp3_path: str,
    out_mp4: str,
    chapters: List[Dict[str, Any]],
    ffmeta_path: str,
    overlay: Dict[str, Any],
    intro_text: str = "",
    outro_text: str = "",
    sources: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Creates a 1920x1080 video:
      - background slideshow from source og:image
      - blur + dark overlay
      - intro line + main title + global timer
      - optional chapter title overlays (from chapters list)
    """
    max_bg = int(topic.get("max_bg_images", 12))
    duration = _ffprobe_duration_seconds(mp3_path)
    if duration <= 0:
        duration = float(topic.get("duration_sec", 1800))

    title = str(topic.get("title") or topic_id).strip()
    podcast_title = str(topic.get("podcast_title") or "Agenda").strip()

    out_mp4_p = Path(out_mp4)
    out_mp4_p.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        img_dir = td_p / "imgs"
        img_dir.mkdir(parents=True, exist_ok=True)

        imgs: List[Path] = []
        if sources:
            imgs = select_and_download_backgrounds(sources, max_images=max_bg, work_dir=img_dir)

        if not imgs:
            placeholder = img_dir / "placeholder.png"
            _run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080", "-frames:v", "1", str(placeholder)],
                timeout=300,
            )
            imgs = [placeholder]

        # concat list with durations
        per = max(6.0, duration / max(1, len(imgs)))
        concat = td_p / "bg.txt"
        lines: List[str] = []
        for p in imgs:
            lines.append(f"file '{_ffconcat_quote(p)}'")
            lines.append(f"duration {per:.3f}")
        lines.append(f"file '{_ffconcat_quote(imgs[-1])}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Overlay config
        bg_blur = float(overlay.get("bg_blur_sigma", 18))
        dark_overlay = float(overlay.get("bg_dark_overlay", 0.38))
        title_fs = int(overlay.get("title_fontsize", 42))
        timer_fs = int(overlay.get("timer_fontsize", 30))
        chap_fs = int(overlay.get("chapter_fontsize", 40))

        title_x = str(overlay.get("title_x", "(w-text_w)/2"))
        title_y = str(overlay.get("title_y", "h-220"))
        timer_x = str(overlay.get("timer_x", "w-tw-60"))
        timer_y = str(overlay.get("timer_y", "h-305"))

        intro_sec = int(overlay.get("intro_seconds", topic.get("intro_seconds", 10) or 10))
        outro_sec = int(overlay.get("outro_seconds", topic.get("outro_seconds", 12) or 12))

        def between(t0: float, t1: float) -> str:
            # escape commas in expression for ffmpeg
            return f"between(t\\,{t0:.3f}\\,{t1:.3f})"

        intro_line = _escape_drawtext_text(intro_text or f"{podcast_title} • Deep Dive Overview")
        outro_line = _escape_drawtext_text(outro_text or "Full sources included • Subscribe for the next briefing")
        main_title = _escape_drawtext_text(title)

        # Global timer
        timer_text = "%{pts\\:hms}"

        # Base filters
        filters: List[str] = []
        filters.append(
            "[0:v]"
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "format=yuv420p,"
            "zoompan=z='1.07':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=25"
        )
        filters.append(f"gblur=sigma={bg_blur:.1f}")
        filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{dark_overlay:.2f}:t=fill")

        # Intro
        filters.append(
            "drawtext="
            f"fontfile='{DEFAULT_FONTFILE}':"
            f"text='{intro_line}':x=60:y=h-220:fontsize={title_fs}:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=18:"
            f"enable='{between(0.0, float(intro_sec))}'"
        )

        # Main title
        filters.append(
            "drawtext="
            f"fontfile='{DEFAULT_FONTFILE}':"
            f"text='{main_title}':x={title_x}:y={title_y}:fontsize={title_fs}:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=18"
        )

        # Timer
        filters.append(
            "drawtext="
            f"fontfile='{DEFAULT_FONTFILE}':"
            f"text='{timer_text}':x={timer_x}:y={timer_y}:fontsize={timer_fs}:fontcolor=white:"
            "box=1:boxcolor=black@0.40:boxborderw=14"
        )

        # Chapter overlays (best effort; keep it short)
        max_ch = int(overlay.get("max_chapters_overlay", 12))
        ch_used = 0
        for ch in chapters:
            if ch_used >= max_ch:
                break
            if not isinstance(ch, dict):
                continue
            try:
                t0 = float(ch.get("start_sec", 0))
                t1 = float(ch.get("end_sec", t0 + 1))
                if t1 <= t0:
                    t1 = t0 + 1
                txt = _escape_drawtext_text(str(ch.get("title", "Segment")).strip())
                filters.append(
                    "drawtext="
                    f"fontfile='{DEFAULT_FONTFILE}':"
                    f"text='{txt}':x=(w-text_w)/2:y=h-160:fontsize={chap_fs}:fontcolor=white:"
                    "box=1:boxcolor=black@0.55:boxborderw=18:"
                    f"enable='{between(t0, t1)}'"
                )
                ch_used += 1
            except Exception:
                continue

        # Outro
        start_outro = max(0.0, duration - float(outro_sec))
        filters.append(
            "drawtext="
            f"fontfile='{DEFAULT_FONTFILE}':"
            f"text='{outro_line}':x=60:y=h-220:fontsize={title_fs}:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=18:"
            f"enable='{between(start_outro, duration)}'"
        )

        vf = ",".join(filters) + "[v]"

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat),
            "-i", mp3_path,
            "-i", ffmeta_path,
            "-filter_complex", vf,
            "-map", "[v]",
            "-map", "1:a",
            "-map_metadata", "2",
            "-shortest",
            "-movflags", "+faststart",
            "-c:v", "libx264",
            "-crf", "20",
            "-preset", "veryfast",
            "-r", "25",
            "-c:a", "aac",
            "-b:a", "192k",
            out_mp4,
        ]

        subprocess.check_call(cmd)
        return out_mp4


def render_waveform_video(*args: Any, **kwargs: Any) -> str:
    """
    Backward-compatible wrapper.

    Accepts either:
      - render_background_video(**kwargs) style (topic_id/topic/mp3_path/out_mp4/chapters/ffmeta_path/overlay/sources)
      - legacy positional style:
          render_waveform_video(mp3_path, ffmeta_path, out_mp4, segments, overlay_cfg)
        where 'segments' can be chapters.
    """
    if kwargs and "mp3_path" in kwargs and "out_mp4" in kwargs:
        # new call style
        return render_background_video(
            topic_id=str(kwargs.get("topic_id") or kwargs.get("topic") or "topic"),
            topic=kwargs.get("topic") if isinstance(kwargs.get("topic"), dict) else {},
            mp3_path=str(kwargs["mp3_path"]),
            out_mp4=str(kwargs["out_mp4"]),
            chapters=kwargs.get("chapters") if isinstance(kwargs.get("chapters"), list) else [],
            ffmeta_path=str(kwargs.get("ffmeta_path") or ""),
            overlay=kwargs.get("overlay") if isinstance(kwargs.get("overlay"), dict) else {},
            intro_text=str(kwargs.get("intro_text") or ""),
            outro_text=str(kwargs.get("outro_text") or ""),
            sources=kwargs.get("sources") if isinstance(kwargs.get("sources"), list) else None,
        )

    # legacy positional
    mp3_path = str(args[0]) if len(args) > 0 else ""
    ffmeta_path = str(args[1]) if len(args) > 1 else ""
    out_mp4 = str(args[2]) if len(args) > 2 else ""
    segments = args[3] if len(args) > 3 else []
    overlay_cfg = args[4] if len(args) > 4 else {}

    return render_background_video(
        topic_id="topic",
        topic={},
        mp3_path=mp3_path,
        out_mp4=out_mp4,
        chapters=segments if isinstance(segments, list) else [],
        ffmeta_path=ffmeta_path,
        overlay=overlay_cfg if isinstance(overlay_cfg, dict) else {},
        sources=None,
            )
