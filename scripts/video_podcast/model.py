# ASCII-only. No ellipses. Keep <= 500 lines.

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List

from .util import load_json, strip_html


@dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    description: str
    pub_rfc822: str
    audio_url: str


def parse_episodes(episodes_json: Path) -> List[Episode]:
    j = load_json(episodes_json)
    raw = j.get("episodes")
    if not isinstance(raw, dict):
        raise ValueError("episodes.json must contain a top-level 'episodes' object")
    out: List[Episode] = []
    for _, v in raw.items():
        if not isinstance(v, dict):
            continue
        guid = str(v.get("guid") or "").strip()
        title = str(v.get("title") or "").strip()
        desc = strip_html(str(v.get("description_html") or ""))
        pub = str(v.get("pubDate_rfc822") or "").strip()
        audio = str(v.get("audio_url") or "").strip()
        if not guid or not audio:
            continue
        if not title:
            title = guid
        if not desc:
            desc = title
        out.append(Episode(guid=guid, title=title, description=desc, pub_rfc822=pub, audio_url=audio))
    out.sort(key=lambda e: parsedate_to_datetime(e.pub_rfc822).timestamp() if e.pub_rfc822 else 0.0)
    return out
