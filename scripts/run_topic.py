import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from image_fetch import select_and_download_backgrounds
from script_generate import generate_30min_script_and_chapters
from tts_generate import tts_chunks_to_mp3
from video_render import render_waveform_video


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


def utc_now_tag() -> str:
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


def normalize_chapters(chapters: List[Dict[str, Any]], total_sec: int) -> List[Dict[str, Any]]:
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
        cleaned.append({"start_sec": max(0, s), "title": t})

    cleaned.sort(key=lambda x: x["start_sec"])
    if cleaned[0]["start_sec"] != 0:
        cleaned.insert(0, {"start_sec": 0, "title": "Overview"})

    # Remove non-increasing
    dedup = []
    last = -1
    for ch in cleaned:
        if ch["start_sec"] <= last:
            continue
        dedup.append(ch)
        last = ch["start_sec"]
    cleaned = dedup

    # Scale timestamps to fit total_sec (chapter timing is heuristic)
    if len(cleaned) > 1:
        last_start = cleaned[-1]["start_sec"]
        if last_start > 0:
            target_last = max(1, int(total_sec * 0.90))
            scale = target_last / float(last_start)
            scaled = []
            for ch in cleaned:
                s = int(round(ch["start_sec"] * scale))
                scaled.append({"start_sec": max(0, min(s, max(0, total_sec - 1))), "title": ch["title"]})
            scaled.sort(key=lambda x: x["start_sec"])

            final = []
            last = -1
            for ch in scaled:
                if ch["start_sec"] <= last:
                    continue
                final.append(ch)
                last = ch["start_sec"]

            if not final or final[0]["start_sec"] != 0:
                final.insert(0, {"start_sec": 0, "title": "Overview"})
            cleaned = final

    return cleaned


def load_topic() -> Dict[str, Any]:
    topic_path = TOPICS_DIR / f"{TOPIC_ID}.json"
    if not topic_path.exists():
        raise RuntimeError(f"Missing topic config: {topic_path}")
    return json.loads(topic_path.read_text(encoding="utf-8"))


