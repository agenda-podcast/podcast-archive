import json
import re
from typing import Any, Dict, List

from google import genai


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    if not text:
        return ""
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    if not end_marker:
        return text[start:].strip()
    end = text.find(end_marker, start)
    if end == -1:
        return ""
    return text[start:end].strip()


def _cleanup_script(script: str) -> str:
    if not script:
        return ""
    script = script.replace("\r\n", "\n").replace("\r", "\n")
    script = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", script)

    lines = [ln.rstrip() for ln in script.split("\n")]
    cleaned: List[str] = []

    for ln in lines:
        s = ln.strip()
        if not s:
            cleaned.append("")
            continue
        if s.startswith("SPEAKER_A:") or s.startswith("SPEAKER_B:") or s.startswith("==="):
            cleaned.append(s)
        else:
            cleaned.append(f"SPEAKER_A: {s}")

    # Collapse excessive blank lines (max 1)
    out: List[str] = []
    blank = 0
    for ln in cleaned:
        if ln == "":
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)

    return "\n".join(out).strip()


def _safe_json_load(s: str) -> Any:
    if not s:
        return None
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
        s = re.sub(r"\n```$", "", s).strip()
    return json.loads(s)


def _build_sources_txt(items: List[Dict[str, Any]], max_items: int = 60) -> str:
    sources_lines: List[str] = []
    n = 0
    for it in items:
        url = str(it.get("url", "")).strip()
        if not url.startswith("http"):
            continue
        n += 1
        title = str(it.get("title", "")).strip()
        domain = str(it.get("domain", "")).strip()
        lang = str(it.get("language", "")).strip()
        meta = ", ".join([x for x in [domain, lang] if x])
        sources_lines.append(f"{n}. {title} ({meta})\n{url}".strip())
        if n >= max_items:
            break
    return "\n\n".join(sources_lines) if sources_lines else "No sources provided."


