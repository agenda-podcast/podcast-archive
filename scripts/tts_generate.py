import os
import re
import time
import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import requests


# -----------------------------
# ENV
# -----------------------------
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip()
VOICE_A = os.environ.get("VOICE_A", "Kore").strip()
VOICE_B = os.environ.get("VOICE_B", "Puck").strip()

TTS_MAX_CHARS_PER_CHUNK = int(os.environ.get("TTS_MAX_CHARS_PER_CHUNK", "3200"))
TTS_TURN_GAP_MS = int(os.environ.get("TTS_TURN_GAP_MS", "200"))

# Some providers may error on very long single-line input; keep lines reasonable:
LINE_WRAP = 240

# Gemini REST (Generative Language API style). Endpoint may evolve; this is robust to "v1beta" family.
GEMINI_ENDPOINT = os.environ.get("GEMINI_TTS_ENDPOINT", "").strip()
# If empty, default to a widely used pattern:
# https://generativelanguage.googleapis.com/v1beta/models/{model}:generateSpeech?key=...
if not GEMINI_ENDPOINT:
    GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateSpeech"


# -----------------------------
# Parsing dialogue
# -----------------------------
SPEAKER_A_PREFIX = "SPEAKER_A:"
SPEAKER_B_PREFIX = "SPEAKER_B:"


def _wrap_lines(text: str, width: int = LINE_WRAP) -> str:
    """Soft-wrap long lines without breaking words."""
    out_lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            out_lines.append("")
            continue
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            if cut <= 0:
                cut = width
            out_lines.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def parse_dialogue_turns(script_text: str) -> List[Tuple[str, str]]:
    """
    Returns list of (speaker, text) where speaker in {"A","B"}.
    Expects lines starting with SPEAKER_A: or SPEAKER_B:
    Ignores chapter markers and sources section.
    """
    txt = (script_text or "").replace("\r\n", "\n").replace("\r", "\n")

    turns: List[Tuple[str, str]] = []
    cur_speaker: Optional[str] = None
    cur_buf: List[str] = []

    def flush() -> None:
        nonlocal cur_speaker, cur_buf
        if cur_speaker and cur_buf:
            body = " ".join([x.strip() for x in cur_buf if x.strip()]).strip()
            body = re.sub(r"\s+", " ", body).strip()
            if body:
                turns.append((cur_speaker, body))
        cur_speaker = None
        cur_buf = []

    for line in txt.split("\n"):
        s = line.strip()
        if not s:
            continue

        # Skip markers
        if s.startswith("=== CHAPTER:") or s.startswith("=== SOURCES"):
            continue

        if s.startswith(SPEAKER_A_PREFIX):
            flush()
            cur_speaker = "A"
            cur_buf = [s[len(SPEAKER_A_PREFIX):].strip()]
            continue

        if s.startswith(SPEAKER_B_PREFIX):
            flush()
            cur_speaker = "B"
            cur_buf = [s[len(SPEAKER_B_PREFIX):].strip()]
            continue

        # If line does not start with a speaker, treat as continuation if we are inside a turn
        if cur_speaker:
            cur_buf.append(s)

    flush()
    return turns


