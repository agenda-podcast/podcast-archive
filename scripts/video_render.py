#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


FONTFILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


TIER1 = {
    "reuters.com",
    "bbc.co.uk",
    "bbc.com",
    "nytimes.com",
    "ft.com",
    "wsj.com",
    "apnews.com",
    "theguardian.com",
    "economist.com",
}
TIER2 = {
    "npr.org",
    "pbs.org",
    "cbsnews.com",
    "abcnews.go.com",
    "nbcnews.com",
    "politico.com",
    "axios.com",
    "bloomberg.com",
}


def _domain(u: str) -> str:
    try:
        h = urlparse(u).netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""


def _trust_score(source_url: str) -> int:
    d = _domain(source_url)
    if d in TIER1:
        return 100
    if d in TIER2:
        return 60
    if d:
        return 20
    return 0


def _http_get(url: str, timeout: int = 12) -> requests.Response:
    return requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": "agenda-video-render/1.0 (+https://github.com/agenda-podcast/podcast-archive)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        allow_redirects=True,
    )


def _extract_og_image(html: str) -> str:
    # property="og:image" content="..."
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # name="twitter:image"
    m = re.search(r'name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _download_image(url: str, out_path: Path) -> bool:
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": "agenda-video-render/1.0 (+https://github.com/agenda-podcast/podcast-archive)",
                "Accept": "image/*,*/*;q=0.8",
                "Referer": url,
            },
            stream=True,
            allow_redirects=True,
        )
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").lower()
        if "image" not in ct:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)
        return out_path.exists() and out_path.stat().st_size > 10_000
    except Exception:
        return False


def _pick_best_background(sources: List[Dict[str, Any]], tmp_dir: Path) -> Optional[Path]:
    # Sort by trust tier first
    scored = []
    for s in sources or []:
        u = str(s.get("url") or "")
        if u.startswith("http"):
            scored.append((_trust_score(u), u))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Try top N pages for og:image
    for i, (_, page_url) in enumerate(scored[:12]):
        try:
            resp = _http_get(page_url, timeout=12)
            if resp.status_code >= 400:
                continue
            img = _extract_og_image(resp.text or "")
            if not img or not img.startswith("http"):
                continue
            out = tmp_dir / f"bg_{i:02d}.jpg"
            if _download_image(img, out):
                return out
        except Exception:
            continue

    # Fallback: local poster/cover
    for fallback in [
        Path("poster.jpg"),
        Path("feed/poster.jpg"),
        Path("assets/cover.jpg"),
        Path("assets/cover.png"),
    ]:
        if fallback.exists() and fallback.stat().st_size > 10_000:
            out = tmp_dir / "bg_fallback.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fallback, out)
            return out

    return None


def _ffprobe_duration(path: str) -> float:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True,
            text=True,
            check=True,
        )
        return float((p.stdout or "0").strip() or "0")
    except Exception:
        return 0.0


def _escape_drawtext_text(s: str) -> str:
    """
    For drawtext=text='...'
    - escape backslash
    - escape single quote
    - escape colon (option separator)
    """
    s = (s or "")
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace(":", "\\:")
    return s


def _timer_expr(start: int) -> str:
    # IMPORTANT: the literal ":" between mm and ss MUST be escaped as "\:"
    # Also internal ":" must be escaped as "\:" inside %{...}
    return f"%{{eif\\:(t-{start})/60\\:d2}}\\:%{{eif\\:mod(t-{start},60)\\:d2}}"


def _normalize_segments(chapters: List[Dict[str, Any]], duration: int) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        try:
            title = str(ch.get("title", "Segment")).strip() or "Segment"
            start = int(float(ch.get("start_sec", 0)))
            end = int(float(ch.get("end_sec", start + 1)))
            if end <= start:
                end = start + 1
            if start < 0:
                start = 0
            if end > duration:
                end = duration
            if start >= duration:
                continue
            segs.append({"title": title, "start_sec": start, "end_sec": end})
        except Exception:
            continue

    # If empty, build a single segment
    if not segs:
        segs = [{"title": "Overview", "start_sec": 0, "end_sec": max(1, duration)}]

    # Ensure monotonic ordering
    segs.sort(key=lambda x: x["start_sec"])
    return segs


