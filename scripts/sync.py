#!/usr/bin/env python3
# Copyright (c) Agenda Podcast
# All rights reserved. 
# This code is owned by Agenda Podcast.  Copying, redistribution or usage without
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
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from email. utils import format_datetime, parsedate_to_datetime
from urllib. parse import urlparse, unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

# Default Buzzsprout feed — REPLACE WITH YOUR ACTUAL FEED URL
DEFAULT_FEED_SOURCE = "https://feeds.buzzsprout.com/2562524.rss"

ROOT = Path(".")
AUDIO_DIR = ROOT / "audio"
DATA_FILE = ROOT / "data" / "episodes.json"
FEED_FILE = ROOT / "feed" / "rss.xml"
ALLOWED_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}

# Common namespaces we expect in podcast feeds
NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom"
}

# Register namespaces so ElementTree writes prefixes consistently
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def load_episodes() -> dict: 
    if not DATA_FILE.exists():
        return {"channel": {}, "episodes": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        print(f"Error loading {DATA_FILE}: {e}", file=sys.stderr)
        return {"channel": {}, "episodes":  []}


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


def filename_from_url(url: str, fallback:  str = None) -> str:
    try:
        path = urlparse(url).path
        name = unquote(Path(path).name)
        if not name and fallback:
            name = fallback
        name = name.split("? ")[0]
        if not os.path.splitext(name)[1]:
            name = name + ".mp3"
        return name
    except Exception:
        return fallback or "audio-file"


def download_file(url: str, dest: Path, timeout: int = 30) -> bool:
    dest. parent.mkdir(parents=True, exist_ok=True)
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
    except (URLError, HTTPError) as e:
        print(f"Failed to download {url}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        return False


def fetch_remote_feed(url: str, timeout: int = 20) -> bytes | None:
    req = Request(url, headers={"User-Agent": "podcast-archive-sync/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp. read()
    except Exception as e:
        print(f"Error fetching feed {url}: {e}", file=sys.stderr)
        return None


def element_text(elem: ET.Element | None) -> str:
    return (elem. text or "").strip() if elem is not None else ""


def extract_channel_metadata(channel: ET.Element) -> dict:
    meta = {}
    for tag in ("title", "link", "description", "language", "lastBuildDate", "copyright"):
        meta[tag] = element_text(channel.find(tag))

    img = channel.find("image")
    if img is not None: 
        meta["image"] = element_text(img. find("url"))
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
    ep["guid"] = element_text(item. find("guid")) or ""
    encl = item.find("enclosure")
    if encl is not None:
        ep["enclosure_url"] = (encl.get("url") or "").strip()
        ep["enclosure_length"] = (encl.get("length") or "0").strip()
        ep["enclosure_type"] = (encl.get("type") or "").strip()
    else:
        media = item.find(f"{{{NS['media']}}}content")
        if media is not None:
            ep["enclosure_url"] = (media.get("url") or "").strip()
            ep["enclosure_length"] = (media. get("fileSize") or "0").strip()
            ep["enclosure_type"] = (media.get("type") or "").strip()
        else:
            ep["enclosure_url"] = element_text(item.find("link"))
            ep["enclosure_length"] = "0"
            ep["enclosure_type"] = ""

    ep["filename"] = filename_from_url(ep. get("enclosure_url", ""), fallback=ep.get("guid") or ep.get("title") or "")
    it = {}
    for it_tag in ("subtitle", "summary", "duration", "explicit"):
        it[it_tag] = element_text(item.find(f"{{{NS['itunes']}}}{it_tag}"))
    it_img = item.find(f"{{{NS['itunes']}}}image")
    if it_img is not None:
        it_image_href = it_img.get("href")
        if it_image_href:
            it["image"] = it_image_href
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
    AUDIO_DIR. mkdir(parents=True, exist_ok=True)
    local_files = {p.name: p for p in AUDIO_DIR.iterdir() if p.is_file()}
    by_filename = {ep. get("filename"): ep for ep in episodes}
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
    title = base. replace("_", " ").replace("-", " ").strip()
    title = " ".join(title.split())
    return title or name


def sort_episodes_by_pubdate(episodes: list) -> list:
    def key(e):
        try:
            return parsedate_to_datetime(e. get("pubDate"))
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
            encl. set("url", f"./audio/{filename}")
            try:
                encl.set("length", str(localpath.stat().st_size))
            except Exception:
                pass


def sanitize_tree(tree: ET.ElementTree, public_url: str | None = None, poster: str | None = None) -> None:
    """
    Remove/replace references to buzzsprout (and similar) within the ElementTree.
    - Replace any attribute or text containing buzzsprout with public_url (if provided) or remove it. 
    - Replace channel link and image with public_url/poster when supplied.
    - Remove generator, and sanitize owner email/name.
    """
    buzz_re = re.compile(r"([a-z]+://)?[^\"\s]*buzzsprout[^\"\s]*", re.IGNORECASE)

    root = tree.getroot()
    # Channel-specific replacements
    channel = root.find("channel")
    if channel is not None:
        # channel link
        if public_url:
            link = channel.find("link")
            if link is None:
                link = ET.SubElement(channel, "link")
            link.text = public_url

        # channel image standard
        img = channel.find("image")
        if img is not None and poster:
            url_el = img.find("url")
            if url_el is None:
                url_el = ET.SubElement(img, "url")
            url_el.text = poster

        # itunes: image
        it_img = channel.find(f"{{{NS['itunes']}}}image")
        if it_img is not None:
            if poster:
                it_img.set("href", poster)
            else:
                # if itunes image references buzzsprout, remove href
                href = it_img.get("href") or ""
                if "buzzsprout" in href. lower():
                    it_img.set("href", "")

        # remove generator elements
        for gen in channel.findall("generator"):
            channel.remove(gen)

        # sanitize owner email/name
        owner = channel.find(f"{{{NS['itunes']}}}owner")
        if owner is not None:
            email = owner.find(f"{{{NS['itunes']}}}email")
            name = owner.find(f"{{{NS['itunes']}}}name")
            if email is not None: 
                if public_url:
                    email. text = ""
                elif email.text and "buzzsprout" in email.text.lower():
                    email. text = ""
            if name is not None and name.text and "buzzsprout" in name. text.lower():
                name. text = ""

    # Walk tree and scrub attributes and text nodes
    for elem in root.iter():
        # Sanitize attributes
        for attr, val in list(elem.attrib.items()):
            if val and "buzzsprout" in val. lower():
                replacement = public_url or ""
                new_val = buzz_re.sub(replacement, val)
                # Avoid emptying required attributes like 'url' if we have a poster/public_url fallback
                elem.set(attr, new_val)

        # Sanitize text
        if elem.text:
            if "buzzsprout" in elem. text.lower():
                replacement = public_url or ""
                elem.text = buzz_re.sub(replacement, elem.text)

        # Sanitize tail
        if elem.tail:
            if "buzzsprout" in elem. tail.lower():
                replacement = public_url or ""
                elem.tail = buzz_re.sub(replacement, elem.tail)


def write_feed_xml_from_tree(tree: ET.ElementTree) -> None:
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tree.write(FEED_FILE, encoding="utf-8", xml_declaration=True)


def parse_and_build_structured_data(feed_bytes: bytes) -> tuple[dict, list, ET.ElementTree]:
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    try:
        tree = ET.ElementTree(ET.fromstring(feed_bytes))
    except ET.ParseError as e:
        print(f"Failed to parse feed XML: {e}", file=sys. stderr)
        return {}, [], None

    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        channel = root.find(". //channel")
    if channel is None:
        print("No <channel> element found in feed.", file=sys.stderr)
        return {}, [], tree

    channel_meta = extract_channel_metadata(channel)
    episodes = []
    for item in channel.findall("item"):
        ep = extract_item_metadata(item)
        episodes.append(ep)

    return channel_meta, episodes, tree


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync podcast metadata from remote RSS into data/ and feed/")
    parser.add_argument("--source", "-s", default=DEFAULT_FEED_SOURCE, help="Remote RSS feed URL to import")
    parser.add_argument("--download", "-d", action="store_true", help="Download enclosures into audio/")
    parser.add_argument("--no-remote", action="store_true", help="Do not fetch remote feed; only scan local audio/")
    parser.add_argument("--public-url", help="Public URL to set as channel <link> and to replace Buzzsprout links with")
    parser.add_argument("--poster", help="Public poster/image URL to set as channel image/itunes:image")
    args = parser.parse_args(argv)

    stored = load_episodes()
    existing_channel = stored.get("channel", {})
    existing_episodes = stored.get("episodes", [])

    if args.no_remote:
        print("No-remote mode: skipping remote fetch; will only scan local audio.")
        episodes = existing_episodes. copy()
        episodes = merge_with_local_audio(episodes)
        episodes = sort_episodes_by_pubdate(episodes)
        channel_meta = existing_channel or {}
        save_episodes_structured(channel_meta, episodes)
        if FEED_FILE.exists():
            print(f"Left existing feed at {FEED_FILE} intact.")
        else:
            print("No existing feed. xml found; writing a minimal feed from metadata.")
            from xml.dom. minidom import Document
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

    print(f"Fetching remote feed:  {args.source}")
    fb = fetch_remote_feed(args.source)
    if not fb:
        print("Failed to fetch remote feed; aborting remote import.", file=sys.stderr)
        return

    channel_meta, remote_eps, tree = parse_and_build_structured_data(fb)
    if tree is None:
        print("Unable to parse remote feed into an XML tree; aborting.", file=sys.stderr)
        return

    print(f"Parsed channel:  {channel_meta. get('title','(unknown)')} with {len(remote_eps)} items.")

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
                    ep["enclosure_length"] = str(dest. stat().st_size)
                except Exception:
                    pass
                print(f"Downloaded {filename}")
            else:
                print(f"Failed to download {filename}; leaving remote enclosure URL.")

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

    # Rewrite enclosures to local paths if local file exists
    rewrite_enclosures_to_local(tree)

    # Sanitize tree to remove/replace buzzsprout references
    sanitize_tree(tree, public_url=args.public_url, poster=args.poster)

    episodes = sort_episodes_by_pubdate(episodes)
    # Update channel_meta with any overrides the user provided (public-url/poster)
    if args.public_url:
        channel_meta["link"] = args.public_url
    if args.poster:
        channel_meta["image"] = args.poster

    save_episodes_structured(channel_meta, episodes)
    print(f"Saved channel metadata + {len(episodes)} episodes to {DATA_FILE}")

    write_feed_xml_from_tree(tree)
    print(f"Wrote sanitized feed to {FEED_FILE}")

    print("Sync complete.")


if __name__ == "__main__":
    main()