def load_sources() -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Adjust here if your filenames differ.
    Looks for:
      data/<topic>/fresh.json
      data/<topic>/backlog.json
      data/<topic>/sources.json (fallback)
    """
    fresh_path = DATA_DIR / "fresh.json"
    backlog_path = DATA_DIR / "backlog.json"
    sources_path = DATA_DIR / "sources.json"

    fresh = []
    backlog = []

    if fresh_path.exists():
        fresh = json.loads(fresh_path.read_text(encoding="utf-8"))
    if backlog_path.exists():
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
    if (not fresh and not backlog) and sources_path.exists():
        fresh = json.loads(sources_path.read_text(encoding="utf-8"))

    if not isinstance(fresh, list):
        fresh = []
    if not isinstance(backlog, list):
        backlog = []

    return fresh, len(fresh), len(backlog)


def split_script_by_chapter_markers(script: str) -> List[Dict[str, Any]]:
    """
    Expects markers:
      === CHAPTER: Title ===
    Returns list of {"chapter_title": str, "text": str}
    """
    script = script.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not script:
        return [{"chapter_title": "Episode", "text": "SPEAKER_A: This is an automated overview."}]

    pattern = r"^=== CHAPTER:\s*(.*?)\s*===$"
    lines = script.split("\n")

    chunks: List[Dict[str, Any]] = []
    cur_title = "Overview"
    cur_lines: List[str] = []

    def flush():
        nonlocal cur_lines, cur_title
        text = "\n".join(cur_lines).strip()
        if text:
            chunks.append({"chapter_title": cur_title, "text": text})
        cur_lines = []

    for ln in lines:
        m = re.match(pattern, ln.strip())
        if m:
            flush()
            cur_title = m.group(1).strip() or "Segment"
            continue
        cur_lines.append(ln)

    flush()

    if not chunks:
        chunks = [{"chapter_title": "Episode", "text": "SPEAKER_A: This is an automated overview."}]
    return chunks


def main() -> None:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is empty.")
    topic = load_topic()

    fresh_items, fresh_count, backlog_count = load_sources()
    print(f"[{TOPIC_ID}] fresh={fresh_count}, backlog_total={backlog_count}")

    min_fresh = int(topic.get("min_fresh_sources", 20))
    if fresh_count < min_fresh:
        # Optional: do not fail pipeline; just skip creation.
        raise RuntimeError(f"Not enough fresh sources ({fresh_count} < {min_fresh}).")

    # Limit items passed into script generation
    max_items = int(topic.get("max_items_for_script", 60))
    items_for_script = fresh_items[:max_items]

    package = generate_30min_script_and_chapters(topic, items_for_script, GEMINI_API_KEY)
    script_text = package["script"]
    chapters = package["chapters"]

    date_tag = utc_now_tag()
    base_name = f"{TOPIC_ID}-{date_tag}"

    # Write script artifact
    (OUT_DIR / f"{base_name}.txt").write_text(script_text, encoding="utf-8")

    # TTS by chapter markers -> MP3
    mp3_path = OUT_DIR / f"{base_name}.mp3"
    tts_chunks = split_script_by_chapter_markers(script_text)
    tts_chunks_to_mp3(tts_chunks, mp3_path, GEMINI_API_KEY)

    # Recompute duration and normalize chapter timestamps
    real_duration = ffprobe_duration_sec(mp3_path)
    chapters = normalize_chapters(chapters, real_duration)

    # Video render
    cover = ASSETS_DIR / "cover.png"
    if not cover.exists():
        raise RuntimeError(f"Missing cover: {cover}")

    mp4_path = OUT_DIR / f"{base_name}.mp4"
    render_waveform_video(
        cover_png=cover,
        mp3_path=mp3_path,
        mp4_path=mp4_path,
        chapters=chapters,
        topic_cfg=topic,
    )

    # Episode payload (you may already have RSS builder elsewhere)
    episode = {
        "topic_id": TOPIC_ID,
        "title": f"{topic.get('title','Agenda')} — Daily Overview ({date_tag})",
        "guid": f"{TOPIC_ID}-{date_tag}",
        "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "itunes_duration": real_duration,
        "description_html": package.get("description_html", ""),
        "chapters": chapters,
        "artifacts": {
            "mp3": str(mp3_path),
            "mp4": str(mp4_path),
            "script": str(OUT_DIR / f"{base_name}.txt"),
        },
    }

    (OUT_DIR / f"{base_name}.episode.json").write_text(json.dumps(episode, indent=2), encoding="utf-8")

    print(f"[{TOPIC_ID}] OK: mp3={mp3_path.name} ({real_duration}s), mp4={mp4_path.name}")


if __name__ == "__main__":
    main()    # If only one chapter, keep simple
    if len(cleaned) == 1:
        return cleaned

    last_start = cleaned[-1]["start_sec"]
    if last_start <= 0:
        return [{"start_sec": 0, "title": "Overview"}]

    # Compute scale so that last chapter starts at ~90% of total duration
    target_last = max(1, int(total_sec * 0.90))
    scale = target_last / float(last_start)

    # Apply scaling
    scaled = []
    for ch in cleaned:
        s = int(round(ch["start_sec"] * scale))
        scaled.append({"start_sec": max(0, min(s, max(0, total_sec - 1))), "title": ch["title"]})

    # Re-sort and ensure increasing
    scaled.sort(key=lambda x: x["start_sec"])
    final = []
    last = -1
    for ch in scaled:
        if ch["start_sec"] <= last:
            continue
        final.append(ch)
        last = ch["start_sec"]

    if not final or final[0]["start_sec"] != 0:
        final.insert(0, {"start_sec": 0, "title": "Overview"})

    return final

def utc_today():
    return datetime.now(timezone.utc).date().isoformat()

def main():
    if not TOPIC_ID:
        raise RuntimeError("TOPIC_ID missing")
    if not REPO:
        raise RuntimeError("REPO missing")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    topic_path = Path("topics") / f"{TOPIC_ID}.json"
    if not topic_path.exists():
        raise RuntimeError(f"Missing topic config: {topic_path}")

    topic = json.loads(topic_path.read_text(encoding="utf-8"))

    state = load_state(TOPIC_ID)

    # 1) Collect sources (fresh 24h) and append to backlog (kept 14 days)
    collected = collect_sources(topic, state)

    fresh_count = collected["fresh_count"]
    backlog_total = collected["backlog_total"]

    print(f"[{TOPIC_ID}] fresh={fresh_count}, backlog_total={backlog_total}")

    # Minimum gating: at least 20 fresh OR accumulate to 20 total backlog
    MIN_FRESH = int(topic.get("min_fresh_sources", 20))
    MIN_BACKLOG = int(topic.get("min_backlog_sources", 20))

    should_publish = (fresh_count >= MIN_FRESH) or (backlog_total >= MIN_BACKLOG)

    # Prevent double publish in same UTC day
    if state.get("last_episode_date") == utc_today():
        print(f"[{TOPIC_ID}] Already published today; skipping.")
        should_publish = False

    if not should_publish:
        save_state(TOPIC_ID, state)
        print(f"[{TOPIC_ID}] Not enough sources yet. Saved backlog for accumulation.")
        return

    # 2) Prepare inputs for script: pick top N from backlog (e.g., 30–60)
    max_items = int(topic.get("max_items_for_script", 60))
    items = state["backlog"][:max_items]

    # 3) Generate 30-min script + chapters (timestamps) using Gemini
    package = generate_30min_script_and_chapters(topic, items, GEMINI_API_KEY)
    script_text = package["script"]
    chapters = package["chapters"]  # list of {start_sec, title}

    # 4) TTS -> MP3 (Gemini Speech generation)
    out_dir = Path("out") / TOPIC_ID
    out_dir.mkdir(parents=True, exist_ok=True)

    date_tag = utc_today().replace("-", "")
    base_name = f"{date_tag}-{TOPIC_ID}"
    mp3_path = out_dir / f"{base_name}.mp3"
    tts_to_mp3(script_text, mp3_path, GEMINI_API_KEY)

    # Recompute duration from the actual MP3
    real_duration = ffprobe_duration_sec(mp3_path)

    # Normalize chapters to match actual duration
    chapters = normalize_chapters(chapters, real_duration)

    # 5) Video: cover + waveform + chapters -> MP4
    cover = Path("assets") / TOPIC_ID / "cover.png"
    if not cover.exists():
        raise RuntimeError(f"Missing cover: {cover}")

    mp4_path = out_dir / f"{base_name}.mp4"
    # ---- Download trusted background images (temporary) ----
    tmp_img_dir = OUT_DIR / "_tmp_bg_images"
    bg_images = []
    picked = []
    try:
    # items_for_script — ваш список источников, лучше свежие + топ по trust tier
    bg_images, picked = select_and_download_backgrounds(
        items=items_for_script,
        tmp_dir=tmp_img_dir,
        max_images=int(topic.get("max_bg_images", 8)),
    )
    print(f"[{TOPIC_ID}] backgrounds downloaded: {len(bg_images)}")
        except Exception as e:
    print(f"[{TOPIC_ID}] background download skipped: {e}")
    bg_images = []

# Render video with slideshow backgrounds if available
render_waveform_video(
    cover_png=cover,
    mp3_path=mp3_path,
    mp4_path=mp4_path,
    chapters=chapters,
    topic_cfg=topic,
    bg_images=bg_images if bg_images else None,
)

# Cleanup temp images after video creation
try:
    for p in bg_images:
        try:
            p.unlink()
        except Exception:
            pass
    if tmp_img_dir.exists():
        tmp_img_dir.rmdir()
except Exception:
    pass
    render_waveform_video(
        cover_png=cover,
        mp3_path=mp3_path,
        mp4_path=mp4_path,
        chapters=chapters,
        topic_cfg=topic
    )

    # 6) Upload to GitHub Release (tag == topic-id)
    urls = ensure_release_and_upload(
        repo=REPO,
        token=GITHUB_TOKEN,
        tag=RELEASE_TAG,
        files=[mp3_path, mp4_path]
    )

    mp3_url = urls[str(mp3_path.name)]

    # 7) Update RSS feed for this topic
    episode = {
    "title": f"{topic['title']} — Daily Overview ({utc_today()})",
    "guid": f"{TOPIC_ID}-{date_tag}",
    "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
    "enclosure_url": mp3_url,
    "enclosure_type": "audio/mpeg",
    "description_html": package["description_html"],
    "itunes_duration": real_duration,
    "chapters": chapters,
}

    update_topic_feed(TOPIC_ID, topic, state, episode)

    # 8) Mark published day + (опционально) очистить backlog или “сдвигать окно”
    state["last_episode_date"] = utc_today()
    # Обычно: очищаем backlog, но оставляем “неиспользованные” хвосты
    used_urls = set([x["url"] for x in items])
    state["backlog"] = [x for x in state["backlog"] if x["url"] not in used_urls]

    save_state(TOPIC_ID, state)
    print(f"[{TOPIC_ID}] Published. MP3={mp3_url}")

if __name__ == "__main__":
    main()
