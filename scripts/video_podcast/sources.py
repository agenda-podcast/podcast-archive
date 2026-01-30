# ASCII-only. No ellipses. Keep <= 500 lines.

import time
import urllib.parse
from typing import Any, Dict, List, Tuple

from .util import USER_AGENT, http_get_json, strip_html


TIER_1 = "tier1"
TIER_2 = "tier2"
TIER_3 = "tier3"


def build_query_plan(title: str, desc_html: str, max_per_tier: int = 8) -> Dict[str, List[str]]:
    """Build a 3-tier query plan.

    Tier 1: visual entities and concrete nouns.
    Tier 2: topic-to-visual mapping phrases.
    Tier 3: generic fillers (used only if Tier 1 and Tier 2 clips are insufficient).
    """
    import re

    title = (title or "").strip()
    desc = strip_html(desc_html or "")
    raw = ("%s %s" % (title, desc)).strip()
    lower = raw.lower()
    lower = re.sub(r"http[s]?://\S+", " ", lower)
    lower = re.sub(r"[^a-z0-9\s]", " ", lower)
    words = [w for w in lower.split() if len(w) >= 3]

    stop = set([
        "the", "and", "for", "with", "from", "this", "that", "into", "over", "under",
        "your", "their", "they", "them", "were", "been", "also", "more", "most",
        "about", "will", "what", "when", "where", "which", "than", "then",
    ])
    abstract = set([
        "issue", "issues", "today", "analysis", "overview", "implications", "conflict",
        "debate", "discussion", "changes", "impact", "effects", "policy", "policies",
        "governance", "strategy", "strategic", "history", "historic",
    ])

    freq: Dict[str, int] = {}
    for w in words:
        if w in stop or w in abstract:
            continue
        freq[w] = freq.get(w, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))

    tier1: List[str] = []
    tier2: List[str] = []

    entity_map: List[Tuple[str, List[str]]] = [
        ("new york", ["new york city", "manhattan skyline", "nyc subway", "times square"]),
        ("nyc", ["new york city", "manhattan skyline", "nyc street"]),
        ("washington", ["washington dc", "us capitol", "white house"]),
        ("capitol", ["us capitol", "congress"]),
        ("congress", ["congress", "senate", "house of representatives"]),
        ("supreme", ["supreme court", "courtroom"]),
        ("court", ["courtroom", "judge", "lawyer"]),
        ("europe", ["european union", "brussels", "european parliament"]),
        ("eu", ["european union", "brussels"]),
        ("canada", ["canada winter", "snow storm"]),
        ("storm", ["winter storm", "snow", "ice storm", "power outage"]),
        ("snow", ["snow storm", "blizzard", "snow plow"]),
        ("power", ["power outage", "electric grid", "power lines"]),
        ("crime", ["police lights", "city street at night", "courtroom"]),
        ("prostitution", ["city street at night", "neon lights", "downtown street"]),
        ("ai", ["data center", "servers", "computer chip", "robot"]),
        ("chips", ["computer chip", "semiconductor"]),
        ("security", ["cybersecurity", "data center", "surveillance camera"]),
        ("litigation", ["lawyer", "courtroom", "legal documents"]),
        ("regulation", ["government building", "legal documents", "courtroom"]),
        ("regulatory", ["government building", "legal documents", "courtroom"]),
        ("preemption", ["supreme court", "constitution", "government building"]),
        ("constitution", ["constitution", "supreme court"]),
        ("infrastructure", ["bridge", "power lines", "construction"]),
    ]

    for needle, qs in entity_map:
        if needle in lower:
            for q in qs:
                if q not in tier1:
                    tier1.append(q)
                if len(tier1) >= max_per_tier:
                    break
        if len(tier1) >= max_per_tier:
            break

    visual_nouns = [
        "court", "courthouse", "judge", "lawyer", "capitol", "congress", "senate",
        "white", "house", "police", "city", "storm", "snow", "blizzard", "data",
        "center", "server", "robot", "chip", "cybersecurity", "protest",
    ]
    for w, _ in ranked:
        if w in stop or w in abstract:
            continue
        if w in visual_nouns and w not in tier1:
            tier1.append(w)
        if len(tier1) >= max_per_tier:
            break

    topic_map: List[Tuple[str, List[str]]] = [
        ("ai act", ["european union building", "compliance", "legal documents"]),
        ("federal", ["government building", "capitol building"]),
        ("state", ["state capitol", "government building"]),
        ("governance", ["government building", "legal documents"]),
        ("preemption", ["supreme court", "constitution"]),
        ("gpai", ["artificial intelligence", "data center"]),
        ("foundation model", ["artificial intelligence", "servers"]),
        ("liability", ["courtroom", "lawyer"]),
        ("intellectual property", ["patent", "legal documents"]),
        ("supply chain", ["shipping containers", "warehouse"]),
        ("outages", ["power outage", "electric grid"]),
        ("fatalities", ["ambulance", "hospital"]),
    ]

    for needle, qs in topic_map:
        if needle in lower:
            for q in qs:
                if q not in tier2 and q not in tier1:
                    tier2.append(q)
                if len(tier2) >= max_per_tier:
                    break
        if len(tier2) >= max_per_tier:
            break

    if title and title not in tier1 and len(tier1) < max_per_tier:
        tier1.insert(0, title)

    tier3 = [
        "technology",
        "city skyline",
        "business meeting",
        "computer",
        "news studio",
        "street traffic",
    ][:max_per_tier]

    return {TIER_1: tier1[:max_per_tier], TIER_2: tier2[:max_per_tier], TIER_3: tier3[:max_per_tier]}