def generate_30min_script_and_chapters(topic: Dict[str, Any], items: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    """
    Returns dict:
      {
        "script": str (includes chapter markers === CHAPTER: ... ===),
        "chapters": [{start_sec:int, title:str}, ...],
        "description_html": str,
        "duration_sec": int
      }
    """
    client = genai.Client(api_key=api_key)

    show_title = str(topic.get("title", "Agenda")).strip()
    duration_sec = int(topic.get("duration_sec", 1800))
    segment_count = int(topic.get("segment_count", 10))
    model_name = str(topic.get("gemini_model", "gemini-2.5-flash"))

    sources_txt = _build_sources_txt(items, max_items=int(topic.get("max_items_for_script", 60)))

    prompt = (
        "You are generating a 30-minute English audio overview as a dialogue between two hosts.\n"
        f"Show title: {show_title}\n\n"
        "Hard rules:\n"
        "- Output must be English only.\n"
        "- Neutral, factual tone. No advocacy.\n"
        "- Do not mention Buzzsprout.\n"
        "- Do not invent facts; use only what can be supported by the sources.\n"
        "- Every spoken line must start with exactly 'SPEAKER_A:' or 'SPEAKER_B:'.\n"
        "- Alternate speakers frequently.\n"
        "- Start with a one-line disclaimer that it is an automated overview.\n"
        f"- Create exactly {segment_count} segments.\n"
        "- IMPORTANT: Before each segment, add a marker line in this exact format:\n"
        "  === CHAPTER: <Segment Title> ===\n\n"
        "Output format must be EXACTLY three sections in this order:\n"
        "1) SCRIPT:\n"
        "<dialogue script with chapter markers>\n\n"
        "2) CHAPTERS_JSON:\n"
        "<valid JSON array: [{\"start_sec\":0,\"title\":\"...\"}, ...]>\n\n"
        "3) DESCRIPTION_HTML:\n"
        "<short HTML description + Sources list of URLs>\n\n"
        "Sources:\n"
        f"{sources_txt}\n"
    )

    resp = client.models.generate_content(model=model_name, contents=prompt)
    text = resp.text or ""

    script_raw = _extract_block(text, "1) SCRIPT:", "2) CHAPTERS_JSON:")
    chapters_raw = _extract_block(text, "2) CHAPTERS_JSON:", "3) DESCRIPTION_HTML:")
    desc_raw = _extract_block(text, "3) DESCRIPTION_HTML:", "")

    script = _cleanup_script(script_raw)

    # Fallback if SCRIPT is empty
    if not script:
        fallback_prompt = (
            f"Generate ONLY the dialogue script for a 30-minute English audio overview for: {show_title}\n"
            "Rules:\n"
            "- Neutral, factual.\n"
            "- Every spoken line must start with SPEAKER_A: or SPEAKER_B:\n"
            "- Use exactly this marker line before each segment:\n"
            "  === CHAPTER: <Segment Title> ===\n"
            f"- Create exactly {segment_count} segments.\n\n"
            "Sources:\n"
            f"{sources_txt}\n"
        )
        resp2 = client.models.generate_content(model=model_name, contents=fallback_prompt)
        script = _cleanup_script(resp2.text or "")

    if not script:
        # Absolute last resort (never empty)
        script = (
            "=== CHAPTER: Overview ===\n"
            "SPEAKER_A: This is an automated overview based on publicly available reporting.\n"
            "SPEAKER_B: We do not yet have enough structured text to generate a full episode. Please try again later.\n"
        )

    chapters: List[Dict[str, Any]] = [{"start_sec": 0, "title": "Overview"}]
    try:
        parsed = _safe_json_load(chapters_raw)
        if isinstance(parsed, list) and parsed:
            cleaned = []
            for ch in parsed:
                if not isinstance(ch, dict):
                    continue
                start_sec = int(ch.get("start_sec", 0))
                title_ch = str(ch.get("title", "")).strip()
                if title_ch:
                    cleaned.append({"start_sec": max(0, start_sec), "title": title_ch})
            if cleaned:
                cleaned.sort(key=lambda x: x["start_sec"])
                if cleaned[0]["start_sec"] != 0:
                    cleaned.insert(0, {"start_sec": 0, "title": "Overview"})
                chapters = cleaned
    except Exception:
        pass

    description_html = desc_raw.strip()
    if not description_html:
        urls = []
        for it in items:
            u = str(it.get("url", "")).strip()
            if u.startswith("http"):
                urls.append(u)
            if len(urls) >= 25:
                break
        sources_html = "<br/>".join([f"<a href=\"{u}\">{u}</a>" for u in urls])
        description_html = (
            "<p>Automated overview based on publicly available reporting.</p>"
            "<ul><li>Two-host dialogue format</li><li>Daily topic briefing</li></ul>"
            "<p><b>Sources</b><br/>" + sources_html + "</p>"
        )

    return {
        "script": script,
        "chapters": chapters,
        "description_html": description_html,
        "duration_sec": duration_sec,
    }        sources_lines.append(f"{n}. {t} ({meta})\n{url}".strip())
        if n >= 60:
            break
    return "\n\n".join(sources_lines) if sources_lines else "No sources provided."


def generate_30min_script_and_chapters(topic: Dict[str, Any], items: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    client = genai.Client(api_key=api_key)

    title = str(topic.get("title", "Agenda Overview")).strip()
    duration_sec = int(topic.get("duration_sec", 1800))
    segment_count = int(topic.get("segment_count", 10))
    model_name = str(topic.get("gemini_model", "gemini-2.5-flash"))

    sources_txt = _build_sources_txt(items)

    prompt = (
        "You are generating a 30-minute English audio overview as a dialogue between two hosts.\n"
        f"Show title: {title}\n\n"
        "Hard rules:\n"
        "- Output must be English only.\n"
        "- Neutral, factual, no opinions.\n"
        "- Do not mention Buzzsprout.\n"
        "- Do not invent facts; use only what can be supported by the provided sources list.\n"
        "- Every spoken line must start with exactly one tag: 'SPEAKER_A:' or 'SPEAKER_B:'.\n"
        "- Alternate speakers frequently.\n"
        "- Start with a one-line disclaimer that it is an automated overview.\n"
        f"- Create {segment_count} segments with smooth transitions.\n\n"
        "Output format must be EXACTLY three sections:\n"
        "1) SCRIPT:\n"
        "<dialogue script>\n\n"
        "2) CHAPTERS_JSON:\n"
        "<valid JSON array: [{\"start_sec\":0,\"title\":\"...\"}, ...]>\n\n"
        "3) DESCRIPTION_HTML:\n"
        "<short HTML description with bullet points and a Sources list of URLs>\n\n"
        "Sources:\n"
        f"{sources_txt}\n"
    )

    resp = client.models.generate_content(model=model_name, contents=prompt)
    text = resp.text or ""

    script_raw = _extract_block(text, "1) SCRIPT:", "2) CHAPTERS_JSON:")
    chapters_raw = _extract_block(text, "2) CHAPTERS_JSON:", "3) DESCRIPTION_HTML:")
    desc_raw = _extract_block(text, "3) DESCRIPTION_HTML:", "")

    script = _cleanup_script(script_raw)

    # Fallback generation if SCRIPT is empty
    if not script:
        fallback_prompt = (
            f"Generate ONLY the dialogue script for a 30-minute English audio overview for: {title}\n"
            "Rules:\n"
            "- Every line must start with SPEAKER_A: or SPEAKER_B:\n"
            "- Neutral, factual.\n"
            "- Start with one-line automated overview disclaimer.\n"
            "Sources:\n"
            f"{sources_txt}\n"
        )
        resp2 = client.models.generate_content(model=model_name, contents=fallback_prompt)
        script = _cleanup_script(resp2.text or "")

        if not script:
            # Last-resort minimal script (never empty)
            script = (
                "SPEAKER_A: This is an automated overview based on publicly available reporting.\n"
                "SPEAKER_B: Today we did not have enough structured text to generate a full dialogue. Please check sources collection and try again.\n"
                "SPEAKER_A: We will return with a complete overview when more reporting is available.\n"
            )

    chapters: List[Dict[str, Any]] = [{"start_sec": 0, "title": "Overview"}]
    try:
        parsed = _safe_json_load(chapters_raw)
        if isinstance(parsed, list) and parsed:
            cleaned = []
            for ch in parsed:
                if not isinstance(ch, dict):
                    continue
                start_sec = int(ch.get("start_sec", 0))
                title_ch = str(ch.get("title", "")).strip()
                if title_ch:
                    cleaned.append({"start_sec": max(0, start_sec), "title": title_ch})
            if cleaned:
                cleaned.sort(key=lambda x: x["start_sec"])
                if cleaned[0]["start_sec"] != 0:
                    cleaned.insert(0, {"start_sec": 0, "title": "Overview"})
                chapters = cleaned
    except Exception:
        pass

    description_html = desc_raw.strip()
    if not description_html:
        urls = []
        for it in items:
            u = str(it.get("url", "")).strip()
            if u.startswith("http"):
                urls.append(u)
            if len(urls) >= 25:
                break
        sources_html = "<br/>".join([f"<a href=\"{u}\">{u}</a>" for u in urls])
        description_html = (
            "<p>Automated overview based on publicly available reporting.</p>"
            "<ul>"
            "<li>Neutral, factual summary</li>"
            "<li>Two-host dialogue format</li>"
            "</ul>"
            "<p><b>Sources</b><br/>" + sources_html + "</p>"
        )

    return {
        "script": script,
        "chapters": chapters,
        "description_html": description_html,
        "duration_sec": duration_sec,
    }
