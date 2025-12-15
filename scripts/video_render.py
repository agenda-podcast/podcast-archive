import subprocess
from pathlib import Path
from typing import List, Dict


# --------- Visual tuning (hardcoded) ----------
INTRO_SECONDS = 10
OUTRO_SECONDS = 12

INTRO_TEXT = "AGENDA • Automated Deep Dive Overview"
OUTRO_TEXT = "Full sources in description • Subscribe for daily briefings"

FONT_FAMILY = "Sans"
TITLE_FONTSIZE = 42
TIMER_FONTSIZE = 34


def _ffprobe_duration_sec(audio_path: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    try:
        dur = float(out)
        return max(1, int(round(dur)))
    except Exception:
        return 1800


def _clean_chapters(chapters: List[Dict], total_sec: int) -> List[Dict]:
    if not chapters:
        return [{"start_sec": 0, "title": "Overview"}]

    cleaned = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            s = int(ch.get("start_sec", 0))
        except Exception:
            s = 0
        t = str(ch.get("title", "")).strip() or "Segment"
        s = max(0, min(s, max(0, total_sec - 1)))
        cleaned.append({"start_sec": s, "title": t})

    cleaned.sort(key=lambda x: x["start_sec"])

    if not cleaned or cleaned[0]["start_sec"] != 0:
        cleaned.insert(0, {"start_sec": 0, "title": "Overview"})

    out = []
    last = -1
    for ch in cleaned:
        if ch["start_sec"] <= last:
            continue
        out.append(ch)
        last = ch["start_sec"]

    return out if out else [{"start_sec": 0, "title": "Overview"}]


def _write_ffmetadata(meta_path: Path, chapters: List[Dict], total_sec: int) -> None:
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
    """
    Escape drawtext text value for FFmpeg.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ")
    return s


def _chapter_windows(chapters: List[Dict], total_sec: int) -> List[Dict]:
    """
    Produce [{start, end, title}] windows for chapters.
    end is inclusive-ish for enable(between(t,start,end)).
    """
    windows = []
    for i, ch in enumerate(chapters):
        start = int(ch["start_sec"])
        if i + 1 < len(chapters):
            end = max(start, int(chapters[i + 1]["start_sec"]) - 1)
        else:
            end = total_sec
        windows.append({"start": start, "end": end, "title": ch["title"]})
    return windows


def _build_filters_for_chapters(chapters: List[Dict], total_sec: int) -> str:
    """
    Build drawtext chain:
      - chapter title lower-third during each chapter window
      - timer for current chapter: MM:SS elapsed in that segment
    """
    windows = _chapter_windows(chapters, total_sec)
    filters = []

    for w in windows:
        start = w["start"]
        end = w["end"]
        title = _escape_drawtext(w["title"])
        enable = f"between(t,{start},{end})"

        # Title lower-third (centered, above waveform)
        filters.append(
            "drawtext="
            f"font='{FONT_FAMILY}':"
            f"text='{title}':"
            "x=(w-text_w)/2:"
            "y=h-220:"
            f"fontsize={TITLE_FONTSIZE}:"
            "fontcolor=white:"
            "box=1:"
            "boxcolor=black@0.55:"
            "boxborderw=18:"
            f"enable='{enable}'"
        )

        # Segment timer (right side): elapsed MM:SS since chapter start
        # Use eif to format with leading zeros; compute elapsed = t - start
        timer_expr = (
            "%{eif\\:(t-" + str(start) + ")/60\\:d2}"
            ":%{eif\\:mod(t-" + str(start) + ",60)\\:d2}"
        )

        filters.append(
            "drawtext="
            f"font='{FONT_FAMILY}':"
            f"text='{timer_expr}':"
            "x=w-tw-60:"
            "y=h-305:"
            f"fontsize={TIMER_FONTSIZE}:"
            "fontcolor=white:"
            "box=1:"
            "boxcolor=black@0.40:"
            "boxborderw=14:"
            f"enable='{enable}'"
        )

    return ",".join(filters)


def _build_intro_outro_filters(total_sec: int) -> str:
    filters = []

    intro_text = _escape_drawtext(INTRO_TEXT)
    outro_text = _escape_drawtext(OUTRO_TEXT)

    intro_end = max(1, min(INTRO_SECONDS, total_sec))
    outro_start = max(0, total_sec - OUTRO_SECONDS)

    # Intro lower-third (bottom-left)
    filters.append(
        "drawtext="
        f"font='{FONT_FAMILY}':"
        f"text='{intro_text}':"
        "x=60:"
        "y=h-220:"
        "fontsize=40:"
        "fontcolor=white:"
        "box=1:"
        "boxcolor=black@0.55:"
        "boxborderw=18:"
        f"enable='between(t,0,{intro_end})'"
    )

    # Outro lower-third (bottom-left)
    filters.append(
        "drawtext="
        f"font='{FONT_FAMILY}':"
        f"text='{outro_text}':"
        "x=60:"
        "y=h-220:"
        "fontsize=40:"
        "fontcolor=white:"
        "box=1:"
        "boxcolor=black@0.55:"
        "boxborderw=18:"
        f"enable='between(t,{outro_start},{total_sec})'"
    )

    return ",".join(filters)


def render_waveform_video(cover_png: Path, mp3_path: Path, mp4_path: Path, chapters: List[Dict]) -> None:
    """
    Render MP4:
      - looped cover image as background
      - waveform overlay
      - chapter title lower-third
      - segment timer (MM:SS elapsed within current chapter)
      - intro/outro lower-third
      - embedded chapter metadata
    """
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    if not cover_png.exists():
        raise RuntimeError(f"Cover image not found: {cover_png}")
    if not mp3_path.exists():
        raise RuntimeError(f"Audio file not found: {mp3_path}")

    total_sec = _ffprobe_duration_sec(mp3_path)
    ch_clean = _clean_chapters(chapters, total_sec)

    meta_path = mp4_path.with_suffix(".ffmeta")
    _write_ffmetadata(meta_path, ch_clean, total_sec)

    chapter_draw = _build_filters_for_chapters(ch_clean, total_sec)
    intro_outro_draw = _build_intro_outro_filters(total_sec)

    # Base: cover + waveform => label output as [v0]
    filter_complex = (
        "[0:v]scale=1920:1080,format=yuv420p[bg];"
        "[1:a]showwaves=s=1920x280:mode=line:rate=25,format=rgba[w];"
        "[bg][w]overlay=0:750:format=auto[v0]"
    )

    # Apply text overlays on [v0] -> [v]
    overlays = ",".join([x for x in [intro_outro_draw, chapter_draw] if x])
    if overlays:
        filter_complex += f";[v0]{overlays}[v]"
        video_map = "[v]"
    else:
        video_map = "[v0]"

    subprocess.check_call([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(cover_png),
        "-i", str(mp3_path),
        "-i", str(meta_path),
        "-filter_complex", filter_complex,
        "-map", video_map,
        "-map", "1:a",
        "-map_metadata", "2",
        "-shortest",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "192k",
        str(mp4_path)
    ])

    try:
        meta_path.unlink()
    except Exception:
        pass
