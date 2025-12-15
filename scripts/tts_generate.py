#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agenda Podcast — TTS generator (Gemini premium + Piper fallback)

Key features:
- Two-voice dialogue (A/B)
- Topic flag: premium_tts true/false selects Gemini vs Piper
- Speed: caching, one-pass concat+encode, optional silence trimming
- Reliability: retries, quota fallback to Piper

Runtime deps:
- Python: requests
- ffmpeg in PATH
- Piper CLI in PATH (installed via pip: piper-tts OR piper)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import requests

# -----------------------------
# Defaults / ENV
# -----------------------------
DEFAULT_GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip()
DEFAULT_VOICE_A = os.getenv("VOICE_A", "Kore").strip()   # Gemini female-ish
DEFAULT_VOICE_B = os.getenv("VOICE_B", "Puck").strip()   # Gemini male-ish

# Piper voices (downloaded on demand)
DEFAULT_PIPER_VOICE_A = os.getenv("PIPER_VOICE_A", "en_US-hfc_female-medium").strip()
DEFAULT_PIPER_VOICE_B = os.getenv("PIPER_VOICE_B", "en_US-hfc_male-medium").strip()

# Chunking
TTS_MAX_CHARS_PER_CHUNK = int(os.getenv("TTS_MAX_CHARS_PER_CHUNK", "3200"))
TTS_TURN_GAP_MS = int(os.getenv("TTS_TURN_GAP_MS", "200"))

# Caching / IO
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "data/tts_cache")).resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Piper model cache
PIPER_VOICES_DIR = Path(os.getenv("PIPER_VOICES_DIR", "data/piper_voices")).resolve()
PIPER_VOICES_DIR.mkdir(parents=True, exist_ok=True)

# Optional: trim silence from each turn to speed up final concat and reduce dead air
ENABLE_SILENCE_TRIM = (os.getenv("TTS_SILENCE_TRIM", "true").strip().lower() in ("1", "true", "yes", "y"))

# If premium but Gemini quota is hit, fallback to Piper unless explicitly disabled
ALLOW_FALLBACK_TO_PIPER = (os.getenv("TTS_ALLOW_FALLBACK_TO_PIPER", "true").strip().lower() in ("1", "true", "yes", "y"))

# Gemini REST endpoint for generateContent (per docs)
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Piper voices repo (common source used by Piper ecosystem)
# We download: .onnx and .onnx.json
PIPER_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


# -----------------------------
# Data model
# -----------------------------
@dataclass
class TTSTurn:
    speaker: str  # "A" or "B"
    text: str


# -----------------------------
# Utilities
# -----------------------------
def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _safe_print(msg: str) -> None:
    print(msg, flush=True)


def _which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def _ffmpeg_available() -> bool:
    return _which("ffmpeg") is not None


def _ensure_ffmpeg() -> None:
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg not found in PATH. Install it in workflow: apt-get install -y ffmpeg")


# -----------------------------
# Dialogue parsing
# -----------------------------
_SPEAKER_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(A|B)\s*[:\-–]\s*|"
    r"(HOST|Host|host)\s*[:\-–]\s*|"
    r"(GUEST|Guest|guest)\s*[:\-–]\s*|"
    r"(Male|Female)\s*[:\-–]\s*"
    r")(.+?)\s*$"
)

def parse_dialogue(script_text: str) -> List[TTSTurn]:
    """
    Robustly parse dialogue turns.

    Accepted formats:
      A: ...
      B: ...
      Host: ...
      Guest: ...
      Male: ...
      Female: ...

    If no markers found, fallback:
      - Split into paragraphs; alternate A/B.
    """
    if not script_text or not script_text.strip():
        return []

    lines = [ln.rstrip() for ln in script_text.splitlines()]
    turns: List[TTSTurn] = []
    current_speaker: Optional[str] = None
    current_buf: List[str] = []

    def flush():
        nonlocal current_speaker, current_buf
        if current_speaker and current_buf:
            text = " ".join([x.strip() for x in current_buf if x.strip()]).strip()
            if text:
                turns.append(TTSTurn(speaker=current_speaker, text=text))
        current_speaker = None
        current_buf = []

    for ln in lines:
        m = _SPEAKER_LINE_RE.match(ln)
        if m:
            # new speaker line
            flush()
            a, host, guest, mf, content = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            if a in ("A", "B"):
                current_speaker = a
            elif host:
                current_speaker = "A"
            elif guest:
                current_speaker = "B"
            elif mf:
                current_speaker = "A" if mf.lower() == "male" else "B"
            else:
                current_speaker = "A"
            current_buf.append(content.strip())
        else:
            # continuation
            if current_speaker:
                if ln.strip():
                    current_buf.append(ln.strip())
            else:
                # ignore leading junk until a speaker marker appears
                pass

    flush()

    if turns:
        return turns

    # Fallback: paragraphs alternating A/B
    paras = [p.strip() for p in re.split(r"\n\s*\n+", script_text.strip()) if p.strip()]
    if not paras:
        # ultimate fallback: split by sentences
        sents = [s.strip() for s in re.split(r"(?<=[\.\!\?])\s+", script_text.strip()) if s.strip()]
        paras = sents

    turns = []
    speaker = "A"
    for p in paras:
        turns.append(TTSTurn(speaker=speaker, text=p))
        speaker = "B" if speaker == "A" else "A"
    return turns


