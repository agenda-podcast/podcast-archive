#!/usr/bin/env python3
# Copyright (c) Agenda Podcast
# All rights reserved. 
# This code is owned by Agenda Podcast. Copying, redistribution or usage without
# explicit written permission is prohibited.
"""
Sync script for podcast-archive. 

This script fetches a remote RSS feed, imports channel + episode metadata,
downloads enclosures into audio/ (if requested), sanitizes references to hosts
(e.g., Buzzsprout) and writes: 
- data/episodes.json  -> a JSON object with "channel" and "episodes"
- feed/rss.xml        -> sanitized RSS feed

Usage examples:
  python scripts/sync.py --download --public-url "https://example.org/podcast" --poster "https://example.org/podcast/poster.jpg"
"""
import os
import re
import json
import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests
import feedparser
from dateutil import parser as dtparser

# -----------------------------
# Required env
# -----------------------------
BUZZSPROUT_RSS = os.environ["BUZZSPROUT_RSS"]
REPO = os.environ["REPO"]                  # e.g. "agenda-podcast/podcast-archive"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
RELEASE_TAG = os.environ.get("RELEASE_TAG", "audio-archive")

# Optional metadata env
PODCAST_TITLE = os.environ.get("PODCAST_TITLE", "Agenda")
PODCAST_LINK = os.environ.get("PODCAST_LINK", f"https://github.com/{REPO}")
PODCAST_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "Podcast archive feed.")
PODCAST_IMAGE = os.environ.get("PODCAST_IMAGE", "")
ITUNES_CATEGORY = os.environ.get("ITUNES_CATEGORY", "News")
ITUNES_SUBCATEGORY = os.environ.get("ITUNES_SUBCATEGORY", "")

DATA_FILE = "data/episodes.json"
RSS_OUT = "feed/rss.xml"

os.makedirs("data", exist_ok=True)
os.makedirs("feed", exist_ok=True)
os.makedirs("audio_tmp", exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "agenda-podcast-archiver"
})

def gh_api(method: str, url: str, **kwargs) -> requests.Response:
    r = SESSION.request(method, url, timeout=180, **kwargs)
    return r

def ensure_release(tag: str) -> dict:
    # Check if release exists
    r = gh_api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    if r.status_code == 200:
        return r.json()

    # Create release
    r = gh_api("POST", f"https://api.github.com/repos/{REPO}/releases", json={
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": False
    })
    r.raise_for_status()
    return r.json()

def list_release_assets(release: dict) -> list:
    r = gh_api("GET", release["assets_url"])
    r.raise_for_status()
    return r.json()

def delete_asset(asset_api_url: str) -> None:
    r = gh_api("DELETE", asset_api_url)
    # 204 expected; ignore if already gone
    if r.status_code not in (204, 404):
        r.raise_for_status()

def upload_asset(release: dict, file_path: str, content_type: str = "audio/mpeg") -> None:
    filename = os.path.basename(file_path)

    # Delete existing asset with same filename (idempotency)
    assets = list_release_assets(release)
    for a in assets:
        if a.get("name") == filename:
            delete_asset(a["url"])
            break

    upload_url = release["upload_url"].split("{")[0]  # strip template
    with open(file_path, "rb") as f:
        r = SESSION.post(
            f"{upload_url}?name={filename}",
            headers={"Content-Type": content_type},
            data=f,
            timeout=300
        )
    r.raise_for_status()

def safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("_")
    return s[:180] if s else "episode.mp3"

def stable_guid(entry) -> str:
    # Prefer feed-provided GUID/ID; fallback to link; last resort: hash of title+date
    guid = entry.get("id") or entry.get("guid") or entry.get("link")
    if guid:
        return str(guid)
    raw = (entry.get("title","") + "|" + entry.get("published","")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def parse_pubdate(entry) -> datetime:
    for k in ("published", "updated"):
        if entry.get(k):
            try:
                dt = dtparser.parse(entry[k])
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def resolve_download_url(url: str) -> str:
    """
    Buzzsprout sometimes serves episode .mp3 URLs that redirect (or are protected).
    We resolve to the final URL using browser-like headers.
    """
    headers = {
        "User-Agent": "AgendaPodcastArchiver/1.0 (+https://github.com/%s)" % REPO,
        "Referer": BUZZSPROUT_RSS,
        "Accept": "*/*",
    }

    # Try HEAD first (fast) – some servers block HEAD; then fallback to GET
    try:
        r = requests.head(url, headers=headers, allow_redirects=True, timeout=60)
        if r.status_code < 400 and r.url:
            return r.url
    except Exception:
        pass

    r = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=60)
    r.raise_for_status()
    return r.url or url


def download_file(url: str, out_path: str) -> int:
    """
    Download with headers to avoid Buzzsprout/Cloudflare 403.
    Returns bytes length.
    """
    headers = {
        "User-Agent": "AgendaPodcastArchiver/1.0 (+https://github.com/%s)" % REPO,
        "Referer": BUZZSPROUT_RSS,
        "Accept": "*/*",
    }

    with requests.get(url, headers=headers, stream=True, timeout=300, allow_redirects=True) as r:
        r.raise_for_status()
        total = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        return total
      

