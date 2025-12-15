import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from dateutil import parser as dtparser


def _clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _safe_url(u: str) -> str:
    u = (u or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return ""


def _pick(item: Dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _parse_date(item: Dict[str, Any]) -> str:
    # Try common keys; returns ISO date string or empty
    for k in ("published", "published_at", "date", "datetime", "time", "pubDate"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            try:
                dt = dtparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return ""


def _format_source_line(idx: int, item: Dict[str, Any]) -> str:
    title = _clean_text(_pick(item, "title", "headline", default="(untitled)"))
    url = _safe_url(_pick(item, "url", "link", default=""))
    domain = _clean_text(_pick(item, "domain", default=""))
    lang = _clean_text(_pick(item, "lang", "language", default=""))
    d = _parse_date(item)

    meta_bits = []
    if domain:
        meta_bits.append(domain)
    if d:
        meta_bits.append(d)
    if lang:
        meta_bits.append(lang)

    meta = " • ".join(meta_bits)
    if meta:
        return f"{idx}. {title} ({meta})\n{url}".strip()
    return f"{idx}. {title}\n{url}".strip()


def _build_sources_block(items: List[Dict[str, Any]], max_sources: int = 25) -> str:
    lines: List[str] = []
    n = 0
    for it in items:
        url = _safe_url(_pick(it, "url", "link", default=""))
        if not url:
            continue
        n += 1
        lines.append(_format_source_line(n, it))
        if n >= max_sources:
            break
    return "\n\n".join(lines)


def _topic_fields(topic: Dict[str, Any]) -> Tuple[str, str, str]:
    title = (topic.get("title") or topic.get("name") or "Agenda Topic").strip()
    angle = (topic.get("angle") or topic.get("prompt") or "").strip()
    language = (topic.get("language") or "EN").strip()
    return title, angle, language


def _desired_chapter_plan(total_minutes: int = 30) -> List[Tuple[str, int]]:
    """
    Simple, predictable chapter plan for a 30-min overview.
    Returns list of (chapter_title, minutes_allocated).
    """
    return [
        ("Opening & context", 2),
        ("What happened", 6),
        ("Why it matters", 7),
        ("Key actors & incentives", 6),
        ("Signals & scenarios", 6),
        ("Practical takeaways", 2),
        ("Wrap-up", 1),
    ]


def _chapter_start_seconds(plan: List[Tuple[str, int]]) -> List[Dict[str, Any]]:
    out = []
    acc = 0
    for title, mins in plan:
        out.append({"title": title, "start_sec": int(acc * 60)})
        acc += max(1, int(mins))
    # Ensure first is 0
    if not out or out[0]["start_sec"] != 0:
        out.insert(0, {"title": "Overview", "start_sec": 0})
    return out


def _build_dialogue_script(
    podcast_title: str,
    topic_title: str,
    topic_angle: str,
    items: List[Dict[str, Any]],
    intro_text: str = "",
    outro_text: str = "",
) -> str:
    """
    Produces a dialogue script using SPEAKER_A / SPEAKER_B lines.
    Includes chapter markers required by run_topic.py TTS splitting.
    """
    plan = _desired_chapter_plan(30)

    sources_block = _build_sources_block(items, max_sources=30)

    # Keep intro/outro optional; these are visual overlays too, but TTS can say them.
    intro_text = (intro_text or "").strip()
    outro_text = (outro_text or "").strip()
    if not intro_text:
        intro_text = f"Welcome back to {podcast_title}. Today: {topic_title}."
    if not outro_text:
        outro_text = "That’s the overview. Full sources are included. See you tomorrow."

    angle_line = f"Angle: {topic_angle}" if topic_angle else ""

    # The script is intentionally structured and repetitive to reduce TTS parsing failures.
    parts: List[str] = []

    # Global opener
    parts.append("=== CHAPTER: Opening & context ===")
    parts.append(f"SPEAKER_A: {intro_text}")
    if angle_line:
        parts.append(f"SPEAKER_B: {angle_line}")
    parts.append("SPEAKER_A: We pulled a set of fresh, credible sources across outlets and regions.")
    parts.append("SPEAKER_B: We’ll separate confirmed facts from interpretation, then close with scenarios and takeaways.")

    # Chapter content templates (source-driven but robust)
    for ch_title, mins in plan[1:]:
        parts.append(f"\n=== CHAPTER: {ch_title} ===")
        parts.append(f"SPEAKER_A: Let’s move into {ch_title.lower()}.")
        parts.append("SPEAKER_B: First, the key points that multiple sources agree on.")

        # Use up to 6 sources per chapter to keep it manageable
        start_idx = (plan.index((ch_title, mins)) - 1) * 6
        chunk = items[start_idx:start_idx + 6] if items else []

        if chunk:
            for it in chunk[:6]:
                t = _clean_text(_pick(it, "title", "headline", default="(untitled)"))
                url = _safe_url(_pick(it, "url", "link", default=""))
                dom = _clean_text(_pick(it, "domain", default=""))
                if dom:
                    parts.append(f"SPEAKER_A: One data point from {dom}: {t}.")
                else:
                    parts.append(f"SPEAKER_A: One data point: {t}.")
                if url:
                    parts.append("SPEAKER_B: We’ll include the link in the source list for reference.")
        else:
            parts.append("SPEAKER_A: Sources today are sparse for this sub-topic, so we’ll keep this section brief and factual.")
            parts.append("SPEAKER_B: The pipeline will keep accumulating evidence until we can support a full segment.")

        # Add analysis scaffolding (works even if sources are thin)
        parts.append("SPEAKER_A: Here is the most conservative interpretation.")
        parts.append("SPEAKER_B: And here is the alternative explanation if incentives or constraints differ.")
        parts.append("SPEAKER_A: Watch for follow-up confirmations, official statements, and second-day reporting corrections.")

    # Wrap
    parts.append("\n=== CHAPTER: Wrap-up ===")
    parts.append("SPEAKER_A: Quick recap: what changed, why it matters, and what to watch next.")
    parts.append("SPEAKER_B: If you want a deeper dive, check the sources and revisit this episode as updates land.")
    parts.append(f"SPEAKER_A: {outro_text}")

    # Append sources (not spoken as dialogue; keep outside speaker tags)
    parts.append("\n=== SOURCES (for description) ===")
    parts.append(sources_block if sources_block else "No valid source URLs were provided in the input list.")

    return "\n".join(parts).strip()


def generate_30min_script_and_chapters(topic: Dict[str, Any], items: List[Dict[str, Any]], gemini_api_key: str) -> Dict[str, Any]:
    """
    This repository previously used an LLM here. To make the pipeline robust and deterministic,
    we generate a structured dialogue script locally from the collected sources.

    If you later re-enable Gemini generation, keep the chapter markers:
      === CHAPTER: ... ===
    and speaker prefixes:
      SPEAKER_A: ...
      SPEAKER_B: ...
    """
    podcast_title = (topic.get("podcast_title") or "Agenda").strip()
    topic_title, topic_angle, _lang = _topic_fields(topic)

    intro_text = str(topic.get("intro_text", "") or "").strip()
    outro_text = str(topic.get("outro_text", "") or "").strip()

    script = _build_dialogue_script(
        podcast_title=podcast_title,
        topic_title=topic_title,
        topic_angle=topic_angle,
        items=items or [],
        intro_text=intro_text,
        outro_text=outro_text,
    )

    # Chapter plan (approx) — the video renderer will normalize to the real mp3 duration.
    plan = _desired_chapter_plan(30)
    chapters = _chapter_start_seconds(plan)

    # Description HTML for RSS / episode page
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sources_html = _build_sources_block(items or [], max_sources=25).replace("\n", "<br>")
    description_html = (
        f"<p><b>{_clean_text(topic_title)}</b> — {now}.</p>"
        f"<p>{_clean_text(topic_angle)}</p>" if topic_angle else f"<p><b>{_clean_text(topic_title)}</b> — {now}.</p>"
    )
    description_html += "<p><b>Sources</b><br>" + (sources_html or "No sources.") + "</p>"

    return {
        "script": script,
        "chapters": chapters,
        "description_html": description_html,
    }