def chunk_text(text: str, max_chars: int) -> List[str]:
    """
    Chunk text into <= max_chars segments, splitting on sentence boundaries if possible.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []

    if len(t) <= max_chars:
        return [t]

    # Split into sentences-ish
    parts = re.split(r"(?<=[\.\!\?])\s+", t)
    chunks: List[str] = []
    cur = ""

    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not cur:
            cur = p
            continue
        if len(cur) + 1 + len(p) <= max_chars:
            cur = cur + " " + p
        else:
            chunks.append(cur)
            cur = p

    if cur:
        chunks.append(cur)

    # If any chunk is still too big, hard-split
    final: List[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            start = 0
            while start < len(c):
                final.append(c[start:start + max_chars])
                start += max_chars

    return final


# -----------------------------
# WAV helpers
# -----------------------------
def _wav_header(num_channels: int, sample_rate: int, bits: int, data_size: int) -> bytes:
    byte_rate = sample_rate * num_channels * bits // 8
    block_align = num_channels * bits // 8
    fmt_chunk_size = 16
    audio_format = 1  # PCM
    riff_chunk_size = 36 + data_size

    return b"".join([
        b"RIFF",
        struct.pack("<I", riff_chunk_size),
        b"WAVE",
        b"fmt ",
        struct.pack("<IHHIIHH", fmt_chunk_size, audio_format, num_channels, sample_rate, byte_rate, block_align, bits),
        b"data",
        struct.pack("<I", data_size),
    ])


def write_silence_wav(path: Path, duration_ms: int, sample_rate: int = 24000, num_channels: int = 1, bits: int = 16) -> None:
    duration_ms = max(0, int(duration_ms))
    samples = int(sample_rate * duration_ms / 1000)
    bytes_per_sample = bits // 8
    data = b"\x00" * (samples * num_channels * bytes_per_sample)
    hdr = _wav_header(num_channels, sample_rate, bits, len(data))
    path.write_bytes(hdr + data)


def concat_wavs(wav_paths: List[Path], out_wav: Path) -> None:
    """
    Concatenate WAV files with same format (PCM). We enforce same format by controlling generation.
    """
    # Read all, strip headers, concatenate data.
    all_data = []
    sample_rate = 24000
    num_channels = 1
    bits = 16

    for p in wav_paths:
        b = p.read_bytes()
        if len(b) < 44 or b[:4] != b"RIFF":
            raise RuntimeError(f"Not a WAV file: {p}")
        # Extract format quickly
        # Assume PCM 16-bit 24kHz mono; if different, still try but warn.
        data = b[44:]
        all_data.append(data)

    payload = b"".join(all_data)
    out_wav.write_bytes(_wav_header(num_channels, sample_rate, bits, len(payload)) + payload)


def wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "192k") -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on runner. Install ffmpeg or add it to workflow.")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(wav_path),
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        str(mp3_path),
    ]
    subprocess.check_call(cmd)


# -----------------------------
# Gemini TTS REST call
# -----------------------------
def gemini_tts_wav(text: str, voice: str, api_key: str) -> bytes:
    """
    Calls Gemini TTS endpoint and expects WAV bytes back (base64 inside JSON).
    The exact response schema may vary; this function attempts common fields.
    """
    url = GEMINI_ENDPOINT.format(model=GEMINI_TTS_MODEL)
    params = {"key": api_key}

    # Keep payload conservative; providers change schemas.
    payload: Dict[str, Any] = {
        "input": {"text": text},
        "voice": {"name": voice},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
    }

    r = requests.post(url, params=params, json=payload, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:400]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError("TTS response was not JSON.")

    # Common patterns:
    # data["audioContent"] (base64)
    # data["audio"]["content"] (base64)
    # data["candidates"][0]["audio"]["content"] (base64)
    b64 = None
    if isinstance(data, dict):
        if "audioContent" in data:
            b64 = data.get("audioContent")
        elif "audio" in data and isinstance(data["audio"], dict):
            b64 = data["audio"].get("content") or data["audio"].get("audioContent")
        elif "candidates" in data and isinstance(data["candidates"], list) and data["candidates"]:
            c0 = data["candidates"][0]
            if isinstance(c0, dict):
                aud = c0.get("audio")
                if isinstance(aud, dict):
                    b64 = aud.get("content") or aud.get("audioContent")

    if not b64 or not isinstance(b64, str):
        raise RuntimeError(f"TTS response missing audio content keys. Keys: {list(data.keys())}")

    import base64
    return base64.b64decode(b64)


# -----------------------------
# Public API used by run_topic.py
# -----------------------------
def tts_chunks_to_mp3(chunks: List[Dict[str, Any]], mp3_path: Path, api_key: str) -> None:
    """
    chunks: [{"chapter_title": "...", "text": "SPEAKER_A: ...\nSPEAKER_B: ..."}]
    Produces one MP3 file.
    """
    if not chunks:
        raise RuntimeError("No chunks provided for TTS.")

    # Flatten all chapters into turns
    turns: List[Tuple[str, str]] = []
    for ch in chunks:
        text = str(ch.get("text", "") or "")
        turns.extend(parse_dialogue_turns(text))

    if not turns:
        raise RuntimeError("No dialogue turns parsed from script.")

    # Prepare temp
    tmp_dir = Path("tts_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    wav_parts: List[Path] = []

    try:
        part_idx = 0
        for speaker, turn_text in turns:
            voice = VOICE_A if speaker == "A" else VOICE_B
            wrapped = _wrap_lines(turn_text)
            subchunks = chunk_text(wrapped, TTS_MAX_CHARS_PER_CHUNK)
            for sc in subchunks:
                part_idx += 1
                wav_path = tmp_dir / f"part_{part_idx:05d}.wav"

                wav_bytes = gemini_tts_wav(sc, voice=voice, api_key=api_key)
                wav_path.write_bytes(wav_bytes)
                wav_parts.append(wav_path)

                # small gap between subchunks to reduce "run-on"
                if TTS_TURN_GAP_MS > 0:
                    part_idx += 1
                    gap_path = tmp_dir / f"part_{part_idx:05d}_gap.wav"
                    write_silence_wav(gap_path, TTS_TURN_GAP_MS)
                    wav_parts.append(gap_path)

        if not wav_parts:
            raise RuntimeError("TTS produced no audio parts.")

        out_wav = tmp_dir / "final.wav"
        concat_wavs(wav_parts, out_wav)

        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        wav_to_mp3(out_wav, mp3_path)

    finally:
        # Cleanup temp files
        try:
            for p in tmp_dir.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
            try:
                tmp_dir.rmdir()
            except Exception:
                pass
        except Exception:
            pass
