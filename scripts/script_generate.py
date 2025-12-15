import json
import re
from typing import Any, Dict, List

from google import genai


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    """
    Extract text between markers. Returns empty string if markers not found.
    """
    if not text:
        return ""
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start) if end_marker else -1
    if end == -1 and end_marker:
        return ""
    return text[start:end].strip() if end_marker else text[start:].strip()


def _cleanup_script(script: str) -> str:
    """
    Ensure strict dialogue formatting and remove unwanted characters.
    Enforces lines starting with SPEAKER_A: or SPEAKER_B:
    """
    if not script:
        return ""

    # Normalize line endings
    script = script.replace("\r\n", "\n").replace("\r", "\n")

    # Remove non-printable control chars except newline and tab
    script = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", script)

    # Trim trailing spaces per line
    lines = [ln.rstrip() for ln in script.split("\n")]

    # Ensure every non-empty line is tagged; if not, tag as SPEAKER_A
    cleaned: List[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            cleaned.append("")
            continue
        if s.startswith("SPEAKER_A:") or s.startswith("SPEAKER_B:"):
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
    """
    Try to parse JSON even if the model wrapped it in code fences.
    """
    if not s:
        return None
    s = s.strip()

    # Remove code fences if present
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
        s = re.sub(r"\n```$", "", s).strip()

    return json.loads(s)


def generate_30min_script_and_chapters(topic: Dict[str, Any], items: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    """
    Generates:
      - Dialogue script in English with SPEAKER_A / SPEAKER_B
      - Chapters JSON: [{ "start_sec": int, "title": str }, ...]
      - Description HTML
      - duration_sec (target, 1800 default)

    The script is designed for TTS turn-taking with two voices.
    """
    client = genai.Client(api_key=api_key)

    title = str(topic.get("title", "Agenda Overview")).strip()
    duration_sec = int(topic.get("duration_sec", 1800))
    segment_count = int(topic.get("segment_count", 10))

    # Build sources list
    sources_lines: List[str] = []
    for i, it in enumerate(items, 1):
        t = str(it.get("title", "")).strip()
        d = str(it.get("domain", "")).strip()
        lang = str(it.get("language", "")).strip()
        url = str(it.get("url", "")).strip()
        if not url.startswith("http"):
            continue
        meta = ", ".join([x for x in [d, lang] if x])
        sources_lines.append(f"{i}. {t} ({meta})\n{url}".strip())

    sources_txt = "\n\n".join(sources_lines) if sources_lines else "No sources provided."

    prompt = (
        "You are generating a 30-minute English audio overview as a dialogue between two hosts.\n"
        f"Show title: {title}\n\n"
        "Hard rules:\n"
        "- Output must be English only.\n"
        "- Neutral, factual, no opinions or political advocacy.\n"
        "- Do not mention Buzzsprout.\n"
        "- Do not invent facts; use only what can reasonably be supported by the provided sources list.\n"
        "- Every spoken line must start with exactly one of these tags: 'SPEAKER_A:' or 'SPEAKER_B:'.\n"
        "- Alternate speakers frequently. Avoid long monologues.\n"
        "- Include a brief disclaimer at the start (one line) that it is an automated overview.\n"
        "- Target total length: about 30 minutes of narration (approximately 3900-4500 words).\n"
        "- Structure into "
        f"{segment_count} segments with smooth transitions.\n\n"
        "Output format must be EXACTLY three sections in this order:\n"
        "1) SCRIPT:\n"
        "<dialogue script>\n\n"
        "2) CHAPTERS_JSON:\n"
        "<valid JSON array: [{\"start_sec\":0,\"title\":\"...\"}, ...]>\n\n"
        "3) DESCRIPTION_HTML:\n"
        "<short HTML description with bullet points and a Sources list of URLs>\n\n"
        "Sources:\n"
        f"{sources_txt}\n"
    )

    resp = client.models.generate_content(
        model=str(topic.get("gemini_model", "gemini-2.5-flash")),
        contents=prompt,
    )

    text = resp.text or ""

    script_raw = _extract_block(text, "1) SCRIPT:", "2) CHAPTERS_JSON:")
    chapters_raw = _extract_block(text, "2) CHAPTERS_JSON:", "3) DESCRIPTION_HTML:")
    desc_raw = _extract_block(text, "3) DESCRIPTION_HTML:", "")

    script = _cleanup_script(script_raw)

    chapters: List[Dict[str, Any]] = [{"start_sec": 0, "title": "Overview"}]
    try:
        parsed = _safe_json_load(chapters_raw)
        if isinstance(parsed, list) and parsed:
            cleaned_ch = []
            for ch in parsed:
                if not isinstance(ch, dict):
                    continue
                start_sec = int(ch.get("start_sec", 0))
                title_ch = str(ch.get("title", "")).strip()
                if title_ch:
                    cleaned_ch.append({"start_sec": max(0, start_sec), "title": title_ch})
            if cleaned_ch:
                # Ensure sorted by time and first starts at 0
                cleaned_ch.sort(key=lambda x: x["start_sec"])
                if cleaned_ch[0]["start_sec"] != 0:
                    cleaned_ch.insert(0, {"start_sec": 0, "title": "Overview"})
                chapters = cleaned_ch
    except Exception:
        # Keep fallback
        pass

    description_html = desc_raw.strip()
    if not description_html:
        # Minimal fallback description
        urls = [it.get("url") for it in items if str(it.get("url", "")).startswith("http")]
        urls = urls[:20]
        sources_html = "<br/>".join([f"<a href=\"{u}\">{u}</a>" for u in urls])
        description_html = (
            "<p>Automated overview based on publicly available reporting.</p>"
            "<p><b>Sources</b><br/>" + sources_html + "</p>"
        )

    return {
        "script": script,
        "chapters": chapters,
        "description_html": description_html,
        "duration_sec": duration_sec,
    }