def text_queries(title: str, desc: str, max_q: int = 12) -> List[str]:
    import re
    text = ("%s %s" % (title, desc)).lower()
    text = re.sub(r"http[s]?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [w for w in text.split() if len(w) >= 4]
    stop = set([
        "that", "this", "with", "from", "your", "about", "into", "have", "will", "they",
        "them", "what", "when", "where", "which", "their", "there", "were", "been",
        "also", "more", "over", "under", "than", "then", "very", "much", "most",
    ])
    freq: Dict[str, int] = {}
    for w in words:
        if w in stop:
            continue
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    qs: List[str] = []
    if title.strip():
        qs.append(title.strip())
    for w, _ in ranked:
        if w not in qs:
            qs.append(w)
        if len(qs) >= max_q:
            break
    return qs[:max_q]


def search_assets_by_tier(pexels_key: str, pixabay_key: str, query_plan: Dict[str, List[str]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {TIER_1: [], TIER_2: [], TIER_3: []}
    for tier in [TIER_1, TIER_2, TIER_3]:
        queries = query_plan.get(tier) or []
        assets: List[Dict[str, Any]] = []
        for q in queries:
            time.sleep(0.2)
            try:
                assets += pexels_search(pexels_key, q, per_page=10)
            except Exception:
                pass
            time.sleep(0.2)
            try:
                assets += pixabay_search(pixabay_key, q, per_page=15)
            except Exception:
                pass
        assets = dedupe_assets(assets)
        for a in assets:
            a["tier"] = tier
        out[tier] = assets
    return out


def pexels_search(api_key: str, q: str, per_page: int = 12) -> List[Dict[str, Any]]:
    url = "https://api.pexels.com/videos/search?query=%s&per_page=%d" % (urllib.parse.quote(q), per_page)
    j = http_get_json(url, headers={"Authorization": api_key, "User-Agent": USER_AGENT})
    vids = j.get("videos") or []
    out: List[Dict[str, Any]] = []
    for v in vids:
        vid = str(v.get("id") or "")
        page_url = str(v.get("url") or "")
        user = v.get("user") or {}
        author = str(user.get("name") or "")
        files = v.get("video_files") or []
        best = None
        best_area = -1
        for f in files:
            link = f.get("link")
            w = int(f.get("width") or 0)
            h = int(f.get("height") or 0)
            if not link or w <= 0 or h <= 0:
                continue
            area = w * h
            if area > best_area:
                best_area = area
                best = (link, w, h)
        if not best:
            continue
        link, w, h = best
        out.append({
            "source": "pexels",
            "asset_id": vid,
            "author": author,
            "page_url": page_url,
            "download_url": link,
            "width": w,
            "height": h,
            "license_url": "https://www.pexels.com/license/",
        })
    return out


def pixabay_search(api_key: str, q: str, per_page: int = 20) -> List[Dict[str, Any]]:
    url = "https://pixabay.com/api/videos/?key=%s&q=%s&per_page=%d" % (
        urllib.parse.quote(api_key),
        urllib.parse.quote(q),
        per_page,
    )
    j = http_get_json(url, headers={"User-Agent": USER_AGENT})
    hits = j.get("hits") or []
    out: List[Dict[str, Any]] = []
    for h in hits:
        vid = str(h.get("id") or "")
        author = str(h.get("user") or "")
        page_url = str(h.get("pageURL") or "")
        videos = h.get("videos") or {}
        cand: List[Tuple[int, str, int, int]] = []
        for key in ["large", "medium", "small", "tiny"]:
            v = videos.get(key)
            if not isinstance(v, dict):
                continue
            link = v.get("url")
            w = int(v.get("width") or 0)
            ht = int(v.get("height") or 0)
            if link and w > 0 and ht > 0:
                cand.append((w * ht, link, w, ht))
        if not cand:
            continue
        cand.sort(reverse=True)
        _, link, w, ht = cand[0]
        out.append({
            "source": "pixabay",
            "asset_id": vid,
            "author": author,
            "page_url": page_url,
            "download_url": link,
            "width": w,
            "height": ht,
            "license_url": "https://pixabay.com/service/license/",
        })
    return out


def dedupe_assets(assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for a in assets:
        key = "%s:%s" % (a.get("source"), a.get("asset_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def search_assets(pexels_key: str, pixabay_key: str, queries: List[str]) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    for q in queries:
        time.sleep(0.2)
        try:
            assets += pexels_search(pexels_key, q, per_page=10)
        except Exception:
            pass
        time.sleep(0.2)
        try:
            assets += pixabay_search(pixabay_key, q, per_page=15)
        except Exception:
            pass
    return dedupe_assets(assets)
