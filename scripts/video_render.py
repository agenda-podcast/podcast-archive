import subprocess
from pathlib import Path
from typing import List, Dict, Any


DEFAULTS = {
    "intro_text": "AGENDA • Automated Deep Dive Overview",
    "outro_text": "Full sources in description • Subscribe for daily briefings",
    "intro_seconds": 10,
    "outro_seconds": 12,
    "font_family": "Sans",
    "title_fontsize": 42,
    "timer_fontsize": 34,
    "title_x": "(w-text_w)/2",
    "title_y": "h-220",
    "timer_x": "w-tw-60",
    "timer_y": "h-305",
    "intro_x": "60",
    "intro_y": "h-220",
    "outro_x": "60",
    "outro_y": "h-220",
    "boxcolor": "black@0.55",
    "timer_boxcolor": "black@0.40",
    "boxborderw": 18,
    "timer_boxborderw": 14,
}


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


def _clean_chapters(chapters: List[Dict[str, Any]], total_sec: int) -> List[Dict[str, Any]]:
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


def render_waveform_video(
    cover_png: Path,
    mp3_path: Path,
    mp4_path: Path,
    chapters: List[Dict[str, Any]],
    topic_cfg: Dict[str, Any] | None = None
) -> None:
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    if not cover_png.exists():
        raise RuntimeError(f"Cover image not found: {cover_png}")
    if not mp3_path.exists():
        raise RuntimeError(f"Audio file not found: {mp3_path}")

    total_sec = _ffprobe_duration_sec(mp3_path)
    ch_clean = _clean_chapters(chapters, total_sec)

    meta_path = mp4_path.with_suffix(".ffmeta")
    _write_ffmetadata(meta_path, ch_clean, total_sec)

    cfg = _cfg(topic_cfg or {})
    chapter_draw = _build_filters_for_chapters(ch_clean, total_sec, cfg)
    intro_outro_draw = _build_intro_outro_filters(total_sec, cfg)

    filter_complex = (
        "[0:v]scale=1920:1080,format=yuv420p[bg];"
        "[1:a]showwaves=s=1920x280:mode=line:rate=25,format=rgba[w];"
        "[bg][w]overlay=0:750:format=auto[v0]"
    )

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
        pass    lines = [";FFMETADATA1"]

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


def _cfg(topic_cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = topic_cfg.get("video_overlay") if isinstance(topic_cfg, dict) else None
    v = v if isinstance(v, dict) else {}
    merged = dict(DEFAULTS)
    for k, val in v.items():
        merged[k] = val
    return merged


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

        # Title lower-third
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

        # Segment timer (elapsed within the current chapter) MM:SS
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

    intro_text = _escape_drawtext(str(cfg["intro_text"]))
    outro_text = _escape_drawtext(str(cfg["outro_text"]))

    intro_seconds = int(cfg["intro_seconds"])
    outro_seconds = int(cfg["outro_seconds"])

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


def render_waveform_video(
    cover_png: Path,
    mp3_path: Path,
    mp4_path: Path,
    chapters: List[Dict[str, Any]],
    topic_cfg: Dict[str, Any] | None = None
) -> None:
    """
    Render MP4:
      - cover as background
      - waveform overlay
      - intro/outro lower-third (per topic config)
      - chapter title lower-third + segment timer (per topic config)
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

    cfg = _cfg(topic_cfg or {})

    chapter_draw = _build_filters_for_chapters(ch_clean, total_sec, cfg)
    intro_outro_draw = _build_intro_outro_filters(total_sec, cfg)

    # Base: cover + waveform => [v0]
    filter_complex = (
        "[0:v]scale=1920:1080,format=yuv420p[bg];"
        "[1:a]showwaves=s=1920x280:mode=line:rate=25,format=rgba[w];"
        "[bg][w]overlay=0:750:format=auto[v0]"
    )

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
