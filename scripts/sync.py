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
# Environment (required)
# -----------------------------
RSS = os.environ.get("RSS", "").strip()
REPO = os.environ.get("REPO", "").strip()  # e.g. "agenda-podcast/podcast-archive"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
RELEASE_TAG = os.environ.get("RELEASE_TAG", "audio-archive").strip()

# -----------------------------
# Environment (optional metadata)
# -----------------------------
PODCAST_TITLE = (os.environ.get("PODCAST_TITLE", "Agenda") or "Agenda").strip()
PODCAST_LINK = (os.environ.get("PODCAST_LINK", f"https://github.com/{REPO}") or f"https://github.com/{REPO}").strip()
PODCAST_DESCRIPTION = (os.environ.get("PODCAST_DESCRIPTION", "Podcast archive feed.") or "Podcast archive feed.").strip()
PODCAST_IMAGE = (os.environ.get("PODCAST_IMAGE", "") or "").strip()
ITUNES_CATEGORY = (os.environ.get("ITUNES_CATEGORY", "News") or "News").strip()
ITUNES_SUBCATEGORY = (os.environ.get("ITUNES_SUBCATEGORY", "") or "").strip()

DATA_FILE = "data/episodes.json"
RSS_OUT = "feed/rss.xml"
TMP_DIR = "audio_tmp"

