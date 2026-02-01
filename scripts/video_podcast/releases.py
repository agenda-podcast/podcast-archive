# ASCII-only. No ellipses. Keep <= 500 lines.

import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .util import download, http_get_json


def _auth_headers(token: str) -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "video-podcast-render/1.0",
    }
    if token:
        h["Authorization"] = "Bearer %s" % token
    return h


def get_release_by_tag(repo: str, tag: str, token: str) -> Optional[Dict[str, Any]]:
    if not repo or not tag:
        return None
    url = "https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag)
    try:
        return http_get_json(url, headers=_auth_headers(token), timeout_sec=30)
    except Exception:
        return None


def find_asset(release: Dict[str, Any], asset_name: str) -> Optional[Dict[str, Any]]:
    if not release or not asset_name:
        return None
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    for a in assets:
        if isinstance(a, dict) and a.get("name") == asset_name:
            return a
    return None


def asset_download_url(repo: str, tag: str, asset_name: str, token: str) -> Optional[str]:
    rel = get_release_by_tag(repo, tag, token)
    if not rel:
        return None
    a = find_asset(rel, asset_name)
    if not a:
        return None
    url = a.get("browser_download_url")
    if not isinstance(url, str) or not url:
        return None
    return url


def download_release_asset(repo: str, tag: str, asset_name: str, dst: Path, token: str) -> bool:
    url = asset_download_url(repo, tag, asset_name, token)
    if not url:
        return False
    headers = {}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    download(url, dst, timeout_sec=180, headers=headers)
    return dst.exists()


def try_download_any(
    repo: str,
    tag: str,
    candidates: Sequence[str],
    dst: Path,
    token: str,
) -> Tuple[bool, str]:
    for name in candidates:
        ok = download_release_asset(repo, tag, name, dst, token)
        if ok:
            return True, name
    return False, ""


def list_asset_names(repo: str, tag: str, token: str) -> List[str]:
    rel = get_release_by_tag(repo, tag, token)
    if not rel:
        return []
    assets = rel.get("assets")
    if not isinstance(assets, list):
        return []
    out: List[str] = []
    for a in assets:
        if isinstance(a, dict):
            n = a.get("name")
            if isinstance(n, str) and n:
                out.append(n)
    return out


def _extract_zip_mp4s(zip_path: Path, dst_dir: Path, guid: str) -> int:
    if not zip_path.exists():
        return 0
    try:
        z = zipfile.ZipFile(str(zip_path), "r")
    except Exception:
        return 0

    got = 0
    with z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(".mp4"):
                continue
            base = Path(name).name
            # Support both <guid>_main_0001.mp4 and main_0001.mp4 inside the zip.
            if guid and base.startswith(guid + "_"):
                base = base[len(guid) + 1 :]
            dst = dst_dir / base
            try:
                with z.open(info, "r") as src, open(dst, "wb") as out:
                    out.write(src.read())
                if dst.exists():
                    got += 1
            except Exception:
                continue
    return got


def download_clips_for_guid(
    repo: str,
    tag: str,
    guid: str,
    dst_dir: Path,
    token: str,
    max_items: int = 0,
) -> int:
    """Download ordered clip assets for a guid into dst_dir.

    Preferred (current) format: per-clip MP4 assets in the clips release.
      - <guid>_main_0001.mp4, <guid>_main_0002.mp4, and so on.

    Legacy fallback: a single zip asset (older pipelines):
      - clips_<guid>.zip containing MP4 clips.

    When max_items > 0, only the first max_items clips are downloaded.
    """
    if not guid:
        return 0

    dst_dir.mkdir(parents=True, exist_ok=True)

    names_all = list_asset_names(repo, tag, token)
    # Current format.
    prefix = "%s_main_" % guid
    names = [n for n in names_all if n.startswith(prefix) and n.endswith(".mp4")]
    names.sort()
    if max_items and max_items > 0:
        names = names[:max_items]

    got = 0
    for n in names:
        local_name = n[len(guid) + 1 :]
        dst = dst_dir / local_name
        try:
            ok = download_release_asset(repo, tag, n, dst, token)
            if ok:
                got += 1
        except Exception:
            continue

    if got > 0:
        return got

    # Legacy zip fallback.
    zip_candidates = [
        "clips_%s.zip" % guid,
        "%s_clips.zip" % guid,
        "%s.zip" % guid,
    ]
    tmp = dst_dir / ("_tmp_%s_clips.zip" % guid)
    ok, _ = try_download_any(repo, tag, zip_candidates, tmp, token)
    if not ok:
        return 0
    got = _extract_zip_mp4s(tmp, dst_dir, guid)
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return got