def split_turn_into_chunks(text: str, max_chars: int) -> List[str]:
    """
    Split long turn into smaller chunks without breaking words too aggressively.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= max_chars:
        return [t] if t else []

    chunks: List[str] = []
    start = 0
    while start < len(t):
        end = min(start + max_chars, len(t))
        if end < len(t):
            # try to break on last punctuation/space
            window = t[start:end]
            cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "), window.rfind("; "), window.rfind(", "), window.rfind(" "))
            if cut > 200:  # avoid super tiny cut
                end = start + cut + 1
        chunk = t[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


# -----------------------------
# Topic integration
# -----------------------------
def topic_is_premium(topic_json_path: str | Path) -> bool:
    """
    Reads topics/topic-XX.json and returns premium flag.
    Expected field: "premium_tts": true/false
    Default: true (premium) if missing.
    """
    p = Path(topic_json_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        val = data.get("premium_tts", True)
        return bool(val)
    except Exception:
        return True


# -----------------------------
# Caching
# -----------------------------
def _cache_path(engine: str, voice: str, text: str, ext: str) -> Path:
    key = _sha1(f"{engine}|{voice}|{text}")
    d = CACHE_DIR / engine / voice
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.{ext}"


# -----------------------------
# Gemini TTS (generateContent AUDIO)
# -----------------------------
def _gemini_generate_pcm(
    *,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    retries: int = 4,
    timeout_s: int = 120,
) -> bytes:
    """
    Calls Gemini generateContent with responseModalities AUDIO.
    Returns raw PCM bytes (s16le, 24000Hz, mono) per docs.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty")

    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
        "model": model,
    }

    last_err = None
    backoff = 2.0
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
            if r.status_code >= 400:
                # Attempt to parse error
                try:
                    j = r.json()
                except Exception:
                    j = None

                # Common quota cases
                if r.status_code in (401, 403, 429):
                    last_err = RuntimeError(f"Gemini TTS HTTP {r.status_code}: {str(j)[:500]}")
                    raise last_err

                raise RuntimeError(f"Gemini TTS HTTP {r.status_code}: {r.text[:500]}")

            data_b64 = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            return base64.b64decode(data_b64)
        except Exception as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff)
            backoff *= 1.8

    raise RuntimeError(f"TTS failed after retries: {last_err}")


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """
    Wrap PCM in WAV using Python's wave module.
    """
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # s16le
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _gemini_tts_wav_cached(api_key: str, model: str, voice: str, text: str) -> Path:
    """
    Returns path to cached WAV for (voice,text).
    """
    out = _cache_path("gemini", voice, text, "wav")
    if out.exists() and out.stat().st_size > 1000:
        return out

    pcm = _gemini_generate_pcm(api_key=api_key, model=model, voice=voice, text=text)
    wav_bytes = _pcm_to_wav_bytes(pcm, sample_rate=24000)
    out.write_bytes(wav_bytes)
    return out


# -----------------------------
# Piper TTS
# -----------------------------
def _piper_download_voice(voice_id: str) -> Tuple[Path, Path]:
    """
    Download Piper voice files into PIPER_VOICES_DIR if missing.
    voice_id examples:
      en_US-hfc_female-medium
      en_US-hfc_male-medium

    We map it to HF repo paths:
      .../en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx
      .../en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx.json
    """
    # Parse voice_id
    m = re.match(r"^(?P<locale>[a-z]{2}_[A-Z]{2})-(?P<name>.+)-(?P<quality>low|medium|high|x_low|x_high)$", voice_id)
    if not m:
        raise RuntimeError(f"Unsupported Piper voice id format: {voice_id}")

    locale = m.group("locale")
    name = m.group("name")
    quality = m.group("quality")

    # Many Piper voices are stored under language = first two letters
    lang = locale.split("_")[0]  # "en"
    # Construct relative path
    rel_dir = f"{lang}/{locale}/{name}/{quality}"
    onnx_name = f"{locale}-{name}-{quality}.onnx"
    json_name = f"{onnx_name}.json"

    out_dir = PIPER_VOICES_DIR / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    onnx_path = out_dir / onnx_name
    json_path = out_dir / json_name

    if onnx_path.exists() and json_path.exists() and onnx_path.stat().st_size > 10_000 and json_path.stat().st_size > 100:
        return onnx_path, json_path

    base_url = f"{PIPER_VOICES_BASE}/{rel_dir}"
    onnx_url = f"{base_url}/{onnx_name}"
    js_url = f"{base_url}/{json_name}"

    _safe_print(f"[piper] downloading voice {voice_id} ...")
    r1 = requests.get(onnx_url, timeout=180)
    r1.raise_for_status()
    onnx_path.write_bytes(r1.content)

    r2 = requests.get(js_url, timeout=60)
    r2.raise_for_status()
    json_path.write_bytes(r2.content)

    return onnx_path, json_path


