#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _extract_json(text: str) -> Dict[str, Any]:
    """
    Model sometimes wraps JSON in text. Extract first {...} block.
    """
    t = (text or "").strip()
    if not t:
        return {}
    # already JSON
    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            pass
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _gemini_generate_text(*, api_key: str, model: str, prompt: str, max_tokens: int = 8192) -> str:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty (needed for script generation).")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens},
    }
    r = requests.post(url, json=payload, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini script HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = (((cands[0] or {}).get("content") or {}).get("parts") or [])
    if not parts:
        return ""
    return str((parts[0] or {}).get("text") or "")


def _build_chapters(topic: Dict[str, Any], segment_titles: List[str]) -> List[Dict[str, Any]]:
    duration = int(topic.get("duration_sec", 1800))
    seg_count = int(topic.get("segment_count", 10))
    if seg_count <= 0:
        seg_count = 10

    titles = (segment_titles or [])[:seg_count]
    while len(titles) < seg_count:
        titles.append(f"Segment {len(titles) + 1}")

    seg_len = max(60, duration // seg_count)
    chapters: List[Dict[str, Any]] = []
    t = 0
    for i in range(seg_count):
        start = t
        end = duration if i == seg_count - 1 else min(duration, t + seg_len)
        chapters.append({"title": titles[i], "start_sec": start, "end_sec": end})
        t = end
    return chapters


def generate_30min_script_and_chapters(
    *,
    topic: Dict[str, Any],
    sources: List[Dict[str, Any]],
    api_key: str,
    model: str,
) -> Dict[str, Any]:
    """
    Returns:
      {"script": "<A:/B: dialogue>", "chapters": [{"title","start_sec","end_sec"}, ...]}
    """
    topic_title = str(topic.get("title") or "Agenda").strip()
    topic_desc = str(topic.get("description") or "").strip()
    language = str(topic.get("language") or "EN").strip().upper()
    duration = int(topic.get("duration_sec", 1800))
    seg_count = int(topic.get("segment_count", 10))

    # Condense sources for prompt
    src_lines: List[str] = []
    for i, s in enumerate(sources[:60], start=1):
        title = str(s.get("title") or "Untitled").strip()
        url = str(s.get("url") or "").strip()
        pub = str(s.get("published") or "").strip()
        site = str(s.get("source") or s.get("raw", {}).get("source") or "").strip()
        meta = " • ".join([x for x in [site, pub] if x])
        if meta:
            src_lines.append(f"{i}. {title} ({meta})\n{url}")
        else:
            src_lines.append(f"{i}. {title}\n{url}")

    sources_block = "\n\n".join(src_lines)

    prompt = f"""
You are writing a full 30-minute "deep dive overview" podcast in a TWO-SPEAKER DIALOGUE format.

Hard rules:
- Output MUST be valid JSON only (no markdown), with keys: "segment_titles" (array), "script" (string).
- The "script" must consist ONLY of lines that start with "A:" or "B:".
- Both speakers must contribute real analysis. Speaker B must NOT be only fillers.
- Provide deep dive: what happened, why it matters, actors & incentives, legal/policy mechanics, counterarguments, scenarios, and practical takeaways.
- Use the sources ONLY for facts; do not invent claims. If uncertain, say so explicitly.
- Language: {"English" if language == "EN" else "English"}.

Target length:
- About {duration//60} minutes total.
- About {seg_count} segments.

Topic:
Title: {topic_title}
Description: {topic_desc}
Date: {_utc_today()}

Sources (for grounding):
{sources_block}

Now produce JSON:
{{
  "segment_titles": ["...", "..."],
  "script": "A: ...\\nB: ...\\nA: ... (many lines)"
}}
""".strip()

    text = _gemini_generate_text(api_key=api_key, model=model, prompt=prompt, max_tokens=8192)
    data = _extract_json(text)

    segment_titles = data.get("segment_titles") if isinstance(data.get("segment_titles"), list) else []
    segment_titles = [str(x).strip() for x in segment_titles if str(x).strip()]

    script = str(data.get("script") or "").strip()

    # Safety: if model didn't comply, create a fallback structured script shell
    if not script or "A:" not in script:
        segment_titles = segment_titles or ["Opening & context", "What happened", "Why it matters", "Key actors", "Signals", "Scenarios", "Risks", "Legal mechanics", "Takeaways", "Close"]
        chapters = _build_chapters(topic, segment_titles)
        # Minimal fallback script (still dialogue)
        lines = []
        for ch in chapters:
            lines.append(f"A: {ch['title']}. Here is what we know from the sources, and what remains uncertain.")
            lines.append("B: Let’s break it down carefully: what changed, who it affects, and what the next decision points are.")
        script = "\n".join(lines)

    chapters = _build_chapters(topic, segment_titles)

    return {"script": script, "chapters": chapters}
