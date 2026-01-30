#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .model import Episode, parse_episodes
from .repo_state import load_state, save_state, write_status_csv, write_video_rss
from .sources import text_queries
from .util import now_iso, load_json, save_json
from .youtube_auth import build_credentials, refresh_credentials_or_fail


def _youtube_url(video_id: str) -> str:
    return "https://www.youtube.com/watch?v=%s" % video_id


def _read_manifest(man_path: Path) -> Dict[str, Any]:
    j = load_json(man_path)
    if not isinstance(j, dict):
        raise ValueError("manifest must be a dict")
    return j


def _write_manifest(man_path: Path, manifest: Dict[str, Any]) -> None:
    save_json(man_path, manifest)


def _upload_one(
    service: Any,
    video_path: Path,
    title: str,
    description: str,
    tags: List[str],
    category_id: str,
    privacy_status: str,
) -> str:
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    req = service.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print("[youtube] upload_progress=%d" % pct)

    vid = str(response.get("id") or "").strip()
    if not vid:
        raise RuntimeError("YouTube API returned no video id")
    return vid


def _build_service() -> Any:
    # Imported lazily so the repo can run without YouTube deps unless enabled.
    from googleapiclient.discovery import build

    err = refresh_credentials_or_fail()
    if err:
        raise RuntimeError("YouTube OAuth refresh failed: %s" % err)

    creds = build_credentials()
    return build("youtube", "v3", credentials=creds)


def _needs_upload(entry: Dict[str, Any]) -> bool:
    yt = entry.get("youtube")
    if not isinstance(yt, dict):
        return True
    vid = str(yt.get("video_id") or "").strip()
    return not bool(vid)


def _clean_tags(tags: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for t in tags:
        tt = (t or "").strip()
        if not tt:
            continue
        if tt in seen:
            continue
        seen.add(tt)
        out.append(tt)
        if len(out) >= 20:
            break
    return out


def upload_all(
    repo_root: Path,
    episodes: List[Episode],
    state_path: Path,
    status_csv: Path,
    rss_path: Path,
    out_dir: Path,
    privacy_status: str,
    category_id: str,
    max_items: int,
    force_guid: str,
) -> int:
    repo = (repo_root / ".git").exists()
    if not repo:
        raise RuntimeError("repo-root must be a git checkout")

    state = load_state(state_path)
    processed = state.get("processed")
    if not isinstance(processed, dict):
        print("state.processed must be a dict", file=sys.stderr)
        return 2

    service = _build_service()

    uploads: List[Tuple[str, str]] = []
    force_guid = (force_guid or "").strip()
    uploaded_n = 0

    for ep in episodes:
        if force_guid and ep.guid != force_guid:
            continue
        entry = processed.get(ep.guid)
        if not isinstance(entry, dict):
            continue
        if (not force_guid) and (not _needs_upload(entry)):
            continue
        asset = str(entry.get("video_asset_name") or "").strip()
        if not asset:
            continue
        video_path = out_dir / "videos" / asset
        if not video_path.exists():
            continue
        manifest_asset = str(entry.get("manifest_asset_name") or "").strip()
        manifest_path = out_dir / "manifests" / manifest_asset if manifest_asset else None

        title = ep.title
        desc = ep.description
        tags = _clean_tags(text_queries(title, desc, max_q=15))

        if manifest_path and manifest_path.exists():
            man = _read_manifest(manifest_path)
            title = str(man.get("title") or title)
            desc = str(man.get("description") or desc)
            tags = _clean_tags(text_queries(title, desc, max_q=15))

        print("[youtube] upload_start guid=%s file=%s" % (ep.guid, asset))
        vid = _upload_one(
            service=service,
            video_path=video_path,
            title=title,
            description=desc,
            tags=tags,
            category_id=category_id,
            privacy_status=privacy_status,
        )

        entry["youtube"] = {
            "video_id": vid,
            "video_url": _youtube_url(vid),
            "uploaded_at": now_iso(),
            "privacy_status": privacy_status,
            "category_id": category_id,
        }
        processed[ep.guid] = entry
        save_state(state_path, state)

        if manifest_path and manifest_path.exists():
            man = _read_manifest(manifest_path)
            man["youtube"] = dict(entry["youtube"])
            _write_manifest(manifest_path, man)

        uploads.append((ep.guid, vid))
        uploaded_n += 1
        print("[youtube] upload_ok guid=%s video_id=%s" % (ep.guid, vid))

        if max_items > 0 and uploaded_n >= max_items:
            break

    # Always rewrite CSV and RSS so they include YouTube links when available.
    write_status_csv(status_csv, episodes, state)

    import os
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not gh_repo:
        print("GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2
    video_tag = "video-podcast"
    any_entry = next(iter(processed.values()), None)
    if isinstance(any_entry, dict):
        video_tag = str(any_entry.get("video_tag") or video_tag)
    write_video_rss(rss_path, gh_repo, video_tag, episodes, state)

    print("[youtube] uploaded_count=%d" % len(uploads))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--episodes-json", default="data/episodes.json")
    ap.add_argument("--state-path", default="data/video-data/state.json")
    ap.add_argument("--status-csv", default="data/video-data/status.csv")
    ap.add_argument("--rss-path", default="rss/video-rss/video_podcast.xml")
    ap.add_argument("--out-dir", default="work/video-podcast")
    ap.add_argument("--privacy-status", default="private")
    ap.add_argument("--category-id", default="25")
    ap.add_argument("--max-items", default="0")
    ap.add_argument("--force-guid", default="")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    episodes = parse_episodes((repo_root / args.episodes_json).resolve())
    state_path = (repo_root / args.state_path).resolve()
    status_csv = (repo_root / args.status_csv).resolve()
    rss_path = (repo_root / args.rss_path).resolve()
    out_dir = (repo_root / args.out_dir).resolve()

    try:
        max_items = int(str(args.max_items).strip())
    except Exception:
        print("max-items must be int", file=sys.stderr)
        return 2

    ps = str(args.privacy_status).strip().lower()
    if ps not in ["private", "unlisted", "public"]:
        print("privacy-status must be private|unlisted|public", file=sys.stderr)
        return 2

    try:
        return upload_all(
            repo_root=repo_root,
            episodes=episodes,
            state_path=state_path,
            status_csv=status_csv,
            rss_path=rss_path,
            out_dir=out_dir,
            privacy_status=ps,
            category_id=str(args.category_id).strip(),
            max_items=max_items,
            force_guid=str(args.force_guid or "").strip(),
        )
    except RuntimeError as e:
        msg = str(e)
        if "deleted_client" in msg:
            print("[youtube][FAIL] OAuth client was deleted.", file=sys.stderr)
            print("[youtube][FIX] Create a NEW OAuth Client ID in Google Cloud (type: Desktop app).", file=sys.stderr)
            print("[youtube][FIX] Run youtube_oauth_local to generate a NEW refresh token.", file=sys.stderr)
            print("[youtube][FIX] Update GitHub repo secrets: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN.", file=sys.stderr)
            return 2
        if "invalid_client" in msg:
            print("[youtube][FAIL] Invalid client credentials.", file=sys.stderr)
            print("[youtube][FIX] Verify YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET match the OAuth client.", file=sys.stderr)
            return 2
        if "invalid_grant" in msg:
            print("[youtube][FAIL] Refresh token is invalid or revoked.", file=sys.stderr)
            print("[youtube][FIX] Re-run youtube_oauth_local to generate a new refresh token.", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
