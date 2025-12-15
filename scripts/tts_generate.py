import os
import re
import io
import json
import time
import base64
import wave
import random
import subprocess
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import requests

# Gemini TTS uses generateContent with AUDIO modality (not generateSpeech).
# Docs: https://ai.google.dev/gemini-api/docs/speech-generation


DEFAULT_SAMPLE_RATE = 24000  # Gemini TTS PCM rate in docs
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


GEMINI_TTS_MODEL = _env_str("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
TTS_MAX_CHARS_PER_CHUNK = _env_int("TTS_MAX_CHARS_PER_CHUNK", 3200)
TTS_TURN_GAP_MS = _env_int("TTS_TURN_GAP_MS", 200)

VOICE_A = _env_str("VOICE_A", "Kore")
VOICE_B = _env_str("VOICE_B", "Puck")


@dataclass
class TTSChunk:
    text: str
    voice: str


def _split_text_soft(text: str, max_chars: int) -> List[str]:
    """
    Split text into chunks <= max_chars, preferring paragraph/sentence boundaries.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    # Split by paragraphs then by sentences if needed
    paras = re.split(r"\n\s*\n+", text)
    out: List[str] = []

    def push_piece(piece: str):
        piece = piece.strip()
        if not piece:
            return
        if len(piece) <= max_chars:
            out.append(piece)
            return
        # sentence-ish split
        parts = re.split(r"(?<=[\.\!\?])\s+", piece)
        buf = ""
        for p in parts:
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

    # final safety split if any still too big
    final: List[str] = []
    for s in out:
        if len(s) <= max_chars:
            final.append(s)
        else:
            for i in range(0, len(s), max_chars):
                final.append(s[i : i + max_chars])
    return [x.strip() for x in final if x.strip()]


def _silence_pcm(ms: int, rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS, width: int = DEFAULT_SAMPLE_WIDTH) -> bytes:
    frames = int(rate * (ms / 1000.0))
    return b"\x00" * frames * channels * width


def _gemini_tts_pcm_bytes(
    text: str,
    voice: str,
    api_key: str,
    model: str,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    timeout_s: int = 120,
    max_retries: int = 6,
) -> bytes:
    """
    Calls Gemini TTS via REST:
      POST /v1beta/models/{model}:generateContent
    Returns raw PCM (s16le, 24kHz, mono) bytes decoded from inlineData.data.
    """
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
                    "prebuiltVoiceConfig": {
                        "voiceName": voice
                    }
                }
            }
        },
        "model": model,
    }

    # Retry for transient failures
    last_err = None
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

            # Retryable
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(30, (2 ** attempt) + random.random())
                time.sleep(sleep_s)
                continue

            # Non-retryable
            raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:400]}")

        except Exception as e:
            last_err = e
            # retry network-ish
            sleep_s = min(30, (2 ** attempt) + random.random())
            time.sleep(sleep_s)

    raise RuntimeError(f"TTS failed after retries: {last_err}")


def _write_wav(path: str, pcm: bytes, rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS, width: int = DEFAULT_SAMPLE_WIDTH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def _ffmpeg_wav_to_mp3(wav_path: str, mp3_path: str) -> None:
    os.makedirs(os.path.dirname(mp3_path) or ".", exist_ok=True)
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


def tts_chunks_to_mp3(tts_chunks: List[Dict[str, Any]], mp3_path: str, api_key: str) -> str:
    """
    Expected by run_topic.py.
    tts_chunks: list of dicts with at least {"text": "..."} and optionally {"voice": "..."}.
    Produces MP3 at mp3_path. Returns mp3_path.
    """
    if not isinstance(tts_chunks, list) or len(tts_chunks) == 0:
        raise RuntimeError("tts_chunks_to_mp3: empty tts_chunks")

    model = _env_str("GEMINI_TTS_MODEL", GEMINI_TTS_MODEL)
    gap_ms = _env_int("TTS_TURN_GAP_MS", TTS_TURN_GAP_MS)

    pcm_all = bytearray()

    # Alternate voices if not provided
    alt = [VOICE_A, VOICE_B]
    alt_i = 0

    for chunk in tts_chunks:
        text = (chunk.get("text") if isinstance(chunk, dict) else "") or ""
        text = str(text).strip()
        if not text:
            continue

        voice = ""
        if isinstance(chunk, dict):
            voice = str(chunk.get("voice") or "").strip()
        if not voice:
            voice = alt[alt_i % 2]
            alt_i += 1

        # Enforce size limit
        parts = _split_text_soft(text, _env_int("TTS_MAX_CHARS_PER_CHUNK", TTS_MAX_CHARS_PER_CHUNK))
        for i, part in enumerate(parts):
            pcm = _gemini_tts_pcm_bytes(
                text=part,
                voice=voice,
                api_key=api_key,
                model=model,
            )
            pcm_all.extend(pcm)

            # gap between subparts and turns
            pcm_all.extend(_silence_pcm(gap_ms))

    if len(pcm_all) == 0:
        raise RuntimeError("tts_chunks_to_mp3: produced 0 audio bytes")

    tmp_wav = mp3_path.replace(".mp3", ".wav")
    _write_wav(tmp_wav, bytes(pcm_all))

    _ffmpeg_wav_to_mp3(tmp_wav, mp3_path)

    # Cleanup wav
    try:
        os.remove(tmp_wav)
    except OSError:
        pass

    return mp3_path
