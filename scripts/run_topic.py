#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import inspect
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import requests

from tts_generate import tts_chunks_to_mp3, script_to_tts_chunks
from script_generate import generate_30min_script_and_chapters

TOPICS_DIR = Path("topics")
DATA_DIR = Path("data")
OUTPUTS_DIR = Path("outputs")

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


def is_url(v: Any) -> bool:
    return isinstance(v, str) and (v.startswith("http://") or v.startswith("https://"))


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


def write_ffmetadata(chapters: List[Dict[str, Any]], out_path: Path) -> None:
    lines: List[str] = []
    lines.append(";FFMETADATA1")

    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            title = str(ch.get("title", "Segment")).strip()
            start = int(float(ch.get("start_sec", 0)))
            end_val = ch.get("end_sec", None)
            end = int(float(end_val)) if end_val is not None else (start + 1)
            if end <= start:
                end = start + 1

            lines.append("[CHAPTER]")
            lines.append("TIMEBASE=1/1")
            lines.append(f"START={start}")
            lines.append(f"END={end}")
            lines.append(f"title={title}")
        except Exception:
            continue

    safe_mkdir(out_path.parent)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _call_script_generator(topic_id: str, topic: Dict[str, Any], picked: List[Dict[str, Any]]) -> Any:
    """
    Supports multiple possible signatures without breaking.
    """
    fn = generate_30min_script_and_chapters
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())

    # Preferred: keyword style
    if "topic" in params and "sources" in params:
        return fn(topic=topic, sources=picked)

    # Common positional styles
    if len(params) >= 3:
        # Try (topic_id, topic, sources)
        return fn(topic_id, topic, picked)

    if len(params) == 2:
        # Try (topic, sources)
        return fn(topic, picked)

    # Fallback single arg
    return fn(topic)


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

    # Premium flag from topic; env can override for emergency
    premium_tts = bool(topic.get("premium_tts", True))
    premium_override = (os.getenv("PREMIUM_TTS", "") or "").strip().lower()
    if premium_override in ("0", "false", "no"):
        premium_tts = False
    if premium_override in ("1", "true", "yes"):
        premium_tts = True

    gemini_model_env = (os.getenv("GEMINI_TTS_MODEL", "") or "").strip()
    gemini_model_topic = (topic.get("gemini_tts_model", "") or "").strip()
    gemini_model = (gemini_model_env or gemini_model_topic) or None

    voice_a = (os.getenv("VOICE_A", "") or "").strip() or (topic.get("gemini_voice_a") or "") or "Kore"
    voice_b = (os.getenv("VOICE_B", "") or "").strip() or (topic.get("gemini_voice_b") or "") or "Puck"

    piper_voice_a = (os.getenv("PIPER_VOICE_A", "") or "").strip() or (topic.get("piper_voice_a") or "") or "en_US-ryan-medium"
    piper_voice_b = (os.getenv("PIPER_VOICE_B", "") or "").strip() or (topic.get("piper_voice_b") or "") or "en_US-amy-medium"
    piper_model_dir = (os.getenv("PIPER_MODEL_DIR", "") or "").strip() or "assets/piper"

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
        "provider_requested": "gemini" if premium_tts else "piper",
        "tts_engine": "gemini" if premium_tts else "piper",
        "gemini_model": gemini_model,
        "voices": {
            "gemini": {"A": voice_a, "B": voice_b},
            "piper": {"A": piper_voice_a, "B": piper_voice_b},
        },
        "piper_model_dir": piper_model_dir,
        "fresh_count": len(fresh),
        "backlog_count": len(backlog),
        "skipped": False,
        "skip_reason": "",
        "assets": {},
        "errors": [],
    }

    save_json(sources_path, {"fresh": fresh, "backlog": backlog})

    if len(fresh) < min_fresh:
        summary["skipped"] = True
        summary["skip_reason"] = f"fresh<{min_fresh} (fresh={len(fresh)})"
        save_json(summary_path, summary)
        save_json(latest_path, {"topic_id": topic_id, "latest_base": None, "timestamp_utc": utc_now_iso(), "skipped": True})
        print(f"[{topic_id}] SKIP: {summary['skip_reason']}", flush=True)
        return

    picked = pick_sources_for_script(topic, fresh, backlog)
    save_json(sources_path, {"picked": picked, "fresh": fresh, "backlog": backlog})

    # Save picked into data/<topic>/picked_for_script.json
    paths = topic_paths(topic_id)
    save_json(paths["picked"], picked)

    # Generate script + chapters
    gen = _call_script_generator(topic_id, topic, picked)

    script_text: str = ""
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

    tts_chunks = script_to_tts_chunks(script_text)
    if not tts_chunks:
        raise RuntimeError("No dialogue turns parsed from script.")

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
            piper_model_dir=piper_model_dir,
        )
    except Exception as e:
        summary["errors"].append({"stage": "tts", "error": str(e), "traceback": traceback.format_exc()})
        save_json(summary_path, summary)
        raise

    summary["tts_seconds"] = round(time.time() - t0, 2)

    # Video render (best effort)
    disable_video = (os.getenv("DISABLE_VIDEO", "0").strip().lower() in ("1", "true", "yes", "y"))
    video_ok = False

    if not disable_video:
        try:
            from video_render import render_background_video  # type: ignore

            overlay = topic.get("video_overlay", {}) if isinstance(topic.get("video_overlay", {}), dict) else {}
            intro_text = str(topic.get("intro_text", "") or "").strip()
            outro_text = str(topic.get("outro_text", "") or "").strip()

            render_background_video(
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

            if mp4_path.exists() and mp4_path.stat().st_size > 1000:
                video_ok = True

        except Exception as e:
            summary["errors"].append({"stage": "video", "error": str(e), "traceback": traceback.format_exc()})
            video_ok = False

    summary["video_enabled"] = (not disable_video)
    summary["video_ok"] = video_ok

    # Upload to Release
    release = ensure_release(repo, gh_token, release_tag)
    assets_uploaded: Dict[str, str] = {}

    if mp3_path.exists() and mp3_path.stat().st_size > 1000:
        assets_uploaded["mp3"] = upload_asset(release, gh_token, mp3_path)

    if video_ok and mp4_path.exists() and mp4_path.stat().st_size > 1000:
        assets_uploaded["mp4"] = upload_asset(release, gh_token, mp4_path)

    assets_uploaded["script"] = upload_asset(release, gh_token, script_path)
    assets_uploaded["chapters"] = upload_asset(release, gh_token, chapters_path)
    assets_uploaded["sources"] = upload_asset(release, gh_token, sources_path)

    summary["assets"] = assets_uploaded

    save_json(summary_path, summary)
    save_json(
        latest_path,
        {"topic_id": topic_id, "latest_base": base_name, "assets": assets_uploaded, "timestamp_utc": utc_now_iso(), "skipped": False},
    )

    print(
        f"[{topic_id}] OK. assets={list(assets_uploaded.keys())} provider_requested={summary['provider_requested']} "
        f"tts_engine={summary['tts_engine']} video_ok={video_ok}",
        flush=True,
    )


if __name__ == "__main__":
    main()
