# ASCII-only. No ellipses. Keep <= 500 lines.

import hashlib
import json
import random
import re
import shutil
import select
import subprocess
import threading
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional


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


def run_ffmpeg_with_progress(
    cmd: List[str],
    segment_plan: List[Dict[str, Any]],
    expected_total_sec: float,
    target_fps: int,
    timeout_sec: int = 7200,
) -> None:
    """Run ffmpeg with structured progress logs.

    segment_plan items:
      - kind: intro|clip|outro
      - idx: int (for clip only)
      - file: str (for clip only)
      - abs_start: float
      - abs_end: float
      - dur: float
      - src_start: float (for clip only)
      - src_dur: float (for clip only)

    This function enforces a hard-fail guard if ffmpeg output time exceeds expected_total_sec
    by a wide margin, to prevent runaway runs on Actions.
    """
    # Ensure progress output is enabled. Keep stderr so ffmpeg still prints codec details.
    cmd2 = list(cmd)
    if "-progress" not in cmd2:
        cmd2.insert(1, "-progress")
        cmd2.insert(2, "pipe:1")
    if "-nostats" not in cmd2:
        cmd2.insert(1, "-nostats")
    start_ts = time.time()
    last_out_ms = -1
    last_seg_key = ""
    backward_jumps = 0

    last_progress_ts = time.time()
    last_advance_ts = time.time()
    last_hb_ts = time.time()
    last_out_sec = -1.0

    def _seg_for_out_sec(t: float) -> Dict[str, Any]:
        for s in segment_plan:
            if t >= float(s.get("abs_start") or 0.0) and t < float(s.get("abs_end") or 0.0):
                return s
        return {"kind": "unknown", "abs_start": 0.0, "abs_end": float(expected_total_sec), "dur": float(expected_total_sec)}

    def _fmt_sec(s: float) -> str:
        if s < 0:
            s = 0.0
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s - (h * 3600) - (m * 60)
        return "%02d:%02d:%06.3f" % (h, m, sec)

    print("[ffmpeg][plan] segments=%d expected_total_sec=%.3f target_fps=%d" % (
        len(segment_plan), float(expected_total_sec), int(target_fps)
    ), flush=True)
    for s in segment_plan:
        kind = str(s.get("kind") or "unknown")
        if kind == "clip":
            print("[ffmpeg][plan_segment] kind=clip idx=%s file=%s abs_start=%s abs_end=%s src_start=%.3f src_dur=%.3f" % (
                str(s.get("idx")),
                str(s.get("file")),
                _fmt_sec(float(s.get("abs_start") or 0.0)),
                _fmt_sec(float(s.get("abs_end") or 0.0)),
                float(s.get("src_start") or 0.0),
                float(s.get("src_dur") or 0.0),
            ), flush=True)
        else:
            print("[ffmpeg][plan_segment] kind=%s abs_start=%s abs_end=%s dur=%.3f" % (
                kind,
                _fmt_sec(float(s.get("abs_start") or 0.0)),
                _fmt_sec(float(s.get("abs_end") or 0.0)),
                float(s.get("dur") or 0.0),
            ), flush=True)
    print("[ffmpeg][cmd] %s" % (" ".join(cmd2)), flush=True)

    p = subprocess.Popen(
        cmd2,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    stderr_tail: Deque[str] = deque(maxlen=200)
    last_stderr_ts = time.time()
    stderr_lines = 0

    def _stderr_reader() -> None:
        try:
            assert p.stderr is not None
            for line in p.stderr:
                line = line.rstrip("\n")
                if line:
                    stderr_tail.append(line)
                    nonlocal last_stderr_ts, stderr_lines
                    last_stderr_ts = time.time()
                    stderr_lines += 1
                    print("[ffmpeg][stderr] %s" % line, flush=True)
        except Exception:
            return

    th = None
    try:
        th = threading.Thread(target=_stderr_reader)
        th.start()

        assert p.stdout is not None
        progress_kv: Dict[str, str] = {}
        last_print_sec = -1.0
        progress_events = 0

        while True:
            if time.time() - start_ts > float(timeout_sec):
                try:
                    p.terminate()
                except Exception:
                    pass
                raise RuntimeError("ffmpeg timeout exceeded")

            # Use select() so we can emit heartbeat logs even if ffmpeg is not producing output.
            rlist, _, _ = select.select([p.stdout], [], [], 1.0)
            now = time.time()

            # Always emit a wall-clock heartbeat, even if ffmpeg is chatty but not advancing time.
            if now - last_hb_ts >= 30.0:
                age_adv = now - last_advance_ts
                age_prog = now - last_progress_ts
                age_stderr = now - last_stderr_ts
                wall = now - start_ts
                print("[ffmpeg][heartbeat] wall_sec=%.1f out_time=%s seg=%s prog_age_sec=%.1f adv_age_sec=%.1f stderr_age_sec=%.1f stderr_lines=%d" % (
                    float(wall),
                    _fmt_sec(float(last_out_sec)),
                    str(last_seg_key),
                    float(age_prog),
                    float(age_adv),
                    float(age_stderr),
                    int(stderr_lines),
                ), flush=True)
                progress_events = 0
                last_hb_ts = now

            # Stall detection should be based on timeline advance, not just progress events.
            # Some ffmpeg states can emit progress=continue repeatedly while out_time_ms stays fixed.
            near_end = False
            if expected_total_sec is not None and last_out_sec is not None:
                near_end = last_out_sec >= float(expected_total_sec) - 0.25
            if (not near_end) and now - last_advance_ts >= 240.0 and now - start_ts >= 60.0:
                print("[ffmpeg][stall] no_out_time_advance_for_sec=%.1f seg=%s out_time=%s expected_total=%s terminating=1" % (
                    float(now - last_advance_ts),
                    str(last_seg_key),
                    _fmt_sec(float(last_out_sec)),
                    _fmt_sec(float(expected_total_sec)),
                ), flush=True)
                try:
                    p.terminate()
                except Exception:
                    pass
                raise RuntimeError("ffmpeg stalled: out_time not advancing")
            if not rlist:
                if p.poll() is not None:
                    break
                continue

            line = p.stdout.readline()
            if line == "" and p.poll() is not None:
                break
            if not line:
                continue
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                progress_kv[k.strip()] = v.strip()

            if progress_kv.get("progress") in ("continue", "end"):
                last_progress_ts = time.time()
                progress_events += 1
                out_ms_s = progress_kv.get("out_time_ms", "")
                out_sec = None
                if out_ms_s.isdigit():
                    out_ms = int(out_ms_s)
                    out_sec = float(out_ms) / 1000000.0
                    if last_out_ms >= 0 and out_ms + 2000000 < last_out_ms:
                        backward_jumps += 1
                        print("[ffmpeg][warn] out_time_ms moved backward from %d to %d jumps=%d" % (
                            int(last_out_ms), int(out_ms), int(backward_jumps)
                        ), flush=True)
                    last_out_ms = out_ms
                    if out_sec > last_out_sec + 0.001:
                        last_advance_ts = time.time()
                    last_out_sec = out_sec

                if out_sec is not None:
                    # Segment switch logs.
                    seg = _seg_for_out_sec(out_sec)
                    kind = str(seg.get("kind") or "unknown")
                    seg_key = kind
                    if kind == "clip":
                        seg_key = "clip:%s:%s" % (str(seg.get("idx")), str(seg.get("file")))
                    if seg_key != last_seg_key:
                        last_seg_key = seg_key
                        local = out_sec - float(seg.get("abs_start") or 0.0)
                        if kind == "clip":
                            print("[ffmpeg][segment] kind=clip idx=%s file=%s abs=%s local=%s src_start=%.3f src_dur=%.3f" % (
                                str(seg.get("idx")),
                                str(seg.get("file")),
                                _fmt_sec(out_sec),
                                _fmt_sec(local),
                                float(seg.get("src_start") or 0.0),
                                float(seg.get("src_dur") or 0.0),
                            ), flush=True)
                        else:
                            print("[ffmpeg][segment] kind=%s abs=%s local=%s dur=%.3f" % (
                                kind,
                                _fmt_sec(out_sec),
                                _fmt_sec(local),
                                float(seg.get("dur") or 0.0),
                            ), flush=True)

                    # Periodic progress log (once per ~15 seconds of output time).
                    if out_sec - last_print_sec >= 15.0:
                        last_print_sec = out_sec
                        print("[ffmpeg][progress] abs=%s seg=%s" % (_fmt_sec(out_sec), seg_key), flush=True)

                    # Near-end marker to distinguish "done encoding" vs "finalizing".
                    if out_sec >= float(expected_total_sec) - (2.0 / float(max(1, int(target_fps)))):
                        print("[ffmpeg][phase] nearing_end abs=%s expected_total=%s" % (
                            _fmt_sec(out_sec), _fmt_sec(float(expected_total_sec))
                        ), flush=True)

                    # Hard-fail guard: output time must not exceed expected total by more than 2 seconds + 2 frames.
                    guard = float(expected_total_sec) + 2.0 + (2.0 / float(max(1, int(target_fps))))
                    if out_sec > guard:
                        print("[ffmpeg][guard] out_time_exceeds_expected abs=%s expected_total=%s" % (
                            _fmt_sec(out_sec), _fmt_sec(float(expected_total_sec))
                        ), flush=True)
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        raise RuntimeError("ffmpeg runaway duration detected")

                pr = progress_kv.get("progress")
                progress_kv = {}
                if pr == "end":
                    break

        rc = p.wait()
        if rc != 0:
            tail = "\n".join(stderr_tail[-50:])
            raise RuntimeError("ffmpeg failed rc=%d tail=%s" % (int(rc), tail))
    finally:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
        try:
            if th is not None:
                th.join(timeout=5.0)
        except Exception:
            pass

def ffprobe_duration_sec(p: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p),
    ]
    out = run(cmd).stdout.strip()
    return float(out)


def ffprobe_video_dims(p: Path) -> Tuple[int, int]:
    """Return (width, height) for the first video stream. (0,0) on failure."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(p),
        ]
        out = run(cmd).stdout.strip()
        if not out or "x" not in out:
            return 0, 0
        w_s, h_s = out.split("x", 1)
        return int(float(w_s)), int(float(h_s))
    except Exception:
        return 0, 0


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
