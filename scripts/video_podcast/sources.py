# ASCII-only. No ellipses. Keep <= 500 lines.

import time
import urllib.parse
from typing import Any, Dict, List, Tuple

from .util import USER_AGENT, http_get_json


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
