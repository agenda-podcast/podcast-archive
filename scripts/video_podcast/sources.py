# ASCII-only. No ellipses. Keep <= 500 lines.

import time
import urllib.parse
from typing import Any, Dict, List, Tuple

from .util import USER_AGENT, http_get_json


_SENSITIVE_TERMS = [
    "prostitution",
    "porn",
    "pornography",
    "sex",
    "sexual",
    "escort",
    "brothel",
    "nude",
    "nudity",
    "fetish",
    "trafficking",
    "onlyfans",
]


_SENSITIVE_PROXY_QUERIES = {
    "prostitution": [
        "city streets at night",
        "police patrol car lights",
        "courthouse steps",
        "social services office",
        "public safety community outreach",
        "subway station at night",
        "city skyline night",
        "interview microphone street",
    ],
    "trafficking": [
        "police investigation",
        "courthouse steps",
        "community outreach",
        "public safety",
    ],
    "sex": [
        "city streets at night",
        "courthouse steps",
        "public safety",
    ],
    "porn": [
        "computer screen blur",
        "internet safety",
        "data privacy",
    ],
    "nude": [
        "city skyline",
        "street interview",
    ],
}


def _normalize_spaces(s: str) -> str:
    return " ".join((s or "").strip().split())


def _contains_term(hay: str, term: str) -> bool:
    hay_l = (hay or "").lower()
    term_l = (term or "").lower()
    if not term_l:
        return False
    return term_l in hay_l


def _detect_sensitive_terms(title: str, desc: str) -> List[str]:
    text = "%s %s" % (title or "", desc or "")
    found: List[str] = []
    for t in _SENSITIVE_TERMS:
        if _contains_term(text, t):
            found.append(t)
    return sorted(set(found))


def _location_prefix(title: str, desc: str) -> str:
    text = ("%s %s" % (title or "", desc or "")).lower()
    if "new york" in text or "nyc" in text:
        return "new york city"
    if "los angeles" in text or "la " in text:
        return "los angeles"
    if "washington dc" in text or "capitol" in text:
        return "washington dc"
    return ""


def apply_sensitive_query_policy(
    title: str,
    desc: str,
    queries: List[str],
    max_q: int = 12,
) -> Tuple[List[str], Dict[str, Any]]:
    """Filter unsafe search tokens and add safe proxy queries.

    This only affects Pexels/Pixabay search queries. It does not change
    episode metadata.
    """
    import re

    orig = [q for q in (queries or []) if (q or "").strip()]
    found = _detect_sensitive_terms(title, desc)
    prefix = _location_prefix(title, desc)

    filtered: List[str] = []
    dropped: List[str] = []

    for q in orig:
        qq = " %s " % (q or "")
        qq_l = qq.lower()
        changed = False
        for t in found:
            if t in qq_l:
                # Remove as whole word when possible.
                qq = re.sub(r"\b" + re.escape(t) + r"\b", " ", qq, flags=re.IGNORECASE)
                changed = True
        qq = _normalize_spaces(qq)
        if not qq:
            dropped.append(q)
            continue
        # If query became too generic after removal, keep it only if it is not just a single token.
        if changed and len(qq.split()) < 2:
            dropped.append(q)
            continue
        if qq not in filtered:
            filtered.append(qq)

    proxies: List[str] = []
    if found:
        proxy_raw: List[str] = []
        for t in found:
            proxy_raw += _SENSITIVE_PROXY_QUERIES.get(t, [])
        # Deduplicate while keeping order.
        seen = set()
        for p in proxy_raw:
            pp = _normalize_spaces(p)
            if not pp:
                continue
            if prefix:
                pp = _normalize_spaces("%s %s" % (prefix, pp))
            if pp.lower() in seen:
                continue
            seen.add(pp.lower())
            proxies.append(pp)

    # Combine. Prefer filtered originals, then proxies.
    combined: List[str] = []
    for q in filtered:
        combined.append(q)
        if len(combined) >= max_q:
            break
    if len(combined) < max_q:
        for p in proxies:
            if p not in combined:
                combined.append(p)
            if len(combined) >= max_q:
                break

    policy = {
        "sensitive_detected": bool(found),
        "matched_terms": found,
        "location_prefix": prefix,
        "queries_original": orig,
        "queries_filtered": combined,
        "queries_dropped": dropped,
        "proxy_queries_added": proxies,
    }
    return combined, policy


