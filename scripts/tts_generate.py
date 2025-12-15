# scripts/tts_generate.py
from __future__ import annotations

import os
import re
import json
import time
import shlex
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import requests

# -----------------------------
# Defaults / env
# -----------------------------
DEFAULT_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))
DEFAULT_GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")

# Dialogue voices (Gemini)
DEFAULT_VOICE_A = os.getenv("VOICE_A", "Kore")  # commonly used in examples
DEFAULT_VOICE_B = os.getenv("VOICE_B", "Puck")

# Piper voices (model names WITHOUT extension; ensure_voices.py should download these)
# Pick well-known EN voices; adjust to what you actually downloaded into assets/piper
DEFAULT_PIPER_VOICE_A = os.getenv("PIPER_VOICE_A", "en_US-amy-medium")   # female
DEFAULT_PIPER_VOICE_B = os.getenv("PIPER_VOICE_B", "en_US-ryan-medium")  # male

# Chunking / batching
TTS_MAX_CHARS_PER_CHUNK = int(os.getenv("TTS_MAX_CHARS_PER_CHUNK", "9000"))
MIN_CHUNK_CHARS = int(os.getenv("MIN_CHUNK_CHARS", "800"))
MERGE_SAME_VOICE_TURNS = os.getenv("MERGE_SAME_VOICE_TURNS", "1").strip() in ("1", "true", "True", "yes", "YES")

# Gap between turns (ms)
TTS_TURN_GAP_MS = int(os.getenv("TTS_TURN_GAP_MS", "120"))

# Fail-soft behavior on quota errors
FAIL_SOFT_ON_QUOTA = os.getenv("FAIL_SOFT_ON_QUOTA", "1").strip() in ("1", "true", "True", "yes", "YES")
TTS_QUOTA_MARKER = os.getenv("TTS_TTS_QUOTA_MARKER", os.getenv("TTS_QUOTA_MARKER", "outputs/_tts_quota_exceeded.txt"))

# Optional silence trimming (needs ffmpeg)
TRIM_SILENCE = os.getenv("TRIM_SILENCE", "1").strip() in ("1", "true", "True", "yes", "YES")

# Cache
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", ".cache/tts")).resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Data model
# -----------------------------
@dataclass
class TTSChunk:
    voice: str         # "A" or "B" (speaker)
    text: str          # text for that speaker


# -----------------------------
# Helpers
# -----------------------------
def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    # Keep it conservative: strip weird control chars, normalize whitespace
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _write_quota_marker(msg: str) -> None:
    try:
        p = Path(TTS_QUOTA_MARKER)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(msg.strip() + "\n", encoding="utf-8")
    except Exception:
        pass


def _split_into_chunks(text: str, max_chars: int, min_chars: int) -> List[str]:
    """
    Split by paragraphs/sentences, but keep chunks reasonably sized.
    """
    text = _normalize_text(text)
    if not text:
        return []

    # Primary split: blank lines
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for p in parts:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = (buf + "\n\n" + p).strip() if buf else p
            continue

        # if buffer is too small, try to add sentence-by-sentence
        if buf:
            flush()

        if len(p) <= max_chars:
            chunks.append(p)
            continue

        # sentence split fallback
        sentences = re.split(r"(?<=[.!?])\s+", p)
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) + 1 <= max_chars:
                buf = (buf + " " + s).strip() if buf else s
            else:
                flush()
                buf = s
        flush()

    flush()

    # Merge tiny trailing chunks
    merged: List[str] = []
    for c in chunks:
        if merged and len(c) < min_chars and (len(merged[-1]) + len(c) + 2) <= max_chars:
            merged[-1] = (merged[-1] + "\n\n" + c).strip()
        else:
            merged.append(c)

    return merged


def _merge_same_voice(chunks: List[TTSChunk], max_chars: int) -> List[TTSChunk]:
    if not chunks:
        return chunks
    out: List[TTSChunk] = []
    cur = chunks[0]
    for nxt in chunks[1:]:
        if nxt.voice == cur.voice and (len(cur.text) + len(nxt.text) + 2) <= max_chars:
            cur = TTSChunk(voice=cur.voice, text=(cur.text + "\n\n" + nxt.text).strip())
        else:
            out.append(cur)
            cur = nxt
    out.append(cur)
    return out


