#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# ---------------------------
# Dialogue parsing
# ---------------------------
_SPEAKER_RE = re.compile(r"^\s*(?P<who>[ABab]|host|speaker\s*[12]|male|female)\s*:\s*(?P<text>.+?)\s*$")
_MD_SPEAKER_RE = re.compile(r"^\s*\*\*(?P<who>[^*]+)\*\*\s*:\s*(?P<text>.+?)\s*$")


@dataclass
class TTSTurn:
    speaker: str  # "A" or "B"
    text: str


def _norm_speaker(who: str) -> str:
    w = (who or "").strip().lower()
    if w in ("a", "host", "speaker1", "speaker 1", "male"):
        return "A"
    if w in ("b", "speaker2", "speaker 2", "female"):
        return "B"
    # default
    return "A"


def script_to_tts_chunks(script_text: str) -> List[Dict[str, str]]:
    """
    Robust parser:
    - Accepts lines like "A: ..." / "B: ..."
    - If text is untagged, it is NOT dropped; it attaches to last speaker (or A).
    """
    text = (script_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    turns: List[TTSTurn] = []
    lines = [ln.rstrip() for ln in text.split("\n")]

    buf: List[str] = []
    cur_speaker: Optional[str] = None
    last_speaker: Optional[str] = None

    def flush() -> None:
        nonlocal buf, cur_speaker, last_speaker, turns
        if cur_speaker and buf:
            joined = " ".join([b.strip() for b in buf if b.strip()]).strip()
            if joined:
                turns.append(TTSTurn(cur_speaker, joined))
                last_speaker = cur_speaker
        buf = []

    for ln in lines:
        s = ln.strip()
        if not s:
            if cur_speaker:
                flush()
                cur_speaker = None
            continue

        m = _SPEAKER_RE.match(s)
        mm = _MD_SPEAKER_RE.match(s) if not m else None

        who = None
        body = None
        if m:
            who = _norm_speaker(m.group("who"))
            body = m.group("text")
        elif mm:
            who = _norm_speaker(mm.group("who"))
            body = mm.group("text")

        if who and body:
            if cur_speaker:
                flush()
            cur_speaker = who
            buf = [body]
        else:
            # CRITICAL FIX: do not drop untagged text
            if cur_speaker is None:
                cur_speaker = last_speaker or "A"
            buf.append(s)

    if cur_speaker:
        flush()

    if not turns:
        # fallback: alternate by paragraph
        paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        speaker = "A"
        for p in paras:
            turns.append(TTSTurn(speaker, p))
            speaker = "B" if speaker == "A" else "A"

    return [{"speaker": t.speaker, "text": t.text.strip()} for t in turns if t.text.strip()]


# ---------------------------
# Audio helpers
# ---------------------------
def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _run(cmd: List[str]) -> None:
    subprocess.check_call(cmd)


def _ffmpeg_trim_silence(in_wav: str, out_wav: str) -> None:
    # light trimming (safe)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            in_wav,
            "-af",
            "silenceremove=start_periods=1:start_duration=0.25:start_threshold=-50dB:"
            "stop_periods=1:stop_duration=0.25:stop_threshold=-50dB",
            out_wav,
        ]
    )


def _make_gap_wav(path: str, ms: int = 120) -> None:
    sec = max(0.02, ms / 1000.0)
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{sec:.3f}", path])


def _concat_wavs_to_mp3(wavs: List[str], out_mp3: str) -> None:
    # concat demuxer for audio
    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / "list.txt"
        lst.write_text("\n".join([f"file '{w.replace(\"'\", \"'\\\\''\")}'" for w in wavs]) + "\n", encoding="utf-8")

        tmp_wav = str(Path(td) / "merged.wav")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", tmp_wav])

        # encode mp3 (VBR-ish)
        _run(["ffmpeg", "-y", "-i", tmp_wav, "-codec:a", "libmp3lame", "-q:a", "3", out_mp3])


# ---------------------------
# Piper TTS
# ---------------------------
DEFAULT_PIPER_A = "en_US-ryan-medium"  # male
DEFAULT_PIPER_B = "en_US-amy-medium"   # female


def _resolve_piper_model(model_dir: str, voice: str) -> Path:
    d = Path(model_dir)
    if not d.exists():
        raise RuntimeError(f"PIPER_MODEL_DIR does not exist: {model_dir}")

    v = (voice or "").strip()
    if not v:
        raise RuntimeError("Piper voice is empty")

    # expected: <voice>.onnx and <voice>.onnx.json
    model = d / f"{v}.onnx"
    cfg = d / f"{v}.onnx.json"

    if not model.exists():
        # try: maybe user passed full filename
        if v.endswith(".onnx"):
            model = d / v
            cfg = d / f"{Path(v).name}.json"  # fallback
    if not model.exists():
        raise RuntimeError(f"Piper model missing: {model}")

    # Config naming in piper is typically "<model>.json" => "<voice>.onnx.json"
    if not cfg.exists():
        # attempt alternate: "<voice>.json"
        alt = d / f"{v}.json"
        if alt.exists():
            cfg = alt
        else:
            raise RuntimeError(f"Piper config missing (expected {d}/{v}.onnx.json or {d}/{v}.json)")

    return model


