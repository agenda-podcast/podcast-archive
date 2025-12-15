from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from scripts.collect_sources import merge_dedupe, pub_dt, read_json_list, stable_id
from scripts.feed_build import load_state, save_state, update_topic_feed
from scripts.github_release import ensure_release_and_upload
from scripts.script_generate import generate_30min_script_and_chapters
from scripts.tts_generate import tts_chunks_to_mp3
from scripts.video_render import render_waveform_video


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _load_topic(topic_id: str) -> Dict[str, Any]:
    p = Path("topics") / f"{topic_id}.json"
    if not p.exists():
        raise RuntimeError(f"Missing topic config: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Invalid topic JSON {p}: {e}")


def _normalize_source(it: Dict[str, Any]) -> Dict[str, Any]:
    title = str(it.get("title") or "").strip()
    url = str(it.get("url") or "").strip()
    published = str(it.get("published") or it.get("date") or "").strip()
    lang = str(it.get("lang") or it.get("language") or "").strip()
    domain = str(it.get("domain") or "").strip()
    key = stable_id(title, url)
    return {
        "title": title,
        "url": url,
        "published": published,
        "source": domain,
        "lang": lang,
        "raw": it,
        "key": key,
    }


def _pick_sources(topic_id: str, topic: Dict[str, Any], fresh: List[Dict[str, Any]], backlog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_items = int(topic.get("max_items_for_script", 60))
    combined = merge_dedupe(fresh + backlog)
    combined = sorted(combined, key=lambda x: (int(x.get("tier", 9)), -int(pub_dt(x).timestamp())))
    picks: List[Dict[str, Any]] = []
    for it in combined:
        if len(picks) >= max_items:
            break
        picks.append(_normalize_source(it))

    picked_path = Path("data") / topic_id / "picked_for_script.json"
    picked_path.parent.mkdir(parents=True, exist_ok=True)
    picked_path.write_text(json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    return picks


def _script_to_chunks(script: str) -> List[Dict[str, str]]:
    chunks: List[Dict[str, str]] = []
    for line in (script or "").splitlines():
        ln = line.strip()
        if not ln:
            continue
        speaker = "A"
        text = ln
        if len(ln) >= 2 and ln[1] == ":" and ln[0] in {"A", "B"}:
            speaker = ln[0]
            text = ln[2:].strip()
        chunks.append({"speaker": speaker, "text": text})
    return chunks


def _write_ffmetadata(chapters: List[Dict[str, Any]], path: Path) -> None:
    lines = [";FFMETADATA1"]
    for ch in chapters:
        try:
            start = int(float(ch.get("start_sec", 0)))
        except Exception:
            start = 0
        try:
            end = int(float(ch.get("end_sec", start + 1)))
        except Exception:
            end = start + 1
        if end <= start:
            end = start + 1
        title = str(ch.get("title", "Segment")).replace("\n", " ").strip()
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1",
                f"START={max(0, start)}",
                f"END={max(1, end)}",
                f"title={title}",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    topic_id = os.getenv("TOPIC_ID", "").strip()
    if not topic_id:
        print("TOPIC_ID is required", file=sys.stderr)
        sys.exit(1)

    topic = _load_topic(topic_id)
    data_dir = Path("data") / topic_id
    data_dir.mkdir(parents=True, exist_ok=True)

    fresh = read_json_list(data_dir / "fresh.json")
    backlog = read_json_list(data_dir / "backlog.json")

    stamp = _today_stamp()
    out_dir = Path("outputs") / topic_id
    out_dir.mkdir(parents=True, exist_ok=True)

    script_path = out_dir / f"{topic_id}-{stamp}.script.txt"
    chapters_path = out_dir / f"{topic_id}-{stamp}.chapters.json"
    sources_path = out_dir / f"{topic_id}-{stamp}.sources.json"
    mp3_path = out_dir / f"{topic_id}-{stamp}.mp3"
    mp4_path = out_dir / f"{topic_id}-{stamp}.mp4"
    ffmeta_path = out_dir / f"{topic_id}-{stamp}.ffmeta"
    latest_path = out_dir / "latest.json"
    run_summary_path = out_dir / "run_summary.json"

    errors: List[Dict[str, str]] = []
    assets: Dict[str, str] = {}
    skipped = False
    skip_reason = ""
    video_ok = False

    if not fresh:
        skipped = True
        skip_reason = "fresh.json is empty; gating triggered."

    picked_path = data_dir / "picked_for_script.json"
    picked = read_json_list(picked_path) if picked_path.exists() else []
    if not picked and not skipped:
        picked = _pick_sources(topic_id, topic, fresh, backlog)

    try:
        if skipped:
            raise RuntimeError(skip_reason)

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        gemini_model = os.getenv("GEMINI_SCRIPT_MODEL", "").strip() or str(topic.get("gemini_model", "gemini-2.0-flash"))

        script_out = generate_30min_script_and_chapters(
            topic=topic,
            sources=picked,
            api_key=gemini_api_key,
            model=gemini_model,
        )
        script_text = str(script_out.get("script") or "").strip()
        chapters = script_out.get("chapters") if isinstance(script_out.get("chapters"), list) else []

        script_path.write_text(script_text, encoding="utf-8")
        _safe_json_dump(chapters_path, chapters)
        _safe_json_dump(sources_path, {"picked": picked, "fresh": fresh, "backlog": backlog})

        _write_ffmetadata(chapters, ffmeta_path)

        chunks = _script_to_chunks(script_text)
        provider_requested = "gemini" if topic.get("premium_tts") else "piper"
        tts_engine_used = "piper"
        mp3_out = tts_chunks_to_mp3(
            chunks=chunks,
            mp3_path=str(mp3_path),
            premium=bool(topic.get("premium_tts")),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_TTS_MODEL") or None,
            gemini_voice_a=os.getenv("GEMINI_TTS_VOICE_A") or None,
            gemini_voice_b=os.getenv("GEMINI_TTS_VOICE_B") or None,
            piper_voice_a=os.getenv("PIPER_VOICE_A") or None,
            piper_voice_b=os.getenv("PIPER_VOICE_B") or None,
            piper_model_dir=os.getenv("PIPER_MODEL_DIR") or "assets/piper",
        )

        video_enabled = bool(topic.get("video_enabled", True))
        if video_enabled:
            try:
                render_waveform_video(
                    topic_id=topic_id,
                    topic=topic,
                    mp3_path=mp3_out,
                    out_mp4=str(mp4_path),
                    chapters=chapters,
                    ffmeta_path=str(ffmeta_path),
                    overlay=topic.get("video_overlay", {}),
                    intro_text=str(topic.get("intro_text", "")),
                    outro_text=str(topic.get("outro_text", "")),
                    sources=picked,
                )
                video_ok = True
            except Exception as e:
                video_ok = False
                errors.append({"stage": "video", "error": str(e), "traceback": traceback.format_exc()})

        repo = os.getenv("REPO", "").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()
        release_tag = os.getenv("RELEASE_TAG", "").strip() or topic_id
        upload_files = [script_path, chapters_path, sources_path, mp3_path]
        if video_ok and mp4_path.exists():
            upload_files.append(mp4_path)
        if repo and token:
            assets = ensure_release_and_upload(repo, token, release_tag, upload_files)

        # Feed update (best-effort)
        try:
            state = load_state(topic_id)
            episode = {
                "title": f"{topic.get('title', topic_id)} — Daily Overview ({stamp})",
                "guid": f"{topic_id}-{stamp}",
                "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "enclosure_url": assets.get(Path(mp3_path).name, "") or "",
                "enclosure_type": "audio/mpeg",
                "description_html": "<p>Automated overview based on publicly available reporting.</p>",
                "itunes_duration": int(topic.get("duration_sec", 1800)),
                "chapters": chapters,
            }
            update_topic_feed(topic_id, topic, state, episode)
            save_state(topic_id, state)

            feed_src = Path("feeds") / topic_id / "rss.xml"
            feed_dst = Path("feed") / f"{topic_id}.rss"
            if feed_src.exists():
                feed_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(feed_src, feed_dst)
        except Exception as e:
            errors.append({"stage": "feed", "error": str(e), "traceback": traceback.format_exc()})

    except Exception as e:
        errors.append({"stage": "pipeline", "error": str(e), "traceback": traceback.format_exc()})
        skipped = True
        skip_reason = skip_reason or str(e)

    latest = {
        "topic_id": topic_id,
        "date": stamp,
        "script": str(script_path),
        "chapters": str(chapters_path),
        "sources": str(sources_path),
        "mp3": str(mp3_path),
        "mp4": str(mp4_path) if mp4_path.exists() else "",
    }
    _safe_json_dump(latest_path, latest)

    run_summary = {
        "topic_id": topic_id,
        "timestamp_utc": _utc_now_iso(),
        "premium_tts": bool(topic.get("premium_tts")),
        "provider_requested": provider_requested,
        "tts_engine": locals().get("tts_engine_used", "piper"),
        "gemini_model": os.getenv("GEMINI_SCRIPT_MODEL", "").strip() or topic.get("gemini_model"),
        "voices": {
            "gemini": {"A": os.getenv("GEMINI_TTS_VOICE_A"), "B": os.getenv("GEMINI_TTS_VOICE_B")},
            "piper": {"A": os.getenv("PIPER_VOICE_A"), "B": os.getenv("PIPER_VOICE_B")},
        },
        "piper_model_dir": os.getenv("PIPER_MODEL_DIR") or "assets/piper",
        "fresh_count": len(fresh),
        "backlog_count": len(backlog),
        "skipped": skipped,
        "skip_reason": skip_reason,
        "assets": assets,
        "errors": errors,
        "video_enabled": bool(topic.get("video_enabled", True)),
        "video_ok": video_ok,
    }
    _safe_json_dump(run_summary_path, run_summary)

    if skipped and not assets:
        print(f"[{topic_id}] skipped: {skip_reason}", file=sys.stderr)
    else:
        print(f"[{topic_id}] done. mp3={mp3_path} video_ok={video_ok}")


if __name__ == "__main__":
    main()
