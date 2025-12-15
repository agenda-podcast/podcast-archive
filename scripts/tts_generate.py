#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
import time
import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -------------------------
# Global config
# -------------------------
CACHE_DIR = Path(".cache/tts")
TMP_DIR = Path("outputs/_tmp_tts")
TMP_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip() or "gemini-2.5-flash-preview-tts"

# Your env defaults (you already use these)
DEFAULT_VOICE_A = (os.getenv("VOICE_A", "Kore") or "Kore").strip()
DEFAULT_VOICE_B = (os.getenv("VOICE_B", "Puck") or "Puck").strip()

# Piper defaults (you must provide models)
DEFAULT_PIPER_VOICE_A = (os.getenv("PIPER_VOICE_A", "en_US-lessac-medium") or "en_US-lessac-medium").strip()
DEFAULT_PIPER_VOICE_B = (os.getenv("PIPER_VOICE_B", "en_US-ryan-medium") or "en_US-ryan-medium").strip()

# Chunk / speed knobs
TTS_MAX_CHARS_PER_CHUNK = int(os.getenv("TTS_MAX_CHARS_PER_CHUNK", "3200"))
TTS_TURN_GAP_MS = int(os.getenv("TTS_TURN_GAP_MS", "200"))
TTS_RETRIES = int(os.getenv("TTS_RETRIES", "3"))
TTS_RETRY_SLEEP_SEC = float(os.getenv("TTS_RETRY_SLEEP_SEC", "2.5"))

# Optional post-processing
TRIM_SILENCE = (os.getenv("TTS_TRIM_SILENCE", "1").strip().lower() in ("1", "true", "yes", "y"))
SILENCE_DB = os.getenv("TTS_SILENCE_DB", "-35dB").strip() or "-35dB"
SILENCE_DUR = os.getenv("TTS_SILENCE_DUR", "0.25").strip() or "0.25"


@dataclass
class TTSTurn:
    speaker: str  # "A" or "B"
    text: str


# -------------------------
# Public API (used by run_topic.py)
# -------------------------
def script_to_tts_chunks(script_text: str) -> List[Dict[str, str]]:
    """
    Convert a script into dialogue chunks.

    Supported formats:
      - Lines starting with "A:" / "B:"
      - Lines starting with "HOST A:" / "HOST B:" / "Speaker A:" etc.
      - Fallback: split paragraphs and alternate A/B

    Returns: [{"speaker":"A","text":"..."}, ...]
    """
    if not isinstance(script_text, str):
        return []

    text = script_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    # Try explicit speaker lines
    turns: List[TTSTurn] = []
    speaker_pat = re.compile(r"^\s*(?:HOST\s*)?(?:SPEAKER\s*)?([AB])\s*:\s*(.+?)\s*$", re.IGNORECASE)

    for line in text.split("\n"):
        m = speaker_pat.match(line)
        if m:
            sp = m.group(1).upper()
            payload = m.group(2).strip()
            if payload:
                turns.append(TTSTurn(sp, payload))

    if turns:
        return [{"speaker": t.speaker, "text": t.text} for t in _split_long_turns(turns)]

    # Fallback: paragraphs, alternate A/B
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paras:
        paras = [x.strip() for x in text.split("\n") if x.strip()]

    out: List[TTSTurn] = []
    sp = "A"
    for p in paras:
        out.append(TTSTurn(sp, p))
        sp = "B" if sp == "A" else "A"

    return [{"speaker": t.speaker, "text": t.text} for t in _split_long_turns(out)]


