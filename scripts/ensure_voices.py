#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple, Optional


HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, "") or default).strip()


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parse_voice_id(voice: str) -> Tuple[str, str, str]:
    """
    voice format: <locale>-<name>-<quality>
    Examples:
      en_US-ryan-medium
      en_US-amy-medium
      en_GB-alan-low

    Returns: (locale, name, quality)
    """
    parts = voice.strip().split("-")
    if len(parts) < 3:
        raise ValueError(f"Unexpected voice id format: {voice}")
    locale = parts[0]
    quality = parts[-1]
    name = "-".join(parts[1:-1])
    if "_" not in locale or len(locale) < 4:
        raise ValueError(f"Unexpected locale in voice id: {voice}")
    return locale, name, quality


def _voice_urls(voice: str) -> Tuple[str, str]:
    locale, name, quality = _parse_voice_id(voice)
    lang = locale.split("_", 1)[0].lower()
    base = f"{HF_BASE}/{lang}/{locale}/{name}/{quality}/{voice}"
    # Piper repo uses <voice>.onnx and <voice>.onnx.json
    return (base + ".onnx", base + ".onnx.json")


def _is_html(data: bytes) -> bool:
    head = data[:200].lower()
    return b"<html" in head or b"<!doctype html" in head


def _download(url: str, out_path: Path, retries: int = 3, min_bytes: int = 10_000) -> None:
    last_err: Optional[str] = None
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "agenda-ensure-voices/1.0",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()

            if len(data) < min_bytes:
                # If HF returns an HTML error page it can be small; detect that too.
                if _is_html(data):
                    raise RuntimeError(f"Downloaded HTML instead of file ({len(data)} bytes).")
                raise RuntimeError(f"Downloaded file too small ({len(data)} bytes).")

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            return

        except Exception as e:
            last_err = str(e)
            _log(f"  retry {i}/{retries} failed: {url} -> {last_err}")
            time.sleep(i * 2)

    raise RuntimeError(f"Failed to download after {retries} retries: {url}. Last error: {last_err}")


def _valid_json_file(p: Path) -> bool:
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(obj, dict) and len(obj) > 0
    except Exception:
        return False


def ensure_voice(voice: str, model_dir: Path, force: bool = False) -> Dict[str, str]:
    onnx_url, cfg_url = _voice_urls(voice)
    onnx_path = model_dir / f"{voice}.onnx"
    cfg_path = model_dir / f"{voice}.onnx.json"

    model_dir.mkdir(parents=True, exist_ok=True)

    # Model is large (MBs). Config is small (KBs).
    need_onnx = force or (not onnx_path.exists()) or (onnx_path.stat().st_size < 500_000)
    need_cfg = force or (not cfg_path.exists()) or (cfg_path.stat().st_size < 2_000) or (not _valid_json_file(cfg_path))

    if need_onnx:
        _log(f"  downloading model: {onnx_url}")
        _download(onnx_url, onnx_path, retries=3, min_bytes=500_000)

    if need_cfg:
        _log(f"  downloading config: {cfg_url}")
        # config can be small; validate JSON after download
        _download(cfg_url, cfg_path, retries=3, min_bytes=2_000)
        if not _valid_json_file(cfg_path):
            # If HF served something unexpected
            raise RuntimeError(f"Downloaded config is not valid JSON: {cfg_path} (size={cfg_path.stat().st_size})")

    return {"model": str(onnx_path), "config": str(cfg_path)}


def main() -> None:
    model_dir = Path(_env("PIPER_MODEL_DIR", "assets/piper"))
    voices_raw = _env("PIPER_VOICES", "")
    force = _env("FORCE_VOICES", "0").lower() in ("1", "true", "yes", "y")

    # Defaults (good EN male/female pair)
    if not voices_raw:
        voices = ["en_US-ryan-medium", "en_US-amy-medium"]
    else:
        voices = [v.strip() for v in voices_raw.split(",") if v.strip()]

    _log(f"Ensuring Piper voices in: {model_dir.resolve()}")
    _log(f"Voices: {', '.join(voices)}")

    ok: List[str] = []
    for v in voices:
        _log(f"- {v}:")
        files = ensure_voice(v, model_dir, force=force)
        _log(f"  model:  {Path(files['model']).name}")
        _log(f"  config: {Path(files['config']).name}")
        ok.append(v)

    _log(f"Done. Voices ready: {', '.join(ok)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