def _piper_synthesize_wav(voice_id: str, text: str) -> Path:
    """
    Uses Piper CLI to synthesize wav (cached).
    Requires 'piper' binary in PATH (pip install piper-tts usually provides it).
    """
    piper_bin = _which("piper")
    if not piper_bin:
        raise RuntimeError("Piper CLI not found. Add to workflow: pip install piper-tts (or pip install piper)")

    out = _cache_path("piper", voice_id, text, "wav")
    if out.exists() and out.stat().st_size > 1000:
        return out

    onnx_path, _json_path = _piper_download_voice(voice_id)

    # Piper CLI reads text from stdin
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cmd = [
        piper_bin,
        "--model", str(onnx_path),
        "--output_file", str(tmp_path),
    ]

    proc = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size < 1000:
        raise RuntimeError(f"Piper synthesis failed: {proc.stderr.decode('utf-8', errors='ignore')[:800]}")

    out.write_bytes(tmp_path.read_bytes())
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return out


# -----------------------------
# Audio post-processing
# -----------------------------
def _trim_silence(in_wav: Path) -> Path:
    """
    Trims leading/trailing silence using ffmpeg silenceremove.
    If trimming disabled, returns original.
    """
    if not ENABLE_SILENCE_TRIM:
        return in_wav

    out = in_wav.with_suffix(".trim.wav")
    if out.exists() and out.stat().st_size > 1000:
        return out

    _ensure_ffmpeg()

    # Conservative trimming to avoid cutting consonants
    # remove < -45dB silence longer than 0.20s at start/end
    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_wav),
        "-af", "silenceremove=start_periods=1:start_duration=0.20:start_threshold=-45dB:"
               "stop_periods=1:stop_duration=0.25:stop_threshold=-45dB",
        str(out)
    ]
    p = _run(cmd, check=False)
    if p.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        # If trimming fails, fallback to original
        return in_wav
    return out


def _concat_wavs_to_mp3(wavs: List[Path], mp3_out: Path, gap_ms: int = 0) -> None:
    """
    Concatenate wavs into final mp3, optionally inserting silence gaps.
    Uses ffmpeg concat demuxer for speed.
    """
    _ensure_ffmpeg()
    mp3_out.parent.mkdir(parents=True, exist_ok=True)

    # Optional gap: generate one silence wav and reuse
    silence_wav = None
    if gap_ms and gap_ms > 0:
        silence_wav = mp3_out.with_suffix(".gap.wav")
        if not silence_wav.exists():
            dur = gap_ms / 1000.0
            cmd_sil = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r=24000:cl=mono",
                "-t", f"{dur:.3f}",
                str(silence_wav)
            ]
            _run(cmd_sil, check=True)

    # Build concat file
    concat_txt = mp3_out.with_suffix(".concat.txt")
    lines = []
    for w in wavs:
        lines.append(f"file '{w.as_posix()}'")
        if silence_wav:
            lines.append(f"file '{silence_wav.as_posix()}'")
    concat_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Concat to wav then encode once to mp3 (faster than mp3 concat)
    out_wav = mp3_out.with_suffix(".all.wav")
    cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(out_wav)]
    p = _run(cmd_concat, check=False)

    if p.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 1000:
        # Fallback: re-encode concat directly (slower but robust)
        cmd_concat2 = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), str(out_wav)]
        _run(cmd_concat2, check=True)

    cmd_mp3 = [
        "ffmpeg", "-y",
        "-i", str(out_wav),
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(mp3_out)
    ]
    _run(cmd_mp3, check=True)

    # Cleanup temp concat artifacts
    for pth in [concat_txt, out_wav, silence_wav]:
        if pth and isinstance(pth, Path):
            try:
                pth.unlink(missing_ok=True)
            except Exception:
                pass