os.makedirs("data", exist_ok=True)
os.makedirs("feed", exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# -----------------------------
# Utility: safe filename (FIXED: was missing)
# -----------------------------
def safe_filename(s: str) -> str:
    """
    Convert arbitrary text to a filesystem-safe filename base.
    Keeps only a-z A-Z 0-9 . _ - and replaces others with underscore.
    """
    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("_")
    return s[:180] if s else "episode"

# -----------------------------
# Date parsing
# -----------------------------
def parse_pubdate(entry) -> datetime:
    """
    Parse published/updated fields to a timezone-aware UTC datetime.
    """
    for k in ("published", "updated", "pubDate"):
        v = entry.get(k)
        if v:
            try:
                dt = dtparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                return dt
            except Exception:
                pass
    return datetime.now(timezone.utc)

# -----------------------------
# Stable identity + duplicate prevention
# -----------------------------
def source_key(entry) -> str:
    """
    Stable key to identify an episode from the SOURCE feed across runs.
    Prevents duplicates in our state.
    
    Uses sanitized identifiers that don't expose source URLs.

    Priority:
    1) Extract numeric episode ID from enclosure URL
    2) Extract numeric ID from id/guid/link fields
    3) hash(title|published)
    """
    # 1) Try to extract numeric ID from enclosure URL
    try:
        enclosures = entry.get("enclosures")
        if enclosures and len(enclosures) > 0:
            href = enclosures[0].get("href") or ""
            if isinstance(href, str) and href.startswith("http"):
                # Extract episode-specific numeric ID from URL (usually 7-8 digits)
                # Look for patterns like /episodes/18322949- or /18322949/
                m = re.search(r"/episodes?/(\d{7,})", href)
                if not m:
                    # Try to find last long numeric segment before filename
                    m = re.search(r"/(\d{7,})[-/]", href)
                
                if m:
                    return f"episode:{m.group(1)}"
                # Fallback: hash the URL to create stable key without exposing it
                return "episode:" + hashlib.sha256(href.encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass

    # 2) Try to extract numeric ID from id/guid/link
    for k in ("id", "guid", "link"):
        v = entry.get(k)
        if v:
            # Extract numeric ID if present (7+ digits for episode IDs)
            m = re.search(r"(\d{7,})", str(v))
            if m:
                return f"episode:{m.group(1)}"
            # Hash the value to avoid exposing source
            return f"episode:" + hashlib.sha256(str(v).encode("utf-8")).hexdigest()[:16]

    # 3) Fallback hash based on content
    raw = (entry.get("title", "") + "|" + entry.get("published", "")).encode("utf-8")
    return "episode:" + hashlib.sha256(raw).hexdigest()[:16]


def generate_guid(entry) -> str:
    """
    Generates a GUID that NEVER contains the word 'buzzsprout',
    but remains stable for the same episode.

    Preferred: extract numeric episode id (e.g. Buzzsp-18322949 -> agenda-18322949).
    Otherwise: sha256(source_key).
    """
    raw = str(entry.get("id") or entry.get("guid") or entry.get("link") or "")

    # Extract a long numeric token if present (RSS episode id, 7+ digits)
    m = re.search(r"(\d{7,})", raw)
    if m:
        return f"agenda-{m.group(1)}"

    # Otherwise, hash stable key
    key = source_key(entry)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

# -----------------------------
# GitHub Release helpers
# -----------------------------
def gh_headers(token: str, extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AgendaPodcastArchiver/2.2",
    }
    if extra:
        h.update(extra)
    return h

def ensure_release(repo: str, token: str, tag: str) -> dict:
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
        headers=gh_headers(token),
        timeout=60,
    )
    if r.status_code == 200:
        return r.json()

    r = requests.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=gh_headers(token),
        json={"tag_name": tag, "name": tag, "draft": False, "prerelease": False},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

def list_assets(token: str, release: dict) -> list:
    r = requests.get(release["assets_url"], headers=gh_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()

def delete_asset(token: str, asset_api_url: str) -> None:
    r = requests.delete(asset_api_url, headers=gh_headers(token), timeout=60)
    if r.status_code not in (204, 404):
        r.raise_for_status()

def asset_exists(token: str, release: dict, filename: str, expected_size: int = None) -> tuple[bool, str | None, int]:
    """
    Check if an asset with the given filename already exists in the release.
    Returns (exists, download_url, size) tuple.
    If expected_size is provided, also validates the file size matches.
    """
    for a in list_assets(token, release):
        if a.get("name") == filename:
            asset_size = a.get("size", 0)
            # If size is provided, validate it matches
            if expected_size is not None:
                if asset_size != expected_size:
                    return False, None, 0
            return True, a.get("browser_download_url"), asset_size
    return False, None, 0

def upload_asset(token: str, release: dict, file_path: str) -> None:
    filename = os.path.basename(file_path)

    # Idempotency: remove asset with same name
    for a in list_assets(token, release):
        if a.get("name") == filename:
            delete_asset(token, a["url"])
            break

    upload_url = release["upload_url"].split("{")[0]
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{upload_url}?name={filename}",
            headers=gh_headers(token, {"Content-Type": "audio/mpeg"}),
            data=f,
            timeout=300,
        )
    r.raise_for_status()

# -----------------------------
# RSS download helpers (403 hardening)
# -----------------------------
def resolve_download_url(url: str) -> str:
    """
    Follow redirects with browser-like headers.
    Helps when host protects direct downloads.
    """
    headers = {
        "User-Agent": f"AgendaPodcastArchiver/2.2 (+https://github.com/{REPO})",
        "Referer": RSS,
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
        "User-Agent": f"AgendaPodcastArchiver/2.2 (+https://github.com/{REPO})",
        "Referer": RSS,
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

# -----------------------------
# RSS building
# -----------------------------
def build_rss(episodes_sorted: list) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    atom_self = escape(f"{PODCAST_LINK.rstrip('/')}/feed/rss.xml")
    image_url = escape(PODCAST_IMAGE) if PODCAST_IMAGE else ""

    if ITUNES_CATEGORY and ITUNES_SUBCATEGORY:
        cat_block = (
            f'<itunes:category text="{escape(ITUNES_CATEGORY)}">'
            f'<itunes:category text="{escape(ITUNES_SUBCATEGORY)}"/>'
            f"</itunes:category>"
        )
    else:
        cat_block = f'<itunes:category text="{escape(ITUNES_CATEGORY)}"/>'

    items = []
    for ep in episodes_sorted:
        title = escape(ep["title"])
        guid = escape(ep["guid"])
        pubdate = escape(ep["pubDate_rfc822"])
        enc_url = escape(ep["audio_url"])  # absolute URL
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

# -----------------------------
# Episode sorting
# -----------------------------
def sort_datetime(ep: dict) -> datetime:
    """
    Extract and parse the publication date from an episode for sorting.
    Returns the parsed datetime or epoch if parsing fails.
    """
    try:
        return dtparser.parse(ep.get("pubDate_rfc822", "1970-01-01T00:00:00Z"))
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

# -----------------------------
# State I/O (backward compatible)
# -----------------------------
def migrate_source_keys(episodes_map: dict) -> dict:
    """
    Migrate old source_keys that contain full URLs to sanitized keys.
    This ensures backwards compatibility while removing provider URL exposure.
    """
    migrated = {}
    for old_key, ep in episodes_map.items():
        if not isinstance(ep, dict):
            continue
            
        # If the key starts with "enclosure:" and contains a URL, sanitize it
        if old_key.startswith("enclosure:") and "://" in old_key:
            url = old_key.replace("enclosure:", "", 1)
            # Extract episode-specific numeric ID from URL (usually 8 digits)
            # Look for patterns like /episodes/18322949- or /18322949/
            m = re.search(r"/episodes?/(\d{7,})", url)
            if not m:
                # Try to find last long numeric segment before filename
                m = re.search(r"/(\d{7,})[-/]", url)
            
            if m:
                new_key = f"episode:{m.group(1)}"
            else:
                # Hash the URL to create stable key
                new_key = "episode:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            
            # Update the source_key field in the episode data
            ep["source_key"] = new_key
            migrated[new_key] = ep
        else:
            # Keep as-is if already sanitized
            migrated[old_key] = ep
    
    return migrated

def load_state() -> dict:
    """
    Current state schema:
    {
      "episodes": {
        "<source_key>": {
           "source_key": "...",
           "guid": "...",
           "title": "...",
           "pubDate_rfc822": "...",
           "audio_url": "...",
           "length_bytes": 123,
           "description_html": "..."
        },
        ...
      }
    }
    """
    if not os.path.exists(DATA_FILE):
        return {"episodes": {}}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    episodes = data.get("episodes")

    if isinstance(episodes, dict):
        # Migrate old source_keys to sanitized format
        episodes = migrate_source_keys(episodes)
        return {"episodes": episodes}

    # If older schema used a list, salvage as best possible
    if isinstance(episodes, list):
        migrated = {}
        for ep in episodes:
            if isinstance(ep, dict):
                k = ep.get("source_key") or ep.get("guid")
                if k:
                    migrated[str(k)] = ep
        migrated = migrate_source_keys(migrated)
        return {"episodes": migrated}

    return {"episodes": {}}

def save_state(state: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# -----------------------------
# Main
# -----------------------------
def main():
    if not RSS:
        raise RuntimeError("RSS is empty. Set it as a GitHub Actions secret.")
    if not REPO:
        raise RuntimeError("REPO is empty (should be set by workflow: github.repository).")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is empty (should be secrets.GITHUB_TOKEN).")

    # Load existing state first (preserve all archived episodes)
    state = load_state()
    episodes_map = state.get("episodes", {})
    if not isinstance(episodes_map, dict):
        episodes_map = {}
        state["episodes"] = episodes_map
    
    # Store the count of existing episodes before processing
    existing_episode_count = len(episodes_map)

    # Parse SOURCE feed
    src = feedparser.parse(RSS)
    if not src.entries:
        # Even if SOURCE RSS has no entries, preserve existing episodes
        episodes = list(episodes_map.values())
        episodes.sort(key=sort_datetime, reverse=True)
        
        save_state(state)
        with open(RSS_OUT, "w", encoding="utf-8") as f:
            f.write(build_rss(episodes))
        print(f"OK. SOURCE RSS has no entries. Preserved {len(episodes_map)} existing episodes.")
        return

    release = ensure_release(REPO, GITHUB_TOKEN, RELEASE_TAG)

    new_count = 0
    skipped_no_http = 0
    skipped_download_errors = 0

    for entry in src.entries:
        skey = source_key(entry)

        # If already known, reuse GUID forever to avoid duplicates in directories
        existing = episodes_map.get(skey) if isinstance(episodes_map.get(skey), dict) else None
        if existing and existing.get("guid"):
            guid = existing["guid"]
        else:
            guid = generate_guid(entry)

        # Get enclosure
        if not entry.get("enclosures"):
            continue

        audio_src = entry.enclosures[0].get("href")
        if not audio_src or not str(audio_src).startswith("http"):
            skipped_no_http += 1
            continue

        title = entry.get("title", "Untitled")
        pub_dt = parse_pubdate(entry)
        pub_rfc822 = format_datetime(pub_dt)

        # Determine expected filename
        filename = safe_filename(f"{pub_dt.strftime('%Y%m%d')}-{title}")
        filename = re.sub(r"\.mp3$", "", filename, flags=re.IGNORECASE) + ".mp3"
        target_url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{filename}"

        # If we already have this episode in state with matching GitHub URL, reuse it
        if existing and isinstance(existing.get("audio_url"), str) and f"/releases/download/{RELEASE_TAG}/" in existing["audio_url"]:
            # Check if the asset still exists in releases with matching size
            existing_filename = existing["audio_url"].split("/")[-1]
            existing_size = int(existing.get("length_bytes", 0))
            exists, asset_url, _ = asset_exists(GITHUB_TOKEN, release, existing_filename, existing_size)
            
            if exists:
                episodes_map[skey] = {
                    "source_key": skey,
                    "guid": guid,
                    "title": title,
                    "pubDate_rfc822": pub_rfc822,
                    "audio_url": existing["audio_url"],
                    "length_bytes": existing_size,
                    "description_html": entry.get("summary", ""),
                }
                continue

        # Check if file already exists in GitHub releases (even if not in our state)
        exists, asset_url, asset_size = asset_exists(GITHUB_TOKEN, release, filename)
        if exists and asset_url:
            episodes_map[skey] = {
                "source_key": skey,
                "guid": guid,
                "title": title,
                "pubDate_rfc822": pub_rfc822,
                "audio_url": target_url,
                "length_bytes": int(asset_size),
                "description_html": entry.get("summary", ""),
            }
            print(f"Skipped download for '{title}': file already exists in releases")
            continue

        # Download and upload
        tmp_path = os.path.join(TMP_DIR, filename)

        try:
            final_url = resolve_download_url(audio_src)
            length = download_file(final_url, tmp_path)
        except Exception as e:
            skipped_download_errors += 1
            print(f"WARNING: download failed for '{title}': {e}")
            # Do not publish an episode without a valid enclosure
            continue

        upload_asset(GITHUB_TOKEN, release, tmp_path)

        episodes_map[skey] = {
            "source_key": skey,
            "guid": guid,
            "title": title,
            "pubDate_rfc822": pub_rfc822,
            "audio_url": target_url,
            "length_bytes": int(length),
            "description_html": entry.get("summary", ""),
        }

        try:
            os.remove(tmp_path)
        except OSError:
            pass

        new_count += 1

    # Sort episodes newest-first for RSS output
    episodes = list(episodes_map.values())
    episodes.sort(key=sort_datetime, reverse=True)

    # Persist state and RSS
    state["episodes"] = episodes_map
    save_state(state)

    rss_xml = build_rss(episodes)

    # Hard guard: never allow Buzzsp references in final RSS output
    if "buzzsp" in rss_xml.lower():
        raise RuntimeError("ERROR: 'buzzsp' detected in final RSS output. Aborting.")

    with open(RSS_OUT, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    print(
        f"OK. New archived: {new_count}. "
        f"Existing episodes preserved: {existing_episode_count}. "
        f"Total episodes in state: {len(episodes_map)}. "
        f"Skipped non-http enclosures: {skipped_no_http}. "
        f"Download errors: {skipped_download_errors}."
    )

if __name__ == "__main__":
    main()
