# ASCII-only. No ellipses. Keep <= 500 lines.

import csv
from pathlib import Path
from typing import Any, Dict, List

from .model import Episode
from .util import now_iso, load_json, save_json


STATE_VERSION = 1


def load_state(state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        return {"version": STATE_VERSION, "processed": {}, "updated_at": now_iso()}
    j = load_json(state_path)
    if not isinstance(j, dict):
        raise ValueError("state.json must be a dict")
    if "processed" not in j or not isinstance(j.get("processed"), dict):
        j["processed"] = {}
    if "version" not in j:
        j["version"] = STATE_VERSION
    return j


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    save_json(state_path, state)


def choose_todo(episodes: List[Episode], state: Dict[str, Any], force_guid: str, max_items: int) -> List[Episode]:
    processed = state.get("processed") or {}
    out: List[Episode] = []
    for ep in episodes:
        if force_guid and ep.guid != force_guid:
            continue
        if not force_guid and ep.guid in processed:
            continue
        out.append(ep)
        if max_items > 0 and len(out) >= max_items:
            break
    return out


def write_status_csv(status_path: Path, episodes: List[Episode], state: Dict[str, Any]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    processed = state.get("processed") or {}
    fields = [
        "guid", "title", "pubDate_rfc822", "status",
        "video_asset_name", "manifest_asset_name", "processed_at",
        "youtube_video_id", "youtube_uploaded_at", "youtube_privacy_status",
    ]
    with open(status_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for ep in episodes:
            row = {
                "guid": ep.guid,
                "title": ep.title,
                "pubDate_rfc822": ep.pub_rfc822,
                "status": "PENDING",
                "video_asset_name": "",
                "manifest_asset_name": "",
                "processed_at": "",
                "youtube_video_id": "",
                "youtube_uploaded_at": "",
                "youtube_privacy_status": "",
            }
            p = processed.get(ep.guid)
            if isinstance(p, dict):
                row["status"] = "RENDERED"
                row["video_asset_name"] = str(p.get("video_asset_name") or "")
                row["manifest_asset_name"] = str(p.get("manifest_asset_name") or "")
                row["processed_at"] = str(p.get("processed_at") or "")
                yt = p.get("youtube")
                if isinstance(yt, dict):
                    row["youtube_video_id"] = str(yt.get("video_id") or "")
                    row["youtube_uploaded_at"] = str(yt.get("uploaded_at") or "")
                    row["youtube_privacy_status"] = str(yt.get("privacy_status") or "")
            w.writerow(row)


def write_video_rss(rss_path: Path, repo: str, video_tag: str, episodes: List[Episode], state: Dict[str, Any]) -> None:
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    processed = state.get("processed") or {}

    def esc(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("<channel>")
    parts.append("<title>Video Podcast</title>")
    parts.append("<description>Rendered video episodes (B-roll + RSS audio)</description>")
    parts.append("<link>https://github.com/%s</link>" % esc(repo))

    for ep in episodes:
        p = processed.get(ep.guid)
        if not isinstance(p, dict):
            continue
        asset_name = str(p.get("video_asset_name") or "")
        if not asset_name:
            continue
        enc_url = "https://github.com/%s/releases/download/%s/%s" % (repo, video_tag, asset_name)
        yt_url = ""
        yt = p.get("youtube")
        if isinstance(yt, dict):
            yt_url = str(yt.get("video_url") or "")

        parts.append("<item>")
        parts.append("<guid>%s</guid>" % esc(ep.guid))
        parts.append("<title>%s</title>" % esc(ep.title))
        parts.append("<description>%s</description>" % esc(ep.description))
        parts.append("<pubDate>%s</pubDate>" % esc(ep.pub_rfc822))
        if yt_url:
            parts.append("<link>%s</link>" % esc(yt_url))
        parts.append('<enclosure url="%s" type="video/mp4" />' % esc(enc_url))
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    rss_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
