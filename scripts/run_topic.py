#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import sys
import time
import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Local modules (expected in scripts/)
from tts_generate import tts_chunks_to_mp3, script_to_tts_chunks  # your updated file

# script_generate must exist in repo
from script_generate import generate_30min_script_and_chapters


# =========================
# Paths / Constants
# =========================
TOPICS_DIR = Path("topics")
DATA_DIR = Path("data")
OUTPUTS_DIR = Path("outputs")
FEEDS_DIR = Path("feeds")

GITHUB_API = "https://api.github.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    safe_mkdir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def is_url(s: str) -> bool:
    return isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))


def pick_source_url(item: Dict[str, Any]) -> str:
    for k in ("url", "link", "source_url", "canonical_url"):
        v = item.get(k)
        if is_url(v):
            return v
    return ""


def pick_source_title(item: Dict[str, Any]) -> str:
    for k in ("title", "name", "headline"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "Untitled"


def pick_source_date(item: Dict[str, Any]) -> str:
    for k in ("published", "published_at", "date", "datetime", "ts_utc"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def normalize_source(item: Dict[str, Any]) -> Dict[str, Any]:
    url = pick_source_url(item)
    title = pick_source_title(item)
    dt = pick_source_date(item)
    lang = item.get("lang") or item.get("language") or ""
    src = item.get("source") or item.get("publisher") or item.get("site") or ""
    return {
        "title": title,
        "url": url,
        "published": dt,
        "source": src,
        "lang": lang,
        "raw": item,
        "key": sha1((url or title) + "|" + (dt or "")),
    }


def dedupe_sources(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        k = it.get("key") or sha1((it.get("url", "") or it.get("title", "")) + "|" + (it.get("published", "") or ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# =========================
# GitHub Release helpers
# =========================
def gh_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "agenda-topic-runner/1.0",
    }


def ensure_release(repo: str, token: str, tag: str) -> Dict[str, Any]:
    # Try existing
    r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=gh_headers(token), timeout=60)
    if r.status_code == 200:
        return r.json()

    # Create
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    r = requests.post(f"{GITHUB_API}/repos/{repo}/releases", headers=gh_headers(token), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def list_assets(release: Dict[str, Any], token: str) -> List[Dict[str, Any]]:
    url = release.get("assets_url")
    if not url:
        return []
    r = requests.get(url, headers=gh_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()


def delete_asset(asset_api_url: str, token: str) -> None:
    r = requests.delete(asset_api_url, headers=gh_headers(token), timeout=60)
    if r.status_code not in (204, 404):
        r.raise_for_status()


def upload_asset(release: Dict[str, Any], token: str, file_path: Path) -> str:
    """
    Upload asset. If asset name exists, delete then upload.
    Returns browser_download_url.
    """
    name = file_path.name
    assets = list_assets(release, token)
    for a in assets:
        if a.get("name") == name:
            delete_asset(a["url"], token)
            break

    upload_url = release["upload_url"].split("{")[0]
    with file_path.open("rb") as f:
        r = requests.post(
            f"{upload_url}?name={name}",
            headers={**gh_headers(token), "Content-Type": "application/octet-stream"},
            data=f,
            timeout=600,
        )
    r.raise_for_status()
    return r.json().get("browser_download_url", "")


# =========================
# Topic load + state
# =========================
def load_topic(topic_id: str) -> Dict[str, Any]:
    p = TOPICS_DIR / f"{topic_id}.json"
    if not p.exists():
        raise RuntimeError(f"Topic file missing: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Invalid JSON in {p}: {e}")


def topic_paths(topic_id: str) -> Dict[str, Path]:
    base = DATA_DIR / topic_id
    return {
        "base": base,
        "fresh": base / "fresh.json",
        "backlog": base / "backlog.json",
        "picked": base / "picked_for_script.json",
    }


def load_sources_for_topic(topic_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    paths = topic_paths(topic_id)
    fresh_raw = load_json(paths["fresh"], default=[])
    backlog_raw = load_json(paths["backlog"], default=[])

    fresh = [normalize_source(x) for x in fresh_raw if isinstance(x, dict)]
    backlog = [normalize_source(x) for x in backlog_raw if isinstance(x, dict)]

    fresh = dedupe_sources(fresh)
    backlog = dedupe_sources(backlog)
    return fresh, backlog


def pick_sources_for_script(topic: Dict[str, Any], fresh: List[Dict[str, Any]], backlog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Strategy:
    - Use fresh first, then backlog.
    - Cap at max_items_for_script
    """
    max_items = int(topic.get("max_items_for_script", 60))
    combined = fresh + backlog
    combined = dedupe_sources(combined)

    # Keep only those with URLs (script should cite real sources)
    combined = [x for x in combined if is_url(x.get("url", ""))]

    return combined[:max_items]


# =========================
# Optional: ffmetadata chapters
# =========================
def write_ffmetadata(chapters: List[Dict[str, Any]], out_path: Path) -> None:
    """
    Writes FFmpeg metadata for chapters.
    Expected chapter fields: title, start_sec, end_sec
    """
    lines = [";FFMETADATA1"]
    for ch in chapters or []:
        try:
            title = str(ch.get("title", "Segment")).strip()        if isinstance(x, list):
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
