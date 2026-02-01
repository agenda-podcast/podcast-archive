# ASCII-only. No ellipses. Keep <= 500 lines.

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List


USER_AGENT = "video-podcast-render/1.0"


def log(msg: str) -> None:
    # Keep logs visible in GitHub Actions.
    print(msg, flush=True)


def _tail(s: str, max_chars: int = 1200) -> str:
    s = s or ""
    if len(s) <= max_chars:
        return s
    return s[-max_chars:]


class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = int(status)
        self.body = body or ""
        super().__init__("http_%s status=%d" % (method.lower(), self.status))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_slug(s: str, max_len: int = 80) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        s = "item"
    return s[:max_len]


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as e:
        # Surface stderr to avoid "looks stuck" debugging.
        shown = " ".join(cmd[:8])
        if len(cmd) > 8:
            shown = shown + " [more]"
        log("[cmd][fail] rc=%s cmd=%s" % (str(e.returncode), shown))
        if e.stdout:
            log("[cmd][stdout_tail]\n%s" % _tail(e.stdout))
        if e.stderr:
            log("[cmd][stderr_tail]\n%s" % _tail(e.stderr))
        raise


def ffprobe_duration_sec(p: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p),
    ]
    out = run(cmd).stdout.strip()
    return float(out)


def http_get_json(url: str, headers: Dict[str, str], timeout_sec: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise HttpError("GET", url, int(getattr(e, "code", 0) or 0), _tail(body))
    except urllib.error.URLError as e:
        raise RuntimeError("http_get url_error: %s" % str(e))


def download(url: str, dst: Path, timeout_sec: int = 90, headers: Dict[str, str] = None) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = {"User-Agent": USER_AGENT}
    if headers:
        for k, v in headers.items():
            if k and v:
                h[str(k)] = str(v)
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            with open(dst, "wb") as f:
                shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise HttpError("GET", url, int(getattr(e, "code", 0) or 0), _tail(body))
    except urllib.error.URLError as e:
        raise RuntimeError("download url_error: %s" % str(e))

    try:
        return int(dst.stat().st_size)
    except Exception:
        return 0


def load_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def strip_html(s: str) -> str:
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def rand_for_guid(guid: str) -> random.Random:
    h = hashlib.sha256(guid.encode("utf-8")).digest()
    seed = int.from_bytes(h[:4], "big")
    return random.Random(seed)


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError("Missing required env var: %s" % name)
    return v
