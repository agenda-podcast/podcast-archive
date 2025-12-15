#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
from pathlib import Path
import requests

# Default voice pair (male/female)
VOICES = [
    "en_US-ryan-medium",
    "en_US-amy-medium",
]

# HuggingFace piper voices repo structure
# https://huggingface.co/rhasspy/piper-voices
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _voice_url(voice: str, ext: str) -> str:
    # voice: en_US-amy-medium -> lang=en, locale=en_US, name=amy, quality=medium
    # path: en/en_US/amy/medium/en_US-amy-medium.onnx
    locale = voice.split("-")[0] + "_" + voice.split("-")[1]  # en_US
    lang = voice.split("_")[0]  # en
    name = voice.split("-")[2]  # amy
    quality = voice.split("-")[3]  # medium
    return f"{BASE}/{lang}/{locale}/{name}/{quality}/{voice}{ext}"


def _download(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=90, stream=True, allow_redirects=True)
    r.raise_for_status()
    with out.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    if not out.exists() or out.stat().st_size < 100_000:
        raise RuntimeError(f"Downloaded file too small: {out}")


def main() -> None:
    model_dir = Path(os.getenv("PIPER_MODEL_DIR", "assets/piper")).resolve()
    model_dir.mkdir(parents=True, exist_ok=True)

    for v in VOICES:
        onnx = model_dir / f"{v}.onnx"
        cfg = model_dir / f"{v}.onnx.json"

        if not onnx.exists():
            _download(_voice_url(v, ".onnx"), onnx)

        if not cfg.exists():
            _download(_voice_url(v, ".onnx.json"), cfg)

    print(f"OK. Piper voices present in {model_dir}")


if __name__ == "__main__":
    main()
