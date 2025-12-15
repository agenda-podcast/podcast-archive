#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import requests

# ============================================================
# Public API (imported by run_topic.py)
#   - script_to_tts_chunks(script_text) -> list[dict]
#   - tts_chunks_to_mp3(chunks, mp3_path, provider=..., premium=..., ...)
# ============================================================


# -------------------------
# Defaults & env knobs
# -------------------------
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-tts"

# Gemini prebuilt voices
DEFAULT_GEMINI_VOICE_A = "Kore"  # female-ish
DEFAULT_GEMINI_VOICE_B = "Puck"  # male-ish

# Piper voice IDs
DEFAULT_PIPER_VOICE_A = "en_US-amy-medium"   # female
DEFAULT_PIPER_VOICE_B = "en_US-ryan-medium"  # male

DEFAULT_LANGUAGE_CODE = "en-US"

# Audio normalization
TARGET_SR = int(os.getenv("TTS_TARGET_SR", "24000"))
TARGET_CH = int(os.getenv("TTS_TARGET_CH", "1"))

# Chunking / dialogue behavior
TTS_MAX_CHARS_PER_CHUNK = int(os.getenv("TTS_MAX_CHARS_PER_CHUNK", "3200"))
MIN_CHUNK_CHARS = int(os.getenv("MIN_CHUNK_CHARS", "300"))
MERGE_SAME_VOICE_TURNS = os.getenv("MERGE_SAME_VOICE_TURNS", "1").strip().lower() in ("1", "true", "yes", "y")
TTS_TURN_GAP_MS = int(os.getenv("TTS_TURN_GAP_MS", "120"))

# Best-effort behavior when Gemini quota/rate limit hits
FAIL_SOFT_ON_QUOTA = os.getenv("FAIL_SOFT_ON_QUOTA", "1").strip().lower() in ("1", "true", "yes", "y")
TTS_QUOTA_MARKER = os.getenv("TTS_QUOTA_MARKER", "outputs/_tts_quota_exceeded.txt")

# Optional silence trimming (FFmpeg silenceremove)
TRIM_SILENCE = os.getenv("TTS_TRIM_SILENCE", "1").strip().lower() in ("1", "true", "yes", "y")
SILENCE_DB = os.getenv("TTS_SILENCE_DB", "-45dB")

# Cache (per-run workspace cache; speeds retries)
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "outputs/_tts_cache")).resolve()

# Piper models directory
PIPER_MODEL_DIR = Path(os.getenv("PIPER_MODEL_DIR", "assets/piper")).resolve()

# Piper voices repo base (versioned)
PIPER_HF_BASE = os.getenv(
    "PIPER_HF_BASE",
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0",
).rstrip("/")

# Networking
HTTP_TIMEOUT = int(os.getenv("TTS_HTTP_TIMEOUT", "120"))

# Gemini endpoint
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com").rstrip("/")


# -------------------------
# Data structures
# -------------------------
@dataclass
class TTSTurn:
    speaker: str  # "A" or "B"
    text: str


