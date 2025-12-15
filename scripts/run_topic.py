#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import time
import hashlib
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import requests

from tts_generate import tts_chunks_to_mp3, script_to_tts_chunks
from script_generate import generate_30min_script_and_chapters


# =========================
# Paths / Constants
# =========================
TOPICS_DIR = Path("topics")
DATA_DIR = Path("data")
OUTPUTS_DIR = Path("outputs")

GITHUB_API = "https://api.github.com"


# =========================
# Utils
# =========================
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


def is_url(s: Any) -> bool:
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
    key = sha1((url or title) + "|" + (dt or ""))
    return {
        "title": title,
        "url": url,
        "published": dt,
        "source": src,
        "lang": lang,
        "raw": item,
        "key": key,
    }


def dedupe_sources(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
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
    r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=gh_headers(token), timeout=60)
    if r.status_code == 200:
        return r.json()

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
    name = file_path.name

    # idempotency: remove asset with same name
    for a in list_assets(release, token):
        if a.get("name") == name:
            delete_asset(a["url"], token)
            break

    upload_url = release["upload_url"].split("{")[0]
    with file_path.open("rb") as f:
        r = requests.post(
            f"{upload_url}?name={name}",
            headers={**gh_headers(token), "Content-Type": "application/octet-stream"},
            data=f,
            timeout=900,
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

    fresh: List[Dict[str, Any]] = []
    backlog: List[Dict[str, Any]] = []

    if isinstance(fresh_raw, list):
        for x in fresh_raw:
            if isinstance(x, dict):
                fresh.append(normalize_source(x))

    if isinstance(backlog_raw, list):
        for x in backlog_raw:
            if isinstance(x, dict):
                backlog.append(normalize_source(x))

    return dedupe_sources(fresh), dedupe_sources(backlog)


def pick_sources_for_script(topic: Dict[str, Any], fresh: List[Dict[str, Any]], backlog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_items = int(topic.get("max_items_for_script", 60))
    combined = dedupe_sources(fresh + backlog)
    combined = [x for x in combined if is_url(x.get("url", ""))]
    return combined[:max_items]


# =========================
# Chapters metadata (optional)
# =========================
def write_ffmetadata(chapters: List[Dict[str, Any]], out_path: Path) -> None:
    """
    Writes FFmpeg metadata for chapters.
    Expected chapter fields: title, start_sec, end_sec
    """
    lines = [";FFMETADATA1"]
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        try:
            title = str(ch.get("title", "Segment")).strip()
            start = int(float(ch.get("start_sec", 0)))
            end = int(float(ch.get("end_sec", start + 1)))
            if end <= start:
                end = start + 1
            lines.extend([
                "[CHAPTER]",
                "TIMEBASE=1/1",
                f"START={start}",
                f"END={end}",
                f"title={title}",
            ])
        except Exception:
            continue

    safe_mkdir(out_path.parent)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# =========================
# Main
# =========================
def main() -> None:
    topic_id = (os.getenv("TOPIC_ID", "") or "").strip()
    repo = (os.getenv("REPO", "") or "").strip()
    gh_token = (os.getenv("GITHUB_TOKEN", "") or "").strip()
    gemini_api_key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
    release_tag = (os.getenv("RELEASE_TAG", "") or topic_id).strip()

    if not topic_id:
        raise RuntimeError("TOPIC_ID is empty")
    if not repo:
        raise RuntimeError("REPO is empty (expected github.repository)")
    if not gh_token:
        raise RuntimeError("GITHUB_TOKEN is empty")
    if not release_tag:
        release_tag = topic_id

    topic = load_topic(topic_id)

    premium_tts = bool(topic.get("premium_tts", True))

    gemini_model = (os.getenv("GEMINI_TTS_MODEL", "") or topic.get("gemini_tts_model", "") or "").strip() or None
    voice_a = (os.getenv("VOICE_A", "") or "").strip() or None
    voice_b = (os.getenv("VOICE_B", "") or "").strip() or None
    piper_voice_a = (os.getenv("PIPER_VOICE_A", "") or "").strip() or None
    piper_voice_b = (os.getenv("PIPER_VOICE_B", "") or "").strip() or None

    fresh, backlog = load_sources_for_topic(topic_id)

    min_fresh = int(topic.get("min_fresh_sources", 20))
    print(f"[{topic_id}] fresh={len(fresh)}, backlog_total={len(backlog)}", flush=True)

    out_dir = OUTPUTS_DIR / topic_id
    safe_mkdir(out_dir)

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    base_name = f"{topic_id}-{run_stamp}"

    script_path = out_dir / f"{base_name}.script.txt"
    chapters_path = out_dir / f"{base_name}.chapters.json"
    ffmeta_path = out_dir / f"{base_name}.ffmeta"
    mp3_path = out_dir / f"{base_name}.mp3"
    mp4_path = out_dir / f"{base_name}.mp4"
    sources_path = out_dir / f"{base_name}.sources.json"
    summary_path = out_dir / "run_summary.json"
    latest_path = out_dir / "latest.json"

    summary: Dict[str, Any] = {
        "topic_id": topic_id,
        "timestamp_utc": utc_now_iso(),
        "premium_tts": premium_tts,
        "tts_engine": "gemini" if premium_tts else "piper",
        "gemini_model": gemini_model,
        "voices": {
            "gemini": {"A": voice_a, "B": voice_b},
            "piper": {"A": piper_voice_a, "B": piper_voice_b},
        },
        "fresh_count": len(fresh),
        "backlog_count": len(backlog),
        "skipped": False,
        "skip_reason": "",
        "assets": {},
        "errors": [],
    }

    # Always snapshot sources for debug/audit
    save_json(sources_path, {"fresh": fresh, "backlog": backlog})

    # Guard: not enough fresh sources
    if len(fresh) < min_fresh:
        summary["skipped"] = True
        summary["skip_reason"] = f"fresh<{min_fresh} (fresh={len(fresh)})"
        save_json(summary_path, summary)
        save_json(latest_path, {"topic_id": topic_id, "latest_base": None, "timestamp_utc": utc_now_iso(), "skipped": True})
        print(f"[{topic_id}] SKIP: {summary['skip_reason']}", flush=True)
        return

    picked = pick_sources_for_script(topic, fresh, backlog)
    save_json(sources_path, {"picked": picked, "fresh": fresh, "backlog": backlog})

    # Save picked also into data/<topic>/picked_for_script.json
    paths = topic_paths(topic_id)
    save_json(paths["picked"], picked)

    # Generate script + chapters
    gen = None
    try:
        gen = generate_30min_script_and_chapters(topic=topic, sources=picked)
    except TypeError:
        gen = generate_30min_script_and_chapters(topic_id, topic, picked)

    script_text = ""
    chapters: List[Dict[str, Any]] = []

    if isinstance(gen, tuple) and len(gen) >= 2:
        script_text = str(gen[0] or "")
        if isinstance(gen[1], list):
            chapters = gen[1]
    elif isinstance(gen, dict):
        script_text = str(gen.get("script") or gen.get("script_text") or "")
        ch = gen.get("chapters")
        if isinstance(ch, list):
            chapters = ch
    else:
        script_text = str(gen or "")

    if not script_text.strip():
        raise RuntimeError("Script generator returned empty script.")

    script_path.write_text(script_text, encoding="utf-8")
    save_json(chapters_path, chapters)
    write_ffmetadata(chapters, ffmeta_path)

    # Build TTS chunks from script text
    tts_chunks = script_to_tts_chunks(script_text)
    if not tts_chunks:
        raise RuntimeError("No dialogue turns parsed from script.")

    # Run TTS
    t0 = time.time()
    try:
        tts_chunks_to_mp3(
            tts_chunks,
            mp3_path,
            api_key=gemini_api_key,
            premium=premium_tts,
            gemini_model=gemini_model,
            voice_a=voice_a,
            voice_b=voice_b,
            piper_voice_a=piper_voice_a,
            piper_voice_b=piper_voice_b,
        )
    except Exception as e:
        summary["errors"].append({"stage": "tts", "error": str(e), "traceback": traceback.format_exc()})
        save_json(summary_path, summary)
        raise

    summary["tts_seconds"] = round(time.time() - t0, 2)

    # Optional video render (best effort)
    disable_video = (os.getenv("DISABLE_VIDEO", "0").strip().lower() in ("1", "true", "yes", "y"))
    video_ok = False

    if not disable_video:
        try:
            import video_render  # type: ignore

            render_fn = None
            if hasattr(video_render, "render_background_video"):
                render_fn = getattr(video_render, "render_background_video")
            elif hasattr(video_render, "render_waveform_video"):
                render_fn = getattr(video_render, "render_waveform_video")

            if render_fn is None:
                raise RuntimeError("video_render has no render_background_video/render_waveform_video")

            overlay = topic.get("video_overlay", {}) if isinstance(topic.get("video_overlay", {}), dict) else {}
            intro_text = str(topic.get("intro_text", "") or "").strip()
            outro_text = str(topic.get("outro_text", "") or "").strip()

            # Flexible call
            try:
                render_fn(
                    topic_id=topic_id,
                    topic=topic,
                    mp3_path=str(mp3_path),
                    out_mp4=str(mp4_path),
                    chapters=chapters,
                    ffmeta_path=str(ffmeta_path),
                    overlay=overlay,
                    intro_text=intro_text,
                    outro_text=outro_text,
                    sources=picked,
                )
            except TypeError:
                render_fn(str(mp3_path), str(mp4_path))

            if mp4_path.exists() and mp4_path.stat().st_size > 1000:
                video_ok = True

        except Exception as e:
            summary["errors"].append({"stage": "video", "error": str(e), "traceback": traceback.format_exc()})
            video_ok = False

    summary["video_enabled"] = (not disable_video)
    summary["video_ok"] = video_ok

    # Upload assets to Release
    release = ensure_release(repo, gh_token, release_tag)
    assets_uploaded: Dict[str, str] = {}

    if mp3_path.exists() and mp3_path.stat().st_size > 1000:
        assets_uploaded["mp3"] = upload_asset(release, gh_token, mp3_path)

    if video_ok and mp4_path.exists() and mp4_path.stat().st_size > 1000:
        assets_uploaded["mp4"] = upload_asset(release, gh_token, mp4_path)

    # Supporting artifacts
    assets_uploaded["script"] = upload_asset(release, gh_token, script_path)
    assets_uploaded["chapters"] = upload_asset(release, gh_token, chapters_path)
    assets_uploaded["sources"] = upload_asset(release, gh_token, sources_path)

    summary["assets"] = assets_uploaded

    save_json(summary_path, summary)
    save_json(latest_path, {"topic_id": topic_id, "latest_base": base_name, "assets": assets_uploaded, "timestamp_utc": utc_now_iso(), "skipped": False})

    print(f"[{topic_id}] OK. assets={list(assets_uploaded.keys())} tts_engine={summary['tts_engine']} video_ok={video_ok}", flush=True)


if __name__ == "__main__":
    main()
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
            title = str(ch.get("title", "Segment")).strip()
            start = int(float(ch.get("start_sec", 0)))
            end = int(float(ch.get("end_sec", start + 1)))
            if end <= start:
                end = start + 1
            lines += [
                "[CHAPTER]",
                "TIMEBASE=1/1",
                f"START={start}",
                f"END={end}",
                f"title={title}",
            ]
        except Exception:
            continue
    safe_mkdir(out_path.parent)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# =========================
# Main
# =========================
def main() -> None:
    topic_id = (os.getenv("TOPIC_ID", "") or "").strip()
    repo = (os.getenv("REPO", "") or "").strip()
    gh_token = (os.getenv("GITHUB_TOKEN", "") or "").strip()
    gemini_api_key = (os.getenv("GEMINI_API_KEY", "") or "").strip()

    release_tag = (os.getenv("RELEASE_TAG", "") or topic_id).strip()

    if not topic_id:
        raise RuntimeError("TOPIC_ID is empty")
    if not repo:
        raise RuntimeError("REPO is empty (expected github.repository)")
    if not gh_token:
        raise RuntimeError("GITHUB_TOKEN is empty")

    topic = load_topic(topic_id)

    # premium flag from topic JSON
    premium_tts = bool(topic.get("premium_tts", True))

    # TTS config (voices already set in env, but we also record what was used)
    gemini_model = (os.getenv("GEMINI_TTS_MODEL", topic.get("gemini_tts_model") or os.getenv("GEMINI_TTS_MODEL", "") ) or "").strip()
    # If not set, tts_generate.py has defaults; we log anyway
    voice_a = (os.getenv("VOICE_A", "") or "").strip()
    voice_b = (os.getenv("VOICE_B", "") or "").strip()
    piper_voice_a = (os.getenv("PIPER_VOICE_A", "") or "").strip()
    piper_voice_b = (os.getenv("PIPER_VOICE_B", "") or "").strip()

    # Load sources
    fresh, backlog = load_sources_for_topic(topic_id)

    min_fresh = int(topic.get("min_fresh_sources", 20))
    _print = lambda s: print(s, flush=True)

    _print(f"[{topic_id}] fresh={len(fresh)}, backlog_total={len(backlog)}")

    # Outputs
    out_dir = OUTPUTS_DIR / topic_id
    safe_mkdir(out_dir)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d")
    base_name = f"{topic_id}-{run_id}"
    script_path = out_dir / f"{base_name}.script.txt"
    chapters_path = out_dir / f"{base_name}.chapters.json"
    ffmeta_path = out_dir / f"{base_name}.ffmeta"
    mp3_path = out_dir / f"{base_name}.mp3"
    mp4_path = out_dir / f"{base_name}.mp4"
    sources_path = out_dir / f"{base_name}.sources.json"
    summary_path = out_dir / "run_summary.json"

    summary: Dict[str, Any] = {
        "topic_id": topic_id,
        "timestamp_utc": utc_now_iso(),
        "premium_tts": premium_tts,
        "tts_engine": "gemini" if premium_tts else "piper",
        "gemini_model": gemini_model or None,
        "voices": {
            "gemini": {"A": voice_a or None, "B": voice_b or None},
            "piper": {"A": piper_voice_a or None, "B": piper_voice_b or None},
        },
        "fresh_count": len(fresh),
        "backlog_count": len(backlog),
        "skipped": False,
        "skip_reason": "",
        "assets": {},
        "errors": [],
    }

    # Guard: if not enough fresh sources, skip expensive work but still write artifacts
    if len(fresh) < min_fresh:
        summary["skipped"] = True
        summary["skip_reason"] = f"fresh<{min_fresh} (fresh={len(fresh)})"
        save_json(summary_path, summary)

        # Still write sources snapshot for transparency
        save_json(sources_path, {"fresh": fresh, "backlog": backlog})
        _print(f"[{topic_id}] SKIP: {summary['skip_reason']}")
        return

    # Pick sources for script
    picked = pick_sources_for_script(topic, fresh, backlog)
    save_json(sources_path, {"picked": picked, "fresh": fresh, "backlog": backlog})

    # Also store picked in data for audit
    paths = topic_paths(topic_id)
    save_json(paths["picked"], picked)

    # Generate script + chapters
    try:
        gen = generate_30min_script_and_chapters(topic=topic, sources=picked)
    except TypeError:
        # Backward compat: some versions expect (topic_id, topic, sources)
        gen = generate_30min_script_and_chapters(topic_id, topic, picked)

    # Normalize generator return shape
    script_text = ""
    chapters: List[Dict[str, Any]] = []

    if isinstance(gen, tuple) and len(gen) >= 2:
        script_text = str(gen[0] or "")
        chapters = gen[1] if isinstance(gen[1], list) else []
    elif isinstance(gen, dict):
        script_text = str(gen.get("script", "") or gen.get("script_text", "") or "")
        chapters = gen.get("chapters", []) if isinstance(gen.get("chapters", []), list) else []
    else:
        script_text = str(gen or "")

    if not script_text.strip():
        raise RuntimeError("Script generator returned empty script.")

    script_path.write_text(script_text, encoding="utf-8")
    save_json(chapters_path, chapters)
    write_ffmetadata(chapters, ffmeta_path)

    # Build TTS chunks from script
    tts_chunks = script_to_tts_chunks(script_text)
    if not tts_chunks:
        raise RuntimeError("No dialogue turns parsed from script.")

    # Run TTS
    tts_started = time.time()
    tts_fallback = False
    try:
        tts_chunks_to_mp3(
            tts_chunks,
            mp3_path,
            api_key=gemini_api_key,
            premium=premium_tts,
            gemini_model=(gemini_model or None),
            voice_a=(voice_a or None),
            voice_b=(voice_b or None),
            piper_voice_a=(piper_voice_a or None),
            piper_voice_b=(piper_voice_b or None),
        )
    except Exception as e:
        # Detect if quota fallback marker exists (from tts_generate.py)
        quota_marker = Path(os.getenv("TTS_QUOTA_MARKER", "outputs/_tts_quota_exceeded.txt"))
        if quota_marker.exists():
            tts_fallback = True
        summary["errors"].append({"stage": "tts", "error": str(e), "traceback": traceback.format_exc()})
        save_json(summary_path, summary)
        raise

    summary["tts_seconds"] = round(time.time() - tts_started, 2)
    summary["tts_fallback_to_piper"] = bool(tts_fallback)

    # Optional video render
    disable_video = (os.getenv("DISABLE_VIDEO", "0").strip().lower() in ("1", "true", "yes", "y"))
    video_ok = False
    if not disable_video:
        try:
            # Try to import video_render dynamically
            import video_render  # type: ignore

            # You earlier asked "remove waveform visual" — so we prefer a non-waveform renderer if available.
            # We try in order:
            # 1) render_background_video(...)
            # 2) render_waveform_video(...) (legacy name, but may still exist)
            render_fn = None
            if hasattr(video_render, "render_background_video"):
                render_fn = getattr(video_render, "render_background_video")
            elif hasattr(video_render, "render_waveform_video"):
                render_fn = getattr(video_render, "render_waveform_video")

            if render_fn is None:
                raise RuntimeError("video_render has no render_background_video/render_waveform_video function")

            # Call with best-effort signature flexibility
            overlay = topic.get("video_overlay", {}) if isinstance(topic.get("video_overlay", {}), dict) else {}
            intro_text = str(topic.get("intro_text", "") or "").strip()
            outro_text = str(topic.get("outro_text", "") or "").strip()

            try:
                render_fn(
                    topic_id=topic_id,
                    topic=topic,
                    mp3_path=str(mp3_path),
                    out_mp4=str(mp4_path),
                    chapters=chapters,
                    ffmeta_path=str(ffmeta_path),
                    overlay=overlay,
                    intro_text=intro_text,
                    outro_text=outro_text,
                    sources=picked,
                )
            except TypeError:
                # fallback simpler signature
                render_fn(str(mp3_path), str(mp4_path))

            if mp4_path.exists() and mp4_path.stat().st_size > 1000:
                video_ok = True

        except Exception as e:
            summary["errors"].append({"stage": "video", "error": str(e), "traceback": traceback.format_exc()})
            video_ok = False

    summary["video_enabled"] = (not disable_video)
    summary["video_ok"] = video_ok

    # Upload assets to Release
    release = ensure_release(repo, gh_token, release_tag)

    assets_uploaded: Dict[str, str] = {}
    if mp3_path.exists() and mp3_path.stat().st_size > 1000:
        assets_uploaded["mp3"] = upload_asset(release, gh_token, mp3_path)

    if video_ok and mp4_path.exists() and mp4_path.stat().st_size > 1000:
        assets_uploaded["mp4"] = upload_asset(release, gh_token, mp4_path)

    # Upload supporting artifacts
    assets_uploaded["script"] = upload_asset(release, gh_token, script_path)
    assets_uploaded["chapters"] = upload_asset(release, gh_token, chapters_path)
    assets_uploaded["sources"] = upload_asset(release, gh_token, sources_path)

    summary["assets"] = assets_uploaded

    # Persist summary
    save_json(summary_path, summary)

    # Also store a stable “latest” pointer for the topic
    latest_path = out_dir / "latest.json"
    save_json(latest_path, {"topic_id": topic_id, "latest_base": base_name, "assets": assets_uploaded, "timestamp_utc": utc_now_iso()})

    _print(f"[{topic_id}] OK. assets={list(assets_uploaded.keys())}, tts_engine={summary['tts_engine']}, video_ok={video_ok}")


if __name__ == "__main__":
    main()
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
