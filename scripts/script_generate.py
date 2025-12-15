from google import genai

def generate_30min_script_and_chapters(topic: dict, items: list, api_key: str) -> dict:
    """
    Produces:
      - 30-minute script (EN)
      - chapters list with start_sec + title
    """
    client = genai.Client(api_key=api_key)

    # Build sources block
    sources = []
    for i, it in enumerate(items, 1):
        sources.append(f"{i}. {it['title']} ({it['domain']}, {it.get('language','')})\n{it['url']}")
    sources_txt = "\n\n".join(sources)

    prompt = f"""
You are producing a 30-minute English audio news overview for the show:
{topic['title']}

Requirements:
- Neutral, factual, no opinions.
- Use only information that can be reasonably inferred from the provided sources list.
- Structure into 8–12 segments with smooth transitions.
- Provide a chapters list with timestamps assuming normal narration pace.
- Add a short disclaimer at the beginning: "This is an observation based on publicly available information. We did deep dive recearch and here is the most recent finding."

Output format EXACTLY:
1) SCRIPT:
<full script text>

2) CHAPTERS_JSON:
<JSON array of objects: {{ "start_sec": <int>, "title": "<string>" }}>

3) DESCRIPTION_HTML:
<short HTML summary with bullet points and a "Sources" list of URLs>

Sources:
{sources_txt}
"""

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    text = resp.text or ""
    # Minimal parsing (robust enough for CI); you can harden later.
    def _between(a, b):
        if a not in text or b not in text:
            return ""
        return text.split(a, 1)[1].split(b, 1)[0].strip()

    script = _between("1) SCRIPT:", "2) CHAPTERS_JSON:").strip()
    chapters_json = _between("2) CHAPTERS_JSON:", "3) DESCRIPTION_HTML:").strip()
    description_html = text.split("3) DESCRIPTION_HTML:", 1)[1].strip() if "3) DESCRIPTION_HTML:" in text else ""

    import json
    chapters = json.loads(chapters_json) if chapters_json else [{"start_sec": 0, "title": "Overview"}]

    # Approx duration: we will later replace with real duration from ffprobe if desired
    duration_sec = int(topic.get("duration_sec", 1800))

    return {
        "script": script,
        "chapters": chapters,
        "description_html": description_html,
        "duration_sec": duration_sec
    }
