#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ffmpeg_ops import (
    ffmpeg_concat_with_intro_outro_and_frame,
    ffmpeg_make_clip,
    ffmpeg_normalize_video,
    ffmpeg_render_one_pass_with_intro_outro_and_frame,
)
from .model import Episode
from .releases import download_clips_for_guid
from .sources import apply_sensitive_query_policy, build_tiered_queries, search_assets
from .util import (
    ffprobe_duration_sec,
    ffprobe_video_dims,
    now_iso,
    rand_for_guid,
    safe_slug,
    sha256_file,
    download,
    save_json,
    infer_asset_page_url,
    make_timecoded_url,
)


CLIP_SEC_T2 = 30.0
CLIP_SEC_T3 = 15.0
MIN_ASSET_SEC = 16.0
T1_MIN_SEC = 40.0
T1_MAX_SEC = 600.0

# Expected asset locations in the repository.
# Keep these files out of git history if they are large. Git LFS is a common option.
DEFAULT_INTRO_OUTRO_MP4 = "data/raw_2_1440p_crf15_aac256.mp4"
DEFAULT_FRAME_PNG = "data/video_frame.png"


def _list_ordered_clips(dir_path: Path) -> List[Path]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    return sorted([p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"])


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
    render_one_pass: bool,
    dry_run: bool,
) -> Tuple[Optional[str], Optional[str]]:
    rng = rand_for_guid(ep.guid)
    video_title = safe_slug(ep.title, max_len=60)
    video_asset = "%s_%s.mp4" % (ep.guid, video_title)
    manifest_asset = "%s_%s.json" % (ep.guid, video_title)
    clip_asset_prefix = "%s_%s" % (ep.guid, video_title)

    out_clips_dir = out_clips_root / ep.guid
    work = run_root / ep.guid
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    if out_clips_dir.exists() and out_clips_dir.is_dir():
        for p in out_clips_dir.glob("*.zip"):
            try:
                p.unlink()
            except Exception:
                pass

    audio_path = work / "audio.mp3"
    download(ep.audio_url, audio_path)

    # Step 1) Compute durations precisely.
    audio_dur = ffprobe_duration_sec(audio_path)

    one_pass = bool(render_one_pass)
    one_pass_env = os.environ.get("RENDER_ONE_PASS", "").strip().lower()
    if one_pass_env in ("1", "true", "yes", "on"):
        one_pass = True

    print("[episode] guid=%s title=%s" % (ep.guid, ep.title))
    print("[mode] one_pass=%s (flag=%s env=%s)" % (
        "1" if one_pass else "0",
        "1" if render_one_pass else "0",
        one_pass_env or "(unset)",
    ))
    print("[durations] audio_sec=%.3f" % audio_dur)

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

    local_clips = _list_ordered_clips(out_clips_dir)
    prov: List[Dict[str, Any]] = []
    clips: List[Path] = []
    segments: List[Dict[str, Any]] = []
    duration_log: List[Dict[str, Any]] = []

    trimmed = None
    raw_dir = None

    if one_pass:
        # One-pass mode uses raw sources and a single final encode.
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

        intro_outro_mp4 = (repo_root / DEFAULT_INTRO_OUTRO_MP4).resolve()
        intro_silence = ffprobe_duration_sec(intro_outro_mp4)
        print("[intro_outro] file=%s dur_sec=%.3f" % (str(intro_outro_mp4), intro_silence))
        outro_silence = float(intro_silence)
        total_audio = float(intro_silence) + float(audio_dur) + float(outro_silence)
        print(
            "[durations] T_audio=%.3f T_intro_silence=%.3f T_outro_silence=%.3f T_total=%.3f"
            % (float(audio_dur), float(intro_silence), float(outro_silence), float(total_audio))
        )
        print("[intro_outro] file=%s dur_sec=%.3f" % (intro_outro_mp4.name, float(intro_silence)))

        picks = [a for a in assets if int(a.get("tier") or 3) == 1]
        if not picks:
            raise RuntimeError("no Tier-1 assets found")
        rng.shuffle(picks)

        # Step 2) Clip acquisition (Tier-1 only, horizontal only).
        attempts = 0
        max_attempts = max(1, len(picks)) * 5
        clip_i = 1
        d_sum = 0.0
        while d_sum < audio_dur and attempts < max_attempts:
            a = picks[attempts % len(picks)]
            attempts += 1
            asset_key = "%s-%s" % (a["source"], a["asset_id"])
            src_path = raw_dir / ("%s.mp4" % asset_key)
            try:
                if not src_path.exists():
                    download(a["download_url"], src_path)
                w, h = ffprobe_video_dims(src_path)
                if w and h and w < h:
                    print("[clip][reject] vertical asset=%s w=%d h=%d" % (asset_key, int(w), int(h)))
                    try:
                        src_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                file_dur = ffprobe_duration_sec(src_path)
                if file_dur < MIN_ASSET_SEC:
                    continue
                start = 0.0
                use_dur = float(file_dur)
                clip_name = "raw_%04d.mp4" % clip_i
                print("[clip] %s file_dur=%.3f use_start=0.000 use_dur=%.3f tier=1" % (clip_name, float(file_dur), float(use_dur)))
                duration_log.append({
                    "clip_index": clip_i,
                    "clip_name": clip_name,
                    "path": str(src_path),
                    "file_duration_sec": round(float(file_dur), 3),
                    "start_sec": 0.0,
                    "planned_duration_sec": round(float(use_dur), 3),
                    "tier": 1,
                    "query": a.get("query") or "",
                })
                segments.append({"path": str(src_path), "start_sec": 0.0, "dur_sec": float(use_dur)})
                prov.append({
                    "clip_index": clip_i,
                    "clip_name": clip_name,
                    "tier": 1,
                    "mode": "full",
                    "source": a["source"],
                    "asset_id": a["asset_id"],
                    "author": a.get("author") or "",
                    "page_url": a.get("page_url") or "",
                    "download_url": a.get("download_url") or "",
                    "license_url": a.get("license_url") or "",
                    "query": a.get("query") or "",
                    "start_sec": 0.0,
                    "duration_sec": round(float(use_dur), 3),
                    "file_duration_sec": round(float(file_dur), 3),
                })
                d_sum += float(use_dur)
                clip_i += 1
            except Exception:
                continue

        if d_sum < audio_dur and segments:
            rep_i = 0
            print("[clip][repeat] need_more_sec=%.3f" % (float(audio_dur) - float(d_sum)))
            while d_sum < audio_dur:
                base_seg = segments[rep_i % len(segments)]
                base_prov = prov[rep_i % len(prov)]
                clip_name = "raw_%04d.mp4" % clip_i
                print("[clip][repeat] %s from=%s" % (clip_name, str(base_prov.get("clip_name") or "")))
                duration_log.append({
                    "clip_index": clip_i,
                    "clip_name": clip_name,
                    "path": str(base_seg.get("path") or ""),
                    "file_duration_sec": round(float(base_prov.get("file_duration_sec") or 0.0), 3),
                    "start_sec": round(float(base_seg.get("start_sec") or 0.0), 3),
                    "planned_duration_sec": round(float(base_seg.get("dur_sec") or 0.0), 3),
                    "tier": 1,
                    "query": str(base_prov.get("query") or ""),
                    "repeat_of": str(base_prov.get("clip_name") or ""),
                })
                segments.append({
                    "path": str(base_seg.get("path") or ""),
                    "start_sec": float(base_seg.get("start_sec") or 0.0),
                    "dur_sec": float(base_seg.get("dur_sec") or 0.0),
                })
                prov.append({
                    **base_prov,
                    "clip_index": clip_i,
                    "clip_name": clip_name,
                    "mode": "repeat",
                })
                d_sum += float(base_seg.get("dur_sec") or 0.0)
                clip_i += 1
                rep_i += 1

        # Trim last clip as needed so sum(clip durations) == T_audio.
        if segments:
            excess = float(d_sum) - float(audio_dur)
            if excess > 0.0005:
                last = segments[-1]
                new_d = float(last.get("dur_sec") or 0.0) - float(excess)
                if new_d < 0.1:
                    raise RuntimeError("last clip too short after trim")
                last["dur_sec"] = float(new_d)
                trimmed = {
                    "clip_index": int(duration_log[-1].get("clip_index") or 0),
                    "clip_name": str(duration_log[-1].get("clip_name") or ""),
                    "trim_sec": round(float(excess), 3),
                    "new_duration_sec": round(float(new_d), 3),
                }
                d_sum = float(audio_dur)
                print(
                    "[trim] clip=%s trim_sec=%.3f new_dur=%.3f"
                    % (trimmed["clip_name"], float(excess), float(new_d))
                )

        if not segments or abs(float(d_sum) - float(audio_dur)) > 0.01:
            raise RuntimeError("failed to build segments matching audio duration")

    else:
        # Per-clip mode (fallback): keep the existing clip encoding pipeline.
        if local_clips and len(local_clips) >= 1:
            clips = local_clips
        else:
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

                tiered_final: List[Dict[str, Any]] = []
                for item in tiered_orig:
                    q = str(item.get("query") or "")
                    if q in q_filtered:
                        tiered_final.append({"tier": int(item.get("tier") or 3), "query": q})
                for q in q_filtered:
                    if not any(str(it.get("query") or "") == q for it in tiered_final):
                        tiered_final.append({"tier": 3, "query": q})

                assets = search_assets(pexels_key, pixabay_key, tiered_final)
                if not assets:
                    raise RuntimeError("no candidate assets found")

                raw_dir = work / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)

                picks = [a for a in assets if int(a.get("tier") or 3) == 1]
                rng.shuffle(picks)
                if not picks:
                    raise RuntimeError("no tier-1 assets found")

                pick_i = 0
                clip_i = 1
                main_total = 0.0
                max_iters = max(len(picks) * 5, int(audio_dur / 5.0) + 10)
                while main_total < audio_dur and pick_i < max_iters:
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
                        w, h = ffprobe_video_dims(src_path)
                        if w > 0 and h > 0 and w < h:
                            print("[clip][reject] non_horizontal file=%s w=%d h=%d" % (src_path.name, w, h))
                            try:
                                src_path.unlink()
                            except Exception:
                                pass
                            continue

                        clip_name = "main_%04d.mp4" % clip_i
                        clip_path = out_clips_dir / clip_name

                        if tier == 1:
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

                    except Exception:
                        continue

                try:
                    if raw_dir.exists():
                        shutil.rmtree(raw_dir)
                except Exception:
                    pass

    if one_pass:
        if len(segments) < 1:
            raise RuntimeError("no usable segments produced")
    else:
        if len(clips) < 1:
            raise RuntimeError("no usable clips produced")

    intro_outro_mp4 = (repo_root / DEFAULT_INTRO_OUTRO_MP4).resolve()
    frame_src_png = (repo_root / DEFAULT_FRAME_PNG).resolve()
    # Create a transparent 16:9 canvas with the original frame centered.
    # This avoids any stretching while ensuring ffmpeg sees a 16:9 overlay input.
    frame_png = (work / "frame_16x9.png").resolve()
    try:
        from .util import ensure_png_canvas_16x9

        ensure_png_canvas_16x9(src_png=frame_src_png, dst_png=frame_png, out_w=1920, out_h=1080)
        print(f"[frame] src={frame_src_png} dst={frame_png} out=1920x1080")
    except Exception as e:
        # Fall back to the original frame if preprocessing fails.
        frame_png = frame_src_png
        print(f"[frame][warn] preprocessing_failed using_original err={e}")

    final_video = work / "video.mp4"
    ffmpeg_cmd: List[str] = []
    expected_total = 0.0
    if one_pass:
        intro_silence = ffprobe_duration_sec(intro_outro_mp4)
        outro_silence = float(intro_silence)
        ffmpeg_cmd, expected_total = ffmpeg_render_one_pass_with_intro_outro_and_frame(
            segments=[{
                "path": str(s["path"]),
                "start_sec": float(s["start_sec"]),
                "dur_sec": float(s["dur_sec"]),
            } for s in segments],
            podcast_audio=audio_path,
            intro_outro_mp4=intro_outro_mp4,
            frame_png=frame_png,
            dst=final_video,
            main_dur_sec=float(audio_dur),
            intro_silence_sec=float(intro_silence),
            outro_silence_sec=float(outro_silence),
        )
        print("[ffmpeg] %s" % " ".join([str(x) for x in ffmpeg_cmd]))
    else:
        out_clips_release_dir.mkdir(parents=True, exist_ok=True)
        for c in clips:
            name = c.name
            rel_name = "%s_%s" % (ep.guid, name)
            dst = out_clips_release_dir / rel_name
            if not dst.exists():
                shutil.copyfile(c, dst)

        ffmpeg_concat_with_intro_outro_and_frame(
            clips=clips,
            podcast_audio=audio_path,
            intro_outro_mp4=intro_outro_mp4,
            frame_png=frame_png,
            dst=final_video,
        )

    # Step 5) Verification gates.
    final_dur = ffprobe_duration_sec(final_video)
    # Allow tolerance for mux/container rounding (AAC + MP4) and timestamp
    # quantization. In practice this can exceed 1 frame for long outputs.
    target_fps = 30.0
    tol = max(0.25, (2.0 / target_fps) + 0.05)
    if one_pass:
        if abs(float(final_dur) - float(expected_total)) > tol:
            raise RuntimeError(
                "final duration mismatch: got=%.3f expected=%.3f tol=%.3f"
                % (float(final_dur), float(expected_total), float(tol))
            )
        if float(final_dur) > float(expected_total) + tol:
            raise RuntimeError("final duration exceeds expected total")

    out_videos_dir.mkdir(parents=True, exist_ok=True)
    out_manifests_dir.mkdir(parents=True, exist_ok=True)

    video_out = out_videos_dir / video_asset
    manifest_out = out_manifests_dir / manifest_asset
    shutil.copyfile(final_video, video_out)

    intro_silence_sec = float(ffprobe_duration_sec(intro_outro_mp4))

    segments_timeline: list[dict] = []
    t_abs = intro_silence_sec
    for s, p in zip(segments, prov):
        src_start = float(s.get("start_sec", 0.0))
        src_dur = float(s.get("dur_sec", 0.0))
        src = str(p.get("source", ""))
        asset_id = str(p.get("asset_id", ""))
        page_url = infer_asset_page_url(src, asset_id, str(p.get("page_url", "")))
        segments_timeline.append(
            {
                "kind": p.get("kind", ""),
                "file": p.get("file", ""),
                "source": src,
                "asset_id": asset_id,
                "page_url": page_url,
                "page_url_timecoded": make_timecoded_url(page_url, src_start),
                "src_start_sec": round(src_start, 3),
                "src_end_sec": round(src_start + src_dur, 3),
                "src_dur_sec": round(src_dur, 3),
                "out_abs_start_sec": round(t_abs, 3),
                "out_abs_end_sec": round(t_abs + src_dur, 3),
            }
        )
        t_abs += src_dur

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
        "render_mode": "one_pass" if one_pass else "per_clip",
        "video_sha256": sha256_file(video_out),
        "audio_duration_sec": round(float(audio_dur), 3),
        "intro_silence_sec": round(float(intro_silence_sec), 3),
        "outro_silence_sec": round(float(intro_silence_sec), 3),
        "expected_total_sec": round(float(expected_total) if one_pass else 0.0, 3),
        "final_duration_sec": round(float(final_dur), 3),
        "clip_sec_t2": CLIP_SEC_T2,
        "clip_sec_t3": CLIP_SEC_T3,
        "clips_count": len(clips),
        "segments_count": len(segments),
        "clips_tag": clips_tag,
        "clip_asset_prefix": clip_asset_prefix,
        "used_release_clips": used_release_clips,
        "used_release_clips_count": used_release_clips_count,
        "duration_log": duration_log,
        "segments_timeline": segments_timeline,
        "trimmed_last": trimmed,
        "final_ffmpeg_cmd": " ".join([str(x) for x in ffmpeg_cmd]) if ffmpeg_cmd else "",
        "query_policy": query_policy,
        "provenance": prov,
        "license_notes": {
            "pexels": "https://www.pexels.com/license/",
            "pixabay": "https://pixabay.com/service/license/",
        },
    }
    save_json(manifest_out, manifest)

    try:
        if work.exists():
            shutil.rmtree(work)
    except Exception:
        pass

    return video_asset, manifest_asset
