import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from script_generate import generate_30min_script_and_chapters
from tts_generate import tts_chunks_to_mp3
from video_render import render_waveform_video
from image_fetch import select_and_download_backgrounds


TOPIC_ID = os.environ.get("TOPIC_ID", "topic-01").strip()
REPO = os.environ.get("REPO", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
RELEASE_TAG = os.environ.get("RELEASE_TAG", TOPIC_ID).strip()

PODCAST_OWNER_NAME = os.environ.get("PODCAST_OWNER_NAME", "Agenda").strip()
PODCAST_OWNER_EMAIL = os.environ.get("PODCAST_OWNER_EMAIL", "").strip()

ROOT = Path(".")
TOPICS_DIR = ROOT / "topics"
DATA_DIR = ROOT / "data" / TOPIC_ID
OUT_DIR = ROOT / "outputs" / TOPIC_ID
ASSETS_DIR = ROOT / "assets" / TOPIC_ID

OUT_DIR.mkdir(parents=True, exist_ok=True)


def utc_date_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def ffprobe_duration_sec(audio_path: Path) -> int:
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


def load_topic() -> Dict[str, Any]:
    topic_path = TOPICS_DIR / f"{TOPIC_ID}.json"
    if not topic_path.exists():
        raise RuntimeError(f"Missing topic config: {topic_path}")
    return json.loads(topic_path.read_text(encoding="utf-8"))


def load_sources() -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Expected files:
      data/<topic>/fresh.json
      data/<topic>/backlog.json
      data/<topic>/sources.json (fallback)
    """
    fresh_path = DATA_DIR / "fresh.json"
    backlog_path = DATA_DIR / "backlog.json"
    sources_path = DATA_DIR / "sources.json"

    fresh: List[Dict[str, Any]] = []
    backlog: List[Dict[str, Any]] = []

    if fresh_path.exists():
        x = json.loads(fresh_path.read_text(encoding="utf-8"))
        if isinstance(x, list):
            fresh = x

    if backlog_path.exists():
        x = json.loads(backlog_path.read_text(encoding="utf-8"))
        if isinstance(x, list):
            backlog = x

    if (not fresh and not backlog) and sources_path.exists():
        x = json.loads(sources_path.read_text(encoding="utf-8"))
        if isinstance(x, list):
            fresh = x

    return fresh, len(fresh), len(backlog)


def split_script_by_chapter_markers(script: str) -> List[Dict[str, Any]]:
    """
    Splits on markers:
      === CHAPTER: Title ===
    Returns:
      [{"chapter_title": "...", "text": "...SPEAKER_A/B..."}]
    """
    script = (script or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not script:
        return [{"chapter_title": "Episode", "text": "SPEAKER_A: This is an automated overview."}]

    marker = re.compile(r"^=== CHAPTER:\s*(.*?)\s*===$")
    lines = script.split("\n")

    chunks: List[Dict[str, Any]] = []
    cur_title = "Overview"
    cur_lines: List[str] = []

    def flush() -> None:
        nonlocal cur_lines, cur_title
        text = "\n".join(cur_lines).strip()
        if text:
            chunks.append({"chapter_title": cur_title, "text": text})
        cur_lines = []

    for ln in lines:
        m = marker.match(ln.strip())
        if m:
            flush()
            cur_title = (m.group(1) or "").strip() or "Segment"
            continue
        cur_lines.append(ln)

    flush()

    if not chunks:
        chunks = [{"chapter_title": "Episode", "text": "SPEAKER_A: This is an automated overview."}]
    return chunks


def normalize_chapters(chapters: List[Dict[str, Any]], total_sec: int) -> List[Dict[str, Any]]:
    """
    Ensures:
      - sorted
      - starts at 0
      - monotonic increasing
      - clamped to [0, total_sec-1]
    """
    if not chapters:
        return [{"start_sec": 0, "title": "Overview"}]

    cleaned: List[Dict[str, Any]] = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            s = int(ch.get("start_sec", 0))
        except Exception:
            s = 0
        title = str(ch.get("title", "")).strip() or "Segment"
        s = max(0, min(s, max(0, total_sec - 1)))
        cleaned.append({"start_sec": s, "title": title})

    cleaned.sort(key=lambda x: x["start_sec"])
    if not cleaned or cleaned[0]["start_sec"] != 0:
        cleaned.insert(0, {"start_sec": 0, "title": "Overview"})

    out: List[Dict[str, Any]] = []
    last = -1
    for ch in cleaned:
        if ch["start_sec"] <= last:
            continue
        out.append(ch)
        last = ch["start_sec"]

    return out if out else [{"start_sec": 0, "title": "Overview"}]


def main() -> None:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is empty.")
    if not TOPIC_ID:
        raise RuntimeError("TOPIC_ID is empty.")

    topic = load_topic()

    fresh_items, fresh_count, backlog_count = load_sources()
    print(f"[{TOPIC_ID}] fresh={fresh_count}, backlog_total={backlog_count}")

    min_fresh = int(topic.get("min_fresh_sources", 20))
    if fresh_count < min_fresh:
        raise RuntimeError(f"Not enough fresh sources ({fresh_count} < {min_fresh}).")

    max_items = int(topic.get("max_items_for_script", 60))
    items_for_script = fresh_items[:max_items]

    # 1) Generate script + chapters
    package = generate_30min_script_and_chapters(topic, items_for_script, GEMINI_API_KEY)
    script_text = package.get("script", "") or ""
    chapters = package.get("chapters", []) or []

    date_tag = utc_date_tag()
    base_name = f"{TOPIC_ID}-{date_tag}"

    script_path = OUT_DIR / f"{base_name}.txt"
    script_path.write_text(script_text, encoding="utf-8")

    # 2) TTS by chapters -> MP3
    mp3_path = OUT_DIR / f"{base_name}.mp3"
    tts_chunks = split_script_by_chapter_markers(script_text)
    tts_chunks_to_mp3(tts_chunks, mp3_path, GEMINI_API_KEY)

    real_duration = ffprobe_duration_sec(mp3_path)
    chapters = normalize_chapters(chapters, real_duration)

    # 3) Download background images temporarily (trusted tier) -> render slideshow video
    cover = ASSETS_DIR / "cover.png"
    if not cover.exists():
        raise RuntimeError(f"Missing cover: {cover}")

    tmp_img_dir = OUT_DIR / "_tmp_bg_images"
    bg_images: List[Path] = []
    picked = []

    try:
        bg_images, picked = select_and_download_backgrounds(
            items=items_for_script,
            tmp_dir=tmp_img_dir,
            max_images=int(topic.get("max_bg_images", 8)),
        )
        print(f"[{TOPIC_ID}] backgrounds downloaded: {len(bg_images)}")
    except Exception as e:
        print(f"[{TOPIC_ID}] background download skipped: {e}")
        bg_images = []
        picked = []

    mp4_path = OUT_DIR / f"{base_name}.mp4"
    render_waveform_video(
        cover_png=cover,
        mp3_path=mp3_path,
        mp4_path=mp4_path,
        chapters=chapters,
        topic_cfg=topic,
        bg_images=bg_images if bg_images else None,
    )

    # 4) Cleanup temp images
    try:
        for p in bg_images:
            try:
                p.unlink()
            except Exception:
                pass
        if tmp_img_dir.exists():
            for extra in tmp_img_dir.glob("*"):
                try:
                    extra.unlink()
                except Exception:
                    pass
            try:
                tmp_img_dir.rmdir()
            except Exception:
                pass
    except Exception:
        pass

    # 5) Save episode descriptor (for RSS/release step)
    episode = {
        "topic_id": TOPIC_ID,
        "title": f"{topic.get('title', 'Agenda')} — Daily Overview ({date_tag})",
        "guid": f"{TOPIC_ID}-{date_tag}",
        "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "itunes_duration": int(real_duration),
        "description_html": package.get("description_html", ""),
        "chapters": chapters,
        "artifacts": {
            "mp3": str(mp3_path),
            "mp4": str(mp4_path),
            "script": str(script_path),
        },
        "backgrounds_used": [
            {
                "tier": getattr(x, "tier", None),
                "domain": getattr(x, "domain", None),
                "source_url": getattr(x, "source_url", None),
                "image_url": getattr(x, "image_url", None),
            }
            for x in picked
        ],
    }

    (OUT_DIR / f"{base_name}.episode.json").write_text(
        json.dumps(episode, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[{TOPIC_ID}] OK: mp3={mp3_path.name} ({real_duration}s), mp4={mp4_path.name}")


if __name__ == "__main__":
    main()
