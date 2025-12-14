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
# Environment
# -----------------------------
BUZZSPROUT_RSS = os.environ.get("BUZZSPROUT_RSS", "").strip()
REPO = os.environ.get("REPO", "").strip()  # e.g. "agenda-podcast/podcast-archive"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
RELEASE_TAG = os.environ.get("RELEASE_TAG", "audio-archive").strip()

# Optional metadata
PODCAST_TITLE = os.environ.get("PODCAST_TITLE", "Agenda").strip() or "Agenda"
PODCAST_LINK = os.environ.get("PODCAST_LINK", f"https://github.com/{REPO}".rstrip("/")).strip() or f"https://github.com/{REPO}"
PODCAST_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "Podcast archive feed.").strip() or "Podcast archive feed."
PODCAST_IMAGE = os.environ.get("PODCAST_IMAGE", "").strip()
ITUNES_CATEGORY = os.environ.get("ITUNES_CATEGORY", "News").strip() or "News"
ITUNES_SUBCATEGORY = os.environ.get("ITUNES_SUBCATEGORY", "").strip()

DATA_FILE = "data/episodes.json"
RSS_OUT = "feed/rss.xml"

# Local temp
TMP_DIR = "audio_tmp"

os.makedirs("data", exist_ok=True)
os.makedirs("feed", exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# -----------------------------
# Helpers
# -----------------------------
def safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("_")
    return s[:180] if s else "episode"

def stable_guid(entry) -> str:
    guid = entry.get("id") or entry.get("guid") or entry.get("link")
    if guid:
        return str(guid)
    raw = (entry.get("title", "") + "|" + entry.get("published", "")).encode("utf-8")
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

def ensure_release(repo: str, token: str, tag: str) -> dict:
    if not repo or not token:
        raise RuntimeError("Missing REPO or GITHUB_TOKEN in environment.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AgendaPodcastArchiver/1.0",
    }

    # Try get release by tag
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=headers, timeout=60)
    if r.status_code == 200:
        return r.json()

    # Create release
    r = requests.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        json={"tag_name": tag, "name": tag, "draft": False, "prerelease": False},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def list_release_assets(repo: str, token: str, release: dict) -> list:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AgendaPodcastArchiver/1.0",
    }
    r = requests.get(release["assets_url"], headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

def delete_asset(token: str, asset_url: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AgendaPodcastArchiver/1.0",
    }
    r = requests.delete(asset_url, headers=headers, timeout=60)
    if r.status_code not in (204, 404):
        r.raise_for_status()

def upload_asset(repo: str, token: str, release: dict, file_path: str) -> None:
    filename = os.path.basename(file_path)

    # Delete existing asset with same name (idempotent)
    assets = list_release_assets(repo, token, release)
    for a in assets:
        if a.get("name") == filename:
            delete_asset(token, a["url"])
            break

    upload_url = release["upload_url"].split("{")[0]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AgendaPodcastArchiver/1.0",
        "Content-Type": "audio/mpeg",
    }

    with open(file_path, "rb") as f:
        r = requests.post(f"{upload_url}?name={filename}", headers=headers, data=f, timeout=300)
    r.raise_for_status()