def build_rss(episodes: list) -> str:
    # episodes must be sorted newest-first for most clients
    now = format_datetime(datetime.now(timezone.utc))

    itunes_ns = 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
    atom_ns = 'xmlns:atom="http://www.w3.org/2005/Atom"'
    content_ns = 'xmlns:content="http://purl.org/rss/1.0/modules/content/"'

    atom_self = escape(f"{PODCAST_LINK.rstrip('/')}/feed/rss.xml")
    image_url = escape(PODCAST_IMAGE) if PODCAST_IMAGE else ""

    cat_block = ""
    if ITUNES_CATEGORY and ITUNES_SUBCATEGORY:
        cat_block = f'<itunes:category text="{escape(ITUNES_CATEGORY)}"><itunes:category text="{escape(ITUNES_SUBCATEGORY)}"/></itunes:category>'
    elif ITUNES_CATEGORY:
        cat_block = f'<itunes:category text="{escape(ITUNES_CATEGORY)}"/>'

    items = []
    for ep in episodes:
        # All URLs must be absolute for best compatibility
        enc_url = escape(ep["audio_url"])
        title = escape(ep["title"])
        guid = escape(ep["guid"])
        pubdate = escape(ep["pubDate_rfc822"])
        desc = ep.get("description_html","")  # already HTML
        dur = escape(str(ep.get("duration_seconds",""))) if ep.get("duration_seconds") else ""

        items.append(
f"""    <item>
      <title>{title}</title>
      <itunes:title>{title}</itunes:title>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pubdate}</pubDate>
      <description><![CDATA[{desc}]]></description>
      <enclosure url="{enc_url}" length="{ep.get("length_bytes",0)}" type="audio/mpeg"/>
      {f"<itunes:duration>{dur}</itunes:duration>" if dur else ""}
      <itunes:explicit>false</itunes:explicit>
    </item>"""
        )

    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" {atom_ns} {itunes_ns} {content_ns}>
  <channel>
    <atom:link href="{atom_self}" rel="self" type="application/rss+xml"/>
    <title>{escape(PODCAST_TITLE)}</title>
    <link>{escape(PODCAST_LINK)}</link>
    <language>en-us</language>
    <copyright>© {datetime.now(timezone.utc).year} {escape(PODCAST_TITLE)}</copyright>
    <description><![CDATA[{PODCAST_DESCRIPTION}]]></description>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{escape(PODCAST_TITLE)}</itunes:author>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
    {f'<itunes:image href="{image_url}"/>' if image_url else ""}
    {cat_block}
{os.linesep.join(items)}
  </channel>
</rss>
"""

def main():
# Load existing state
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
else:
    state = {}

episodes_raw = state.get("episodes")

# Migrate old formats to dict keyed by guid
if isinstance(episodes_raw, dict):
    episodes_map = episodes_raw
elif isinstance(episodes_raw, list):
    episodes_map = {}
    for ep in episodes_raw:
        if isinstance(ep, dict) and ep.get("guid"):
            episodes_map[str(ep["guid"])] = ep
elif episodes_raw is None:
    episodes_map = {}
else:
    # Unknown type, reset safely
    episodes_map = {}

state["episodes"] = episodes_map

    # Parse SOURCE feed (Buzzsprout)
    src = feedparser.parse(BUZZSPROUT_RSS)
    if not src.entries:
        raise RuntimeError("SOURCE RSS has no entries; check BUZZSPROUT_RSS")

    release = ensure_release(RELEASE_TAG)

    new_count = 0

    for entry in src.entries:
        guid = stable_guid(entry)
        if guid in episodes_map:
            continue

        # Buzzsprout enclosure URL must be absolute
        if not entry.get("enclosures"):
            continue

        enc = entry.enclosures[0]
        audio_src = enc.get("href")
        if not audio_src or not str(audio_src).startswith("http"):
            # If it is relative, it cannot be downloaded: skip
            continue

        title = entry.get("title","Untitled")
        pub_dt = parse_pubdate(entry)
        pub_rfc822 = format_datetime(pub_dt)

        # filename
        base = safe_filename(f"{pub_dt.strftime('%Y%m%d')}-{title}") + ".mp3"
        tmp_path = os.path.join("audio_tmp", base)

        # download
        length = download_file(audio_src, tmp_path)

        # upload to release
        upload_asset(release, tmp_path)

        # target URL (stable)
        target_url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{base}"

        episodes_map[guid] = {
            "guid": guid,
            "title": title,
            "pubDate_rfc822": pub_rfc822,
            "audio_url": target_url,
            "length_bytes": length,
            "description_html": entry.get("summary","")
        }

        # cleanup
        try:
            os.remove(tmp_path)
        except OSError:
            pass

        new_count += 1

    # Build sorted list newest-first
    episodes = list(episodes_map.values())
    def sort_key(e):
        try:
            return dtparser.parse(e["pubDate_rfc822"])
        except Exception:
            return datetime(1970,1,1,tzinfo=timezone.utc)
    episodes.sort(key=sort_key, reverse=True)

    # Save state
    state["episodes"] = episodes_map
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # Write RSS
    rss = build_rss(episodes)
    with open(RSS_OUT, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"OK. New episodes archived: {new_count}. Total: {len(episodes)}")

if __name__ == "__main__":
    main()
