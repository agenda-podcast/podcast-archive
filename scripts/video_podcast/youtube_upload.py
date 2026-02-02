#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .model import Episode, parse_episodes
from .repo_state import load_state, save_state, write_status_csv, write_video_rss
from .sources import text_queries
from .util import now_iso, load_json, save_json
from .youtube_auth import build_credentials

THUMB_LEFT_PNG = "data/thumbnail_left.png"
THUMB_TEMPLATE_PNG = "data/thumbnail_template.png"


def _gh_delete_release_asset(tag: str, asset_name: str) -> None:
    """Delete an asset from a GitHub release tag using `gh`.

    This is used to clean up large video assets once they are successfully uploaded
    to YouTube.

    The workflow must export GH_TOKEN (or GITHUB_TOKEN) so `gh` is authorized.
    """
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        raise RuntimeError("GH_TOKEN (or GITHUB_TOKEN) is required to delete release assets")

    # gh reads GH_TOKEN from the environment; ensure it is set for this subprocess.
    env = dict(os.environ)
    env["GH_TOKEN"] = gh_token

    cmd = ["gh", "release", "delete-asset", tag, asset_name, "--yes"]
    print(f"[cleanup][release] deleting asset tag={tag} asset={asset_name}")
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to delete release asset: {proc.stdout.strip()}")


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
            "defaultLanguage": "en-US",
            "defaultAudioLanguage": "en-US",
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

    # Upload a custom thumbnail if the left image is provided.
    try:
        from googleapiclient.http import MediaFileUpload as _MediaFileUpload

        from .thumbnails import ensure_thumbnail_template, render_episode_thumbnail

        repo_root = Path(__file__).resolve().parents[2]
        left_img = (repo_root / THUMB_LEFT_PNG).resolve()
        template_png = (repo_root / THUMB_TEMPLATE_PNG).resolve()

        # Generate the reusable template once.
        ensure_thumbnail_template(
            left_img_path=left_img,
            template_png=template_png,
        )

        thumb_out = video_path.with_suffix(".thumbnail.png")
        render_episode_thumbnail(
            template_png=template_png,
            out_png=thumb_out,
            episode_title=title,
            episode_description=description,
        )

        if thumb_out.is_file():
            tb_media = _MediaFileUpload(str(thumb_out), mimetype="image/png", resumable=False)
            service.thumbnails().set(videoId=vid, media_body=tb_media).execute()
            print("[youtube] thumbnail_uploaded=1")
    except Exception as e:
        print("[youtube] thumbnail_skipped=1 err=%s" % str(e).replace("\n", " "))
    return vid


def _build_service() -> Any:
    # Imported lazily so the repo can run without YouTube deps unless enabled.
    from googleapiclient.discovery import build

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


def _find_video_asset_for_guid(out_dir: Path, guid: str) -> Optional[str]:
    vids = []
    d = out_dir / "videos"
    if d.exists():
        vids.extend(sorted(d.glob("*%s*.mp4" % guid)))
    if not vids:
        return None
    return vids[0].name


