import os
import re
import json
import time
import base64
import wave
import random
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Union

import requests


# =========================
# Defaults / ENV
# =========================
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2  # 16-bit PCM


def _env_int(name: str, default: int) -> int:
    try:
        v = str(os.environ.get(name, "")).strip()
        return int(v) if v else default
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    v = v.strip() if isinstance(v, str) else ""
    return v if v else default


# Model & voices
GEMINI_TTS_MODEL = _env_str("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
VOICE_A = _env_str("VOICE_A", "Kore")
VOICE_B = _env_str("VOICE_B", "Puck")

# SPEED + QUOTA: fewer requests
# You can still override in workflow
TTS_MAX_CHARS_PER_CHUNK = _env_int("TTS_MAX_CHARS_PER_CHUNK", 9000)
TTS_TURN_GAP_MS = _env_int("TTS_TURN_GAP_MS", 120)

# Merge rules (reduce request count further)
MERGE_SAME_VOICE_TURNS = _env_int("MERGE_SAME_VOICE_TURNS", 1)  # 1 = enabled
MIN_CHUNK_CHARS = _env_int("MIN_CHUNK_CHARS", 800)             # merge tiny leftovers into previous chunk

# Behavior when quota is hit
# If set to "1", we will write a marker file and raise a clear error
FAIL_SOFT_ON_QUOTA = _env_int("FAIL_SOFT_ON_QUOTA", 1)
TTS_QUOTA_MARKER = _env_str("TTS_QUOTA_MARKER", "outputs/_tts_quota_exceeded.txt")


class QuotaExceededError(RuntimeError):
    """Raised when TTS quota is exceeded for the day."""


# =========================
# Text splitting / merging
# =========================
def _normalize_ws(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_text_soft(text: str, max_chars: int) -> List[str]:
    """
    Split text into chunks <= max_chars, preferring paragraph/sentence boundaries.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    paras = re.split(r"\n\s*\n+", text)
    out: List[str] = []

    def push_piece(piece: str) -> None:
        piece = piece.strip()
        if not piece:
            return

        if len(piece) <= max_chars:
            out.append(piece)
            return

        parts = re.split(r"(?<=[\.\!\?])\s+", piece)
        buf = ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if not buf:
                buf = p
            elif len(buf) + 1 + len(p) <= max_chars:
                buf = buf + " " + p
            else:
                out.append(buf.strip())
                buf = p
        if buf.strip():
            out.append(buf.strip())

    for para in paras:
        push_piece(para)

    # hard split fallback
    final: List[str] = []
    for s in out:
        if len(s) <= max_chars:
            final.append(s)
        else:
            for i in range(0, len(s), max_chars):
                final.append(s[i : i + max_chars])
    return [x.strip() for x in final if x.strip()]


def _merge_small_chunks(chunks: List[str], min_chars: int) -> List[str]:
    """
    Merge very small chunks into the previous one to reduce request count.
    """
    if not chunks:
        return []
    merged = [chunks[0]]
    for c in chunks[1:]:
        if len(c) < min_chars and len(merged[-1]) + 1 + len(c) <= TTS_MAX_CHARS_PER_CHUNK:
            merged[-1] = merged[-1].rstrip() + " " + c.lstrip()
        else:
            merged.append(c)
    return merged


def _collapse_turns(tts_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Reduce request count by merging consecutive turns with the same resolved voice.
    """
    out: List[Dict[str, Any]] = []
    alt = [VOICE_A, VOICE_B]
    alt_i = 0

    def resolve_voice(chunk: Dict[str, Any]) -> str:
        nonlocal alt_i
        v = str(chunk.get("voice") or "").strip()
        if v:
            return v
        v = alt[alt_i % 2]
        alt_i += 1
        return v

    for ch in tts_chunks:
        if not isinstance(ch, dict):
            continue
        text = _normalize_ws(str(ch.get("text") or ""))
        if not text:
            continue
        voice = resolve_voice(ch)

        if MERGE_SAME_VOICE_TURNS and out and out[-1]["voice"] == voice:
            # Merge with a newline boundary (helps prosody)
            out[-1]["text"] = (out[-1]["text"].rstrip() + "\n\n" + text).strip()
        else:
            out.append({"text": text, "voice": voice})

    return out


# =========================
# Audio helpers
# =========================
def _silence_pcm(ms: int) -> bytes:
    frames = int(DEFAULT_SAMPLE_RATE * (ms / 1000.0))
    return b"\x00" * frames * DEFAULT_CHANNELS * DEFAULT_SAMPLE_WIDTH


def _write_wav(path: Union[str, Path], pcm: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(DEFAULT_CHANNELS)
        wf.setsampwidth(DEFAULT_SAMPLE_WIDTH)
        wf.setframerate(DEFAULT_SAMPLE_RATE)
        wf.writeframes(pcm)


def _ffmpeg_wav_to_mp3(wav_path: Union[str, Path], mp3_path: Union[str, Path]) -> None:
    wav_path = str(wav_path)
    mp3_path = str(mp3_path)
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", wav_path,
        "-codec:a", "libmp3lame",
        "-b:a", "192k",
        mp3_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr[:800]}")


# =========================
# Gemini TTS REST call
# =========================
def _is_quota_error(status: int, body_text: str) -> bool:
    """
    Detect quota/limit errors. Providers may return 429 or 403 with quota text.
    """
    t = (body_text or "").lower()
    if status in (402, 403, 429):
        if "quota" in t or "limit" in t or "exceeded" in t or "rate" in t:
            return True
    return False


def _gemini_tts_pcm_bytes(
    text: str,
    voice: str,
    api_key: str,
    model: str,
    timeout_s: int = 120,
    max_retries: int = 5,
) -> bytes:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty")
    if not model:
        raise RuntimeError("GEMINI_TTS_MODEL is empty")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            }
        },
    }

    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout_s)

            if r.status_code == 200:
                data = r.json()
                b64 = (
                    data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("inlineData", {})
                        .get("data", "")
                )
                if not b64:
                    raise RuntimeError(f"TTS returned 200 but no inlineData.data (voice={voice})")
                return base64.b64decode(b64)

            # QUOTA / LIMIT -> no point retrying
            if _is_quota_error(r.status_code, r.text):
                raise QuotaExceededError(f"TTS quota/limit exceeded (HTTP {r.status_code}): {r.text[:300]}")

            # Retryable (transient)
            if r.status_code in (500, 502, 503, 504):
                sleep_s = min(10.0, (1.6 ** attempt) + random.random() * 0.4)
                time.sleep(sleep_s)
                continue

            # Non-retryable
            raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:400]}")

        except QuotaExceededError as qe:
            last_err = qe
            # do not retry
            break
        except Exception as e:
            last_err = e
            sleep_s = min(10.0, (1.6 ** attempt) + random.random() * 0.4)
            time.sleep(sleep_s)

    if last_err is None:
        last_err = RuntimeError("TTS failed after retries: unknown error (no exception captured)")
    raise RuntimeError(f"TTS failed after retries: {last_err}")


