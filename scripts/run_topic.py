import os
import json
import subprocess
from pathlib import Path
from typing import List, Dict
from pathlib import Path
from datetime import datetime, timezone, timedelta

from sources_collect import collect_sources
from script_generate import generate_30min_script_and_chapters
from tts_generate import tts_to_mp3
from video_render import render_waveform_video
from feed_build import update_topic_feed, load_state, save_state
from github_release import ensure_release_and_upload

TOPIC_ID = os.environ.get("TOPIC_ID", "").strip()
REPO = os.environ.get("REPO", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
RELEASE_TAG = os.environ.get("RELEASE_TAG", TOPIC_ID).strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

def ffprobe_duration_sec(audio_path: Path) -> int:
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


def normalize_chapters(chapters: List[Dict], total_sec: int) -> List[Dict]:
    """
    Ensure:
      - sorted by start_sec
      - first chapter starts at 0
      - last chapter start < total_sec
      - if model timestamps exceed total_sec, compress proportionally
      - if model timestamps end far earlier than total_sec, stretch proportionally
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
        cleaned.append({"start_sec": max(0, s), "title": t})

    cleaned.sort(key=lambda x: x["start_sec"])

    # Ensure first is 0
    if cleaned[0]["start_sec"] != 0:
        cleaned.insert(0, {"start_sec": 0, "title": "Overview"})

    # Remove duplicates / non-increasing starts
    dedup = []
    last = -1
    for ch in cleaned:
        s = ch["start_sec"]
        if s <= last:
            continue
        dedup.append(ch)
        last = s
    cleaned = dedup

    # If only one chapter, keep simple
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
    render_waveform_video(cover, mp3_path, mp4_path, chapters)

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