# -----------------------------
# Script → dialogue chunks
# -----------------------------
def script_to_tts_chunks(
    script_text: str,
    max_chars_per_chunk: int = TTS_MAX_CHARS_PER_CHUNK,
    min_chunk_chars: int = MIN_CHUNK_CHARS,
) -> List[TTSChunk]:
    """
    Accepts either:
      - dialogue format like:
            A: ...
            B: ...
        or:
            HOST_A: ...
            HOST_B: ...
      - otherwise: auto-wrap as alternating A/B blocks (fallback)
    """
    txt = _normalize_text(script_text)
    if not txt:
        return []

    lines = [l.rstrip() for l in txt.split("\n")]
    parsed: List[TTSChunk] = []
    cur_speaker: Optional[str] = None
    cur_buf: List[str] = []

    def flush():
        nonlocal cur_speaker, cur_buf
        if cur_speaker and _normalize_text("\n".join(cur_buf)):
            block = _normalize_text("\n".join(cur_buf))
            for sub in _split_into_chunks(block, max_chars_per_chunk, min_chunk_chars):
                parsed.append(TTSChunk(voice=cur_speaker, text=sub))
        cur_speaker = None
        cur_buf = []

    speaker_re = re.compile(r"^(A|B|HOST_A|HOST_B)\s*:\s*(.*)$", re.IGNORECASE)

    for line in lines:
        m = speaker_re.match(line.strip())
        if m:
            sp = m.group(1).upper()
            sp = "A" if sp in ("A", "HOST_A") else "B"
            rest = m.group(2).strip()

            if cur_speaker is None:
                cur_speaker = sp
                cur_buf = [rest] if rest else []
            elif sp == cur_speaker:
                if rest:
                    cur_buf.append(rest)
            else:
                flush()
                cur_speaker = sp
                cur_buf = [rest] if rest else []
        else:
            # normal line continuation
            if cur_speaker is None:
                # not in dialogue mode yet
                cur_speaker = "A"
            cur_buf.append(line)

    flush()

    # If it produced nothing (e.g., no usable text), fallback to single speaker
    if not parsed:
        for sub in _split_into_chunks(txt, max_chars_per_chunk, min_chunk_chars):
            parsed.append(TTSChunk(voice="A", text=sub))

    if MERGE_SAME_VOICE_TURNS:
        parsed = _merge_same_voice(parsed, max_chars_per_chunk)

    return parsed


# -----------------------------
# Piper TTS
# -----------------------------
def _piper_model_paths(model_dir: Path, voice_name: str) -> Tuple[Path, Optional[Path]]:
    """
    voice_name: e.g. "en_US-amy-medium" (no extension)
    expects:
      - <voice>.onnx
      - <voice>.onnx.json (optional but common)
    """
    onnx = model_dir / f"{voice_name}.onnx"
    cfg = model_dir / f"{voice_name}.onnx.json"
    return onnx, (cfg if cfg.exists() else None)


