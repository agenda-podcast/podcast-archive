# ASCII-only. No ellipses. Keep <= 500 lines.

import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .util import USER_AGENT, download, http_get_json


def _headers(token: str) -> Dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = "Bearer %s" % token
    return h


def get_release_by_tag(repo: str, tag: str, token: str) -> Optional[Dict[str, Any]]:
    if not repo or not tag:
        return None
    url = "https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag)
    try:
        return http_get_json(url, headers=_headers(token), timeout_sec=30)
    except Exception:
        return None


def find_asset(release_json: Dict[str, Any], asset_name: str) -> Optional[Dict[str, Any]]:
    if not isinstance(release_json, dict):
        return None
    assets = release_json.get("assets") or []
    for a in assets:
        if not isinstance(a, dict):
            continue
        if str(a.get("name") or "") == asset_name:
            return a
    return None


def download_release_asset(repo: str, tag: str, asset_name: str, token: str, dst: Path) -> bool:
    rel = get_release_by_tag(repo, tag, token)
    if not rel:
        return False
    a = find_asset(rel, asset_name)
    if not a:
        return False
    url = str(a.get("browser_download_url") or "")
    if not url:
        return False
    try:
        if token:
            dst.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers=_headers(token), method="GET")
            with urllib.request.urlopen(req, timeout=180) as resp:
                dst.write_bytes(resp.read())
        else:
            download(url, dst)
        return True
    except Exception:
        return False


def write_json(dst: Path, obj: Any) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
