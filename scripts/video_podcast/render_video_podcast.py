#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

import argparse
import os
import shutil
import sys
from pathlib import Path

from .model import parse_episodes
from .repo_state import choose_todo, load_state, save_state, write_status_csv, write_video_rss
from .render_video_podcast_impl import render_episode
from .util import now_iso


DEFAULT_VIDEO_TAG = "video-podcast"
DEFAULT_MANIFEST_TAG = "video-podcast-manifests"
DEFAULT_CLIPS_TAG = "video-podcast-clips"


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
    ap.add_argument("--render-one-pass", action="store_true")
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

    failures = 0
    successes = 0

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
                render_one_pass=bool(args.render_one_pass),
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
                successes += 1
        except Exception as e:
            failures += 1
            print("[fail] guid=%s err=%s" % (ep.guid, str(e)), file=sys.stderr)

    write_status_csv(status_csv, episodes, state)
    write_video_rss(rss_path, repo, args.video_tag, episodes, state)

    # Hard-fail guard: if we planned work and are not in dry-run mode, do not
    # allow a green workflow that produced no outputs.
    if not args.dry_run and len(todo) > 0:
        if failures > 0:
            print("[summary] successes=%d failures=%d" % (successes, failures), file=sys.stderr)
            return 1
        if successes == 0:
            print("[summary] no episodes succeeded", file=sys.stderr)
            return 1

    try:
        if run_root.exists():
            shutil.rmtree(run_root)
    except Exception:
        pass

    print("[done] out_dir=%s" % str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