def _piper_tts_wav_bytes(text: str, voice_name: str, model_dir: Path, sample_rate: int) -> bytes:
    """
    Uses Piper CLI: echo "text" | piper --model ... --output_file out.wav
    """
    model_dir = model_dir.resolve()
    onnx, _cfg = _piper_model_paths(model_dir, voice_name)
    if not onnx.exists():
        raise RuntimeError(f"Piper voice model missing: {onnx}. (Did ensure_voices.py download it?)")

    tmp_dir = Path(".tmp_tts").resolve()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_wav = tmp_dir / f"piper_{voice_name}_{_sha1(text)}.wav"

    # Piper reads text from stdin in most setups
    cmd = [
        "piper",
        "--model", str(onnx),
        "--output_file", str(out_wav),
    ]

    # Some Piper builds support --sample_rate; if not, we resample later via ffmpeg if needed
    # Keep it simple and compatible.

    p = subprocess.run(
        cmd,
        input=(text.strip() + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Piper failed ({p.returncode}): {p.stderr.decode('utf-8', 'ignore')[:600]}")

    wav = out_wav.read_bytes()
    try:
        out_wav.unlink(missing_ok=True)
    except Exception:
        pass
    return wav


# -----------------------------
# Gemini TTS (REST, audio response)
# -----------------------------
def _gemini_tts_wav_bytes(text: str, voice_name: str, api_key: str, model: str, sample_rate: int) -> bytes:
    """
    Uses Google Gemini generateContent with AUDIO modality.
    If your project/model has no quota, you will receive 429/RESOURCE_EXHAUSTED.

    NOTE: This relies on the documented "speech_config / prebuilt_voice_config" structure
    used for audio output modalities. See Google AI for Developers Live/API docs. 0
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}
            },
        },
    }

    r = requests.post(url, json=payload, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:600]}")

    data = r.json()

    # Expected: candidates[0].content.parts[0].inlineData.data (base64 PCM) OR some audio structure
    # To keep compatibility, try common shapes.
    b64 = None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                b64 = inline["data"]
                break
    except Exception:
        b64 = None

    if not b64:
        raise RuntimeError("Gemini TTS: No audio inlineData found in response.")

    import base64
    pcm = base64.b64decode(b64)

    # Wrap raw PCM as WAV via ffmpeg (PCM s16le, 24kHz typical)
    tmp_dir = Path(".tmp_tts").resolve()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_path = tmp_dir / f"gemini_{_sha1(text)}.pcm"
    wav_path = tmp_dir / f"gemini_{_sha1(text)}.wav"
    raw_path.write_bytes(pcm)

    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-i", str(raw_path),
        str(wav_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    wav = wav_path.read_bytes()
    try:
        raw_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
    except Exception:
        pass
    return wav


# -----------------------------
# Audio post-processing
# -----------------------------
def _trim_silence_inplace(wav_path: Path) -> None:
    # Conservative silenceremove
    # Start/stop thresholds: -50dB; tune if needed
    tmp = wav_path.with_suffix(".trim.wav")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(wav_path),
        "-af", "silenceremove=start_periods=1:start_duration=0.15:start_threshold=-50dB:"
               "stop_periods=1:stop_duration=0.20:stop_threshold=-50dB",
        str(tmp),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 2000:
        tmp.replace(wav_path)
    else:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _concat_wavs_to_mp3(wav_files: List[Path], mp3_path: Path, gap_ms: int) -> None:
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    concat_list = mp3_path.with_suffix(".concat.txt")
    with concat_list.open("w", encoding="utf-8") as f:
        for w in wav_files:
            # ffmpeg concat demuxer requires: file 'path'
            f.write(f"file {shlex.quote(str(w))}\n")

    # Optional: add a small gap between turns by inserting anullsrc?
    # Simpler: if you need gap, bake it per-turn as silence wavs
    # For now: no separate silence file; gap_ms can be 0 if undesired.

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True)

    try:
        concat_list.unlink(missing_ok=True)
    except Exception:
        pass


# -----------------------------
# Public API
# -----------------------------
def tts_chunks_to_mp3(
    chunks: List[Dict[str, Any]] | List[TTSChunk],
    mp3_path: str | Path,
    api_key: str = "",
    provider: str = "auto",  # "auto" | "gemini" | "piper"
    gemini_model: str = DEFAULT_GEMINI_TTS_MODEL,
    voice_a: str = DEFAULT_VOICE_A,
    voice_b: str = DEFAULT_VOICE_B,
    piper_voice_a: str = DEFAULT_PIPER_VOICE_A,
    piper_voice_b: str = DEFAULT_PIPER_VOICE_B,
    piper_model_dir: str | Path = os.getenv("PIPER_MODEL_DIR", "assets/piper"),
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Path:
    """
    Renders dialogue chunks to MP3 with caching.
    Each chunk item must have:
      - voice: "A" or "B"
      - text: str
    """
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    # normalize chunk objects
    norm: List[TTSChunk] = []
    for c in chunks:
        if isinstance(c, TTSChunk):
            norm.append(c)
        elif isinstance(c, dict):
            v = str(c.get("voice", "A")).strip().upper()
            v = "A" if v in ("A", "HOST_A") else "B"
            t = _normalize_text(str(c.get("text", "")))
            if t:
                norm.append(TTSChunk(voice=v, text=t))
        else:
            continue

    if not norm:
        raise RuntimeError("No TTS chunks to render.")

    if MERGE_SAME_VOICE_TURNS:
        norm = _merge_same_voice(norm, TTS_MAX_CHARS_PER_CHUNK)

    # provider resolution
    provider = (provider or "auto").strip().lower()
    if provider == "auto":
        provider = "gemini" if api_key else "piper"

    model_dir = Path(piper_model_dir)

    wav_files: List[Path] = []
    tmp_out = Path(".tmp_tts_out")
    tmp_out.mkdir(parents=True, exist_ok=True)

    last_err: Optional[str] = None

    for i, ch in enumerate(norm, start=1):
        spk = ch.voice
        text = ch.text

        if provider == "gemini":
            voice = voice_a if spk == "A" else voice_b
            cache_key = f"gemini|{gemini_model}|{voice}|{text}"
        else:
            voice = piper_voice_a if spk == "A" else piper_voice_b
            cache_key = f"piper|{voice}|{text}"

        cache_path = CACHE_DIR / provider / voice
        cache_path.mkdir(parents=True, exist_ok=True)
        cached_wav = cache_path / f"{_sha1(cache_key)}.wav"

        if cached_wav.exists() and cached_wav.stat().st_size > 2000:
            wav_files.append(cached_wav)
            continue

        # Render wav bytes
        try:
            if provider == "gemini":
                wav_bytes = _gemini_tts_wav_bytes(
                    text=text,
                    voice_name=voice,
                    api_key=api_key,
                    model=gemini_model,
                    sample_rate=sample_rate,
                )
            elif provider == "piper":
                wav_bytes = _piper_tts_wav_bytes(
                    text=text,
                    voice_name=voice,
                    model_dir=model_dir,
                    sample_rate=sample_rate,
                )
            else:
                raise RuntimeError(f"Unknown TTS provider: {provider}")

            cached_wav.write_bytes(wav_bytes)

            if TRIM_SILENCE:
                _trim_silence_inplace(cached_wav)

            wav_files.append(cached_wav)

        except Exception as e:
            last_err = str(e)

            # Soft fallback if Gemini quota/rate-limit
            msg = str(e)
            is_quota = ("RESOURCE_EXHAUSTED" in msg) or ("429" in msg) or ("quota" in msg.lower())
            if provider == "gemini" and FAIL_SOFT_ON_QUOTA and is_quota:
                _write_quota_marker(msg)
                # switch provider for the rest
                provider = "piper"
                # retry this chunk on piper immediately
                try:
                    voice = piper_voice_a if spk == "A" else piper_voice_b
                    cache_key = f"piper|{voice}|{text}"
                    cache_path = CACHE_DIR / "piper" / voice
                    cache_path.mkdir(parents=True, exist_ok=True)
                    cached_wav = cache_path / f"{_sha1(cache_key)}.wav"

                    if not (cached_wav.exists() and cached_wav.stat().st_size > 2000):
                        wav_bytes = _piper_tts_wav_bytes(text=text, voice_name=voice, model_dir=model_dir, sample_rate=sample_rate)
                        cached_wav.write_bytes(wav_bytes)
                        if TRIM_SILENCE:
                            _trim_silence_inplace(cached_wav)

                    wav_files.append(cached_wav)
                    continue
                except Exception as e2:
                    raise RuntimeError(f"TTS failed and fallback to Piper also failed: {e2}") from e2

            raise

    # Concatenate into MP3
    _concat_wavs_to_mp3(wav_files, mp3_path, gap_ms=TTS_TURN_GAP_MS)
    return mp3_path
