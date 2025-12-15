#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import time
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
      - en_US-amy-low      => locale, name, quality
    Also tolerates extra dashes in name by treating the LAST token as quality
    and the FIRST token as locale.
    """
    parts = [p for p in voice.split("-") if p.strip()]
    if len(parts) < 3:
        raise ValueError(f"Unexpected voice id format: {voice}")

    locale = parts[0]          # en_US
    quality = parts[-1]        # medium / high / low / x_low, etc.
    name = "-".join(parts[1:-1])  # ryan / amy / (any name with dashes)

    if "_" not in locale:
        raise ValueError(f"Unexpected locale in voice id: {voice}")

    if not name:
        raise ValueError(f"Missing name in voice id: {voice}")

    return locale, name, quality


def _voice_url(voice: str, ext: str) -> str:
    # voice: en_US-amy-medium -> lang=en, locale=en_US, name=amy, quality=medium
    # path: en/en_US/amy/medium/en_US-amy-medium.onnx(.json)
    locale, name, quality = _parse_voice_id(voice)
    lang = locale.split("_")[0]  # en

    return f"{BASE}/{lang}/{locale}/{name}/{quality}/{voice}{ext}"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, out: Path, *, min_bytes: int = 200_000, retries: int = 3) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    # If file exists and looks non-trivial, keep it
    if out.exists() and out.stat().st_size >= min_bytes:
        return

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "agenda-ensure-voices/1.1 (+https://github.com/agenda-podcast/podcast-archive)",
                    "Accept": "*/*",
                },
                method="GET",
            )
            with urlopen(req, timeout=120) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status} while fetching {url}")

                tmp = out.with_suffix(out.suffix + ".tmp")
                with tmp.open("wb") as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)

                if (not tmp.exists()) or tmp.stat().st_size < min_bytes:
                    size = tmp.stat().st_size if tmp.exists() else 0
                    raise RuntimeError(f"Downloaded file too small ({size} bytes): {url}")

                tmp.replace(out)
                return

        except (HTTPError, URLError, TimeoutError, RuntimeError) as e:
            last_err = e
            time.sleep(attempt * 2)

    raise RuntimeError(f"Failed to download after {retries} retries: {url}. Last error: {last_err}")


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
        _download(onnx_url, onnx, min_bytes=500_000, retries=3)

        print(f"  config: {cfg.name}")
        _download(cfg_url, cfg, min_bytes=10_000, retries=3)

        print(f"  ok: size(model)={onnx.stat().st_size} sha256={_sha256_file(onnx)[:12]}...")

    print(f"OK. Piper voices present in {model_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
