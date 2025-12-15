import json
import os
import re
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def parse_dt_any(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None

    # RFC822-ish
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
    url = (url or "").strip()
    if not url.startswith("http"):
        return url

    p = urlparse(url)
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    fragment = ""

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


def pub_dt(it: Dict[str, Any]) -> datetime:
    dt = parse_dt_any(str(it.get("published", "") or ""))
    return dt if dt else datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_fresh(item: Dict[str, Any], fresh_after: datetime) -> bool:
    dt = pub_dt(item)
    if dt.year == 1970:
        return False
    return dt >= fresh_after


@dataclass
class SourceItem:
    title: str
    url: str
    domain: str
    published: str
    lang: str
    tier: int
    provider: str
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
def google_news_rss(query: str, hl: str, gl: str, ceid: str) -> List[Dict[str, Any]]:
    q = requests.utils.quote(query, safe="")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    feed = feedparser.parse(url)
    out: List[Dict[str, Any]] = []
    for e in getattr(feed, "entries", []) or []:
        title = clean_ws(getattr(e, "title", "") or "")
        link = getattr(e, "link", "") or ""
        published = getattr(e, "published", "") or getattr(e, "updated", "") or ""
        out.append({"title": title, "url": link, "published": published})
    return out


def gdelt_doc_search(query: str, max_records: int = 50, timeout: int = 30) -> List[Dict[str, Any]]:
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
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
    out: List[Dict[str, Any]] = []
    for a in arts:
        title = clean_ws(str(a.get("title", "") or ""))
        link = str(a.get("url", "") or "")
        seendate = str(a.get("seendate", "") or "")  # YYYYMMDDHHMMSS
        out.append({"title": title, "url": link, "published": seendate})
    return out


def parse_gdelt_seendate(seendate: str) -> str:
    s = (seendate or "").strip()
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
    out: List[Dict[str, Any]] = []
    for it in items:
        url = canonicalize_url(str(it.get("url", "") or ""))
        title = clean_ws(str(it.get("title", "") or ""))
        if not url or not title:
            continue

        sid = str(it.get("id") or stable_id(title, url))
        if sid in seen:
            continue
        seen.add(sid)

        dom = str(it.get("domain") or domain_of(url))
        it["url"] = url
        it["title"] = title
        it["id"] = sid
        it["domain"] = dom
        it["tier"] = int(it.get("tier") or trust_tier(dom))

        out.append(it)
    return out


# -----------------------------
# Topic selection (NO TOPIC_ID REQUIRED)
# -----------------------------
def discover_topic_ids() -> List[str]:
    tid = (os.environ.get("TOPIC_ID") or "").strip()
    if tid:
        return [tid]

    tids = (os.environ.get("TOPIC_IDS") or "").strip()
    if tids:
        parts = re.split(r"[,\s]+", tids)
        return [p.strip() for p in parts if p.strip()]

    topics_dir = Path("topics")
    if not topics_dir.exists():
        return []
    files = sorted(topics_dir.glob("topic-*.json"))
    return [f.stem for f in files]


def load_topic(topic_id: str) -> Dict[str, Any]:
    p = Path("topics") / f"{topic_id}.json"
    if not p.exists():
        raise RuntimeError(f"Missing topic config: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


# -----------------------------
# Per-topic collect
# -----------------------------
def collect_for_topic(topic_id: str) -> Dict[str, Any]:
    topic = load_topic(topic_id)

    min_fresh_sources = int(topic.get("min_fresh_sources", 20))
    freshness_hours = int(topic.get("freshness_hours", 24))
    max_results_per_query = int(topic.get("max_results_per_query", 50))
    backlog_max = int(topic.get("backlog_max", 2000))
    fresh_cap = int(topic.get("fresh_cap", 500))

    queries = topic.get("queries") or []
    if isinstance(queries, str):
        queries = [queries]
    queries = [clean_ws(q) for q in queries if clean_ws(q)]
    if not queries:
        title = clean_ws(str(topic.get("title", "") or ""))
        if not title:
            raise RuntimeError(f"{topic_id}: no queries and no title fallback")
        queries = [title]

    languages = topic.get("languages") or [{"hl": "en-US", "gl": "US", "ceid": "US:en", "lang": "en"}]
    if isinstance(languages, dict):
        languages = [languages]

    data_dir = Path("data") / topic_id
    fresh_path = data_dir / "fresh.json"
    backlog_path = data_dir / "backlog.json"
    snapshot_path = data_dir / "last_collect_snapshot.json"

    existing_fresh = read_json_list(fresh_path)
    existing_backlog = read_json_list(backlog_path)

    fresh_after = now_utc() - timedelta(hours=freshness_hours)

    collected: List[Dict[str, Any]] = []

    # Google News RSS
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

                collected.append(
                    to_dict(SourceItem(
                        title=title,
                        url=url,
                        domain=dom,
                        published=pub,
                        lang=lang,
                        tier=trust_tier(dom),
                        provider="google_news_rss",
                        query=q,
                    ))
                )

            time.sleep(0.15)

    # GDELT
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

            collected.append(
                to_dict(SourceItem(
                    title=title,
                    url=url,
                    domain=dom,
                    published=pub,
                    lang="multi",
                    tier=trust_tier(dom),
                    provider="gdelt",
                    query=q,
                ))
            )

        time.sleep(0.15)

    merged_all = merge_dedupe(existing_fresh + existing_backlog + collected)

    fresh_items = [it for it in merged_all if is_fresh(it, fresh_after)]
    backlog_items = [it for it in merged_all if it not in fresh_items]

    # Sort fresh: tier asc, published desc
    fresh_items = sorted(
        fresh_items,
        key=lambda x: (int(x.get("tier", 9)), -int(pub_dt(x).timestamp())),
        reverse=False,
    )[:fresh_cap]

    backlog_items = merge_dedupe(backlog_items)
    backlog_items = sorted(
        backlog_items,
        key=lambda x: (int(x.get("tier", 9)), -int(pub_dt(x).timestamp())),
        reverse=False,
    )[:backlog_max]

    if len(fresh_items) >= min_fresh_sources:
        write_json(fresh_path, fresh_items)
        write_json(backlog_path, backlog_items)
        status = "OK"
        fresh_written = len(fresh_items)
    else:
        # Accumulate only; keep fresh empty so downstream topic run can skip
        write_json(fresh_path, [])
        write_json(backlog_path, merge_dedupe(existing_backlog + collected)[:backlog_max])
        status = "SKIP"
        fresh_written = 0

    snapshot = {
        "topic_id": topic_id,
        "status": status,
        "min_fresh_sources": min_fresh_sources,
        "freshness_hours": freshness_hours,
        "queries": queries,
        "collected_now": len(collected),
        "fresh_written": fresh_written,
        "backlog_now": len(read_json_list(backlog_path)),
        "timestamp_utc": now_utc().isoformat().replace("+00:00", "Z"),
    }
    write_json(snapshot_path, snapshot)

    print(
        f"[{topic_id}] status={status} collected={len(collected)} "
        f"fresh_written={fresh_written} backlog={snapshot['backlog_now']} "
        f"(min_fresh={min_fresh_sources}, window={freshness_hours}h)"
    )
    return snapshot


def main() -> None:
    topic_ids = discover_topic_ids()
    if not topic_ids:
        raise RuntimeError("No topics found. Add topics/topic-*.json or set TOPIC_ID/TOPIC_IDS.")

    results: List[Dict[str, Any]] = []
    failed = 0

    for tid in topic_ids:
        try:
            results.append(collect_for_topic(tid))
        except Exception as e:
            failed += 1
            print(f"[{tid}] ERROR: {e}")

    Path("data").mkdir(parents=True, exist_ok=True)
    write_json(
        Path("data") / "collect_run_summary.json",
        {"results": results, "failed": failed, "timestamp_utc": now_utc().isoformat().replace("+00:00", "Z")},
    )

    if failed:
        print(f"Collect completed with failures: {failed}")
    else:
        print("Collect completed successfully for all topics.")


if __name__ == "__main__":
    main()