def tts_chunks_to_mp3(
    chunks: List[Dict[str, str]],
    mp3_path: Path,
    api_key: str = "",
    premium: bool = True,
    gemini_model: Optional[str] = None,
    voice_a: Optional[str] = None,
    voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
) -> None:
    """
    Main entry: turns -> WAV pieces -> final MP3.
    """
    if not chunks or not isinstance(chunks, list):
        raise RuntimeError("No chunks provided to TTS.")

    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve voice settings
    gem_model = (gemini_model or DEFAULT_GEMINI_MODEL).strip()
    vA = (voice_a or DEFAULT_VOICE_A).strip()
    vB = (voice_b or DEFAULT_VOICE_B).strip()

    pA = (piper_voice_a or DEFAULT_PIPER_VOICE_A).strip()
    pB = (piper_voice_b or DEFAULT_PIPER_VOICE_B).strip()

    # Normalize turns
    turns: List[TTSTurn] = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        sp = str(c.get("speaker", "A")).strip().upper()
        if sp not in ("A", "B"):
            sp = "A"
        t = str(c.get("text", "")).strip()
        if not t:
            continue
        turns.append(TTSTurn(sp, t))

    if not turns:
        raise RuntimeError("No dialogue turns parsed from script.")

    # Render each turn into wav (cached)
    wav_parts: List[Path] = []
    for idx, turn in enumerate(turns):
        voice = vA if turn.speaker == "A" else vB
        piper_voice = pA if turn.speaker == "A" else pB

        wav = _render_turn_to_wav(
            text=turn.text,
            premium=premium,
            api_key=api_key,
            gemini_model=gem_model,
            gemini_voice=voice,
            piper_voice=piper_voice,
        )
        wav_parts.append(wav)

        # Optional gap between turns
        if TTS_TURN_GAP_MS > 0 and idx < (len(turns) - 1):
            gap_wav = _silence_wav(TTS_TURN_GAP_MS)
            wav_parts.append(gap_wav)

    # Stitch into final mp3 with ffmpeg (fast)
    _concat_wavs_to_mp3(wav_parts, mp3_path)

    # Cleanup tmp non-cached artifacts (silence wav is cached too)
    # We keep CACHE_DIR persistent.


# -------------------------
# Internal: splitting
# -------------------------
def _split_long_turns(turns: List[TTSTurn]) -> List[TTSTurn]:
    """
    Split very long turns to stay under TTS_MAX_CHARS_PER_CHUNK.
    Keeps speaker assignment.
    """
    out: List[TTSTurn] = []
    for t in turns:
        txt = re.sub(r"\s+", " ", t.text).strip()
        if len(txt) <= TTS_MAX_CHARS_PER_CHUNK:
            out.append(TTSTurn(t.speaker, txt))
            continue

        # Split by sentences
        parts = _split_text(txt, TTS_MAX_CHARS_PER_CHUNK)
        for p in parts:
            p2 = p.strip()
            if p2:
                out.append(TTSTurn(t.speaker, p2))
    return out


def _split_text(text: str, max_chars: int) -> List[str]:
    # Sentence-ish splitter with fallbacks
    if len(text) <= max_chars:
        return [text]

    # Primary: sentence boundaries
    sentences = re.split(r"(?<=[\.\!\?])\s+", text)
    buf = ""
    out: List[str] = []

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not buf:
            buf = s
            continue
        if len(buf) + 1 + len(s) <= max_chars:
            buf = buf + " " + s
        else:
            out.append(buf)
            buf = s

    if buf:
        out.append(buf)

    # If still any chunk too long, hard wrap
    final: List[str] = []
    for chunk in out:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            for i in range(0, len(chunk), max_chars):
                final.append(chunk[i:i + max_chars])
    return final