# -----------------------------
# Public API used by run_topic.py
# -----------------------------
def tts_chunks_to_mp3(
    tts_chunks: List[dict] | List[TTSTurn],
    mp3_path: str | Path,
    api_key: str,
    *,
    premium: bool = True,
    gemini_model: str | None = None,
    voice_a: str | None = None,
    voice_b: str | None = None,
    piper_voice_a: str | None = None,
    piper_voice_b: str | None = None,
) -> Path:
    """
    Generate MP3 from chunks. Each chunk should be {speaker:"A"/"B", text:"..."}.

    premium=True  => Gemini TTS
    premium=False => Piper

    Auto-fallback: if premium and Gemini quota exhausted, uses Piper (unless disabled).
    """
    mp3_out = Path(mp3_path)
    gemini_model = (gemini_model or DEFAULT_GEMINI_TTS_MODEL).strip()
    voice_a = (voice_a or DEFAULT_VOICE_A).strip()
    voice_b = (voice_b or DEFAULT_VOICE_B).strip()
    piper_voice_a = (piper_voice_a or DEFAULT_PIPER_VOICE_A).strip()
    piper_voice_b = (piper_voice_b or DEFAULT_PIPER_VOICE_B).strip()

    # Normalize input
    turns: List[TTSTurn] = []
    for ch in tts_chunks:
        if isinstance(ch, TTSTurn):
            turns.append(ch)
        elif isinstance(ch, dict):
            sp = str(ch.get("speaker", "A")).strip().upper()
            sp = "B" if sp == "B" else "A"
            txt = str(ch.get("text", "")).strip()
            if txt:
                turns.append(TTSTurn(speaker=sp, text=txt))

    if not turns:
        raise RuntimeError("No dialogue turns to synthesize.")

    # Chunk long turns
    expanded: List[TTSTurn] = []
    for t in turns:
        for piece in split_turn_into_chunks(t.text, TTS_MAX_CHARS_PER_CHUNK):
            expanded.append(TTSTurn(speaker=t.speaker, text=piece))

    wavs: List[Path] = []

    def synth_one(turn: TTSTurn) -> Path:
        if premium:
            # Gemini voice per speaker
            voice = voice_a if turn.speaker == "A" else voice_b
            wav = _gemini_tts_wav_cached(api_key=api_key, model=gemini_model, voice=voice, text=turn.text)
            return wav
        else:
            # Piper voice per speaker
            v = piper_voice_a if turn.speaker == "A" else piper_voice_b
            return _piper_synthesize_wav(v, turn.text)

    # Synthesis loop with fallback
    fell_back = False
    for i, t in enumerate(expanded, 1):
        try:
            wav = synth_one(t)
        except Exception as e:
            msg = str(e).lower()

            quota_like = (
                "resource_exhausted" in msg
                or "quota" in msg
                or "limit" in msg
                or "429" in msg
                or "403" in msg
            )

            if premium and ALLOW_FALLBACK_TO_PIPER and quota_like:
                if not fell_back:
                    _safe_print("[tts] Gemini quota/limit detected — falling back to Piper for remaining chunks.")
                    fell_back = True
                # switch to Piper for the rest
                prem_before = premium
                premium = False
                try:
                    wav = synth_one(t)
                except Exception as e2:
                    raise RuntimeError(f"TTS failed (Gemini quota + Piper fallback failed): {e2}") from e2
                finally:
                    # keep premium False once we fallback
                    pass
            else:
                raise

        wavs.append(_trim_silence(wav))
        if i % 10 == 0:
            _safe_print(f"[tts] synthesized {i}/{len(expanded)} chunks")

    _concat_wavs_to_mp3(wavs, mp3_out, gap_ms=TTS_TURN_GAP_MS)
    _safe_print(f"[tts] mp3 ready: {mp3_out} (chunks={len(expanded)}, fallback={'yes' if fell_back else 'no'})")
    return mp3_out


def script_to_tts_chunks(script_text: str) -> List[dict]:
    """
    Helper: parse dialogue from script text and return list[dict] chunks.
    """
    turns = parse_dialogue(script_text)
    return [{"speaker": t.speaker, "text": t.text} for t in turns]


# -----------------------------
# CLI (optional)
# -----------------------------
def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="Path to a script text file")
    ap.add_argument("--out", required=True, help="Output mp3 file path")
    ap.add_argument("--topic", default="", help="Optional topic json path to read premium_tts")
    ap.add_argument("--premium", default="", help="Override premium true/false (optional)")
    ap.add_argument("--api-key", default=os.getenv("GEMINI_API_KEY", ""), help="Gemini API key")
    args = ap.parse_args()

    script_text = Path(args.script).read_text(encoding="utf-8")

    premium = True
    if args.topic:
        premium = topic_is_premium(args.topic)
    if args.premium.strip():
        premium = args.premium.strip().lower() in ("1", "true", "yes", "y")

    chunks = script_to_tts_chunks(script_text)
    tts_chunks_to_mp3(chunks, args.out, api_key=args.api_key, premium=premium)


if __name__ == "__main__":
    _cli()
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