# ============================================================
# Dialogue parsing
# ============================================================
_SPEAKER_RE = re.compile(
    r"^\s*(?P<who>"
    r"A|B|HOST|HOST\s*1|HOST\s*2|NARRATOR|SPEAKER\s*1|SPEAKER\s*2|"
    r"MALE|FEMALE|M|F"
    r")\s*[:\-]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)

_MD_SPEAKER_RE = re.compile(
    r"^\s*\*\*(?P<who>[^*]{1,30})\*\*\s*[:\-]\s*(?P<text>.+?)\s*$"
)


def _norm_speaker(token: str) -> Optional[str]:
    t = token.strip().lower()
    if t in ("a", "host", "host 1", "narrator", "speaker 1", "male", "m"):
        return "A"
    if t in ("b", "host 2", "speaker 2", "female", "f"):
        return "B"
    return None


def script_to_tts_chunks(script_text: str) -> List[Dict[str, str]]:
    """
    Robust dialogue parser.
    Returns list of dicts: {"speaker": "A"|"B", "text": "..."}.

    Supported formats:
      - A: ...
      - B: ...
      - HOST: ...
      - FEMALE: ...
      - **Host:** ...
      - If no explicit speakers: alternates per paragraph.
    """
    text = (script_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    turns: List[TTSTurn] = []

    lines = [ln.strip() for ln in text.split("\n")]
    buf: List[str] = []
    cur_speaker: Optional[str] = None
    any_tagged = False

    def flush_buf() -> None:
        nonlocal buf, cur_speaker, turns
        if cur_speaker and buf:
            joined = " ".join([b.strip() for b in buf if b.strip()]).strip()
            if joined:
                turns.append(TTSTurn(cur_speaker, joined))
        buf = []

    for ln in lines:
        if not ln:
            if cur_speaker:
                flush_buf()
                cur_speaker = None
            continue

        m = _SPEAKER_RE.match(ln)
        mm = _MD_SPEAKER_RE.match(ln) if not m else None

        who = None
        body = None

        if m:
            who = _norm_speaker(m.group("who"))
            body = m.group("text")
        elif mm:
            who = _norm_speaker(mm.group("who"))
            body = mm.group("text")

        if who and body:
            any_tagged = True
            if cur_speaker:
                flush_buf()
            cur_speaker = who
            buf = [body]
        else:
            if cur_speaker:
                buf.append(ln)
            else:
                buf.append(ln)

    if cur_speaker:
        flush_buf()

    if any_tagged and turns:
        return [{"speaker": t.speaker, "text": t.text.strip()} for t in turns if t.text.strip()]

    # Fallback: alternate per paragraph
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    speaker = "A"
    for p in paragraphs:
        turns.append(TTSTurn(speaker, p))
        speaker = "B" if speaker == "A" else "A"

    return [{"speaker": t.speaker, "text": t.text.strip()} for t in turns if t.text.strip()]


def _split_text_soft(text: str, max_chars: int) -> List[str]:
    sents = re.split(r"(?<=[\.\!\?])\s+", text.strip())
    chunks: List[str] = []
    cur = ""
    for s in sents:
        if not s:
            continue
        if not cur:
            cur = s
            continue
        if len(cur) + 1 + len(s) <= max_chars:
            cur = cur + " " + s
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)

    final: List[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            for i in range(0, len(c), max_chars):
                final.append(c[i : i + max_chars])
    return final


def _merge_and_chunk_turns(turns: List[TTSTurn]) -> List[TTSTurn]:
    merged: List[TTSTurn] = []
    if MERGE_SAME_VOICE_TURNS:
        for t in turns:
            if merged and merged[-1].speaker == t.speaker:
                merged[-1] = TTSTurn(t.speaker, (merged[-1].text + "\n" + t.text).strip())
            else:
                merged.append(t)
    else:
        merged = turns[:]

    out: List[TTSTurn] = []
    for t in merged:
        txt = t.text.strip()
        if len(txt) <= TTS_MAX_CHARS_PER_CHUNK:
            out.append(t)
            continue
        parts = _split_text_soft(txt, TTS_MAX_CHARS_PER_CHUNK)
        for part in parts:
            if part.strip():
                out.append(TTSTurn(t.speaker, part.strip()))

    final: List[TTSTurn] = []
    for t in out:
        if len(t.text) < MIN_CHUNK_CHARS and final and final[-1].speaker == t.speaker:
            final[-1] = TTSTurn(t.speaker, (final[-1].text + "\n" + t.text).strip())
        else:
            final.append(t)
    return final


# ============================================================
# Gemini TTS (HTTP)
# ============================================================
def _is_quota_error(text: str) -> bool:
    t = (text or "").lower()
    return ("resource_exhausted" in t) or ("quota" in t and "exceeded" in t) or ("rate limit" in t)


def _mark_quota_exceeded(msg: str) -> None:
    try:
        p = Path(TTS_QUOTA_MARKER)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((msg or "")[:2000], encoding="utf-8")
    except Exception:
        pass


def _gemini_tts_wav_bytes(
    *,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    retries: int = 4,
    backoff_sec: float = 2.0,
) -> bytes:
    if not api_key:
        raise RuntimeError("Gemini API key is empty")

    url = f"{GEMINI_API_BASE}/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "languageCode": language_code,
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}},
            },
        },
    }

    last_err: Optional[str] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}: {r.text[:600]}"
                if r.status_code in (429, 403) and _is_quota_error(r.text):
                    _mark_quota_exceeded(last_err)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(backoff_sec * attempt)
                    continue
                raise RuntimeError(last_err)

            data = r.json()
            wav_b64 = None
            for cand in (data.get("candidates") or []):
                content = cand.get("content") or {}
                for part in (content.get("parts") or []):
                    inline = part.get("inlineData") or part.get("inline_data") or None
                    if inline and isinstance(inline, dict):
                        wav_b64 = inline.get("data")
                        if wav_b64:
                            break
                if wav_b64:
                    break

            if not wav_b64:
                raise RuntimeError(f"Gemini TTS: no audio inlineData in response: {json.dumps(data)[:800]}")

            return base64.b64decode(wav_b64)
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(backoff_sec * attempt)
            else:
                break

    raise RuntimeError(f"TTS failed after retries: {last_err}")


