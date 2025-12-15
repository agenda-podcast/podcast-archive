import subprocess
from pathlib import Path
from typing import List, Dict


def _ffprobe_duration_sec(audio_path: Path) -> int:
    """
    Return duration in seconds using ffprobe.
    """
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
    """
    Ensure chapters:
      - list of dicts with start_sec(int) and title(str)
      - sorted
      - first starts at 0
      - strictly increasing starts
      - all starts within [0, total_sec-1]
    """
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

    # Ensure strictly increasing
    out = []
    last = -1
    for ch in cleaned:
        if ch["start_sec"] <= last:
            continue
        out.append(ch)
        last = ch["start_sec"]

    if not out:
        return [{"start_sec": 0, "title": "Overview"}]

    return out


def _write_ffmetadata(meta_path: Path, chapters: List[Dict], total_sec: int) -> None:
    """
    Write ffmetadata with accurate chapter START/END.
    """
    lines = [";FFMETADATA1"]

    # If only one chapter, cover whole duration
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

        title = str(ch["title"]).replace("\n", " ").strip()
        if not title:
            title = f"Chapter {i + 1}"

        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1",
            f"START={start}",
            f"END={end}",
            f"title={title}",
        ]

    meta_path.write_text("\n".join(lines), encoding="utf-8")


def render_waveform_video(cover_png: Path, mp3_path: Path, mp4_path: Path, chapters: List[Dict]) -> None:
    """
    Render MP4:
      - static cover image (looped)
      - waveform overlay generated from the audio
      - embeds chapter metadata

    Requires: ffmpeg, ffprobe installed on runner.
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

    # Build video:
    # - scale cover to 1920x1080
    # - waveform at bottom
    # - map metadata from ffmetadata input
    #
    # Notes:
    # - showwaves works well and is deterministic
    # - we do not attempt speech-to-text subtitles; only chapters
    filter_complex = (
        "[0:v]scale=1920:1080,format=yuv420p[bg];"
        "[1:a]showwaves=s=1920x280:mode=line:rate=25,format=rgba[w];"
        "[bg][w]overlay=0:750:format=auto[v]"
    )

    # Use 3 inputs: cover, audio, metadata
    subprocess.check_call([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(cover_png),
        "-i", str(mp3_path),
        "-i", str(meta_path),
        "-filter_complex", filter_complex,
        "-map", "[v]",
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