def _piper_tts_wav_bytes(text: str, voice: str, model_dir: str) -> bytes:
    model = _resolve_piper_model(model_dir, voice)

    with tempfile.TemporaryDirectory() as td:
        out_wav = str(Path(td) / "out.wav")
        p = subprocess.run(
            ["piper", "--model", str(model), "--output_file", out_wav],
            input=(text or "").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if p.returncode != 0 or not Path(out_wav).exists():
            err = (p.stderr or b"").decode("utf-8", "ignore")[:1400]
            raise RuntimeError(f"Piper failed ({p.returncode}): {err}")
        b = Path(out_wav).read_bytes()
        if len(b) < 1000:
            raise RuntimeError("Piper produced empty WAV.")
        return b


# ---------------------------
# Gemini TTS (optional)
# ---------------------------
def _gemini_tts_wav_bytes(text: str, api_key: str, model: str, voice: str) -> bytes:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty for premium TTS.")
    if not model:
        raise RuntimeError("Gemini TTS model is empty.")
    if not voice:
        raise RuntimeError("Gemini voice is empty.")

    # v1beta speech endpoint is not guaranteed stable across SDK versions; use REST.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateSpeech?key={api_key}"
    payload = {
        "input": {"text": text},
        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
    }

    r = requests.post(url, json=payload, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini TTS HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    audio_b64 = data.get("audio") or data.get("audioContent") or ""
    if not audio_b64:
        raise RuntimeError("Gemini TTS returned empty audio.")
    import base64

    pcm = base64.b64decode(audio_b64)
    # Wrap raw PCM into WAV via ffmpeg
    with tempfile.TemporaryDirectory() as td:
        raw = str(Path(td) / "a.pcm")
        wav = str(Path(td) / "a.wav")
        Path(raw).write_bytes(pcm)
        _run(["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", raw, wav])
        return Path(wav).read_bytes()


# ---------------------------
# Public API
# ---------------------------
def tts_chunks_to_mp3(
    chunks: List[Dict[str, str]],
    mp3_path: Path,
    *,
    api_key: str = "",
    premium: bool = True,
    gemini_model: Optional[str] = None,
    voice_a: Optional[str] = None,
    voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
    piper_model_dir: str = "assets/piper",
) -> None:
    """
    chunks: [{"speaker":"A"|"B", "text":"..."}]
    premium=True => Gemini TTS (if configured)
    premium=False => Piper TTS (requires assets/piper voices)
    """
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    if not chunks:
        raise RuntimeError("No TTS chunks provided.")

    # Choose voices
    if premium:
        va = voice_a or "Kore"
        vb = voice_b or "Puck"
        provider = "gemini"
    else:
        va = piper_voice_a or DEFAULT_PIPER_A
        vb = piper_voice_b or DEFAULT_PIPER_B
        provider = "piper"

    # Cache directory
    cache_dir = mp3_path.parent / "_tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Gap wav (reuse)
    gap_ms = int(os.getenv("TTS_TURN_GAP_MS", "120").strip() or "120")
    gap_wav = str(cache_dir / f"_gap_{gap_ms}ms.wav")
    if not Path(gap_wav).exists():
        _make_gap_wav(gap_wav, ms=gap_ms)

    wav_files: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        for i, ch in enumerate(chunks):
            sp = (ch.get("speaker") or "A").strip().upper()
            text = (ch.get("text") or "").strip()
            if not text:
                continue

            voice = va if sp == "A" else vb

            cache_key = _sha256(f"{provider}|{voice}|{text}")
            cached_wav = cache_dir / f"{cache_key}.wav"

            if cached_wav.exists() and cached_wav.stat().st_size > 1000:
                wav_files.append(str(cached_wav))
                wav_files.append(gap_wav)
                continue

            # Render turn
            if provider == "piper":
                wav_bytes = _piper_tts_wav_bytes(text=text, voice=voice, model_dir=piper_model_dir)
                tmp_in = td_path / f"turn_{i:04d}.wav"
                tmp_in.write_bytes(wav_bytes)

                # Trim silence → cache
                tmp_out = td_path / f"turn_{i:04d}.trim.wav"
                _ffmpeg_trim_silence(str(tmp_in), str(tmp_out))
                shutil.copyfile(tmp_out, cached_wav)
                wav_files.append(str(cached_wav))
                wav_files.append(gap_wav)

            else:
                # Gemini TTS
                if not gemini_model:
                    raise RuntimeError("Gemini TTS model not provided (GEMINI_TTS_MODEL).")
                wav_bytes = _gemini_tts_wav_bytes(text=text, api_key=api_key, model=gemini_model, voice=voice)
                tmp_in = td_path / f"turn_{i:04d}.wav"
                tmp_in.write_bytes(wav_bytes)
                tmp_out = td_path / f"turn_{i:04d}.trim.wav"
                _ffmpeg_trim_silence(str(tmp_in), str(tmp_out))
                shutil.copyfile(tmp_out, cached_wav)
                wav_files.append(str(cached_wav))
                wav_files.append(gap_wav)

        # Remove trailing gap if present
        if wav_files and wav_files[-1] == gap_wav:
            wav_files.pop()

        if not wav_files:
            raise RuntimeError("No WAV segments produced by TTS.")

        _concat_wavs_to_mp3(wav_files, str(mp3_path))