# ============================================================
# Piper voice resolution + download
# ============================================================
_PIPER_VOICE_MAP: Dict[str, str] = {
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    "en_US-ryan-medium": "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_GB-alba-medium": "en/en_GB/alba/medium/en_GB-alba-medium",
}


def _resolve_piper_model_paths(voice: str, model_dir: Path) -> Tuple[Path, Path]:
    v = (voice or "").strip()
    if not v:
        raise RuntimeError("Piper voice is empty")

    vp = Path(v)
    if vp.suffix.lower() == ".onnx":
        model_path = vp if vp.is_absolute() else (model_dir / vp)
        cfg_path = Path(str(model_path) + ".json")  # expects model.onnx.json
        return model_path, cfg_path

    model_path = model_dir / f"{v}.onnx"
    cfg_path = model_dir / f"{v}.onnx.json"
    return model_path, cfg_path


def _download_to(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def _ensure_piper_voice(voice_id: str, model_dir: Path) -> Tuple[Path, Path]:
    model_path, cfg_path = _resolve_piper_model_paths(voice_id, model_dir)

    if model_path.suffix.lower() == ".onnx" and model_path.exists():
        if cfg_path.exists():
            return model_path, cfg_path
        inferred = model_path.stem
        rel = _PIPER_VOICE_MAP.get(inferred)
        if rel:
            _download_to(cfg_path, f"{PIPER_HF_BASE}/{rel}.onnx.json")
            return model_path, cfg_path
        raise RuntimeError(f"Piper config missing: {cfg_path}. Place matching .onnx.json next to the .onnx file.")

    if model_path.exists() and cfg_path.exists():
        return model_path, cfg_path

    rel = _PIPER_VOICE_MAP.get(voice_id)
    if not rel:
        raise RuntimeError(
            f"Unknown Piper voice_id '{voice_id}'. Add it to _PIPER_VOICE_MAP in tts_generate.py or pass a direct .onnx path."
        )

    if not model_path.exists():
        _download_to(model_path, f"{PIPER_HF_BASE}/{rel}.onnx")
    if not cfg_path.exists():
        _download_to(cfg_path, f"{PIPER_HF_BASE}/{rel}.onnx.json")

    return model_path, cfg_path


def _piper_tts_wav_bytes(*, text: str, voice: str, model_dir: Path) -> bytes:
    model_path, cfg_path = _ensure_piper_voice(voice, model_dir)
    if not model_path.exists():
        raise RuntimeError(f"Piper model missing after ensure: {model_path}")
    if not cfg_path.exists():
        raise RuntimeError(f"Piper config missing after ensure: {cfg_path}")

    with tempfile.TemporaryDirectory() as td:
        out_wav = Path(td) / "out.wav"
        cmd = ["piper", "--model", str(model_path), "--output_file", str(out_wav)]
        p = subprocess.run(
            cmd,
            input=(text.strip() + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if p.returncode != 0:
            raise RuntimeError(f"Piper failed ({p.returncode}): {p.stderr.decode('utf-8', 'ignore')[:1200]}")
        if not out_wav.exists() or out_wav.stat().st_size < 1000:
            raise RuntimeError("Piper produced empty WAV output")
        return out_wav.read_bytes()


# ============================================================
# WAV post-processing via FFmpeg
# ============================================================
def _ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def _normalize_wav_bytes(wav_bytes: bytes) -> bytes:
    if not _ffmpeg_exists():
        return wav_bytes

    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.wav"
        outp = Path(td) / "out.wav"
        inp.write_bytes(wav_bytes)

        afilters: List[str] = []
        if TRIM_SILENCE:
            afilters.append(
                f"silenceremove=start_periods=1:start_duration=0.04:start_threshold={SILENCE_DB}:"
                f"stop_periods=1:stop_duration=0.06:stop_threshold={SILENCE_DB}"
            )

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(inp),
            "-ac", str(TARGET_CH),
            "-ar", str(TARGET_SR),
            "-c:a", "pcm_s16le",
        ]
        if afilters:
            cmd += ["-af", ",".join(afilters)]
        cmd += [str(outp)]

        subprocess.check_call(cmd)
        return outp.read_bytes()


def _make_silence_wav(duration_ms: int) -> bytes:
    if duration_ms <= 0 or not _ffmpeg_exists():
        return b""
    with tempfile.TemporaryDirectory() as td:
        outp = Path(td) / "silence.wav"
        dur = max(duration_ms / 1000.0, 0.01)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout={'mono' if TARGET_CH == 1 else 'stereo'}:sample_rate={TARGET_SR}",
            "-t", f"{dur:.3f}",
            "-ac", str(TARGET_CH),
            "-ar", str(TARGET_SR),
            "-c:a", "pcm_s16le",
            str(outp),
        ]
        subprocess.check_call(cmd)
        return outp.read_bytes()


# ============================================================
# Cache helpers
# ============================================================
def _hash_key(parts: Iterable[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _cache_get(key: str) -> Optional[bytes]:
    try:
        p = CACHE_DIR / f"{key}.wav"
        if p.exists() and p.stat().st_size > 1000:
            return p.read_bytes()
    except Exception:
        return None
    return None


def _cache_put(key: str, wav_bytes: bytes) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.wav").write_bytes(wav_bytes)
    except Exception:
        pass


# ============================================================
# MP3 build
# ============================================================
def _concat_wavs_to_mp3(wav_paths: List[Path], out_mp3: Path) -> None:
    if not _ffmpeg_exists():
        raise RuntimeError("ffmpeg is required to build MP3 but was not found")

    out_mp3.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        list_file = Path(td) / "concat.txt"
        lines = [f"file '{wp.as_posix()}'" for wp in wav_paths]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(out_mp3),
        ]
        subprocess.check_call(cmd)


# ============================================================
# Engine selection (supports provider=...)
# ============================================================
def _select_engine(
    *,
    provider: Optional[str],
    premium: Optional[bool],
    api_key: str,
) -> str:
    """
    provider: "gemini" | "piper" | "auto" | None
    premium: bool or None
    Rule:
      - provider wins if provided
      - else premium True -> gemini, False -> piper
      - auto: gemini if api_key else piper
    """
    if provider:
        p = provider.strip().lower()
        if p in ("gemini", "google", "genai"):
            return "gemini" if api_key else "piper"
        if p in ("piper", "local"):
            return "piper"
        if p in ("auto",):
            return "gemini" if api_key else "piper"

    if premium is None:
        return "gemini" if api_key else "piper"
    return "gemini" if premium and api_key else "piper"


# ============================================================
# Main: chunks -> mp3
# ============================================================
def tts_chunks_to_mp3(
    chunks: Union[List[Dict[str, str]], List[TTSTurn]],
    mp3_path: Union[str, Path],
    *,
    # New arg (your run_topic.py passes this)
    provider: Optional[str] = None,
    # Legacy arg (kept)
    premium: Optional[bool] = True,
    api_key: str = "",
    gemini_model: Optional[str] = None,
    voice_a: Optional[str] = None,
    voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
    piper_model_dir: Optional[Union[str, Path]] = None,
    # Swallow any unexpected kwargs from other scripts safely
    **_ignored_kwargs: Any,
) -> None:
    """
    Renders dialogue chunks into an MP3 using either:
      - Gemini TTS (provider='gemini' or premium=True)
      - Piper local TTS (provider='piper' or premium=False)

    `chunks` elements: {"speaker":"A"|"B", "text":"..."}.
    """
    if not chunks:
        raise RuntimeError("No chunks provided")

    out_mp3 = Path(mp3_path) if isinstance(mp3_path, str) else mp3_path
    out_mp3.parent.mkdir(parents=True, exist_ok=True)

    # Normalize input
    turns: List[TTSTurn] = []
    if isinstance(chunks[0], TTSTurn):  # type: ignore[index]
        turns = list(chunks)  # type: ignore[assignment]
    else:
        for c in chunks:  # type: ignore[assignment]
            if not isinstance(c, dict):
                continue
            sp = str(c.get("speaker", "A")).strip().upper()
            if sp not in ("A", "B"):
                sp = "A"
            tx = str(c.get("text", "")).strip()
            if tx:
                turns.append(TTSTurn(sp, tx))

    turns = _merge_and_chunk_turns(turns)
    if not turns:
        raise RuntimeError("No non-empty turns after preprocessing")

    # Voices / models
    gem_model = (gemini_model or os.getenv("GEMINI_TTS_MODEL", "") or DEFAULT_GEMINI_MODEL).strip()
    gA = (voice_a or os.getenv("VOICE_A", "") or DEFAULT_GEMINI_VOICE_A).strip()
    gB = (voice_b or os.getenv("VOICE_B", "") or DEFAULT_GEMINI_VOICE_B).strip()

    pA = (piper_voice_a or os.getenv("PIPER_VOICE_A", "") or DEFAULT_PIPER_VOICE_A).strip()
    pB = (piper_voice_b or os.getenv("PIPER_VOICE_B", "") or DEFAULT_PIPER_VOICE_B).strip()

    model_dir = Path(piper_model_dir) if piper_model_dir else PIPER_MODEL_DIR

    engine = _select_engine(provider=provider, premium=premium, api_key=api_key)

    # Gap wav once
    gap_wav = _make_silence_wav(TTS_TURN_GAP_MS) if TTS_TURN_GAP_MS > 0 else b""

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        wav_files: List[Path] = []

        gap_path = None
        if gap_wav:
            gap_path = td_path / "gap.wav"
            gap_path.write_bytes(gap_wav)

        for i, t in enumerate(turns, start=1):
            voice = (gA if t.speaker == "A" else gB) if engine == "gemini" else (pA if t.speaker == "A" else pB)

            cache_key = _hash_key(
                [
                    "engine=" + engine,
                    "model=" + (gem_model if engine == "gemini" else ""),
                    "voice=" + voice,
                    "sr=" + str(TARGET_SR),
                    "ch=" + str(TARGET_CH),
                    "trim=" + ("1" if TRIM_SILENCE else "0"),
                    t.text.strip(),
                ]
            )
            cached = _cache_get(cache_key)
            if cached:
                wav_bytes = cached
            else:
                wav_bytes = _render_turn_to_wav(
                    engine=engine,
                    text=t.text,
                    voice=voice,
                    api_key=api_key,
                    gemini_model=gem_model,
                    piper_model_dir=model_dir,
                )
                wav_bytes = _normalize_wav_bytes(wav_bytes)
                _cache_put(cache_key, wav_bytes)

            wp = td_path / f"turn_{i:04d}.wav"
            wp.write_bytes(wav_bytes)
            wav_files.append(wp)

            if gap_path and i < len(turns):
                wav_files.append(gap_path)

        _concat_wavs_to_mp3(wav_files, out_mp3)


def _render_turn_to_wav(
    *,
    engine: str,
    text: str,
    voice: str,
    api_key: str,
    gemini_model: str,
    piper_model_dir: Path,
) -> bytes:
    last_err: Optional[str] = None

    if engine == "gemini":
        try:
            return _gemini_tts_wav_bytes(
                api_key=api_key,
                model=gemini_model,
                voice=voice,
                text=text,
                language_code=DEFAULT_LANGUAGE_CODE,
            )
        except Exception as e:
            last_err = str(e)
            if FAIL_SOFT_ON_QUOTA and _is_quota_error(last_err):
                fallback_voice = DEFAULT_PIPER_VOICE_A if voice == DEFAULT_GEMINI_VOICE_A else DEFAULT_PIPER_VOICE_B
                return _piper_tts_wav_bytes(text=text, voice=fallback_voice, model_dir=piper_model_dir)
            raise

    return _piper_tts_wav_bytes(text=text, voice=voice, model_dir=piper_model_dir)


# Backward compatibility (older code)
def tts_to_mp3(script_text: str, mp3_path: Union[str, Path], api_key: str = "") -> None:
    chunks = script_to_tts_chunks(script_text)
    tts_chunks_to_mp3(chunks, mp3_path, api_key=api_key, provider="gemini")


# Optional local CLI test
def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input text file (script)")
    ap.add_argument("--out", dest="out", required=True, help="Output mp3")
    ap.add_argument("--provider", dest="provider", default="auto", help="auto|gemini|piper")
    ap.add_argument("--api_key", dest="api_key", default=os.getenv("GEMINI_API_KEY", ""))
    args = ap.parse_args()

    script_text = Path(args.inp).read_text(encoding="utf-8")
    chunks = script_to_tts_chunks(script_text)
    if not chunks:
        raise SystemExit("No chunks parsed")

    tts_chunks_to_mp3(chunks, args.out, api_key=args.api_key, provider=args.provider)


if __name__ == "__main__":
    _cli()
