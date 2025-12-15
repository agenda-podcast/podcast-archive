import hashlib
import requests
from datetime import datetime, timezone, timedelta

GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

def _hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def _now_utc():
    return datetime.now(timezone.utc)

def _fmt_gdelt(dt: datetime) -> str:
    # YYYYMMDDHHMMSS
    return dt.strftime("%Y%m%d%H%M%S")

def collect_sources(topic: dict, state: dict) -> dict:
    """
    Collect fresh sources (last 24h) from GDELT DOC API (multilingual).
    Persist them into state['backlog'] with dedup and rolling retention.
    GDELT DOC API provides global coverage and supports time filtering. 7
    """
    state.setdefault("seen_hashes", {})
    state.setdefault("backlog", [])

    # retention window
    retention_days = int(topic.get("retention_days", 14))
    min_keep_dt = _now_utc() - timedelta(days=retention_days)

    # drop old
    backlog_new = []
    for it in state["backlog"]:
        try:
            dt = datetime.fromisoformat(it["seen_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= min_keep_dt:
            backlog_new.append(it)
    state["backlog"] = backlog_new

    # Build dynamic query (simple): topic['query'] must be present.
    # You can later expand this to multilingual variants using Gemini.
    query = topic.get("query", "").strip()
    if not query:
        raise RuntimeError("topic.query is required")

    end = _now_utc()
    start = end - timedelta(hours=int(topic.get("fresh_hours", 24)))

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": 250,
        "sort": "HybridRel",
        "startdatetime": _fmt_gdelt(start),
        "enddatetime": _fmt_gdelt(end),
    }

    r = requests.get(GDELT_DOC, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    articles = data.get("articles", []) or []
    fresh_added = 0

    for a in articles:
        url = a.get("url") or ""
        title = a.get("title") or ""
        domain = a.get("domain") or ""
        language = a.get("language") or ""
        seendate = a.get("seendate") or ""  # e.g., 20251214073500

        if not url.startswith("http"):
            continue

        key = _hash(url)  # stable
        if key in state["seen_hashes"]:
            continue

        # Save
        state["seen_hashes"][key] = True
        state["backlog"].append({
            "url": url,
            "title": title,
            "domain": domain,
            "language": language,
            "seendate": seendate,
            "seen_utc": _now_utc().isoformat().replace("+00:00", "Z"),
        })
        fresh_added += 1

    # Simple ranking: newest-first, then domain diversity (basic)
    state["backlog"].sort(key=lambda x: x.get("seendate", ""), reverse=True)

    return {"fresh_count": fresh_added, "backlog_total": len(state["backlog"])}