# -------------------------
# Internal: caching + hashing
# -------------------------
def _cache_key(engine: str, voice: str, model: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(engine.encode("utf-8"))
    h.update(b"|")
    h.update(voice.encode("utf-8"))
    h.update(b"|")
    h.update((model or "").encode("utf-8"))
    h.update(b"|")
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


def _render_turn_to_wav(
    text: str,
    premium: bool,
    api_key: str,
    gemini_model: str,
    gemini_voice: str,
    piper_voice: str,
) -> Path:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("Empty turn text.")

    if premium:
        engine = "gemini"
        key = _cache_key(engine, gemini_voice, gemini_model, text)
        out = CACHE_DIR / engine / gemini_model / gemini_voice
        out.mkdir(parents=True, exist_ok=True)
        wav_path = out / f"{key}.wav"
        if wav_path.exists() and wav_path.stat().st_size > 1000:
            return wav_path

        wav_bytes = _gemini_tts_wav_bytes(
            api_key=api_key,
            model=gemini_model,
            voice=gemini_voice,
            text=text,
        )
        wav_path.write_bytes(wav_bytes)
        return wav_path

    # Piper
    engine = "piper"
    key = _cache_key(engine, piper_voice, "", text)
    out = CACHE_DIR / engine / piper_voice
    out.mkdir(parents=True, exist_ok=True)
    wav_path = out / f"{key}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 1000:
        return wav_path

    _piper_tts_to_wav(text=text, voice_name=piper_voice, wav_path=wav_path)
    return wav_path


# -------------------------
# Gemini TTS (google-genai)
# -------------------------
def _gemini_tts_wav_bytes(api_key: str, model: str, voice: str, text: str) -> bytes:
    """
    Uses official google-genai package if available.
    If model quota is exceeded or API fails -> raises RuntimeError.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty but premium_tts=true")

    last_err: Optional[str] = None

    for attempt in range(1, TTS_RETRIES + 1):
        try:
            from google import genai  # type: ignore

            client = genai.Client(api_key=api_key)

            # IMPORTANT:
            # The SDK surface changes; we keep it defensive:
            # - Prefer client.models.generate_content with audio response if supported.
            # - If unavailable, raise a clear error with guidance.
            #
            # Many environments expose:
            #   client.models.generate_content(model=..., contents=[...], config=...)
            #
            # We request audio output; exact schema varies.
            config = {
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "voice_config": {"prebuilt_voice_config": {"voice_name": voice}}
                },
            }

            resp = client.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [{"text": text}]}],
                config=config,
            )

            # Try to extract audio bytes from common shapes
            wav = _extract_audio_bytes(resp)
            if not wav:
                raise RuntimeError("Gemini returned no audio bytes (SDK schema mismatch).")

            return wav

        except Exception as e:
            last_err = str(e)
            # Backoff
            if attempt < TTS_RETRIES:
                time.sleep(TTS_RETRY_SLEEP_SEC * attempt)
            continue

    raise RuntimeError(f"TTS failed after retries: {last_err}")


def _extract_audio_bytes(resp: Any) -> bytes:
    """
    Best-effort extractor for audio bytes from google-genai responses.
    We deliberately avoid hard-coding one fragile schema.
    """
    # Common: resp.candidates[0].content.parts[0].inline_data.data (base64) OR bytes in data
    try:
        # genai objects often are dict-like or have attrs
        obj = resp

        # dict form
        if isinstance(obj, dict):
            return _extract_audio_bytes_from_dict(obj)

        # attr form
        if hasattr(obj, "to_dict"):
            d = obj.to_dict()
            if isinstance(d, dict):
                return _extract_audio_bytes_from_dict(d)

    except Exception:
        pass

    return b""


def _extract_audio_bytes_from_dict(d: Dict[str, Any]) -> bytes:
    # scan for "audio" blocks with base64 "data"
    # NOTE: We avoid adding extra dependencies; use python stdlib base64 if needed.
    import base64

    def walk(x: Any) -> Optional[bytes]:
        if isinstance(x, dict):
            # candidate location patterns
            if "inlineData" in x and isinstance(x["inlineData"], dict):
                inner = x["inlineData"]
                data = inner.get("data")
                if isinstance(data, str) and data:
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        return None
            if "inline_data" in x and isinstance(x["inline_data"], dict):
                inner = x["inline_data"]
                data = inner.get("data")
                if isinstance(data, str) and data:
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        return None

            if "data" in x and "mimeType" in x:
                data = x.get("data")
                if isinstance(data, str) and data:
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        return None

            for v in x.values():
                got = walk(v)
                if got:
                    return got

        if isinstance(x, list):
            for it in x:
                got = walk(it)
                if got:
                    return got

        return None

    b = walk(d)
    return b or b""


# -------------------------
# Piper TTS
# -------------------------
def _piper_tts_to_wav(text: str, voice_name: str, wav_path: Path) -> None:
    """
    Requires:
      - piper binary in PATH (or set PIPER_BIN)
      - voice model path via:
          PIPER_MODEL_DIR (directory containing *.onnx + *.json)
        OR
          PIPER_MODEL_<voice_name> (exact file path to .onnx)
    """
    piper_bin = (os.getenv("PIPER_BIN", "piper") or "piper").strip()

    model_path = _resolve_piper_model_path(voice_name)
    if not model_path:
        raise RuntimeError(
            f"Piper model not found for voice '{voice_name}'. "
            f"Set PIPER_MODEL_DIR or PIPER_MODEL_{_envify(voice_name)} to point to a .onnx file."
        )

    wav_path.parent.mkdir(parents=True, exist_ok=True)

    # Piper supports stdin text
    cmd = [
        piper_bin,
        "--model", str(model_path),
        "--output_file", str(wav_path),
    ]

    try:
        subprocess.run(cmd, input=text.encode("utf-8"), check=True)
    except FileNotFoundError:
        raise RuntimeError("Piper binary not found. Install piper or set PIPER_BIN.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Piper failed: {e}")


def _resolve_piper_model_path(voice_name: str) -> Optional[Path]:
    # 1) Exact env var PIPER_MODEL_<VOICE>
    key = f"PIPER_MODEL_{_envify(voice_name)}"
    v = os.getenv(key, "").strip()
    if v:
        p = Path(v)
        if p.exists():
            return p

    # 2) Directory lookup in PIPER_MODEL_DIR
    model_dir = os.getenv("PIPER_MODEL_DIR", "").strip()
    if model_dir:
        d = Path(model_dir)
        if d.exists() and d.is_dir():
            # Try common naming: voice_name.onnx or contains voice_name
            candidates = list(d.glob("*.onnx"))
            for c in candidates:
                if c.stem == voice_name or voice_name in c.name:
                    return c

    return None


def _envify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()


# -------------------------
# Audio assembly (ffmpeg)
# -------------------------
def _silence_wav(ms: int) -> Path:
    """
    Cached silence wav for turn gaps.
    """
    ms = max(0, int(ms))
    out = CACHE_DIR / "silence"
    out.mkdir(parents=True, exist_ok=True)
    wav_path = out / f"silence_{ms}ms.wav"
    if wav_path.exists() and wav_path.stat().st_size > 100:
        return wav_path

    # Generate silence via ffmpeg (PCM 16kHz mono)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=24000:cl=mono",
        "-t", f"{ms/1000.0:.3f}",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    _run_ffmpeg(cmd)
    return wav_path


def _concat_wavs_to_mp3(wavs: List[Path], mp3_path: Path) -> None:
    if not wavs:
        raise RuntimeError("No wav parts to concatenate.")

    # Create concat list
    concat_txt = TMP_DIR / f"concat_{int(time.time())}.txt"
    lines = []
    for w in wavs:
        wp = Path(w)
        if not wp.exists() or wp.stat().st_size < 50:
            continue
        # ffmpeg concat demuxer requires escaping
        lines.append(f"file '{wp.as_posix()}'")
    if not lines:
        raise RuntimeError("All wav parts are missing/empty.")

    concat_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tmp_wav = TMP_DIR / f"joined_{int(time.time())}.wav"

    # 1) concat to wav
    cmd1 = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(tmp_wav),
    ]
    _run_ffmpeg(cmd1)

    # 2) optional silence trim + encode mp3
    if TRIM_SILENCE:
        # trim leading/trailing silence lightly
        af = f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_duration={SILENCE_DUR}:detection=peak," \
             f"silenceremove=stop_periods=1:stop_threshold={SILENCE_DB}:stop_duration={SILENCE_DUR}:detection=peak"
        cmd2 = [
            "ffmpeg", "-y",
            "-i", str(tmp_wav),
            "-af", af,
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(mp3_path),
        ]
    else:
        cmd2 = [
            "ffmpeg", "-y",
            "-i", str(tmp_wav),
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(mp3_path),
        ]
    _run_ffmpeg(cmd2)

    # Best-effort cleanup tmp files
    try:
        concat_txt.unlink(missing_ok=True)  # py3.11 ok
    except Exception:
        pass
    try:
        tmp_wav.unlink(missing_ok=True)
    except Exception:
        pass


def _run_ffmpeg(cmd: List[str]) -> None:
    # Ensure ffmpeg exists
    if shutil.which(cmd[0]) is None:
        raise RuntimeError("ffmpeg is required but not found in PATH.")
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        out = (p.stdout or "")[-2000:]
        raise RuntimeError(f"ffmpeg failed (rc={p.returncode}). Tail:\n{out}")


# -------------------------
# Module self-test (optional)
# -------------------------
if __name__ == "__main__":
    # Minimal smoke test: parse script into chunks
    demo = "A: Hello.\nB: Hi there.\nA: This is a test."
    chunks = script_to_tts_chunks(demo)
    print(json.dumps(chunks, ensure_ascii=False, indent=2))
