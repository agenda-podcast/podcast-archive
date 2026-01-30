# ASCII-only. No ellipses. Keep <= 500 lines.

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
    candidates: Tuple[str, ...],
    dst: Path,
    token: str,
) -> Tuple[bool, str]:
    for name in candidates:
        ok = download_release_asset(repo, tag, name, dst, token)
        if ok:
            return True, name
    return False, ""
