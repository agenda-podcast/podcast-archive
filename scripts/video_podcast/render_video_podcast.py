#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

import argparse
import os
import shutil
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ffmpeg_ops import (
    ffmpeg_concat_with_intro_outro_and_frame,
    ffmpeg_make_clip,
    ffmpeg_normalize_audio,
    ffmpeg_normalize_video,
)
from .model import Episode, parse_episodes
from .repo_state import choose_todo, load_state, save_state, write_status_csv, write_video_rss
from .releases import download_clips_for_guid
from .sources import apply_sensitive_query_policy, build_tiered_queries, search_assets
from .util import ffprobe_duration_sec, now_iso, rand_for_guid, safe_slug, sha256_file, download, save_json


CLIP_SEC_T2 = 30.0
CLIP_SEC_T3 = 15.0
MIN_ASSET_SEC = 16.0
T1_MIN_SEC = 40.0
T1_MAX_SEC = 600.0

DEFAULT_VIDEO_TAG = "video-podcast"
DEFAULT_MANIFEST_TAG = "video-podcast-manifests"
DEFAULT_CLIPS_TAG = "video-podcast-clips"

# Expected asset locations in the repository.
# Keep these files out of git history if they are large. Git LFS is a common option.
DEFAULT_INTRO_OUTRO_MP4 = "data/raw_2_1440p_crf15_aac256.mp4"
DEFAULT_FRAME_PNG = "data/video_frame.png"


