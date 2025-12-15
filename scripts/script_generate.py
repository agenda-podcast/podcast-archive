# scripts/script_generate.py
# Copyright (c) Agenda Podcast
# All rights reserved.

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ").replace("\u200b", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedupe_sources(sources: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for x in sources or []:
        if not isinstance(x, dict):
            continue
        url = str(x.get("url") or x.get("link") or "").strip()
        key = url.lower() if url else _clean_text(str(x.get("title", ""))).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def _pick_points(sources: List[dict], limit: int = 18) -> List[str]:
    """
    Create short factual bullet points from sources (titles/snippets).
    """
    pts: List[str] = []
    for s in sources[: max(0, limit)]:
        title = _clean_text(str(s.get("title", "")))
        snippet = _clean_text(str(s.get("snippet") or s.get("summary") or s.get("description") or ""))
        publisher = _clean_text(str(s.get("publisher") or s.get("source") or ""))
        # Prefer title; fallback to snippet
        base = title if title else snippet
        if not base:
            continue
        # Keep it short
        base = re.sub(r"\s*\|\s*.+$", "", base)  # drop "Title | Publisher" patterns
        base = base[:220].rstrip(" .,:;")  # cap length
        if publisher and publisher.lower() not in base.lower():
            pts.append(f"{base} ({publisher})")
        else:
            pts.append(base)
    # Deduplicate by normalized text
    uniq = []
    seen = set()
    for p in pts:
        k = re.sub(r"\W+", "", p.lower())
        if k and k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _default_chapter_titles(n: int) -> List[str]:
    base = [
        "Opening & context",
        "What happened",
        "Key facts & timeline",
        "Why it matters",
        "Who is affected",
        "Policy & legal angles",
        "Economic & social impact",
        "Signals & scenarios",
        "Practical takeaways",
        "Closing summary",
    ]
    if n <= len(base):
        return base[:n]
    # extend if needed
    out = base[:]
    i = 1
    while len(out) < n:
        out.append(f"Segment {len(out)+1}")
        i += 1
    return out


def _build_dialogue(
    topic_title: str,
    intro_text: str,
    outro_text: str,
    chapter_titles: List[str],
    points: List[str],
    duration_sec: int,
) -> Dict[str, Any]:
    """
    Build a two-speaker dialogue and chapters with approximate timings.
    This is deterministic and does not call external LLMs (keeps pipeline stable).
    """
    # Heuristic: allocate time per segment evenly
    seg_count = max(1, len(chapter_titles))
    seg_len = max(30, int(duration_sec / seg_count))
    chapters = []
    t = 0
    for i, title in enumerate(chapter_titles):
        chapters.append({"title": title, "start_sec": int(t)})
        t += seg_len

    # Spread points across segments
    per_seg = max(1, int(len(points) / seg_count)) if points else 0

    lines: List[str] = []
    tts_chunks: List[Dict[str, str]] = []

    def say(speaker: str, text: str) -> None:
        text = _clean_text(text)
        if not text:
            return
        # Keep chunks manageable for TTS
        lines.append(f"{speaker}: {text}")
        tts_chunks.append({"speaker": speaker, "text": text})

    # Intro
    say("A", intro_text or f"Welcome back to Agenda. Today’s deep dive: {topic_title}.")
    say("B", "We’ll break down what happened, why it matters, and what to watch next. Let’s begin.")

    # Body
    idx = 0
    for i, ch in enumerate(chapter_titles):
        say("A", f"Segment {i+1}: {ch}.")
        # Insert points
        if points:
            seg_pts = points[idx : idx + per_seg] if per_seg > 0 else []
            idx += len(seg_pts)
            if not seg_pts and idx < len(points):
                seg_pts = [points[idx]]
                idx += 1
            if seg_pts:
                say("B", "Here are the key signals from the reporting:")
                for p in seg_pts[:6]:
                    say("B", f"- {p}")
                say("A", "Now, here’s the interpretation and what it implies going forward.")
                say(
                    "A",
                    "When multiple sources align on the same pattern, the takeaway is usually not the headline itself, "
                    "but the incentives and constraints underneath it.",
                )
            else:
                say("B", "This segment is a structured recap based on the best available reporting.")
                say("A", "Focus on measurable signals and what would falsify the leading narrative.")

        # Keep dialogue flowing even with few sources
        if i in (0, 1):
            say("B", "Pay attention to dates, definitions, and which agency or court actually has authority here.")
        if i in (3, 4):
            say("A", "Ask: who benefits, who absorbs the cost, and what the second-order effects look like.")
        if i == seg_count - 2:
            say("B", "If you only remember one thing: track implementation details, not press releases.")

    # Outro
    say("A", outro_text or "That’s the overview. Full sources are included. Subscribe for the next briefing.")
    say("B", "See you next time.")

    script_text = "\n".join(lines).strip()

    return {
        "script_text": script_text,
        "chapters": chapters,
        "tts_chunks": tts_chunks,
    }


