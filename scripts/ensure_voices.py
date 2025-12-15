#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Tuple
from urllib.request import Request, urlopen

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _parse_voice_id(voice: str) -> Tuple[str, str, str]:
    """
    Piper voice id format expected:
      <locale>-<name>-<quality>
    Example:
      en_US-ryan-medium
      en_US-amy-medium
    """
    parts = voice.strip().split("-")
    if len(parts) < 3:
        raise ValueError(f"Unexpected voice id format: {voice}")
    locale = parts[0]
    name = parts[1]
    quality = "-".join(parts[2:])
    return locale, name, quality


def _voice_url(voice: str, ext: str) -> str:
    locale, name, quality = _parse_voice_id(voice)
    lang = (locale.split("_")[0] or "en").lower()
    return f"{HF_BASE}/{lang}/{locale}/{name}/{quality}/{voice}{ext}"


def _download(url: str, out_path: Path, min_bytes: int, retries: int = 3) -> None:
    last_err = None
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "agenda-ensure-voices/1.0"})
            with urlopen(req, timeout=60) as resp:
                data = resp.read()
            if len(data) < min_bytes:
                raise RuntimeError(f"Downloaded file too small ({len(data)} bytes): {url}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            return
        except Exception as e:
            last_err = e
            time.sleep(2 + i * 2)
    raise RuntimeError(f"Failed to download after {retries} retries: {url}. Last error: {last_err}")


def _is_valid_json_config(p: Path) -> bool:
    if not p.exists():
        return False
    try:
        b = p.read_bytes()
        if len(b) < 200:
            return False
        s = b[:200].decode("utf-8", "ignore").lstrip()
        return s.startswith("{")
    except Exception:
        return False


def _is_valid_onnx(p: Path) -> bool:
    # ONNX files are usually MBs; be conservative but not huge.
    if not p.exists():
        return False
    try:
        return p.stat().st_size > 200_000
    except Exception:
        return False


def main() -> None:
    model_dir = Path(os.getenv("PIPER_MODEL_DIR", "assets/piper")).resolve()
    voices_env = os.getenv("PIPER_VOICES", "").strip()
    voices = [v.strip() for v in voices_env.split(",") if v.strip()] if voices_env else ["en_US-ryan-medium", "en_US-amy-medium"]

    print(f"Ensuring Piper voices in: {model_dir}")
    print("Voices:", ", ".join(voices))

    model_dir.mkdir(parents=True, exist_ok=True)

    for v in voices:
        try:
            model = model_dir / f"{v}.onnx"
            cfg = model_dir / f"{v}.onnx.json"

            onnx_url = _voice_url(v, ".onnx")
            cfg_url = _voice_url(v, ".onnx.json")

            print(f"- {v}:")
            print(f"  model:  {model.name}")
            print(f"  config: {cfg.name}")

            if not _is_valid_onnx(model):
                _download(onnx_url, model, min_bytes=200_000, retries=3)
            else:
                print("  model OK (cached)")

            if not _is_valid_json_config(cfg):
                # configs can be small (a few KB); do not enforce 10KB
                _download(cfg_url, cfg, min_bytes=300, retries=3)
            else:
                print("  config OK (cached)")

        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            raise

    print("OK: voices ensured.")


if __name__ == "__main__":
    main()
