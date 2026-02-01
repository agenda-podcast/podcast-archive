# ASCII-only. No ellipses. Keep <= 500 lines.

import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ffmpeg_ops import ffmpeg_make_clip
from .github_release import download_release_asset
from .sources import apply_sensitive_query_policy, build_tiered_queries, search_assets
from .util import download, ffprobe_duration_sec, now_iso, rand_for_guid, save_json, load_json, sha256_file, strip_html


CLIP_SEC = 15.0
MIN_ASSET_SEC = 16.0

TIER_1 = 1
TIER_2 = 2
TIER_3 = 3


def count_clips(dirp: Path) -> int:
    if not dirp.exists():
        return 0
    return len(list(dirp.glob("clip_*.mp4")))


def sprinkle_positions(n_total: int, n_generic: int, rng) -> List[int]:
    if n_total <= 0 or n_generic <= 0:
        return []
    if n_generic >= n_total:
        return list(range(n_total))

    bucket = float(n_total) / float(n_generic)
    picks: List[int] = []
    used = set()
    for i in range(n_generic):
        lo = int(round(i * bucket))
        hi = int(round((i + 1) * bucket)) - 1
        if lo < 0:
            lo = 0
        if hi >= n_total:
            hi = n_total - 1
        if hi < lo:
            hi = lo

        cand = lo
        if hi > lo:
            cand = rng.randint(lo, hi)

        if cand in used:
            for d in range(1, n_total):
                c1 = cand + d
                c2 = cand - d
                if c1 < n_total and c1 not in used:
                    cand = c1
                    break
                if c2 >= 0 and c2 not in used:
                    cand = c2
                    break
        used.add(cand)
        picks.append(cand)

    picks.sort()
    adjusted: List[int] = []
    used2 = set()
    for p in picks:
        cand = p
        if adjusted and cand == adjusted[-1] + 1:
            if cand + 1 < n_total and (cand + 1) not in used2:
                cand = cand + 1
            elif cand - 1 >= 0 and (cand - 1) not in used2:
                cand = cand - 1
        used2.add(cand)
        adjusted.append(cand)
    adjusted.sort()
    return adjusted


def zip_clips(src_dir: Path, meta_path: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src_dir.glob("clip_*.mp4")):
            z.write(p, arcname=p.name)
        z.write(meta_path, arcname=meta_path.name)


def unzip_to(zip_path: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dst_dir)


def _make_from_assets(
    work: Path,
    raw_dir: Path,
    assets: List[Dict[str, Any]],
    rng,
    limit: int,
    clip_prefix: str,
) -> Tuple[List[Path], List[Dict[str, Any]]]:
    made: List[Path] = []
    prov: List[Dict[str, Any]] = []
    pick_i = 0
    clip_i = 0
    if not assets:
        return made, prov
    while len(made) < limit and pick_i < len(assets) * 3:
        a = assets[pick_i % len(assets)]
        pick_i += 1
        asset_key = "%s-%s" % (a["source"], a["asset_id"])
        src_path = raw_dir / ("%s.mp4" % asset_key)
        try:
            if not src_path.exists():
                download(a["download_url"], src_path)
            dur = ffprobe_duration_sec(src_path)
            if dur < MIN_ASSET_SEC:
                continue
            max_start = max(0.0, dur - CLIP_SEC)
            start = rng.uniform(0.0, max_start) if max_start > 0 else 0.0
            tmp_clip = work / ("%s_%04d.mp4" % (clip_prefix, clip_i))
            ffmpeg_make_clip(src_path, tmp_clip, start, CLIP_SEC)
            made.append(tmp_clip)
            prov.append({
                "source": a["source"],
                "asset_id": a["asset_id"],
                "tier": str(a.get("tier") or ""),
                "author": a.get("author") or "",
                "page_url": a.get("page_url") or "",
                "download_url": a.get("download_url") or "",
                "license_url": a.get("license_url") or "",
                "start_sec": round(start, 3),
                "duration_sec": round(CLIP_SEC, 3),
            })
            clip_i += 1
        except Exception:
            continue
    return made, prov


