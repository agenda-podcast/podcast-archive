#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import time
import json
import hashlib
from pathlib import Path
from typing import Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# Default voice pair (male/female)
VOICES = [
    "en_US-ryan-medium",  # male
    "en_US-amy-medium",   # female
]

# HuggingFace piper voices repo structure:
# https://huggingface.co/rhasspy/piper-voices
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _parse_voice_id(voice: str) -> Tuple[str, str, str]:
    """
    Supports common Piper IDs:
      - en_US-ryan-medium  => locale, name, quality
    Tolerates extra dashes in name by treating:
      first token = locale, last token = quality, middle = name
    """
    parts = [p for p in voice.split("-") if p.strip()]
    if len(parts) < 3:
        raise ValueError(f"Unexpected voice id format: {voice}")

    locale = parts[0]
    quality = parts[-1]
    name = "-".join(parts[1:-1])

    if "_" not in locale:
        raise ValueError(f"Unexpected locale in voice id: {voice}")
    if not name:
        raise ValueError(f"Missing name in voice id: {voice}")

    return locale, name, quality


def _voice_url(voice: str, ext: str) -> str:
    # voice: en_US-amy-medium -> en/en_US/amy/medium/en_US-amy-medium.onnx(.json)
    locale, name, quality = _parse_voice_id(voice)
    lang = locale.split("_")[0]
    return f"{BASE}/{lang}/{locale}/{name}/{quality}/{voice}{ext}"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_bytes(url: str, *, retries: int = 3) -> bytes:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "agenda-ensure-voices/1.2 (+https://github.com/agenda-podcast/podcast-archive)",
                    "Accept": "*/*",
                },
                method="GET",
            )
            with urlopen(req, timeout=180) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status} while fetching {url}")

                data = resp.read()

                # Guard: HF/CDN sometimes returns an HTML block/redirect page
                # (still HTTP 200). Detect and fail fast.
                head = data[:200].lstrip().lower()
                if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                    snippet = data[:400].decode("utf-8", "ignore")
                    raise RuntimeError(f"Got HTML instead of file for {url}: {snippet}")

                return data

        except (HTTPError, URLError, TimeoutError, RuntimeError) as e:
            last_err = e
            time.sleep(attempt * 2)

    raise RuntimeError(f"Failed to download after {retries} retries: {url}. Last error: {last_err}")


def _ensure_binary(url: str, out: Path, *, min_bytes: int, retries: int = 3) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.exists() and out.stat().st_size >= min_bytes:
        return

    data = _fetch_bytes(url, retries=retries)

    if len(data) < min_bytes:
        raise RuntimeError(f"Downloaded file too small ({len(data)} bytes): {url}")

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)


def _ensure_json(url: str, out: Path, *, retries: int = 3) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    # If file already exists and parses as JSON, keep it.
    if out.exists():
        try:
            obj = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return
        except Exception:
            pass  # re-download if invalid

    data = _fetch_bytes(url, retries=retries)

    # Validate JSON content (configs can be only a few KB)
    try:
        text = data.decode("utf-8")
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("config JSON is not an object")
    except Exception as e:
        snippet = data[:400].decode("utf-8", "ignore")
        raise RuntimeError(f"Config JSON invalid for {url}: {e}. Snippet: {snippet}")

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)


def main() -> None:
    model_dir = Path(os.getenv("PIPER_MODEL_DIR", "assets/piper")).resolve()
    model_dir.mkdir(parents=True, exist_ok=True)

    # Allow override: PIPER_VOICES="en_US-ryan-medium,en_US-amy-medium"
    override = (os.getenv("PIPER_VOICES", "") or "").strip()
    voices = VOICES
    if override:
        voices = [v.strip() for v in override.split(",") if v.strip()]

    print(f"Ensuring Piper voices in: {model_dir}")
    print("Voices:", ", ".join(voices))

    for v in voices:
        onnx = model_dir / f"{v}.onnx"
        cfg = model_dir / f"{v}.onnx.json"

        onnx_url = _voice_url(v, ".onnx")
        cfg_url = _voice_url(v, ".onnx.json")

        print(f"- {v}:")
        print(f"  model:  {onnx.name}")
        _ensure_binary(onnx_url, onnx, min_bytes=500_000, retries=3)

        print(f"  config: {cfg.name}")
        _ensure_json(cfg_url, cfg, retries=3)

        print(f"  ok: size(model)={onnx.stat().st_size} sha256={_sha256_file(onnx)[:12]}...")

    print(f"OK. Piper voices present in {model_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