def _list_ordered_clips(dir_path: Path) -> List[Path]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    items = sorted([p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"])
    return items


def render_episode(
    ep: Episode,
    repo_root: Path,
    repo: str,
    out_videos_dir: Path,
    out_manifests_dir: Path,
    out_clips_root: Path,
    out_clips_release_dir: Path,
    run_root: Path,
    pexels_key: str,
    pixabay_key: str,
    gh_token: str,
    clips_tag: str,
    dry_run: bool,
) -> Tuple[Optional[str], Optional[str]]:
    rng = rand_for_guid(ep.guid)
    pub_dt = parsedate_to_datetime(ep.pub_rfc822) if ep.pub_rfc822 else None
    date_prefix = pub_dt.strftime("%Y%m%d") if pub_dt else "00000000"
    base = "%s-%s-%s" % (date_prefix, ep.guid, safe_slug(ep.title))
    video_asset = "%s.mp4" % base
    manifest_asset = "%s.json" % base
    clip_asset_prefix = "%s_main_" % ep.guid

    if dry_run:
        return video_asset, manifest_asset

    work = run_root / ep.guid
    work.mkdir(parents=True, exist_ok=True)

    out_clips_dir = out_clips_root / ep.guid

    # Cleanup legacy outputs from earlier versions (zips or meta files under clips dir).
    if out_clips_dir.exists() and out_clips_dir.is_dir():
        for p in out_clips_dir.glob("*.zip"):
            try:
                p.unlink()
            except Exception:
                pass

    audio_path = work / "audio.mp3"
    download(ep.audio_url, audio_path)

    audio_dur = ffprobe_duration_sec(audio_path)
    # Normalize audio to reduce loudness variance across episodes.
    audio_norm_path = work / "audio_norm.m4a"
    ffmpeg_normalize_audio(audio_path, audio_norm_path)

    query_policy: Dict[str, Any] = {
        "sensitive_detected": False,
        "matched_terms": [],
        "queries_original": [],
        "queries_filtered": [],
        "queries_dropped": [],
        "proxy_queries_added": [],
        "location_prefix": "",
    }

    used_release_clips = False
    used_release_clips_count = 0

    # Reuse already-built local clips if present (useful for local runs).
    local_clips = _list_ordered_clips(out_clips_dir)

    prov: List[Dict[str, Any]] = []
    clips: List[Path] = []

    if local_clips and len(local_clips) >= 1:
        clips = local_clips
    else:
        # Try to reuse ordered clips from Releases (per-guid, per-clip assets).
        if gh_token and clips_tag:
            got = download_clips_for_guid(
                repo=repo,
                tag=clips_tag,
                guid=ep.guid,
                dst_dir=out_clips_dir,
                token=gh_token,
            )
            if got > 0:
                used_release_clips = True
                used_release_clips_count = got
                clips = _list_ordered_clips(out_clips_dir)

        if not clips:
            out_clips_dir.mkdir(parents=True, exist_ok=True)
            tiered_orig = build_tiered_queries(ep.title, ep.description, max_q=12)
            q_orig = [str(x.get("query") or "") for x in tiered_orig]
            q_filtered, query_policy = apply_sensitive_query_policy(ep.title, ep.description, q_orig, max_q=12)
            # Re-apply tiers after filtering, keeping Tier-1 phrases first.
            tiered_final: List[Dict[str, Any]] = []
            for item in tiered_orig:
                q = str(item.get("query") or "")
                if q in q_filtered:
                    tiered_final.append({"tier": int(item.get("tier") or 3), "query": q})
            # Add any proxy queries as Tier-3.
            for q in q_filtered:
                if not any(str(it.get("query") or "") == q for it in tiered_final):
                    tiered_final.append({"tier": 3, "query": q})
            assets = search_assets(pexels_key, pixabay_key, tiered_final)
            if not assets:
                raise RuntimeError("no candidate assets found")

            raw_dir = work / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)

            # Shuffle within tiers but keep Tier-1 first for higher relevance.
            t1 = [a for a in assets if int(a.get("tier") or 3) == 1]
            t2 = [a for a in assets if int(a.get("tier") or 3) == 2]
            t3 = [a for a in assets if int(a.get("tier") or 3) >= 3]
            rng.shuffle(t1)
            rng.shuffle(t2)
            rng.shuffle(t3)
            picks = t1 + t2 + t3

            pick_i = 0
            clip_i = 1
            main_total = 0.0

            while main_total < audio_dur and pick_i < len(picks) * 3:
                a = picks[pick_i % len(picks)]
                pick_i += 1
                asset_key = "%s-%s" % (a["source"], a["asset_id"])
                src_path = raw_dir / ("%s.mp4" % asset_key)
                tier = int(a.get("tier") or 3)

                try:
                    if not src_path.exists():
                        download(a["download_url"], src_path)
                    dur = ffprobe_duration_sec(src_path)
                    if dur < MIN_ASSET_SEC:
                        continue

                    clip_name = "main_%04d.mp4" % clip_i
                    clip_path = out_clips_dir / clip_name

                    # Tier-1 assets: allow long clips without cutting.
                    if tier == 1 and dur >= T1_MIN_SEC and dur <= T1_MAX_SEC:
                        ffmpeg_normalize_video(src_path, clip_path)
                        clip_dur = ffprobe_duration_sec(clip_path)
                        clips.append(clip_path)
                        prov.append({
                            "clip_index": clip_i,
                            "clip_name": clip_name,
                            "tier": tier,
                            "mode": "full",
                            "source": a["source"],
                            "asset_id": a["asset_id"],
                            "author": a.get("author") or "",
                            "page_url": a.get("page_url") or "",
                            "download_url": a.get("download_url") or "",
                            "license_url": a.get("license_url") or "",
                            "query": a.get("query") or "",
                            "start_sec": 0.0,
                            "duration_sec": round(float(clip_dur), 3),
                        })
                        main_total += float(clip_dur)
                        clip_i += 1
                        continue

                    seg = CLIP_SEC_T2 if tier == 2 else CLIP_SEC_T3
                    if dur < (seg + 1.0):
                        continue
                    max_start = max(0.0, dur - seg)
                    start = rng.uniform(0.0, max_start) if max_start > 0 else 0.0
                    ffmpeg_make_clip(src_path, clip_path, start, seg)

                    clips.append(clip_path)
                    prov.append({
                        "clip_index": clip_i,
                        "clip_name": clip_name,
                        "tier": tier,
                        "mode": "trim",
                        "source": a["source"],
                        "asset_id": a["asset_id"],
                        "author": a.get("author") or "",
                        "page_url": a.get("page_url") or "",
                        "download_url": a.get("download_url") or "",
                        "license_url": a.get("license_url") or "",
                        "query": a.get("query") or "",
                        "start_sec": round(start, 3),
                        "duration_sec": round(float(seg), 3),
                    })
                    main_total += float(seg)
                    clip_i += 1
                except Exception:
                    continue

            # Never persist raw downloads.
            try:
                if raw_dir.exists():
                    shutil.rmtree(raw_dir)
            except Exception:
                pass

    if len(clips) < 1:
        raise RuntimeError("no usable clips produced")

    # Prepare per-clip assets for Releases with unique names.
    out_clips_release_dir.mkdir(parents=True, exist_ok=True)
    for c in clips:
        name = c.name
        rel_name = "%s_%s" % (ep.guid, name)
        dst = out_clips_release_dir / rel_name
        if not dst.exists():
            shutil.copyfile(c, dst)

    intro_outro_mp4 = (repo_root / DEFAULT_INTRO_OUTRO_MP4).resolve()
    frame_png = (repo_root / DEFAULT_FRAME_PNG).resolve()

    final_video = work / "video.mp4"
    ffmpeg_concat_with_intro_outro_and_frame(
        clips=clips,
        podcast_audio=audio_norm_path,
        intro_outro_mp4=intro_outro_mp4,
        frame_png=frame_png,
        dst=final_video,
    )

    out_videos_dir.mkdir(parents=True, exist_ok=True)
    out_manifests_dir.mkdir(parents=True, exist_ok=True)

    video_out = out_videos_dir / video_asset
    manifest_out = out_manifests_dir / manifest_asset

    shutil.copyfile(final_video, video_out)

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
        "audio_norm_sha256": sha256_file(audio_norm_path),
        "video_sha256": sha256_file(video_out),
        "audio_duration_sec": round(audio_dur, 3),
        "clip_sec_t2": CLIP_SEC_T2,
        "clip_sec_t3": CLIP_SEC_T3,
        "clips_count": len(clips),
        "clips_tag": clips_tag,
        "clip_asset_prefix": clip_asset_prefix,
        "used_release_clips": used_release_clips,
        "used_release_clips_count": used_release_clips_count,
        "query_policy": query_policy,
        "provenance": prov,
        "license_notes": {
            "pexels": "https://www.pexels.com/license/",
            "pixabay": "https://pixabay.com/service/license/",
        },
    }
    save_json(manifest_out, manifest)

    # Never keep per-run temp.
    try:
        if work.exists():
            shutil.rmtree(work)
    except Exception:
        pass

    return video_asset, manifest_asset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--episodes-json", default="data/episodes.json")
    ap.add_argument("--state-path", default="data/video-data/state.json")
    ap.add_argument("--status-csv", default="data/video-data/status.csv")
    ap.add_argument("--rss-path", default="feed/video_podcast.xml")
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
    gh_token = os.environ.get("GH_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    if not args.dry_run and (not pexels_key or not pixabay_key):
        print("PEXELS_API_KEY and PIXABAY_API_KEY must be set in environment.", file=sys.stderr)
        return 2

    episodes = parse_episodes(episodes_json)
    state = load_state(state_path)

    todo = choose_todo(episodes, state, args.force_guid, args.max_items)
    print("[plan] total=%d todo=%d dry_run=%s" % (len(episodes), len(todo), str(args.dry_run).lower()))

    out_videos = out_dir / "videos"
    out_manifests = out_dir / "manifests"
    out_clips_root = out_dir / "clips"
    out_clips_release_dir = out_dir / "clips_release"
    run_root = out_dir / "_run"
    run_root.mkdir(parents=True, exist_ok=True)

    processed = state.get("processed")
    if not isinstance(processed, dict):
        state["processed"] = {}
        processed = state["processed"]

    for ep in todo:
        print("[episode] guid=%s title=%s" % (ep.guid, ep.title))
        try:
            video_asset, manifest_asset = render_episode(
                ep=ep,
                repo_root=repo_root,
                repo=repo,
                out_videos_dir=out_videos,
                out_manifests_dir=out_manifests,
                out_clips_root=out_clips_root,
                out_clips_release_dir=out_clips_release_dir,
                run_root=run_root,
                pexels_key=pexels_key,
                pixabay_key=pixabay_key,
                gh_token=gh_token,
                clips_tag=args.clips_tag,
                dry_run=args.dry_run,
            )
            if not args.dry_run and video_asset and manifest_asset:
                processed[ep.guid] = {
                    "processed_at": now_iso(),
                    "video_tag": args.video_tag,
                    "manifest_tag": args.manifest_tag,
                    "clips_tag": args.clips_tag,
                    "video_asset_name": video_asset,
                    "manifest_asset_name": manifest_asset,
                }
                save_state(state_path, state)
                print("[ok] guid=%s video=%s manifest=%s" % (ep.guid, video_asset, manifest_asset))
        except Exception as e:
            print("[fail] guid=%s err=%s" % (ep.guid, str(e)), file=sys.stderr)

    write_status_csv(status_csv, episodes, state)
    write_video_rss(rss_path, repo, args.video_tag, episodes, state)

    # Ensure no per-run temp remains.
    try:
        if run_root.exists():
            shutil.rmtree(run_root)
    except Exception:
        pass

    print("[done] out_dir=%s" % str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
