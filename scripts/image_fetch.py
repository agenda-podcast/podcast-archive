import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests


# ---- Trust tiers (edit as needed) ----
TRUST_TIER_1 = {
    "reuters.com", "www.reuters.com",
    "bbc.com", "www.bbc.com",
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
}
TRUST_TIER_3 = {
    "axios.com", "www.axios.com",
    "aljazeera.com", "www.aljazeera.com",
    "dw.com", "www.dw.com",
    "lemonde.fr", "www.lemonde.fr",
    "elpais.com", "www.elpais.com",
    "cbc.ca", "www.cbc.ca",
}

# Some sites block hotlinking; you can add more accept-list domains here if desired.


@dataclass
class PickedImage:
    source_url: str
    image_url: str
    domain: str
    tier: int


def _domain(url: str) -> str:
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


def _pick_meta_image(html: str) -> Optional[str]:
    """
    Prefer og:image / twitter:image. Very lightweight parsing (no bs4 required).
    """
    if not html:
        return None

    # og:image
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    # twitter:image
    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    # itemprop=image
    m = re.search(r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    return None


def _http_get(url: str, timeout: int = 30) -> requests.Response:
    headers = {
        "User-Agent": "AgendaPodcastArchiver/3.0 (+https://github.com/agenda-podcast/podcast-archive)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)


def _head(url: str, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": "AgendaPodcastArchiver/3.0",
        "Accept": "image/*,*/*;q=0.8",
    }
    return requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)


def _is_image_content_type(ct: str) -> bool:
    ct = (ct or "").lower()
    return ct.startswith("image/")


def _download_image(img_url: str, out_path: Path, timeout: int = 45) -> bool:
    headers = {
        "User-Agent": "AgendaPodcastArchiver/3.0",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": img_url,
    }
    try:
        r = requests.get(img_url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if not _is_image_content_type(ct):
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)
        return True
    except Exception:
        return False


def select_and_download_backgrounds(
    items: List[Dict],
    tmp_dir: Path,
    max_images: int = 8,
) -> Tuple[List[Path], List[PickedImage]]:
    """
    items: list of {"url": "...", "title": "...", ...}
    returns:
      - list of downloaded image file paths
      - list of metadata for audit/debug
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Score items by trust tier first
    scored = []
    for it in items:
        src = str(it.get("url", "")).strip()
        if not src.startswith("http"):
            continue
        dom = _domain(src)
        tier = trust_tier(dom)
        scored.append((tier, src, dom))

    scored.sort(key=lambda x: x[0])

    picked_meta: List[PickedImage] = []
    downloaded: List[Path] = []
    seen_img_urls = set()

    for tier, src_url, dom in scored:
        if len(downloaded) >= max_images:
            break

        try:
            page = _http_get(src_url, timeout=30)
            if page.status_code >= 400:
                continue
            html = page.text or ""
            img = _pick_meta_image(html)
            if not img:
                continue
            img_abs = img if img.startswith("http") else urljoin(src_url, img)

            if img_abs in seen_img_urls:
                continue
            seen_img_urls.add(img_abs)

            # Quick HEAD check (not all servers support HEAD; ignore failures)
            try:
                h = _head(img_abs, timeout=15)
                if h.status_code < 400:
                    ct = h.headers.get("Content-Type", "")
                    if ct and not _is_image_content_type(ct):
                        continue
            except Exception:
                pass

            ext = Path(urlparse(img_abs).path).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                # Default to .jpg; ffmpeg can still read by magic bytes most of the time
                ext = ".jpg"

            out = tmp_dir / f"bg_t{tier}_{len(downloaded):02d}{ext}"
            ok = _download_image(img_abs, out, timeout=45)
            if not ok:
                continue

            downloaded.append(out)
            picked_meta.append(PickedImage(source_url=src_url, image_url=img_abs, domain=dom, tier=tier))
        except Exception:
            continue

    return downloaded, picked_meta