def _find_manifest_asset_for_guid(out_dir: Path, guid: str) -> Optional[str]:
    mans = []
    d = out_dir / "manifests"
    if d.exists():
        mans.extend(sorted(d.glob("*%s*.json" % guid)))
    if not mans:
        return None
    return mans[0].name


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
    cleanup_release_tag: str,
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
    episodes_all = list(episodes)
    force_guid = str(force_guid or "").strip()
    force_set = set()
    upload_eps = episodes
    if force_guid:
        force_set.add(force_guid)
        upload_eps = [e for e in episodes if e.guid in force_set]
        if not upload_eps:
            print("[youtube] no episodes match force_guid=%s" % force_guid)
            return 0
    try:
        max_n = int(max_items)
    except Exception:
        max_n = 0
    if max_n < 0:
        max_n = 0

    for ep in upload_eps:
        entry = processed.get(ep.guid)
        if not isinstance(entry, dict):
            entry = {}

        if (not force_set) and (not _needs_upload(entry)):
            continue

        asset = str(entry.get("video_asset_name") or "").strip()
        if not asset and force_set:
            asset = str(_find_video_asset_for_guid(out_dir, ep.guid) or "").strip()
            if asset:
                entry["video_asset_name"] = asset

        if not asset:
            if force_set:
                print("[youtube] skip guid=%s reason=no_video_asset_name" % ep.guid)
            continue

        video_path = out_dir / "videos" / asset
        if not video_path.exists():
            if force_set:
                print("[youtube] skip guid=%s reason=video_missing file=%s" % (ep.guid, asset))
            continue

        manifest_asset = str(entry.get("manifest_asset_name") or "").strip()
        if not manifest_asset and force_set:
            manifest_asset = str(_find_manifest_asset_for_guid(out_dir, ep.guid) or "").strip()
            if manifest_asset:
                entry["manifest_asset_name"] = manifest_asset
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

        processed[ep.guid] = entry
        save_state(state_path, state)

        uploads.append((ep.guid, vid))
        print("[youtube] upload_ok guid=%s video_id=%s" % (ep.guid, vid))

        if cleanup_release_tag:
            try:
                _gh_delete_release_asset(cleanup_release_tag, asset)
                print("[youtube] release_asset_deleted tag=%s asset=%s" % (cleanup_release_tag, asset))
            except Exception as e:
                print("[youtube] release_asset_delete_fail tag=%s asset=%s err=%s" % (cleanup_release_tag, asset, str(e)), file=sys.stderr)
                return 2
        if max_n and (len(uploads) >= max_n):
            print("[youtube] max-items reached=%d" % max_n)
            break


    # Always rewrite CSV and RSS so they include YouTube links when available.
    write_status_csv(status_csv, episodes_all, state)

    import os
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not gh_repo:
        print("GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2
    video_tag = "video-podcast"
    any_entry = next(iter(processed.values()), None)
    if isinstance(any_entry, dict):
        video_tag = str(any_entry.get("video_tag") or video_tag)
    write_video_rss(rss_path, gh_repo, video_tag, episodes_all, state)

    print("[youtube] uploaded_count=%d" % len(uploads))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--episodes-json", default="data/episodes.json")
    ap.add_argument("--state-path", default="data/video-data/state.json")
    ap.add_argument("--status-csv", default="data/video-data/status.csv")
    ap.add_argument("--rss-path", default="feed/video_podcast.xml")
    ap.add_argument("--out-dir", default="work/video-podcast")
    ap.add_argument("--privacy-status", default="private")
    ap.add_argument("--category-id", default="25")
    ap.add_argument("--max-items", default="0", help="Upload at most N items (0 = no limit).")
    ap.add_argument("--force-guid", default="", help="If set, upload only this guid and ignore skip logic.")
    ap.add_argument(
        "--cleanup-release-tag",
        default=os.environ.get("CLEANUP_RELEASE_TAG", ""),
        help="If set, delete uploaded video asset from this GitHub release tag (uses gh + GH_TOKEN).",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    episodes = parse_episodes((repo_root / args.episodes_json).resolve())
    state_path = (repo_root / args.state_path).resolve()
    status_csv = (repo_root / args.status_csv).resolve()
    rss_path = (repo_root / args.rss_path).resolve()
    out_dir = (repo_root / args.out_dir).resolve()

    ps = str(args.privacy_status).strip().lower()
    if ps not in ["private", "unlisted", "public"]:
        print("privacy-status must be private|unlisted|public", file=sys.stderr)
        return 2

    return upload_all(
        repo_root=repo_root,
        episodes=episodes,
        state_path=state_path,
        status_csv=status_csv,
        rss_path=rss_path,
        out_dir=out_dir,
        privacy_status=ps,
        category_id=str(args.category_id).strip(),
        max_items=int(str(args.max_items).strip() or "0"),
        force_guid=str(args.force_guid).strip(),
        cleanup_release_tag=str(args.cleanup_release_tag).strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