def render_background_video(
    *,
    topic_id: str,
    topic: Dict[str, Any],
    mp3_path: str,
    out_mp4: str,
    chapters: List[Dict[str, Any]],
    ffmeta_path: str,
    overlay: Dict[str, Any],
    intro_text: str,
    outro_text: str,
    sources: List[Dict[str, Any]],
) -> str:
    out_mp4_p = Path(out_mp4)
    out_mp4_p.parent.mkdir(parents=True, exist_ok=True)

    duration_f = _ffprobe_duration(mp3_path)
    duration = int(duration_f) if duration_f > 1 else int(topic.get("duration_sec", 1800))
    if duration <= 0:
        duration = 1800

    tmp_dir = Path(f"outputs/{topic_id}/_tmp_images")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    bg = _pick_best_background(sources, tmp_dir)
    if bg is None:
        raise RuntimeError("No background image available (no og:image and no local fallback poster/cover).")

    # Overlay config defaults
    intro_seconds = int(float(overlay.get("intro_seconds", 10) or 10))
    outro_seconds = int(float(overlay.get("outro_seconds", 12) or 12))

    title_fontsize = int(float(overlay.get("title_fontsize", 42) or 42))
    timer_fontsize = int(float(overlay.get("timer_fontsize", 34) or 34))

    # Use safe FFmpeg expressions; DO NOT use "tw" variable
    title_x = str(overlay.get("title_x", "(w-text_w)/2") or "(w-text_w)/2")
    title_y = str(overlay.get("title_y", "h-220") or "h-220")
    timer_x = str(overlay.get("timer_x", "w-text_w-60") or "w-text_w-60")
    timer_y = str(overlay.get("timer_y", "h-305") or "h-305")

    intro_x = str(overlay.get("intro_x", "60") or "60")
    intro_y = str(overlay.get("intro_y", "h*0.78") or "h*0.78")
    outro_x = str(overlay.get("outro_x", "60") or "60")
    outro_y = str(overlay.get("outro_y", "h*0.78") or "h*0.78")

    boxcolor = str(overlay.get("boxcolor", "black@0.55") or "black@0.55")
    timer_boxcolor = str(overlay.get("timer_boxcolor", "black@0.40") or "black@0.40")
    boxborderw = int(float(overlay.get("boxborderw", 18) or 18))
    timer_boxborderw = int(float(overlay.get("timer_boxborderw", 14) or 14))

    bg_blur_sigma = int(float(overlay.get("bg_blur_sigma", 18) or 18))
    bg_dark_overlay = float(overlay.get("bg_dark_overlay", 0.38) or 0.38)
    if bg_dark_overlay < 0:
        bg_dark_overlay = 0.0
    if bg_dark_overlay > 0.9:
        bg_dark_overlay = 0.9

    segments = _normalize_segments(chapters, duration)

    # Build drawtext filters (intro/outro lower third + segment title + timer)
    draws: List[str] = []

    # Background processing: blur + dark overlay
    base = (
        f"[0:v]scale=1920:1080,format=yuv420p"
        f",gblur=sigma={bg_blur_sigma}"
        f",drawbox=x=0:y=0:w=iw:h=ih:color=black@{bg_dark_overlay}:t=fill[v0]"
    )

    # Intro lower-third
    if intro_text:
        intro_txt = _escape_drawtext_text(intro_text)
        draws.append(
            "drawtext="
            f"fontfile='{FONTFILE}':text='{intro_txt}':"
            f"x={intro_x}:y={intro_y}:fontsize={title_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='between(t,0,{max(1,intro_seconds)})'"
        )

    # Outro lower-third (last outro_seconds)
    if outro_text and outro_seconds > 0:
        outro_start = max(0, duration - outro_seconds)
        outro_txt = _escape_drawtext_text(outro_text)
        draws.append(
            "drawtext="
            f"fontfile='{FONTFILE}':text='{outro_txt}':"
            f"x={outro_x}:y={outro_y}:fontsize={title_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='between(t,{outro_start},{duration})'"
        )

    # Per-segment title + timer
    for seg in segments:
        st = int(seg["start_sec"])
        en = int(seg["end_sec"])
        if en <= st:
            en = st + 1
        title = _escape_drawtext_text(str(seg["title"]))
        timer = _timer_expr(st)

        draws.append(
            "drawtext="
            f"fontfile='{FONTFILE}':text='{title}':"
            f"x={title_x}:y={title_y}:fontsize={title_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}:"
            f"enable='between(t,{st},{en})'"
        )
        draws.append(
            "drawtext="
            f"fontfile='{FONTFILE}':text='{timer}':"
            f"x={timer_x}:y={timer_y}:fontsize={timer_fontsize}:fontcolor=white:"
            f"box=1:boxcolor={timer_boxcolor}:boxborderw={timer_boxborderw}:"
            f"enable='between(t,{st},{en})'"
        )

    filter_complex = base + ";" + "[v0]" + ",".join(draws) + "[v]"

    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(bg),
        "-i",
        mp3_path,
        "-i",
        ffmeta_path,
        "-filter_complex",
        filter_complex,
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
        str(out_mp4_p),
    ]

    subprocess.check_call(cmd)

    # Cleanup temp images
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return str(out_mp4_p)


# Backward-compat wrapper (if any old code calls this)
def render_waveform_video(*args: Any, **kwargs: Any) -> str:
    raise RuntimeError("render_waveform_video is deprecated. Use render_background_video(...) instead.")
