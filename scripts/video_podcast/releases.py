# ASCII-only. No ellipses. Keep <= 500 lines.

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


def _release_assets_all(release: Dict[str, Any], token: str) -> List[Dict[str, Any]]:
    """Return all assets for a release.

    The releases/tags endpoint can return an incomplete assets list when the
    release has many assets. In that case, GitHub paginates via assets_url.
    """
    if not release:
        return []

    assets = release.get("assets")
    assets_list: List[Dict[str, Any]] = []
    if isinstance(assets, list):
        for a in assets:
            if isinstance(a, dict):
                assets_list.append(a)

    assets_url = release.get("assets_url")
    if not isinstance(assets_url, str) or not assets_url:
        return assets_list

    # If the embedded list looks large enough, prefer the paginated endpoint.
    # GitHub commonly paginates at 30 items.
    if len(assets_list) < 30:
        return assets_list

    out: List[Dict[str, Any]] = []
    page = 1
    # Hard cap to avoid infinite loops.
    while page <= 50:
        url = "%s?per_page=100&page=%d" % (assets_url, page)
        try:
            data = http_get_json(url, headers=_auth_headers(token), timeout_sec=30)
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        for a in data:
            if isinstance(a, dict):
                out.append(a)
        if len(data) < 100:
            break
        page += 1

    # If pagination fails, fall back to the embedded list.
    return out if out else assets_list


def find_asset(release: Dict[str, Any], asset_name: str, token: str = "") -> Optional[Dict[str, Any]]:
    if not release or not asset_name:
        return None
    for a in _release_assets_all(release, token=token):
        if isinstance(a, dict) and a.get("name") == asset_name:
            return a
    return None


def asset_download_url(repo: str, tag: str, asset_name: str, token: str) -> Optional[str]:
    rel = get_release_by_tag(repo, tag, token)
    if not rel:
        return None
    # Use a token-aware scan in case the embedded assets list is incomplete.
    assets = _release_assets_all(rel, token)
    a = None
    for item in assets:
        if isinstance(item, dict) and item.get("name") == asset_name:
            a = item
            break
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
    out: List[str] = []
    for a in _release_assets_all(rel, token):
        if isinstance(a, dict):
            n = a.get("name")
            if isinstance(n, str) and n:
                out.append(n)
    return out


def download_clips_for_guid(repo: str, tag: str, guid: str, dst_dir: Path, token: str) -> int:
    """Download per-clip assets for a guid into dst_dir.

    Assets are expected to be named like: <guid>_main_0001.mp4, <guid>_main_0002.mp4, and so on.
    They are stored locally as: main_0001.mp4, main_0002.mp4, and so on.
    """
    if not guid:
        return 0
    prefix = "%s_main_" % guid
    names = [n for n in list_asset_names(repo, tag, token) if n.startswith(prefix) and n.endswith(".mp4")]
    names.sort()
    if not names:
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    got = 0
    for n in names:
        # Strip guid_ prefix.
        local_name = n[len(guid) + 1 :]
        dst = dst_dir / local_name
        try:
            ok = download_release_asset(repo, tag, n, dst, token)
            if ok:
                got += 1
        except Exception:
            continue
    return got
