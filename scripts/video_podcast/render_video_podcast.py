#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

import argparse
import os
import shutil
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .clips_cache import CLIP_SEC, ensure_clips
from .ffmpeg_ops import ffmpeg_concat_and_encode, ffmpeg_mux_audio
from .model import Episode, parse_episodes
from .repo_state import choose_todo, load_state, save_state, write_status_csv, write_video_rss
from .util import ffprobe_duration_sec, now_iso, safe_slug, sha256_file, download, load_json, save_json


DEFAULT_VIDEO_TAG = "video-podcast"
DEFAULT_MANIFEST_TAG = "video-podcast-manifests"
DEFAULT_CLIPS_TAG = "video-podcast-clips"


def render_episode(
    ep: Episode,
    repo: str,
    out_videos_dir: Path,
    out_manifests_dir: Path,
    tmp_dir: Path,
    pexels_key: str,
    pixabay_key: str,
    clips_tag: str,
    dry_run: bool,
) -> Tuple[Optional[str], Optional[str]]:
    pub_dt = parsedate_to_datetime(ep.pub_rfc822) if ep.pub_rfc822 else None
    date_prefix = pub_dt.strftime("%Y%m%d") if pub_dt else "00000000"
    base = "%s-%s-%s" % (date_prefix, ep.guid, safe_slug(ep.title))
    video_asset = "%s.mp4" % base
    manifest_asset = "%s.json" % base

    if dry_run:
        return video_asset, manifest_asset

    work = tmp_dir / ep.guid
    work.mkdir(parents=True, exist_ok=True)

    audio_path = work / "audio.mp3"
    download(ep.audio_url, audio_path)

    audio_dur = ffprobe_duration_sec(audio_path)
    need = int((audio_dur + (CLIP_SEC - 0.001)) // CLIP_SEC)

    clips_info = ensure_clips(
        guid=ep.guid,
        title=ep.title,
        desc_html=ep.description,
        repo=repo,
        clips_tag=clips_tag,
        tmp_dir=tmp_dir,
        need=need,
        pexels_key=pexels_key,
        pixabay_key=pixabay_key,
    )

    clips_ordered_dir = clips_info["clips_dir"]
    clips_meta_path = clips_info["clips_meta_path"]
    clip_zip_asset = clips_info["clips_zip_asset"]
    clips_sha = clips_info["clips_sha256"]
    reused = bool(clips_info["reused"])
    generated = bool(clips_info["generated"])

    ordered = sorted(clips_ordered_dir.glob("clip_*.mp4"))
    if len(ordered) < need:
        raise RuntimeError("missing ordered clips")

    silent_video = work / "video_silent.mp4"
    ffmpeg_concat_and_encode(ordered[:need], silent_video)

    final_video = work / "video.mp4"
    ffmpeg_mux_audio(silent_video, audio_path, final_video)

    out_videos_dir.mkdir(parents=True, exist_ok=True)
    out_manifests_dir.mkdir(parents=True, exist_ok=True)

    video_out = out_videos_dir / video_asset
    manifest_out = out_manifests_dir / manifest_asset

    shutil.copyfile(final_video, video_out)

    out_clips_dir = out_videos_dir.parent / "clips"
    out_clips_dir.mkdir(parents=True, exist_ok=True)
    if generated:
        shutil.copyfile(clips_info["clips_zip_path"], out_clips_dir / clip_zip_asset)

    clip_meta_loaded: Any = {}
    if clips_meta_path.exists():
        try:
            clip_meta_loaded = load_json(clips_meta_path)
        except Exception:
            clip_meta_loaded = {}

    manifest = {
        "guid": ep.guid,
        "title": ep.title,
        "description": ep.description,
        "pubDate_rfc822": ep.pub_rfc822,
        "audio_url": ep.audio_url,
        "rendered_at": now_iso(),
        "video_asset_name": video_asset,
        "manifest_asset_name": manifest_asset,
        "repo": repo,
        "audio_sha256": sha256_file(audio_path),
        "video_sha256": sha256_file(video_out),
        "audio_duration_sec": round(audio_dur, 3),
        "clip_sec": CLIP_SEC,
        "clips_count": need,
        "clips_tag": clips_tag,
        "clips_asset_name": clip_zip_asset,
        "clips_sha256": clips_sha,
        "clips_reused": bool(reused),
        "clips_meta": clip_meta_loaded,
        "license_notes": {
            "pexels": "https://www.pexels.com/license/",
            "pixabay": "https://pixabay.com/service/license/",
        },
    }
    save_json(manifest_out, manifest)

    return video_asset, manifest_asset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--episodes-json", default="data/episodes.json")
    ap.add_argument("--state-path", default="data/video-data/state.json")
    ap.add_argument("--status-csv", default="data/video-data/status.csv")
    ap.add_argument("--rss-path", default="rss/video-rss/video_podcast.xml")
    ap.add_argument("--out-dir", default="work/video-podcast")
    ap.add_argument("--max-items", type=int, default=3)
    ap.add_argument("--force-guid", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--video-tag", default=DEFAULT_VIDEO_TAG)
    ap.add_argument("--manifest-tag", default=DEFAULT_MANIFEST_TAG)
    ap.add_argument("--clips-tag", default=DEFAULT_CLIPS_TAG)
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    episodes_json = (repo_root / args.episodes_json).resolve()
    state_path = (repo_root / args.state_path).resolve()
    status_csv = (repo_root / args.status_csv).resolve()
    rss_path = (repo_root / args.rss_path).resolve()
    out_dir = (repo_root / args.out_dir).resolve()

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        print("GITHUB_REPOSITORY is required (owner/repo).", file=sys.stderr)
        return 2

    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    pixabay_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not args.dry_run and (not pexels_key or not pixabay_key):
        print("[warn] API keys are missing. Rendering will work only if clips are reused from Releases.")

    episodes = parse_episodes(episodes_json)
    state = load_state(state_path)

    todo = choose_todo(episodes, state, args.force_guid, args.max_items)
    print("[plan] total=%d todo=%d dry_run=%s" % (len(episodes), len(todo), str(args.dry_run).lower()))

    out_videos = out_dir / "videos"
    out_manifests = out_dir / "manifests"
    tmp_dir = out_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    processed = state.get("processed")
    if not isinstance(processed, dict):
        state["processed"] = {}
        processed = state["processed"]

    for ep in todo:
        print("[episode] guid=%s title=%s" % (ep.guid, ep.title))
        try:
            video_asset, manifest_asset = render_episode(
                ep=ep,
                repo=repo,
                out_videos_dir=out_videos,
                out_manifests_dir=out_manifests,
                tmp_dir=tmp_dir,
                pexels_key=pexels_key,
                pixabay_key=pixabay_key,
                clips_tag=args.clips_tag,
                dry_run=args.dry_run,
            )
            if not args.dry_run and video_asset and manifest_asset:
                processed[ep.guid] = {
                    "processed_at": now_iso(),
                    "video_tag": args.video_tag,
                    "manifest_tag": args.manifest_tag,
                    "video_asset_name": video_asset,
                    "manifest_asset_name": manifest_asset,
                    "clips_tag": args.clips_tag,
                    "clips_asset_name": "clips_%s.zip" % ep.guid,
                }
                save_state(state_path, state)
                print("[ok] guid=%s video=%s manifest=%s" % (ep.guid, video_asset, manifest_asset))
        except Exception as e:
            print("[fail] guid=%s err=%s" % (ep.guid, str(e)), file=sys.stderr)

    write_status_csv(status_csv, episodes, state)
    write_video_rss(rss_path, repo, args.video_tag, episodes, state)

    print("[done] out_dir=%s" % str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
