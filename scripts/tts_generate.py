#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -------------------------
# Script -> TTS chunks
# -------------------------
SPEAKER_RE = re.compile(r"^\s*(A|B|HOST\s*A|HOST\s*B|SPEAKER\s*1|SPEAKER\s*2|NARRATOR)\s*:\s*(.+)\s*$", re.IGNORECASE)


def _normalize_speaker(s: str) -> str:
    s = (s or "").strip().upper()
    if s in ("A", "HOST A", "SPEAKER 1"):
        return "A"
    if s in ("B", "HOST B", "SPEAKER 2"):
        return "B"
    return "A"


def _split_paragraphs(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    return parts if parts else [text.strip()] if text.strip() else []


def _split_to_chunks(text: str, max_chars: int) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]

    # Split by sentences, then pack.
    sents = re.split(r"(?<=[.!?])\s+", t)
    out: List[str] = []
    buf: List[str] = []
    n = 0
    for s in sents:
        s = s.strip()
        if not s:
            continue
        if n + len(s) + 1 > max_chars and buf:
            out.append(" ".join(buf).strip())
            buf = [s]
            n = len(s)
        else:
            buf.append(s)
            n += len(s) + 1
    if buf:
        out.append(" ".join(buf).strip())
    return [x for x in out if x]


def script_to_tts_chunks(script_text: str) -> List[Dict[str, str]]:
    """
    Returns a list of turns:
      [{"speaker": "A"|"B", "text": "..."}]
    If no explicit speaker tags exist, it alternates speakers by paragraph.
    """
    lines = (script_text or "").splitlines()
    tagged_turns: List[Tuple[str, str]] = []

    for ln in lines:
        m = SPEAKER_RE.match(ln)
        if m:
            sp = _normalize_speaker(m.group(1))
            tx = (m.group(2) or "").strip()
            if tx:
                tagged_turns.append((sp, tx))

    turns: List[Tuple[str, str]] = []
    if tagged_turns:
        # merge consecutive same speaker
        last_sp = None
        buf: List[str] = []
        for sp, tx in tagged_turns:
            if sp != last_sp and buf:
                turns.append((last_sp or "A", " ".join(buf).strip()))
                buf = []
            last_sp = sp
            buf.append(tx)
        if buf:
            turns.append((last_sp or "A", " ".join(buf).strip()))
    else:
        # No explicit dialogue tags -> alternate by paragraph for full coverage
        paras = _split_paragraphs(script_text)
        sp = "A"
        for p in paras:
            p = p.strip()
            if not p:
                continue
            turns.append((sp, p))
            sp = "B" if sp == "A" else "A"

    max_chars = int(os.getenv("TTS_MAX_CHARS_PER_CHUNK", "9000"))
    min_chars = int(os.getenv("MIN_CHUNK_CHARS", "800"))

    out: List[Dict[str, str]] = []
    for sp, tx in turns:
        # Ensure we don't create ultra-short "service words" turns
        tx = re.sub(r"\s+", " ", tx).strip()
        if not tx:
            continue
        chunks = _split_to_chunks(tx, max_chars=max_chars)
        for c in chunks:
            if len(c) < 10:
                continue
            out.append({"speaker": sp, "text": c})

    # Optional: merge tiny chunks forward
    merged: List[Dict[str, str]] = []
    for t in out:
        if merged and merged[-1]["speaker"] == t["speaker"] and len(merged[-1]["text"]) < min_chars:
            merged[-1]["text"] = (merged[-1]["text"] + " " + t["text"]).strip()
        else:
            merged.append(t)

    return merged


# -------------------------
# Audio helpers
# -------------------------
def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _run(cmd: List[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _trim_silence_wav(in_wav: Path, out_wav: Path) -> None:
    # Trim leading/trailing silence conservatively
    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_wav),
        "-af", "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.25:stop_periods=1:stop_threshold=-45dB:stop_silence=0.35",
        str(out_wav),
    ]
    p = _run(cmd, timeout=1800)
    if p.returncode != 0 or (not out_wav.exists()) or out_wav.stat().st_size < 1000:
        # fallback: copy original if trim fails
        out_wav.write_bytes(in_wav.read_bytes())


def _make_silence_wav(ms: int, out_wav: Path) -> None:
    sec = max(0.0, float(ms) / 1000.0)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate=24000",
        "-t", f"{sec:.3f}",
        str(out_wav),
    ]
    p = _run(cmd, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:800])


def _ffconcat_quote(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _concat_wavs_to_mp3(wavs: List[Path], out_mp3: Path) -> None:
    if not wavs:
        raise RuntimeError("No WAVs to concatenate.")
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        lst = td_p / "list.txt"
        lines = []
        for w in wavs:
            lines.append("file '" + _ffconcat_quote(w) + "'")
        lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(lst),
            "-c:a", "libmp3lame",
            "-q:a", "3",
            str(out_mp3),
        ]
        p = _run(cmd, timeout=3600)
        if p.returncode != 0 or (not out_mp3.exists()) or out_mp3.stat().st_size < 1000:
            raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:1200])


