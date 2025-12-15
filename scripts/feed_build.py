import os, json
from pathlib import Path
from datetime import datetime, timezone
from xml.sax.saxutils import escape

def load_state(topic_id: str) -> dict:
    p = Path("data") / topic_id / "state.json"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        return {"episodes": [], "backlog": [], "seen_hashes": {}, "last_episode_date": None}
    return json.loads(p.read_text(encoding="utf-8"))

def save_state(topic_id: str, state: dict):
    p = Path("data") / topic_id / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def update_topic_feed(topic_id: str, topic: dict, state: dict, episode: dict):
    state.setdefault("episodes", [])
    state["episodes"].insert(0, episode)
    state["episodes"] = state["episodes"][: int(topic.get("max_feed_items", 50))]

    owner_name = os.environ.get("PODCAST_OWNER_NAME", "Agenda").strip()
    owner_email = os.environ.get("PODCAST_OWNER_EMAIL", "").strip()

    if not owner_email:
        raise RuntimeError("PODCAST_OWNER_EMAIL missing (Apple validation requirement).")

    feed_path = Path("feeds") / topic_id / "rss.xml"
    feed_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    title = topic["title"]
    link = topic.get("link", f"https://{os.environ.get('REPO','')}")
    desc = topic.get("description", title)

    items_xml = []
    for ep in state["episodes"]:
        items_xml.append(f"""
    <item>
      <title>{escape(ep["title"])}</title>
      <guid isPermaLink="false">{escape(ep["guid"])}</guid>
      <pubDate>{escape(ep["pubDate"])}</pubDate>
      <description><![CDATA[{ep.get("description_html","")}]]></description>
      <enclosure url="{escape(ep["enclosure_url"])}" type="audio/mpeg"/>
      <itunes:duration>{int(ep.get("itunes_duration", 1800))}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>
""".rstrip())

    rss = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <atom:link href="{escape(topic.get('public_feed_url',''))}" rel="self" type="application/rss+xml"/>
    <title>{escape(title)}</title>
    <link>{escape(link)}</link>
    <language>en-us</language>
    <description><![CDATA[{desc}]]></description>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{escape(owner_name)}</itunes:author>
    <itunes:owner>
      <itunes:name>{escape(owner_name)}</itunes:name>
      <itunes:email>{escape(owner_email)}</itunes:email>
    </itunes:owner>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
{os.linesep.join(items_xml)}
  </channel>
</rss>
"""
    feed_path.write_text(rss, encoding="utf-8")