def ensure_clips(
    guid: str,
    title: str,
    desc_html: str,
    repo: str,
    clips_tag: str,
    tmp_dir: Path,
    need: int,
    pexels_key: str,
    pixabay_key: str,
) -> Dict[str, Any]:
    rng = rand_for_guid(guid)
    work = tmp_dir / guid
    work.mkdir(parents=True, exist_ok=True)
    raw_dir = work / "raw"
    clips_ordered_dir = work / "clips_ordered"
    clips_meta_path = work / "clips_meta.json"

    gh_token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    clip_zip_asset = "clips_%s.zip" % guid
    clip_zip_path = work / clip_zip_asset

    reused = False
    generated = False

    if gh_token and download_release_asset(repo, clips_tag, clip_zip_asset, gh_token, clip_zip_path):
        try:
            reuse_dir = work / "clips_reuse"
            shutil.rmtree(reuse_dir, ignore_errors=True)
            unzip_to(clip_zip_path, reuse_dir)
            meta_in = reuse_dir / "clips_meta.json"
            if meta_in.exists() and count_clips(reuse_dir) >= 1:
                shutil.rmtree(clips_ordered_dir, ignore_errors=True)
                shutil.copytree(reuse_dir, clips_ordered_dir)
                shutil.copyfile(meta_in, clips_meta_path)
                reused = True
        except Exception:
            reused = False

    desc_text = strip_html(desc_html)
    tiered_orig = build_tiered_queries(title, desc_text, max_q=12)
    q_orig = [str(x.get("query") or "") for x in tiered_orig]
    q_filtered, _policy = apply_sensitive_query_policy(title, desc_text, q_orig, max_q=12)
    tiered_final: List[Dict[str, Any]] = []
    for item in tiered_orig:
        q = str(item.get("query") or "")
        if q in q_filtered:
            tiered_final.append({"tier": int(item.get("tier") or 3), "query": q})
    for q in q_filtered:
        if not any(str(it.get("query") or "") == q for it in tiered_final):
            tiered_final.append({"tier": 3, "query": q})
    assets_by_tier = {TIER_1: [], TIER_2: [], TIER_3: []}

    if not clips_ordered_dir.exists() or count_clips(clips_ordered_dir) < 1:
        if not (pexels_key and pixabay_key):
            raise RuntimeError("API keys are required to generate clips")
        assets_all = search_assets(pexels_key, pixabay_key, tiered_final)
        for a in assets_all:
            tier = int(a.get("tier") or 3)
            if tier <= 1:
                assets_by_tier[TIER_1].append(a)
            elif tier == 2:
                assets_by_tier[TIER_2].append(a)
            else:
                assets_by_tier[TIER_3].append(a)
        for t in [TIER_1, TIER_2, TIER_3]:
            rng.shuffle(assets_by_tier[t])

        raw_dir.mkdir(parents=True, exist_ok=True)

        main_assets = list(assets_by_tier[TIER_1]) + list(assets_by_tier[TIER_2])
        generic_assets = list(assets_by_tier[TIER_3])

        clips_main, prov_main = _make_from_assets(work, raw_dir, main_assets, rng, need, "main")
        if len(clips_main) < 1:
            raise RuntimeError("no usable clips produced")

        generic_needed = max(0, need - len(clips_main))
        clips_generic: List[Path] = []
        prov_generic: List[Dict[str, Any]] = []
        if generic_needed > 0:
            clips_generic, prov_generic = _make_from_assets(work, raw_dir, generic_assets, rng, generic_needed, "gen")

        if len(clips_main) + len(clips_generic) < need:
            raise RuntimeError("insufficient clips")

        order_generic = sprinkle_positions(need, generic_needed, rng)
        rng.shuffle(clips_main)
        rng.shuffle(clips_generic)

        seq: List[Tuple[Path, Dict[str, Any]]] = []
        mi = 0
        gi = 0
        gset = set(order_generic)
        for i in range(need):
            if i in gset:
                seq.append((clips_generic[gi], prov_generic[gi]))
                gi += 1
            else:
                seq.append((clips_main[mi], prov_main[mi]))
                mi += 1

        shutil.rmtree(clips_ordered_dir, ignore_errors=True)
        clips_ordered_dir.mkdir(parents=True, exist_ok=True)
        prov_final: List[Dict[str, Any]] = []
        for idx, (p, info) in enumerate(seq):
            dst = clips_ordered_dir / ("clip_%04d.mp4" % idx)
            shutil.copyfile(p, dst)
            row = dict(info)
            row["clip_index"] = idx
            prov_final.append(row)

        clip_meta = {
            "guid": guid,
            "generated_at": now_iso(),
            "clip_sec": CLIP_SEC,
            "clips_count": need,
            "query_plan": tiered_final,
            "provenance": prov_final,
            "generic_positions": order_generic,
        }
        save_json(clips_meta_path, clip_meta)
        zip_clips(clips_ordered_dir, clips_meta_path, clip_zip_path)
        generated = True

    if reused and count_clips(clips_ordered_dir) < need:
        reused = False
        if not (pexels_key and pixabay_key):
            raise RuntimeError("API keys are required to extend clips")

        assets_all2 = search_assets(pexels_key, pixabay_key, tiered_final)
        generic_assets2 = [a for a in assets_all2 if int(a.get("tier") or 3) >= 3]
        rng.shuffle(generic_assets2)
        raw_dir.mkdir(parents=True, exist_ok=True)

        add_n = need - count_clips(clips_ordered_dir)
        clips_more, prov_more = _make_from_assets(work, raw_dir, generic_assets2, rng, add_n, "add")
        if len(clips_more) < add_n:
            raise RuntimeError("insufficient clips")

        existing = sorted(clips_ordered_dir.glob("clip_*.mp4"))
        existing_meta = load_json(clips_meta_path) if clips_meta_path.exists() else {}
        existing_prov = existing_meta.get("provenance") if isinstance(existing_meta, dict) else None
        if not isinstance(existing_prov, list):
            existing_prov = []

        seq2: List[Tuple[Path, Dict[str, Any]]] = []
        for i, p in enumerate(existing):
            info = existing_prov[i] if i < len(existing_prov) and isinstance(existing_prov[i], dict) else {}
            seq2.append((p, info))
        for i in range(add_n):
            seq2.append((clips_more[i], prov_more[i]))

        order_generic = sprinkle_positions(need, add_n, rng)
        gen_items = [(p, info) for (p, info) in seq2 if int(info.get("tier") or 3) == TIER_3]
        main_items = [(p, info) for (p, info) in seq2 if int(info.get("tier") or 3) != TIER_3]
        rng.shuffle(gen_items)
        rng.shuffle(main_items)

        seq_final: List[Tuple[Path, Dict[str, Any]]] = []
        mi = 0
        gi = 0
        gset = set(order_generic)
        for i in range(need):
            if i in gset:
                seq_final.append(gen_items[gi])
                gi += 1
            else:
                seq_final.append(main_items[mi])
                mi += 1

        shutil.rmtree(clips_ordered_dir, ignore_errors=True)
        clips_ordered_dir.mkdir(parents=True, exist_ok=True)
        prov_final: List[Dict[str, Any]] = []
        for idx, (p, info) in enumerate(seq_final):
            dst = clips_ordered_dir / ("clip_%04d.mp4" % idx)
            shutil.copyfile(p, dst)
            row = dict(info)
            row["clip_index"] = idx
            prov_final.append(row)

        clip_meta = {
            "guid": guid,
            "generated_at": now_iso(),
            "clip_sec": CLIP_SEC,
            "clips_count": need,
            "query_plan": tiered_final,
            "provenance": prov_final,
            "generic_positions": order_generic,
        }
        save_json(clips_meta_path, clip_meta)
        zip_clips(clips_ordered_dir, clips_meta_path, clip_zip_path)
        generated = True

    sha = sha256_file(clip_zip_path) if clip_zip_path.exists() else ""
    return {
        "clips_dir": clips_ordered_dir,
        "clips_meta_path": clips_meta_path,
        "clips_zip_path": clip_zip_path,
        "clips_zip_asset": clip_zip_asset,
        "clips_sha256": sha,
        "reused": bool(reused),
        "generated": bool(generated),
        "query_plan": tiered_final,
        "assets_by_tier": assets_by_tier,
    }