def resolve_download_url(url: str) -> str:
    # Buzzsprout may protect "www.buzzsprout.com/...mp3" links; use browser-like headers + follow redirects
    headers = {
        "User-Agent": f"AgendaPodcastArchiver/1.0 (+https://github.com/{REPO})",
        "Referer": BUZZSPROUT_RSS,
        "Accept": "*/*",
    }

    # HEAD may be blocked; fallback to GET
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
    headers = {
        "User-Agent": f"AgendaPodcastArchiver/1.0 (+https://github.com/{REPO})",
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
    now = format_datetime(datetime.now(timezone.utc))
    atom_self = escape(f"{PODCAST_LINK.rstrip('/')}/feed/rss.xml")
    image_url = escape(PODCAST_IMAGE) if PODCAST_IMAGE else ""

    if ITUNES_CATEGORY and ITUNES_SUBCATEGORY:
        cat_block = f'<itunes:category text="{escape(ITUNES_CATEGORY)}"><itunes:category text="{escape(ITUNES_SUBCATEGORY)}"/></itunes:category>'
    else:
        cat_block = f'<itunes:category text="{escape(ITUNES_CATEGORY)}"/>'

    items = []
    for ep in episodes:
        title = escape(ep["title"])
        guid = escape(ep["guid"])
        pubdate = escape(ep["pubDate_rfc822"])
        enc_url = escape(ep["audio_url"])
        desc_html = ep.get("description_html", "")

        items.append(f"""    <item>
      <title>{title}</title>
      <itunes:title>{title}</itunes:title>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pubdate}</pubDate>
      <description><![CDATA[{desc_html}]]></description>
      <enclosure url="{enc_url}" length="{int(ep.get("length_bytes", 0))}" type="audio/mpeg"/>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
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

def write_skeleton_outputs():
    # Always create baseline outputs so the workflow can commit something even on first run / zero episodes.
    state = {"episodes": {}}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    with open(RSS_OUT, "w", encoding="utf-8") as f:
        f.write(build_rss([]))

def main():
    if not BUZZSPROUT_RSS:
        raise RuntimeError("BUZZSPROUT_RSS is empty. Set the BUZZSPROUT_RSS secret.")
    if not REPO:
        raise RuntimeError("REPO is empty (should be set by workflow: github.repository).")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is empty (should be secrets.GITHUB_TOKEN).")

    # Always write skeleton first (prevents 'No changes' on brand-new repo)
    write_skeleton_outputs()

    # Load state (backward compatible with list/dict)
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    episodes_raw = state.get("episodes")
    if isinstance(episodes_raw, dict):
        episodes_map = episodes_raw
    elif isinstance(episodes_raw, list):
        episodes_map = {}
        for ep in episodes_raw:
            if isinstance(ep, dict) and ep.get("guid"):
                episodes_map[str(ep["guid"])] = ep
    else:
        episodes_map = {}
    state["episodes"] = episodes_map

    # Parse SOURCE feed (Buzzsprout)
    src = feedparser.parse(BUZZSPROUT_RSS)
    if not src.entries:
        # Write outputs and exit cleanly
        episodes = []
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        with open(RSS_OUT, "w", encoding="utf-8") as f:
            f.write(build_rss(episodes))
        print("OK. SOURCE RSS has no entries. Wrote skeleton outputs.")
        return

    # Ensure release exists
    release = ensure_release(REPO, GITHUB_TOKEN, RELEASE_TAG)

    new_count = 0

    for entry in src.entries:
        guid = stable_guid(entry)
        if guid in episodes_map:
            continue

        if not entry.get("enclosures"):
            continue

        audio_src = entry.enclosures[0].get("href")
        if not audio_src or not str(audio_src).startswith("http"):
            continue

        title = entry.get("title", "Untitled")
        pub_dt = parse_pubdate(entry)
        pub_rfc822 = format_datetime(pub_dt)

        filename = safe_filename(f"{pub_dt.strftime('%Y%m%d')}-{title}") + ".mp3"
        tmp_path = os.path.join(TMP_DIR, filename)

        final_url = resolve_download_url(audio_src)
        length = download_file(final_url, tmp_path)

        upload_asset(REPO, GITHUB_TOKEN, release, tmp_path)

        target_url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{filename}"

        episodes_map[guid] = {
            "guid": guid,
            "title": title,
            "pubDate_rfc822": pub_rfc822,
            "audio_url": target_url,
            "length_bytes": length,
            "description_html": entry.get("summary", ""),
        }

        try:
            os.remove(tmp_path)
        except OSError:
            pass

        new_count += 1

    # Sort newest first
    episodes = list(episodes_map.values())
    try:
        episodes.sort(key=lambda e: dtparser.parse(e["pubDate_rfc822"]), reverse=True)
    except Exception:
        pass

    # Persist state + RSS
    state["episodes"] = episodes_map
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    with open(RSS_OUT, "w", encoding="utf-8") as f:
        f.write(build_rss(episodes))

    print(f"OK. New episodes archived: {new_count}. Total: {len(episodes)}")


if __name__ == "__main__":
    main()