def _piper_tts_wav_bytes(text: str, voice: str, model_dir: Path) -> bytes:
    """
    Uses piper CLI. Expects:
      assets/piper/<voice>.onnx
      assets/piper/<voice>.onnx.json
    """
    model = Path(voice)
    if not model.suffix:
        model = model_dir / f"{voice}.onnx"
    elif model.suffix.lower() != ".onnx":
        model = model_dir / f"{voice}.onnx"

    cfg = Path(str(model) + ".json")  # piper uses <model>.json (for onnx it's typically .onnx.json)

    if not model.exists():
        raise RuntimeError(f"Piper model missing: {model}")
    if not cfg.exists():
        raise RuntimeError(f"Piper config missing: {cfg}")

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        out_wav = td_p / "out.wav"

        cmd = ["piper", "--model", str(model), "--output_file", str(out_wav)]
        p = subprocess.run(cmd, input=(text.strip() + "\n").encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0 or (not out_wav.exists()) or out_wav.stat().st_size < 1000:
            raise RuntimeError(f"Piper failed ({p.returncode}): {p.stderr.decode('utf-8', 'ignore')[:1200]}")
        return out_wav.read_bytes()


def tts_chunks_to_mp3(
    tts_chunks: List[Dict[str, str]],
    mp3_path: Path | str,
    api_key: str = "",
    premium: bool = True,
    gemini_model: Optional[str] = None,
    voice_a: Optional[str] = None,
    voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
    piper_model_dir: Optional[str] = None,
    **_: Any,
) -> None:
    """
    Premium TTS:
      - Only uses Gemini-TTS if you have Google Cloud creds (GOOGLE_APPLICATION_CREDENTIALS / ADC).
      - Otherwise falls back to Piper.
    Non-premium:
      - Piper only.

    Output: MP3 at mp3_path.
    """
    out_mp3 = Path(mp3_path)
    _ensure_dir(out_mp3.parent)

    # Defaults
    piper_a = piper_voice_a or os.getenv("PIPER_VOICE_A", "").strip() or "en_US-ryan-medium"
    piper_b = piper_voice_b or os.getenv("PIPER_VOICE_B", "").strip() or "en_US-amy-medium"
    model_dir = Path(piper_model_dir or os.getenv("PIPER_MODEL_DIR", "assets/piper")).resolve()

    # Decide engine
    provider_requested = "gemini" if premium else "piper"

    # Gemini-TTS requires Google Cloud TTS (not AI Studio API key).
    # If no GCP creds, force Piper even if premium.
    has_gcp_creds = bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_CLOUD_PROJECT"))
    if premium and not has_gcp_creds:
        provider_requested = "piper"

    cache_root = Path(os.getenv("TTS_CACHE_DIR", "outputs/_tts_cache")).resolve()
    _ensure_dir(cache_root / provider_requested)

    trim = os.getenv("TRIM_SILENCE", "1").strip().lower() in ("1", "true", "yes", "y")
    gap_ms = int(os.getenv("TTS_TURN_GAP_MS", "120"))

    # Pre-generate gap file
    silence_wav = cache_root / "silence_%dms.wav" % gap_ms
    if gap_ms > 0 and (not silence_wav.exists() or silence_wav.stat().st_size < 1000):
        _make_silence_wav(gap_ms, silence_wav)

    wavs: List[Path] = []
    idx = 0
    for turn in tts_chunks:
        idx += 1
        sp = (turn.get("speaker") or "A").strip().upper()
        txt = (turn.get("text") or "").strip()
        if not txt:
            continue

        # choose voice per speaker
        if provider_requested == "piper":
            voice = piper_a if sp == "A" else piper_b
        else:
            # Placeholder: if you later add GCP Gemini-TTS, map voices here.
            voice = (voice_a or "Puck") if sp == "A" else (voice_b or "Kore")

        key = _sha1_bytes((provider_requested + "|" + voice + "|" + txt).encode("utf-8"))
        wav_path = cache_root / provider_requested / f"{key}.wav"
        trimmed_path = cache_root / provider_requested / f"{key}.trim.wav"

        if wav_path.exists() and wav_path.stat().st_size > 1000:
            pass
        else:
            if provider_requested == "piper":
                wav_bytes = _piper_tts_wav_bytes(text=txt, voice=voice, model_dir=model_dir)
                wav_path.write_bytes(wav_bytes)
            else:
                raise RuntimeError(
                    "Premium Gemini-TTS is not enabled in this repo without Google Cloud Text-to-Speech credentials. "
                    "Set GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_CLOUD_PROJECT or use premium_tts=false (Piper)."
                )

        if trim:
            if not trimmed_path.exists() or trimmed_path.stat().st_size < 1000:
                _trim_silence_wav(wav_path, trimmed_path)
            wavs.append(trimmed_path)
        else:
            wavs.append(wav_path)

        # add gap between turns
        if gap_ms > 0:
            wavs.append(silence_wav)

    # Remove last gap
    if wavs and gap_ms > 0 and wavs[-1] == silence_wav:
        wavs = wavs[:-1]

    _concat_wavs_to_mp3(wavs, out_mp3)