# =========================
# Public API used by run_topic.py
# =========================
def tts_chunks_to_mp3(tts_chunks: List[Dict[str, Any]], mp3_path: Union[str, Path], api_key: str) -> str:
    """
    tts_chunks: list[{"text": "...", "voice": "..."}]
    mp3_path may be str or Path.
    Produces MP3.
    """
    if not isinstance(tts_chunks, list) or not tts_chunks:
        raise RuntimeError("tts_chunks_to_mp3: empty tts_chunks")

    mp3_p = Path(mp3_path)
    if mp3_p.suffix.lower() != ".mp3":
        mp3_p = mp3_p.with_suffix(".mp3")
    wav_p = mp3_p.with_suffix(".wav")

    model = _env_str("GEMINI_TTS_MODEL", GEMINI_TTS_MODEL)
    gap_ms = _env_int("TTS_TURN_GAP_MS", TTS_TURN_GAP_MS)
    max_chars = _env_int("TTS_MAX_CHARS_PER_CHUNK", TTS_MAX_CHARS_PER_CHUNK)
    min_chunk = _env_int("MIN_CHUNK_CHARS", MIN_CHUNK_CHARS)

    # Merge turns to reduce calls
    compact_turns = _collapse_turns(tts_chunks)

    pcm_all = bytearray()

    try:
        for turn in compact_turns:
            voice = str(turn.get("voice") or "").strip() or VOICE_A
            text = str(turn.get("text") or "").strip()
            if not text:
                continue

            parts = _split_text_soft(text, max_chars)
            parts = _merge_small_chunks(parts, min_chunk)

            for part in parts:
                pcm = _gemini_tts_pcm_bytes(
                    text=part,
                    voice=voice,
                    api_key=api_key,
                    model=model,
                )
                pcm_all.extend(pcm)
                pcm_all.extend(_silence_pcm(gap_ms))

    except RuntimeError as e:
        # If quota exceeded -> optional soft fail marker
        msg = str(e).lower()
        if FAIL_SOFT_ON_QUOTA and ("quota" in msg or "limit" in msg or "exceeded" in msg):
            marker = Path(TTS_QUOTA_MARKER)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(e), encoding="utf-8")
        raise

    if not pcm_all:
        raise RuntimeError("tts_chunks_to_mp3: produced 0 audio bytes")

    _write_wav(wav_p, bytes(pcm_all))
    _ffmpeg_wav_to_mp3(wav_p, mp3_p)

    # cleanup wav
    try:
        wav_p.unlink()
    except OSError:
        pass

    return str(mp3_p)
