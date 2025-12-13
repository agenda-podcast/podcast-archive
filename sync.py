#!/usr/bin/env python3
"""
Sync script for podcast-archive.

Additional behavior:
- Downloads episode artwork (if present) into posters/.
- If POSTER_BASE_URL environment variable (or --poster-url) is provided, rewrites channel and item image URLs
  to point to that base URL (typically GitHub Releases download base for tag 'latest').

Usage examples:
  python scripts/sync.py --download --poster-url "https://github.com/OWNER/REPO/releases/download/latest/poster.jpg"
  python scripts/sync.py --download --poster-url-base "https://github.com/OWNER/REPO/releases/download/latest"
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urlparse, unquote, urljoin
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

DEFAULT_FEED_SOURCE = "https://feeds.buzzsprout.com/2562524.rss"

ROOT = Path(".")
AUDIO_DIR = ROOT / "audio"
POSTERS_DIR = ROOT / "posters"
DATA_FILE = ROOT / "data" / "episodes.json"
FEED_FILE = ROOT / "feed" / "rss.xml"
ASSETS_DIR = ROOT / "assets"
PODCAST_POSTER = ASSETS_DIR / "poster.jpg"
ALLOWED_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom"
}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def ensure_dirs():
    for d in (AUDIO_DIR, POSTERS_DIR, ASSETS_DIR, FEED_FILE.parent, DATA_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)


def load_episodes() -> dict:
    if not DATA_FILE.exists():
        return {"channel": {}, "episodes": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        print(f"Error loading {DATA_FILE}: {e}", file=sys.stderr)
        return {"channel": {}, "episodes": []}


def save_episodes_structured(channel: dict, episodes: list) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"channel": channel, "episodes": episodes}
    with DATA_FILE.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def format_rfc2822(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return format_datetime(dt)


def parse_pubdate(text: str) -> str:
    if not text:
        return format_rfc2822(None)
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_rfc2822(dt)
    except Exception:
        return text


def filename_from_url(url: str, fallback: str = None) -> str:
    try:
        path = urlparse(url).path
        name = unquote(Path(path).name)
        if not name and fallback:
            name = fallback
        name = name.split("?")[0]
        if not os.path.splitext(name)[1]:
            name = name + ".jpg" if any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png")) else name + ".mp3"
        return name
    except Exception:
        return fallback or "file.bin"


def download_file(url: str, dest: Path, timeout: int = 30) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return True
    req = Request(url, headers={"User-Agent": "podcast-archive-sync/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            with dest.open("wb") as fh:
                chunk_size = 8192
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        return False


def fetch_remote_feed(url: str, timeout: int = 20) -> bytes | None:
    req = Request(url, headers={"User-Agent": "podcast-archive-sync/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        print(f"Error fetching feed {url}: {e}", file=sys.stderr)
        return None


def element_text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def extract_channel_metadata(channel: ET.Element) -> dict:
    meta = {}
    for tag in ("title", "link", "description", "language", "lastBuildDate", "copyright"):
        meta[tag] = element_text(channel.find(tag))
    # image: try standard <image><url>, then itunes:image@href
    img = channel.find("image")
    if img is not None:
        meta["image"] = element_text(img.find("url"))
    it_img = channel.find(f"{{{NS['itunes']}}}image")
    if it_img is not None:
        href = it_img.get("href")
        if href:
            meta["image"] = href
    meta["itunes"] = {}
    for it_tag in ("author", "subtitle", "summary", "explicit", "type"):
        meta["itunes"][it_tag] = element_text(channel.find(f"{{{NS['itunes']}}}{it_tag}"))
    owner = channel.find(f"{{{NS['itunes']}}}owner")
    if owner is not None:
        meta["itunes"]["owner_name"] = element_text(owner.find(f"{{{NS['itunes']}}}name"))
        meta["itunes"]["owner_email"] = element_text(owner.find(f"{{{NS['itunes']}}}email"))
    cats = []
    for c in channel.findall(f"{{{NS['itunes']}}}category"):
        text = c.get("text") or ""
        subcats = [sc.get("text") for sc in c.findall(f"{{{NS['itunes']}}}category") if sc.get("text")]
        if subcats:
            cats.append({"category": text, "subcategories": subcats})
        else:
            cats.append(text)
    if not cats:
        cats = [element_text(c) for c in channel.findall("category")]
    meta["categories"] = cats
    return meta


def extract_item_metadata(item: ET.Element) -> dict:
    ep = {}
    ep["title"] = element_text(item.find("title"))
    cont = item.find(f"{{{NS['content']}}}encoded")
    ep["description"] = element_text(cont) or element_text(item.find("description"))
    ep["pubDate"] = parse_pubdate(element_text(item.find("pubDate")))
    ep["guid"] = element_text(item.find("guid")) or ""
    encl = item.find("enclosure")
    if encl is not None:
        ep["enclosure_url"] = (encl.get("url") or "").strip()
        ep["enclosure_length"] = (encl.get("length") or "0").strip()
        ep["enclosure_type"] = (encl.get("type") or "").strip()
    else:
        media = item.find(f"{{{NS['media']}}}content")
        if media is not None:
            ep["enclosure_url"] = (media.get("url") or "").strip()
            ep["enclosure_length"] = (media.get("fileSize") or "0").strip()
            ep["enclosure_type"] = (media.get("type") or "").strip()
        else:
            ep["enclosure_url"] = element_text(item.find("link"))
            ep["enclosure_length"] = "0"
            ep["enclosure_type"] = ""
    ep["filename"] = filename_from_url(ep.get("enclosure_url", ""), fallback=ep.get("guid") or ep.get("title") or "")
    it = {}
    for it_tag in ("subtitle", "summary", "duration", "explicit"):
        it[it_tag] = element_text(item.find(f"{{{NS['itunes']}}}{it_tag}"))
    it_img = item.find(f"{{{NS['itunes']}}}image")
    if it_img is not None:
        it_image_href = it_img.get("href")
        if it_image_href:
            it["image"] = it_image_href
    # also try media:thumbnail or media:image
    media_img = item.find(f"{{{NS['media']}}}thumbnail") or item.find(f"{{{NS['media']}}}content")
    if media_img is not None and not it.get("image"):
        it_url = media_img.get("url") or media_img.get("href") or ""
        if it_url:
            it["image"] = it_url
    ep["itunes"] = it
    icats = [element_text(c) for c in item.findall("category")]
    for c in item.findall(f"{{{NS['itunes']}}}category"):
        text = c.get("text") or ""
        subs = [sc.get("text") for sc in c.findall(f"{{{NS['itunes']}}}category") if sc.get("text")]
        if subs:
            icats.append({"category": text, "subcategories": subs})
        else:
            icats.append(text)
    ep["categories"] = icats
    try:
        ep["raw_xml"] = ET.tostring(item, encoding="utf-8").decode("utf-8")
    except Exception:
        ep["raw_xml"] = ""
    return ep


def merge_with_local_audio(episodes: list) -> list:
    local_files = {p.name: p for p in AUDIO_DIR.iterdir() if p.is_file()} if AUDIO_DIR.exists() else {}
    by_filename = {ep.get("filename"): ep for ep in episodes}
    for name, path in sorted(local_files.items()):
        if name not in by_filename:
            ep = {
                "filename": name,
                "title": nice_title_from_filename(name),
                "description": "",
                "pubDate": format_rfc2822(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)),
                "guid": name,
                "enclosure_url": f"./audio/{name}",
                "enclosure_length": str(path.stat().st_size),
                "enclosure_type": "audio/mpeg",
                "itunes": {},
                "categories": []
            }
            episodes.append(ep)
            by_filename[name] = ep
        else:
            ep = by_filename[name]
            ep["enclosure_url"] = f"./audio/{name}"
            try:
                ep["enclosure_length"] = str(path.stat().st_size)
            except Exception:
                pass
    return episodes


def nice_title_from_filename(name: str) -> str:
    base = os.path.splitext(name)[0]
    title = base.replace("_", " ").replace("-", " ").strip()
    title = " ".join(title.split())
    return title or name


def sort_episodes_by_pubdate(episodes: list) -> list:
    def key(e):
        try:
            return parsedate_to_datetime(e.get("pubDate"))
        except Exception:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return sorted(episodes, key=key, reverse=True)
    except Exception:
        return episodes


def rewrite_enclosures_to_local(tree: ET.ElementTree) -> None:
    root = tree.getroot()
    for item in root.findall(".//item"):
        encl = item.find("enclosure")
        if encl is None:
            encl = item.find(f"{{{NS['media']}}}content")
        if encl is None:
            continue
        url = (encl.get("url") or "").strip()
        if not url:
            url = element_text(item.find("link"))
        filename = filename_from_url(url)
        localpath = AUDIO_DIR / filename
        if localpath.exists():
            encl.set("url", f"./audio/{filename}")
            try:
                encl.set("length", str(localpath.stat().st_size))
            except Exception:
                pass


def sanitize_tree(tree: ET.ElementTree, poster_url: str | None = None, poster_base: str | None = None) -> None:
    """
    Replace channel image and item images to point to poster_url or poster_base if provided.
    """
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        return
    # channel itunes:image
    it_img = channel.find(f"{{{NS['itunes']}}}image")
    if it_img is None and poster_url:
        it_img = ET.SubElement(channel, f"{{{NS['itunes']}}}image")
    if it_img is not None and poster_url:
        it_img.set("href", poster_url)
    # standard channel image
    img = channel.find("image")
    if img is None and poster_url:
        img = ET.SubElement(channel, "image")
        url_el = ET.SubElement(img, "url")
        url_el.text = poster_url
    elif img is not None and poster_url:
        url_el = img.find("url")
        if url_el is None:
            url_el = ET.SubElement(img, "url")
        url_el.text = poster_url

    # for items, point item-level itunes:image to poster_base + filename if poster_base present and local file exists
    for item in root.findall(".//item"):
        # try item-level itunes:image
        iimg = item.find(f"{{{NS['itunes']}}}image")
        # prefer media:image or item itunes image present in raw XML
        # find filename derived from original image URL if present
        # if posters/<name> exists and poster_base provided, set itunes:image@href accordingly
        # attempt to find image url in item
        orig_img_url = ""
        it_el = item.find(f"{{{NS['itunes']}}}image")
        if it_el is not None:
            orig_img_url = it_el.get("href") or ""
        # try media:content or media:thumbnail
        if not orig_img_url:
            media_img = item.find(f"{{{NS['media']}}}content") or item.find(f"{{{NS['media']}}}thumbnail")
            if media_img is not None:
                orig_img_url = media_img.get("url") or media_img.get("href") or ""
        if orig_img_url:
            imgname = filename_from_url(orig_img_url, fallback=None)
            if imgname:
                localposter = POSTERS_DIR / imgname
                if localposter.exists() and poster_base:
                    # ensure itunes:image exists
                    if iimg is None:
                        iimg = ET.SubElement(item, f"{{{NS['itunes']}}}image")
                    iimg.set("href", poster_base.rstrip("/") + "/" + imgname)


def parse_and_build_structured_data(feed_bytes: bytes) -> tuple[dict, list, ET.ElementTree]:
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    try:
        tree = ET.ElementTree(ET.fromstring(feed_bytes))
    except Exception as e:
        print(f"Failed to parse feed XML: {e}", file=sys.stderr)
        return {}, [], None
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        channel = root.find(".//channel")
    if channel is None:
        print("No <channel> element found in feed.", file=sys.stderr)
        return {}, [], tree
    channel_meta = extract_channel_metadata(channel)
    episodes = []
    for item in channel.findall("item"):
        ep = extract_item_metadata(item)
        episodes.append(ep)
    return channel_meta, episodes, tree


def download_episode_posters(episodes: list) -> None:
    """Download each episode's itunes image (if present) into posters/ and set ep['poster_local']"""
    for ep in episodes:
        it_img = ep.get("itunes", {}).get("image") or ""
        if it_img:
            fname = filename_from_url(it_img, fallback=ep.get("guid") or ep.get("filename") or "")
            dest = POSTERS_DIR / fname
            ok = download_file(it_img, dest)
            if ok:
                ep.setdefault("local_poster", str(dest.as_posix()))
                ep.setdefault("poster_filename", fname)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync podcast metadata from remote RSS into data/ and feed/")
    parser.add_argument("--source", "-s", default=DEFAULT_FEED_SOURCE, help="Remote RSS feed URL to import")
    parser.add_argument("--download", "-d", action="store_true", help="Download enclosures into audio/")
    parser.add_argument("--no-remote", action="store_true", help="Do not fetch remote feed; only scan local audio/")
    parser.add_argument("--poster-url", help="Specific poster URL to use for the channel (overrides poster-base usage)")
    parser.add_argument("--poster-url-base", help="Base URL where posters will be served (e.g. https://github.com/OWNER/REPO/releases/download/latest)")
    args = parser.parse_args(argv)

    ensure_dirs()
    stored = load_episodes()
    existing_channel = stored.get("channel", {})
    existing_episodes = stored.get("episodes", [])

    if args.no_remote:
        print("No-remote mode: skipping remote fetch; will only scan local audio.")
        episodes = existing_episodes.copy()
        episodes = merge_with_local_audio(episodes)
        episodes = sort_episodes_by_pubdate(episodes)
        channel_meta = existing_channel or {}
        save_episodes_structured(channel_meta, episodes)
        if FEED_FILE.exists():
            print(f"Left existing feed at {FEED_FILE} intact.")
        else:
            print("No existing feed.xml found; writing a minimal feed from metadata.")
            from xml.dom.minidom import Document
            doc = Document()
            rss = doc.createElement("rss")
            rss.setAttribute("version", "2.0")
            doc.appendChild(rss)
            ch = doc.createElement("channel")
            rss.appendChild(ch)
            t = doc.createElement("title")
            t.appendChild(doc.createTextNode(channel_meta.get("title", "Podcast Archive")))
            ch.appendChild(t)
            for ep in episodes:
                item = doc.createElement("item")
                it = doc.createElement("title")
                it.appendChild(doc.createTextNode(ep.get("title", "")))
                item.appendChild(it)
                ch.appendChild(item)
            FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
            with FEED_FILE.open("w", encoding="utf-8") as fh:
                fh.write(doc.toprettyxml(encoding="utf-8").decode("utf-8"))
        print("Saved local-only metadata.")
        return

    print(f"Fetching remote feed: {args.source}")
    fb = fetch_remote_feed(args.source)
    if not fb:
        print("Failed to fetch remote feed; aborting remote import.", file=sys.stderr)
        return

    channel_meta, remote_eps, tree = parse_and_build_structured_data(fb)
    if tree is None:
        print("Unable to parse remote feed into an XML tree; aborting.", file=sys.stderr)
        return

    print(f"Parsed channel: {channel_meta.get('title','(unknown)')} with {len(remote_eps)} items.")

    # download episode posters if available
    download_episode_posters(remote_eps)

    # optionally download enclosures
    if args.download:
        print("Downloading enclosures into audio/ ...")
        for ep in remote_eps:
            url = ep.get("enclosure_url") or ""
            if not url:
                continue
            filename = ep.get("filename") or filename_from_url(url)
            dest = AUDIO_DIR / filename
            ok = download_file(url, dest)
            if ok:
                ep["enclosure_url"] = f"./audio/{filename}"
                try:
                    ep["enclosure_length"] = str(dest.stat().st_size)
                except Exception:
                    pass
                print(f"Downloaded {filename}")
            else:
                print(f"Failed to download {filename}; leaving remote enclosure URL.")

    # Merge remote with existing
    merged_map = {}
    for ep in remote_eps:
        key = ep.get("guid") or ep.get("filename")
        merged_map[key] = ep
    for ep in existing_episodes:
        key = ep.get("guid") or ep.get("filename")
        if key not in merged_map:
            merged_map[key] = ep
    episodes = list(merged_map.values())

    episodes = merge_with_local_audio(episodes)

    # rewrite enclosures to local when local audio exists
    rewrite_enclosures_to_local(tree)

    # If the user supplied poster-url or poster-url-base (or env), sanitize the tree accordingly
    poster_url = args.poster_url or os.environ.get("POSTER_URL")
    poster_base = args.poster_url_base or os.environ.get("POSTER_BASE_URL")
    # If a local podcast poster file is present at assets/poster.jpg and poster_base is not provided,
    # we keep channel image as local path (script will not resolve it); the workflow will upload it to 'latest' release.
    if poster_url:
        sanitize_tree(tree, poster_url=poster_url, poster_base=poster_base)
        channel_meta["image"] = poster_url
    elif poster_base:
        sanitize_tree(tree, poster_url=None, poster_base=poster_base)
        channel_meta["image"] = poster_base.rstrip("/") + "/" + PODCAST_POSTER.name if PODCAST_POSTER.exists() else channel_meta.get("image")

    # Update episodes with local poster filenames if downloaded
    for ep in episodes:
        # if we downloaded poster into POSTERS_DIR, set poster_filename and local references
        pfn = ep.get("poster_filename")
        if pfn:
            ep["poster_local"] = str((POSTERS_DIR / pfn).as_posix())
            # point ep's itunes.image to relative posters path (used for local feed generation)
            ep["itunes"] = ep.get("itunes", {})
            ep["itunes"]["image_local"] = f"./posters/{pfn}"
            # set enclosure poster url to poster_base if provided
            if poster_base:
                ep["itunes"]["image"] = poster_base.rstrip("/") + "/" + pfn

    episodes = sort_episodes_by_pubdate(episodes)
    # apply poster overrides in channel_meta if provided via args/env
    if poster_url:
        channel_meta["link"] = channel_meta.get("link")
        channel_meta["image"] = poster_url

    save_episodes_structured(channel_meta, episodes)
    print(f"Saved channel metadata + {len(episodes)} episodes to {DATA_FILE}")

    # final: write the (possibly sanitized / rewritten) feed XML
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tree.write(FEED_FILE, encoding="utf-8", xml_declaration=True)
    print(f"Wrote feed to {FEED_FILE}")

    print("Sync complete.")


if __name__ == "__main__":
    main()