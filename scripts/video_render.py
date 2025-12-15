import subprocess
from pathlib import Path

def render_waveform_video(cover_png: Path, mp3_path: Path, mp4_path: Path, chapters: list):
    """
    Builds MP4: static cover + waveform overlay.
    Chapters are embedded as ffmetadata (simple, reliable).
    """
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    meta = mp4_path.with_suffix(".ffmeta")

    # Build ffmetadata chapters
    # Chapters require end times; we'll set sequential 1-second placeholders if missing,
    # and you can improve later by estimating from narration or measuring segment lengths.
    # For now embed basic chapters; players still show them.
    lines = [";FFMETADATA1"]
    for i, ch in enumerate(chapters):
        start = int(ch.get("start_sec", 0))
        end = int(chapters[i+1].get("start_sec", start + 60)) if i + 1 < len(chapters) else start + 300
        title = str(ch.get("title", f"Chapter {i+1}")).replace("\n", " ").strip()
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1",
            f"START={start}",
            f"END={end}",
            f"title={title}"
        ]
    meta.write_text("\n".join(lines), encoding="utf-8")

    # Video with waveform (showwaves) over blurred cover
    # - loop cover to audio length
    subprocess.check_call([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(cover_png),
        "-i", str(mp3_path),
        "-filter_complex",
        " [0:v]scale=1920:1080,format=yuv420p[bg];"
        " [1:a]showwaves=s=1920x280:mode=line,format=rgba[w];"
        " [bg][w]overlay=0:750:format=auto[v] ",
        "-map", "[v]",
        "-map", "1:a",
        "-shortest",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "192k",
        "-metadata:s:v:0", "title=Agenda Overview",
        "-i", str(meta),  # metadata as extra input
        "-map_metadata", "2",
        str(mp4_path)
    ])

    try:
        meta.unlink()
    except Exception:
        pass
