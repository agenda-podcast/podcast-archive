#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore


TRUST_TIERS = [
    # Tier 1
    "reuters.com",
    "bbc.co.uk",
    "bbc.com",
    "nytimes.com",
    "ft.com",
    "wsj.com",
    "apnews.com",
    "bloomberg.com",
    "theguardian.com",
    # Tier 2 (still good)
    "economist.com",
    "axios.com",
    "npr.org",
    "propublica.org",
    "aljazeera.com",
]


def _run(cmd: List[str], timeout: int = 1800) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def _ffprobe_duration_seconds(mp3_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        mp3_path,
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
    # very small and robust regex
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _download_image(url: str, out_path: Path, timeout: int = 20) -> bool:
    if requests is None:
        return False
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "agenda-video/1.0"})
        if r.status_code != 200:
            return False
        data = r.content
        if len(data) < 15_000:
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
    """
    Best-effort: from each source URL, fetch HTML, take og:image, download.
    Prioritize by trust tier.
    """
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
        fn = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", _domain(u) + "_" + str(len(out) + 1)) + ".img"
        p = work_dir / fn
        if _download_image(img, p):
            out.append(p)

    return out


def _escape_drawtext_text(s: str) -> str:
    # Escape for ffmpeg drawtext: \, :, ', and % (keep % only for pts expansions)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


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
    Creates a 1920x1080 video with:
      - background slideshow from related sources (og:image)
      - blur + dark overlay
      - title + elapsed timer
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

        # If no images, use a generated background
        if not imgs:
            # Create a single placeholder frame (dark)
            placeholder = img_dir / "placeholder.png"
            _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080", "-frames:v", "1", str(placeholder)], timeout=300)
            imgs = [placeholder]

        # Build concat list with durations
        per = max(5.0, duration / max(1, len(imgs)))
        concat = td_p / "bg.txt"
        lines: List[str] = []
        for p in imgs:
            # concat demuxer supports: file, duration
            lines.append(f"file '{str(p).replace(\"'\", \"'\\\\''\")}'")
            lines.append(f"duration {per:.3f}")
        # repeat last file without duration line requirement
        lines.append(f"file '{str(imgs[-1]).replace(\"'\", \"'\\\\''\")}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Overlay controls
        bg_blur = float(overlay.get("bg_blur_sigma", 18))
        dark_overlay = float(overlay.get("bg_dark_overlay", 0.38))
        title_fs = int(overlay.get("title_fontsize", 42))
        timer_fs = int(overlay.get("timer_fontsize", 30))

        # positions (simple defaults)
        title_x = str(overlay.get("title_x", "(w-text_w)/2"))
        title_y = str(overlay.get("title_y", "h-220"))
        timer_x = str(overlay.get("timer_x", "w-tw-60"))
        timer_y = str(overlay.get("timer_y", "h-305"))

        # Intro/outro seconds
        intro_sec = int(overlay.get("intro_seconds", topic.get("intro_seconds", 10) or 10))
        outro_sec = int(overlay.get("outro_seconds", topic.get("outro_seconds", 12) or 12))

        # Make safe enable expressions (escape commas)
        def between(t0: float, t1: float) -> str:
            return f"between(t\\,{t0:.3f}\\,{t1:.3f})"

        # Texts
        intro_line = _escape_drawtext_text(intro_text or f"{podcast_title} • Deep Dive Overview")
        outro_line = _escape_drawtext_text(outro_text or "Full sources included • Subscribe for the next briefing")
        main_title = _escape_drawtext_text(title)

        # Global timer (no commas)
        timer_text = "%{pts\\:hms}"

        # Filter: scale/crop, slight motion, blur, dark overlay, text
        vf = (
            "[0:v]"
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "format=yuv420p,"
            "zoompan=z='1.07':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=25,"
            f"gblur=sigma={bg_blur:.1f},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{dark_overlay:.2f}:t=fill,"
            # Intro
            f"drawtext=font='Sans':text='{intro_line}':x=60:y=h-220:fontsize={title_fs}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:enable='{between(0, float(intro_sec))}',"
            # Title
            f"drawtext=font='Sans':text='{main_title}':x={title_x}:y={title_y}:fontsize={title_fs}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18,"
            # Timer (global)
            f"drawtext=font='Sans':text='{timer_text}':x={timer_x}:y={timer_y}:fontsize={timer_fs}:fontcolor=white:box=1:boxcolor=black@0.40:boxborderw=14,"
            # Outro
            f"drawtext=font='Sans':text='{outro_line}':x=60:y=h-220:fontsize={title_fs}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:"
            f"enable='{between(max(0.0, duration - float(outro_sec)), duration)}'"
            "[v]"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-i",
            mp3_path,
            "-i",
            ffmeta_path,
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-map_metadata",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-preset",
            "veryfast",
            "-r",
            "25",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            out_mp4,
        ]

        subprocess.check_call(cmd)
        return out_mp4
