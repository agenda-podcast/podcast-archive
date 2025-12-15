import json
import os
import re
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import feedparser


# -----------------------------
# Trust tiers (edit as needed)
# -----------------------------
TRUST_TIER_1 = {
    "reuters.com", "www.reuters.com",
    "bbc.co.uk", "www.bbc.co.uk", "bbc.com", "www.bbc.com",
    "nytimes.com", "www.nytimes.com",
    "ft.com", "www.ft.com",
    "wsj.com", "www.wsj.com",
    "apnews.com", "www.apnews.com",
}
TRUST_TIER_2 = {
    "theguardian.com", "www.theguardian.com",
    "washingtonpost.com", "www.washingtonpost.com",
    "npr.org", "www.npr.org",
    "economist.com", "www.economist.com",
    "bloomberg.com", "www.bloomberg.com",
    "cnbc.com", "www.cnbc.com",
    "aljazeera.com", "www.aljazeera.com",
    "dw.com", "www.dw.com",
}
TRUST_TIER_3 = {
    "axios.com", "www.axios.com",
    "politico.com", "www.politico.com",
    "time.com", "www.time.com",
    "cnn.com", "www.cnn.com",
    "foxnews.com", "www.foxnews.com",
    "cbsnews.com", "www.cbsnews.com",
    "nbcnews.com", "www.nbcnews.com",
    "abcnews.go.com",
}


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def trust_tier(domain: str) -> int:
    d = (domain or "").lower()
    if d in TRUST_TIER_1:
        return 1
    if d in TRUST_TIER_2:
        return 2
    if d in TRUST_TIER_3:
        return 3
    return 9


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt_any(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None

    # RFC822 sometimes
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # ISO-ish fallback
    try:
        from dateutil import parser as dtparser
        dt = dtparser.parse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_ws(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def canonicalize_url(url: str) -> str:
    """
    Remove common tracking params; normalize scheme/host; keep path and useful params.
    """
    url = (url or "").strip()
    if not url.startswith("http"):
        return url

    p = urlparse(url)
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()

    # Remove fragments
    fragment = ""

    # Filter query params
    qs = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_"):
            continue
        if kl in {"fbclid", "gclid", "mc_cid", "mc_eid", "cmpid"}:
            continue
        qs.append((k, v))
    query = urlencode(qs, doseq=True)

    return urlunparse((scheme, netloc, p.path, p.params, query, fragment))


def stable_id(title: str, url: str) -> str:
    raw = (clean_ws(title) + "|" + canonicalize_url(url)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class SourceItem:
    title: str
    url: str
    domain: str
    published: str
    lang: str
    tier: int
    provider: str  # "google_news_rss" | "gdelt"
    query: str


def to_dict(it: SourceItem) -> Dict[str, Any]:
    return {
        "title": it.title,
        "url": it.url,
        "domain": it.domain,
        "published": it.published,
        "lang": it.lang,
        "tier": it.tier,
        "provider": it.provider,
        "query": it.query,
        "id": stable_id(it.title, it.url),
    }


# -----------------------------
# Collectors
# -----------------------------
def google_news_rss(query: str, hl: str, gl: str, ceid: str, timeout: int = 25) -> List[Dict[str, Any]]:
    """
    Google News RSS (no API key). Region/language via hl/gl/ceid.
    """
    # Example:
    # https://news.google.com/rss/search?q=immigration%20freeze&hl=en-US&gl=US&ceid=US:en
    q = requests.utils.quote(query, safe="")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    feed = feedparser.parse(url)
    out = []
    for e in getattr(feed, "entries", []) or []:
        title = clean_ws(getattr(e, "title", "") or "")
        link = getattr(e, "link", "") or ""
        published = getattr(e, "published", "") or getattr(e, "updated", "") or ""
        out.append({"title": title, "url": link, "published": published})
    return out


def gdelt_doc_search(query: str, mode: str = "ArtList", max_records: int = 50, timeout: int = 30) -> List[Dict[str, Any]]:
    """
    GDELT 2 DOC API (no key): https://api.gdeltproject.org/api/v2/doc/doc
    """
    # ArtList is lightweight; you can switch to "timelinevolraw" etc later.
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": mode,
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "HybridRel",
    }
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code >= 400:
        return []
    try:
        data = r.json()
    except Exception:
        return []

    arts = data.get("articles") or []
    out = []
    for a in arts:
        title = clean_ws(str(a.get("title", "") or ""))
        link = str(a.get("url", "") or "")
        published = str(a.get("seendate", "") or "")  # YYYYMMDDHHMMSS
        out.append({"title": title, "url": link, "published": published})
    return out


def parse_gdelt_seendate(seendate: str) -> str:
    s = (seendate or "").strip()
    # Expected: YYYYMMDDHHMMSS
    if re.fullmatch(r"\d{14}", s):
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return ""
    dt = parse_dt_any(s)
    if dt:
        return dt.isoformat().replace("+00:00", "Z")
    return ""


# -----------------------------
# State I/O
# -----------------------------
def read_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x, list) else []
    except Exception:
        return []


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        url = canonicalize_url(str(it.get("url", "") or ""))
        title = clean_ws(str(it.get("title", "") or ""))
        if not url or not title:
            continue
        sid = it.get("id") or stable_id(title, url)
        if sid in seen:
            continue
        seen.add(sid)
        it["url"] = url
        it["title"] = title
        it["id"] = sid
        out.append(it)
    return out


def is_fresh(item: Dict[str, Any], fresh_after: datetime) -> bool:
    pub = (item.get("published") or "").strip()
    dt = parse_dt_any(pub)
    if not dt:
        # if missing date, treat as not fresh (so it accumulates backlog but doesn't pass gate)
        return False
    return dt >= fresh_after


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    topic_id = os.environ.get("TOPIC_ID", "").strip()
    if not topic_id:
        raise RuntimeError("TOPIC_ID is empty")

    topics_path = Path("topics") / f"{topic_id}.json"
    if not topics_path.exists():
        raise RuntimeError(f"Missing topic config: {topics_path}")

    topic = json.loads(topics_path.read_text(encoding="utf-8"))

    # Config (topic-level defaults)
    min_fresh_sources = int(topic.get("min_fresh_sources", 20))
    freshness_hours = int(topic.get("freshness_hours", 24))
    max_results_per_query = int(topic.get("max_results_per_query", 50))
    backlog_max = int(topic.get("backlog_max", 2000))

    # Queries & languages
    queries = topic.get("queries") or []
    if isinstance(queries, str):
        queries = [queries]
    queries = [clean_ws(q) for q in queries if clean_ws(q)]

    if not queries:
        # fallback to title
        title = clean_ws(str(topic.get("title", "") or ""))
        if not title:
            raise RuntimeError("Topic has no queries and no title fallback")
        queries = [title]

    # Languages are used for Google News hl/gl/ceid only; GDELT is multilingual by nature.
    languages = topic.get("languages") or [{"hl": "en-US", "gl": "US", "ceid": "US:en", "lang": "en"}]
    if isinstance(languages, dict):
        languages = [languages]

    # Paths
    data_dir = Path("data") / topic_id
    fresh_path = data_dir / "fresh.json"
    backlog_path = data_dir / "backlog.json"
    snapshot_path = data_dir / "last_collect_snapshot.json"

    existing_fresh = read_json_list(fresh_path)
    existing_backlog = read_json_list(backlog_path)

    fresh_after = now_utc() - timedelta(hours=freshness_hours)

    collected: List[Dict[str, Any]] = []

    # ---- Collect from Google News RSS (per query x language) ----
    for q in queries:
        for lang_cfg in languages:
            hl = str(lang_cfg.get("hl", "en-US"))
            gl = str(lang_cfg.get("gl", "US"))
            ceid = str(lang_cfg.get("ceid", "US:en"))
            lang = str(lang_cfg.get("lang", "en"))

            try:
                rows = google_news_rss(q, hl=hl, gl=gl, ceid=ceid)
            except Exception:
                rows = []

            for r in rows[:max_results_per_query]:
                url = canonicalize_url(str(r.get("url", "") or ""))
                title = clean_ws(str(r.get("title", "") or ""))
                if not url or not title:
                    continue

                dom = domain_of(url)
                pub_raw = str(r.get("published", "") or "")
                dt = parse_dt_any(pub_raw)
                pub = dt.isoformat().replace("+00:00", "Z") if dt else ""

                item = SourceItem(
                    title=title,
                    url=url,
                    domain=dom,
                    published=pub,
                    lang=lang,
                    tier=trust_tier(dom),
                    provider="google_news_rss",
                    query=q,
                )
                collected.append(to_dict(item))

            time.sleep(0.2)  # be polite

    # ---- Collect from GDELT (per query) ----
    for q in queries:
        try:
            rows = gdelt_doc_search(q, max_records=max_results_per_query)
        except Exception:
            rows = []

        for r in rows[:max_results_per_query]:
            url = canonicalize_url(str(r.get("url", "") or ""))
            title = clean_ws(str(r.get("title", "") or ""))
            if not url or not title:
                continue

            dom = domain_of(url)
            pub = parse_gdelt_seendate(str(r.get("published", "") or ""))

            item = SourceItem(
                title=title,
                url=url,
                domain=dom,
                published=pub,
                lang="multi",
                tier=trust_tier(dom),
                provider="gdelt",
                query=q,
            )
            collected.append(to_dict(item))

        time.sleep(0.2)

    # ---- Merge + dedupe with existing backlog for accumulation ----
    merged_all = merge_dedupe(existing_fresh + existing_backlog + collected)

    # ---- Split into fresh vs backlog using freshness window ----
    fresh_items = [it for it in merged_all if is_fresh(it, fresh_after)]
    backlog_items = [it for it in merged_all if it not in fresh_items]

    # ---- Sort: fresher first; then trust tier ----
    def sort_key(it: Dict[str, Any]) -> Tuple[int, int, str]:
        # Lower tier is better; newer published is better (string compare of ISO is OK here)
        tier = int(it.get("tier", 9))
        pub = str(it.get("published", "") or "")
        # reverse by pub later; keep stable
        return (tier, 0, pub)

    fresh_items.sort(key=lambda x: (int(x.get("tier", 9)), x.get("published", "")), reverse=False)
    # make newest-first within each tier
    fresh_items = sorted(fresh_items, key=lambda x: (int(x.get("tier", 9)), x.get("published", "")), reverse=False)

    # To ensure newest-first overall while keeping tier preference, we can do:
    # primary: tier asc, secondary: published desc
    fresh_items = sorted(fresh_items, key=lambda x: (int(x.get("tier", 9)), -(int(hashlib.md5((x.get("published","") or "").encode()).hexdigest(),16) % (10**12))))
    # The above line is intentionally deterministic but not ideal for true time ordering without parsing.
    # Replace with a real datetime parse to sort desc:
    def pub_dt(it: Dict[str, Any]) -> datetime:
        dt = parse_dt_any(str(it.get("published", "") or ""))
        return dt if dt else datetime(1970, 1, 1, tzinfo=timezone.utc)

    fresh_items = sorted(fresh_items, key=lambda x: (int(x.get("tier", 9)), pub_dt(x)), reverse=False)
    # Now reverse within same tier by pub_dt:
    fresh_items = sorted(fresh_items, key=lambda x: (int(x.get("tier", 9)), -int(pub_dt(x).timestamp())), reverse=False)

    # backlog: keep best tiers + newest-ish, capped
    backlog_items = merge_dedupe(backlog_items)
    backlog_items = sorted(backlog_items, key=lambda x: (int(x.get("tier", 9)), -int(pub_dt(x).timestamp())), reverse=False)
    backlog_items = backlog_items[:backlog_max]

    # ---- Gate logic: only publish fresh.json if enough fresh sources ----
    if len(fresh_items) >= min_fresh_sources:
        # Keep top N fresh (but still save backlog for long-term)
        fresh_cap = int(topic.get("fresh_cap", 500))
        fresh_items = fresh_items[:fresh_cap]
        write_json(fresh_path, fresh_items)
        write_json(backlog_path, backlog_items)
        status = "OK"
    else:
        # Not enough: keep backlog growing; write empty fresh.json for this day
        write_json(fresh_path, [])
        write_json(backlog_path, merge_dedupe(existing_backlog + collected)[:backlog_max])
        status = "SKIP"

    snapshot = {
        "topic_id": topic_id,
        "status": status,
        "min_fresh_sources": min_fresh_sources,
        "freshness_hours": freshness_hours,
        "queries": queries,
        "collected_now": len(collected),
        "fresh_now": len(fresh_items),
        "backlog_now": len(read_json_list(backlog_path)),
        "fresh_written": (len(read_json_list(fresh_path))),
        "timestamp_utc": now_utc().isoformat().replace("+00:00", "Z"),
    }
    write_json(snapshot_path, snapshot)

    print(
        f"[{topic_id}] status={status} collected={len(collected)} "
        f"fresh_written={snapshot['fresh_written']} backlog={snapshot['backlog_now']} "
        f"(min_fresh={min_fresh_sources}, window={freshness_hours}h)"
    )


if __name__ == "__main__":
    main()