def generate_30min_script_and_chapters(*args, **kwargs) -> Dict[str, Any]:
    """
    Backward/forward compatible generator.

    Accepts ANY of these calling patterns:
      1) generate_30min_script_and_chapters(topic=topic_dict, sources=list)
      2) generate_30min_script_and_chapters(topic_id, topic_dict, picked_list)
      3) generate_30min_script_and_chapters(topic_dict, picked_list)
      4) generate_30min_script_and_chapters(topic_id=..., topic=..., sources=...)

    Returns dict:
      { topic_id, script_text, chapters, tts_chunks, sources_used, generated_utc }
    """
    topic_id: Optional[str] = None
    topic: Optional[dict] = None
    sources: List[dict] = []

    # Prefer keyword inputs
    if isinstance(kwargs.get("topic_id"), str):
        topic_id = kwargs["topic_id"]
    if isinstance(kwargs.get("topic"), dict):
        topic = kwargs["topic"]
    # sources may be passed as sources= or picked= or items=
    for k in ("sources", "picked", "items"):
        if isinstance(kwargs.get(k), list):
            sources = kwargs[k]
            break

    # Parse positional variants
    # (topic_id, topic, sources)
    if topic is None:
        for a in args:
            if isinstance(a, dict):
                topic = a
                break
    if not sources:
        for a in reversed(args):
            if isinstance(a, list):
                sources = a
                break
    if topic_id is None:
        # first string positional might be topic_id
        for a in args:
            if isinstance(a, str) and a.strip().startswith("topic-"):
                topic_id = a.strip()
                break

    if topic is None:
        raise RuntimeError("generate_30min_script_and_chapters: topic dict is missing/invalid.")

    topic_id = topic_id or str(topic.get("id") or "topic")

    # Normalize
    sources = _dedupe_sources(sources)

    podcast_title = _clean_text(str(topic.get("podcast_title") or "Agenda"))
    topic_title = _clean_text(str(topic.get("title") or topic_id))
    intro_text = _clean_text(str(topic.get("intro_text") or ""))
    outro_text = _clean_text(str(topic.get("outro_text") or ""))
    duration_sec = int(topic.get("duration_sec") or 1800)
    seg_count = int(topic.get("segment_count") or 10)

    chapter_titles = _default_chapter_titles(seg_count)

    points = _pick_points(sources, limit=max(12, min(60, int(topic.get("max_items_for_script") or 60))))

    built = _build_dialogue(
        topic_title=f"{podcast_title} — {topic_title}" if podcast_title else topic_title,
        intro_text=intro_text,
        outro_text=outro_text,
        chapter_titles=chapter_titles,
        points=points,
        duration_sec=duration_sec,
    )

    # Prepare sources_used for show notes
    sources_used = []
    for s in sources[: min(len(sources), int(topic.get("max_items_for_script") or 60))]:
        title = _clean_text(str(s.get("title", "")))
        url = _clean_text(str(s.get("url") or s.get("link") or ""))
        pub = _clean_text(str(s.get("publisher") or s.get("source") or ""))
        if url or title:
            sources_used.append({"title": title, "url": url, "publisher": pub})

    return {
        "topic_id": topic_id,
        "generated_utc": _now_utc_iso(),
        "script_text": built["script_text"],
        "chapters": built["chapters"],
        "tts_chunks": built["tts_chunks"],
        "sources_used": sources_used,
        "meta": {
            "segment_count": seg_count,
            "duration_sec": duration_sec,
            "sources_in": len(sources),
            "sources_used": len(sources_used),
        },
    }