def text_queries(title: str, desc: str, max_q: int = 12) -> List[str]:
    tiered = build_tiered_queries(title, desc, max_q=max_q)
    out: List[str] = []
    for item in tiered:
        q = str(item.get("query") or "").strip()
        if q and q not in out:
            out.append(q)
        if len(out) >= max_q:
            break
    return out


def _stop_words() -> set:
    return set([
        "that", "this", "with", "from", "your", "about", "into", "have", "will", "they",
        "them", "what", "when", "where", "which", "their", "there", "were", "been",
        "also", "more", "over", "under", "than", "then", "very", "much", "most",
        "some", "just", "like", "because", "after", "before", "again", "today",
    ])


def _clean_for_tokens(s: str) -> str:
    import re
    t = (s or "")
    t = re.sub(r"http[s]?://\S+", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", t)
    return " ".join(t.split()).strip()


def _title_phrases(title: str, max_phrases: int = 6) -> List[str]:
    t = _clean_for_tokens(title)
    if not t:
        return []
    words = t.split()
    stop = _stop_words()

    phrases: List[str] = []
    # Tier-1: exact title first.
    phrases.append(title.strip())

    # Then short sliding-window phrases (2 to 4 words) that are not just stop words.
    for n in [4, 3, 2]:
        for i in range(0, max(0, len(words) - n + 1)):
            seg = words[i:i + n]
            seg_l = [w.lower() for w in seg]
            if all(w in stop for w in seg_l):
                continue
            if sum(1 for w in seg_l if w in stop) >= n - 1:
                continue
            ph = " ".join(seg).strip()
            if ph and ph not in phrases:
                phrases.append(ph)
            if len(phrases) >= max_phrases:
                return phrases[:max_phrases]
    return phrases[:max_phrases]


def _keywords(text: str, max_k: int = 10) -> List[str]:
    t = _clean_for_tokens(text).lower()
    if not t:
        return []
    stop = _stop_words()
    words = [w for w in t.split() if len(w) >= 4 and w not in stop]
    freq: Dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    out: List[str] = []
    for w, _ in ranked:
        if w not in out:
            out.append(w)
        if len(out) >= max_k:
            break
    return out


def build_tiered_queries(title: str, desc: str, max_q: int = 12) -> List[Dict[str, Any]]:
    """Build tiered search queries.

    Tier-1: high-precision title phrases.
    Tier-2: extracted keywords + short phrases.
    Tier-3: safe generic fallbacks.
    """
    title = (title or "").strip()
    desc = (desc or "").strip()

    t1 = _title_phrases(title, max_phrases=6) if title else []
    kw = _keywords("%s %s" % (title, desc), max_k=10)

    t2: List[str] = []
    for w in kw:
        if w and w not in t2:
            t2.append(w)
    # Prefer a few short phrases derived from the title.
    for ph in t1[1:]:
        if ph and ph not in t2:
            t2.append(ph)

    t3: List[str] = []
    # Generic fallbacks, tuned for podcast/news style episodes.
    for g in [
        "podcast microphone",
        "news studio",
        "city skyline",
        "world map",
        "finance chart",
        "crowd street",
    ]:
        if g not in t3:
            t3.append(g)

    out: List[Dict[str, Any]] = []
    for q in t1:
        if q:
            out.append({"tier": 1, "query": q})
    for q in t2:
        if q:
            out.append({"tier": 2, "query": q})
    for q in t3:
        if q:
            out.append({"tier": 3, "query": q})

    # Dedupe while keeping the best tier.
    seen = set()
    ded: List[Dict[str, Any]] = []
    for item in out:
        q = _normalize_spaces(str(item.get("query") or ""))
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        ded.append({"tier": int(item.get("tier") or 3), "query": q})
        if len(ded) >= max_q:
            break
    return ded


def pexels_search(api_key: str, q: str, per_page: int = 12, page: int = 1) -> List[Dict[str, Any]]:
    # Pexels supports pagination via the `page` query parameter.
    url = "https://api.pexels.com/videos/search?query=%s&per_page=%d&page=%d" % (
        urllib.parse.quote(q),
        int(per_page),
        int(page),
    )
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


def pixabay_search(api_key: str, q: str, per_page: int = 20, page: int = 1) -> List[Dict[str, Any]]:
    url = "https://pixabay.com/api/videos/?key=%s&q=%s&per_page=%d&page=%d" % (
        urllib.parse.quote(api_key),
        urllib.parse.quote(q),
        per_page,
        int(page),
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
    seen = {}
    out: List[Dict[str, Any]] = []
    for a in assets:
        key = "%s:%s" % (a.get("source"), a.get("asset_id"))
        tier = int(a.get("tier") or 3)
        if key in seen:
            prev_i = seen[key]
            prev_tier = int(out[prev_i].get("tier") or 3)
            if tier < prev_tier:
                out[prev_i] = a
            continue
        seen[key] = len(out)
        out.append(a)
    return out


def search_assets(pexels_key: str, pixabay_key: str, queries: List[Any]) -> List[Dict[str, Any]]:
    """Search for candidate assets.

    Accepts either:
    - List[str] queries (treated as tier=2)
    - List[{tier:int, query:str}] tiered queries
    """
    tiered: List[Dict[str, Any]] = []
    if queries and isinstance(queries[0], str):
        for q in queries:
            qq = _normalize_spaces(str(q))
            if qq:
                tiered.append({"tier": 2, "query": qq})
    else:
        for item in (queries or []):
            if not isinstance(item, dict):
                continue
            q = _normalize_spaces(str(item.get("query") or ""))
            if not q:
                continue
            tiered.append({"tier": int(item.get("tier") or 3), "query": q})

    assets: List[Dict[str, Any]] = []
    for item in tiered:
        q = str(item.get("query") or "").strip()
        tier = int(item.get("tier") or 3)
        if not q:
            continue
        time.sleep(0.2)
        try:
            for a in pexels_search(pexels_key, q, per_page=10):
                a["tier"] = tier
                a["query"] = q
                assets.append(a)
        except Exception:
            pass
        time.sleep(0.2)
        try:
            for a in pixabay_search(pixabay_key, q, per_page=15):
                a["tier"] = tier
                a["query"] = q
                assets.append(a)
        except Exception:
            pass

    out = dedupe_assets(assets)
    out.sort(key=lambda a: (int(a.get("tier") or 3), str(a.get("source") or ""), str(a.get("asset_id") or "")))
    return out


def search_assets_page(
    pexels_key: str,
    pixabay_key: str,
    q: str,
    *,
    tier: int = 1,
    page: int = 1,
    pexels_per_page: int = 10,
    pixabay_per_page: int = 15,
) -> List[Dict[str, Any]]:
    """Fetch a single paginated page of assets for a single query.

    This is used by the render pipeline to keep requesting *more* results for
    the same query (tier-1 episode title) until duration is covered.
    """
    qq = _normalize_spaces(str(q or ""))
    if not qq:
        return []

    assets: List[Dict[str, Any]] = []

    time.sleep(0.2)
    try:
        for a in pexels_search(pexels_key, qq, per_page=int(pexels_per_page), page=int(page)):
            a["tier"] = int(tier)
            a["query"] = qq
            assets.append(a)
    except Exception:
        pass

    time.sleep(0.2)
    try:
        for a in pixabay_search(pixabay_key, qq, per_page=int(pixabay_per_page), page=int(page)):
            a["tier"] = int(tier)
            a["query"] = qq
            assets.append(a)
    except Exception:
        pass

    out = dedupe_assets(assets)
    out.sort(key=lambda a: (str(a.get("source") or ""), str(a.get("asset_id") or "")))
    return out
