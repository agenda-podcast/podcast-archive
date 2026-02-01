# ASCII-only. No ellipses. Keep <= 500 lines.

import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


USER_AGENT = "video-podcast-render/1.0"


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


def run(
    cmd: List[str],
    timeout_sec: int = 600,
    stream: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess with sane defaults for GitHub Actions.

    - By default, captures stdout/stderr for parsing.
    - For long-running processes (ffmpeg), pass stream=True to avoid "looks stuck".
    - A timeout guard prevents indefinite hangs.
    """
    try:
        if stream:
            return subprocess.run(cmd, text=True, check=True, timeout=timeout_sec)
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        print("[run][timeout] cmd=%s" % " ".join(cmd), file=sys.stderr)
        raise RuntimeError("command timed out") from e
    except subprocess.CalledProcessError as e:
        # Keep stderr reasonably small in exception messages.
        err = (e.stderr or "")
        err = err[-4000:] if len(err) > 4000 else err
        print("[run][fail] cmd=%s" % " ".join(cmd), file=sys.stderr)
        if err:
            print(err, file=sys.stderr)
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
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def download(url: str, dst: Path, timeout_sec: int = 90, headers: Dict[str, str] = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = {"User-Agent": USER_AGENT}
    if headers:
        for k, v in headers.items():
            if k and v:
                h[str(k)] = str(v)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        with open(dst, "wb") as f:
            shutil.copyfileobj(resp, f)


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
