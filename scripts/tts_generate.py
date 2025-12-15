#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


# -----------------------------
# Data structures
# -----------------------------
@dataclass
class TTSTurn:
    speaker: str  # "A" or "B"
    text: str


# -----------------------------
# Config helpers
# -----------------------------
def _env_int(name: str, default: int) -> int:
    v = (os.getenv(name, "") or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _sha1_str(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _norm_text(s: str) -> str:
    # Normalize whitespace to improve cache hits
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# -----------------------------
# Dialogue parsing
# -----------------------------
_SPEAKER_MAP = {
    "HOST": "A",
    "ANCHOR": "A",
    "NARRATOR": "A",
    "SPEAKER A": "A",
    "A": "A",
    "GUEST": "B",
    "CO-HOST": "B",
    "SPEAKER B": "B",
    "B": "B",
}


def script_to_tts_chunks(script_text: str) -> List[Dict[str, str]]:
    """
    Returns list of dicts: [{"speaker":"A","text":"..."}, {"speaker":"B","text":"..."}]
    Accepts formats like:
      A: ...
      B: ...
      HOST: ...
      GUEST: ...
    If no speakers detected -> everything becomes one A chunk.
    """
    text = _norm_text(script_text)
    if not text:
        return []

    lines = text.split("\n")
    turns: List[TTSTurn] = []

    current_speaker: Optional[str] = None
    buf: List[str] = []

    def flush():
        nonlocal buf, current_speaker
        t = _norm_text("\n".join(buf))
        if t:
            turns.append(TTSTurn(speaker=current_speaker or "A", text=t))
        buf = []

    speaker_re = re.compile(r"^\s*([A-Za-z][A-Za-z \-]{0,20})\s*:\s*(.*)\s*$")

    detected_any = False
    for ln in lines:
        m = speaker_re.match(ln)
        if m:
            label = (m.group(1) or "").strip().upper()
            rest = (m.group(2) or "").strip()
            sp = _SPEAKER_MAP.get(label)
            if sp in ("A", "B"):
                detected_any = True
                if current_speaker != sp:
                    flush()
                    current_speaker = sp
                if rest:
                    buf.append(rest)
                continue

        # Normal line
        buf.append(ln)

    flush()

    if not detected_any:
        # No structured speakers; treat as a single speaker A
        whole = _norm_text(text)
        return [{"speaker": "A", "text": whole}] if whole else []

    # Convert to the expected dict list
    out = [{"speaker": t.speaker, "text": _norm_text(t.text)} for t in turns if _norm_text(t.text)]
    return out


# -----------------------------
# Chunking / batching
# -----------------------------
def _merge_same_speaker(turns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not turns:
        return []
    out: List[Dict[str, str]] = []
    cur = {"speaker": turns[0]["speaker"], "text": turns[0]["text"]}
    for t in turns[1:]:
        if t["speaker"] == cur["speaker"]:
            cur["text"] = _norm_text(cur["text"] + "\n\n" + t["text"])
        else:
            out.append(cur)
            cur = {"speaker": t["speaker"], "text": t["text"]}
    out.append(cur)
    return out


def _split_long_text(text: str, max_chars: int) -> List[str]:
    text = _norm_text(text)
    if len(text) <= max_chars:
        return [text] if text else []

    # Split by paragraphs, then by sentences if needed
    paras = re.split(r"\n\s*\n", text)
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    def push():
        nonlocal cur, cur_len
        if cur:
            chunks.append(_norm_text("\n\n".join(cur)))
        cur, cur_len = [], 0

    for p in paras:
        p = p.strip()
        if not p:
            continue
        if cur_len + len(p) + 2 <= max_chars:
            cur.append(p)
            cur_len += len(p) + 2
            continue
        # paragraph itself too large
        if len(p) > max_chars:
            # sentence split
            sents = re.split(r"(?<=[\.\!\?])\s+", p)
            for s in sents:
                s = s.strip()
                if not s:
                    continue
                if cur_len + len(s) + 1 <= max_chars:
                    cur.append(s)
                    cur_len += len(s) + 1
                else:
                    push()
                    cur.append(s)
                    cur_len = len(s)
            continue
        # otherwise start new chunk
        push()
        cur.append(p)
        cur_len = len(p)

    push()
    return [c for c in chunks if c]


def _turns_to_batches(
    turns: List[Dict[str, str]],
    max_chars_per_chunk: int,
    min_chunk_chars: int,
) -> List[List[Dict[str, str]]]:
    """
    Group turns into batches for fewer TTS calls (especially for Gemini multi-speaker).
    """
    batches: List[List[Dict[str, str]]] = []
    cur: List[Dict[str, str]] = []
    cur_len = 0

    def push():
        nonlocal cur, cur_len
        if cur:
            batches.append(cur)
        cur, cur_len = [], 0

    for t in turns:
        sp = t.get("speaker", "A")
        tx = _norm_text(t.get("text", ""))
        if not tx:
            continue

        # Split extremely long turn first
        pieces = _split_long_text(tx, max_chars_per_chunk)
        for piece in pieces:
            add_len = len(piece) + 12  # label overhead
            if not cur:
                cur = [{"speaker": sp, "text": piece}]
                cur_len = add_len
                continue

            if cur_len + add_len <= max_chars_per_chunk:
                cur.append({"speaker": sp, "text": piece})
                cur_len += add_len
            else:
                push()
                cur = [{"speaker": sp, "text": piece}]
                cur_len = add_len

            # If we reached a minimum useful size, we can push on speaker boundary only
            # (simple heuristic; keeps latency lower)
            if cur_len >= max(min_chunk_chars, 1) and len(cur) >= 4:
                # do nothing; allow more accumulation
                pass

    push()
    return batches


# -----------------------------
# WAV helpers
# -----------------------------
def _pcm_s16le_to_wav_bytes(pcm: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tmp = tf.name
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        return Path(tmp).read_bytes()
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def _ffmpeg_exists() -> bool:
    try:
        subprocess.check_output(["ffmpeg", "-version"], stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


def _run(cmd: List[str], timeout: int = 900) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def _ensure_wav_24k_mono(in_wav: Path, out_wav: Path) -> None:
    # Standardize format for concatenation
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(in_wav),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        timeout=900,
    )


def _trim_silence_wav(in_wav: Path, out_wav: Path) -> None:
    # Conservative trimming; avoids chopping words
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(in_wav),
            "-af",
            "silenceremove=start_periods=1:start_duration=0.20:start_threshold=-45dB:"
            "stop_periods=1:stop_duration=0.30:stop_threshold=-45dB",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        timeout=900,
    )


def _write_concat_list(wavs: List[Path], list_path: Path) -> None:
    # IMPORTANT: no backslash inside f-string expressions; build string safely
    def esc(p: str) -> str:
        # ffmpeg concat demuxer wants single quotes; escape single quotes for bash-like rules
        # ' -> '\'' inside single quotes context
        return p.replace("'", "'\\''")

    lines = []
    for w in wavs:
        lines.append("file '" + esc(str(w)) + "'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _concat_wavs_to_wav(wavs: List[Path], out_wav: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        lst = td_p / "concat.txt"
        _write_concat_list(wavs, lst)
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c:a",
                "pcm_s16le",
                str(out_wav),
            ],
            timeout=1800,
        )


def _wav_to_mp3(in_wav: Path, out_mp3: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(in_wav),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out_mp3),
        ],
        timeout=1800,
    )


def _make_silence_wav(seconds: float, out_wav: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=24000:cl=mono",
            "-t",
            f"{seconds:.3f}",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        timeout=300,
    )


# -----------------------------
# Piper helpers
# -----------------------------
def _piper_paths_for_voice(voice_id: str, model_dir: Path) -> Tuple[Path, Path]:
    """
    Expects:
      <model_dir>/<voice_id>.onnx
      <model_dir>/<voice_id>.onnx.json
    """
    onnx = model_dir / f"{voice_id}.onnx"
    cfg = model_dir / f"{voice_id}.onnx.json"
    return onnx, cfg


def _piper_tts_wav_bytes(text: str, voice_id: str, model_dir: Path) -> bytes:
    text = _norm_text(text)
    if not text:
        return b""

    onnx, cfg = _piper_paths_for_voice(voice_id, model_dir)
    if not onnx.exists():
        raise RuntimeError(f"Piper model missing: {onnx}")
    if not cfg.exists():
        raise RuntimeError(f"Piper config missing: {cfg}")

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        raw_wav = td_p / "piper.wav"
        # Piper reads stdin
        cmd = [
            "piper",
            "--model",
            str(onnx),
            "--config",
            str(cfg),
            "--output_file",
            str(raw_wav),
        ]
        p = subprocess.run(
            cmd,
            input=(text + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "ignore")
            raise RuntimeError(f"Piper failed ({p.returncode}): {err[:1200]}")
        if not raw_wav.exists() or raw_wav.stat().st_size < 2000:
            raise RuntimeError("Piper produced empty wav")
        # Standardize to 24k mono
        std_wav = td_p / "std.wav"
        _ensure_wav_24k_mono(raw_wav, std_wav)
        return std_wav.read_bytes()


# -----------------------------
# Gemini helpers (google-genai)
# -----------------------------
def _is_quota_error(msg: str) -> bool:
    m = (msg or "").lower()
    return ("resource_exhausted" in m) or ("quota" in m) or ("429" in m) or ("rate limit" in m)


def _gemini_tts_wav_bytes_multi(
    batch_turns: List[Dict[str, str]],
    api_key: str,
    model: str,
    voice_a: str,
    voice_b: str,
) -> bytes:
    """
    Uses Gemini speech generation with multi-speaker config.
    Output is WAV bytes (PCM 16-bit 24kHz mono wrapped into WAV).
    """
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    # Build a clean conversation prompt
    lines = []
    for t in batch_turns:
        sp = t.get("speaker", "A")
        tx = _norm_text(t.get("text", ""))
        if not tx:
            continue
        label = "Speaker A" if sp == "A" else "Speaker B"
        lines.append(f"{label}: {tx}")
    prompt = (
        "Generate natural, conversational speech for the following two-person dialogue.\n"
        "Do not omit any content. Read every line fully.\n\n"
        + "\n\n".join(lines)
    )

    client = genai.Client(api_key=api_key)

    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[
                    types.SpeakerVoiceConfig(speaker="Speaker A", voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_a))),
                    types.SpeakerVoiceConfig(speaker="Speaker B", voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_b))),
                ]
            )
        ),
    )

    resp = client.models.generate_content(model=model, contents=prompt, config=cfg)

    # Per official doc: candidates[0].content.parts[0].inline_data.data is PCM bytes
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    if not pcm:
        raise RuntimeError("Gemini returned empty audio bytes")
    return _pcm_s16le_to_wav_bytes(pcm, sample_rate=24000, channels=1)


# -----------------------------
# Public: main entry
# -----------------------------
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
) -> None:
    """
    Render TTS and produce MP3 at mp3_path.

    premium=True  -> Gemini (if possible)
    premium=False -> Piper

    Speed optimizations:
      - merges consecutive same-speaker turns
      - batches into larger requests (Gemini multi-speaker)
      - caching wav per (provider+model+voices+text)
      - final silence trimming
    """
    if isinstance(mp3_path, str):
        mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    if not _ffmpeg_exists():
        raise RuntimeError("ffmpeg not found in PATH. Install ffmpeg in workflow.")

    max_chars = _env_int("TTS_MAX_CHARS_PER_CHUNK", 9000)
    min_chunk_chars = _env_int("MIN_CHUNK_CHARS", 800)
    merge_same = _env_bool("MERGE_SAME_VOICE_TURNS", True)
    gap_ms = _env_int("TTS_TURN_GAP_MS", 120)
    fail_soft_on_quota = _env_bool("FAIL_SOFT_ON_QUOTA", True)
    quota_marker = Path(os.getenv("TTS_QUOTA_MARKER", "outputs/_tts_quota_exceeded.txt"))

    turns = tts_chunks or []
    if merge_same:
        turns = _merge_same_speaker(turns)

    # Defaults
    gemini_model = (gemini_model or os.getenv("GEMINI_TTS_MODEL", "") or "").strip() or "gemini-2.5-flash-preview-tts"
    voice_a = (voice_a or os.getenv("VOICE_A", "") or "").strip() or "Kore"
    voice_b = (voice_b or os.getenv("VOICE_B", "") or "").strip() or "Puck"

    piper_voice_a = (piper_voice_a or os.getenv("PIPER_VOICE_A", "") or "").strip() or "en_US-ryan-medium"
    piper_voice_b = (piper_voice_b or os.getenv("PIPER_VOICE_B", "") or "").strip() or "en_US-amy-medium"

    model_dir = Path(piper_model_dir or os.getenv("PIPER_MODEL_DIR", "") or "assets/piper")

    # Cache dir (in repo workspace)
    cache_dir = Path(os.getenv("TTS_CACHE_DIR", "outputs/_tts_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Batch turns
    batches = _turns_to_batches(turns, max_chars_per_chunk=max_chars, min_chunk_chars=min_chunk_chars)
    if not batches:
        raise RuntimeError("No TTS batches produced (empty script/chunks).")

    wav_files: List[Path] = []

    # Pre-generate silence wav if needed
    silence_wav: Optional[Path] = None
    if gap_ms > 0:
        silence_wav = cache_dir / f"_silence_{gap_ms}ms.wav"
        if not silence_wav.exists() or silence_wav.stat().st_size < 1000:
            _make_silence_wav(gap_ms / 1000.0, silence_wav)

    def pick_piper_voice(sp: str) -> str:
        return piper_voice_a if sp == "A" else piper_voice_b

    provider_used = "gemini" if premium else "piper"
    last_quota_error: Optional[str] = None

    for i, batch in enumerate(batches, start=1):
        # Build a cache key for the whole batch
        batch_text = "\n\n".join([f"{t.get('speaker','A')}:{_norm_text(t.get('text',''))}" for t in batch]).strip()
        key = _sha1_str(f"{provider_used}|{gemini_model}|{voice_a}|{voice_b}|{piper_voice_a}|{piper_voice_b}|" + batch_text)
        cached_wav = cache_dir / f"{key}.wav"

        if cached_wav.exists() and cached_wav.stat().st_size > 2000:
            wav_files.append(cached_wav)
            if silence_wav is not None:
                wav_files.append(silence_wav)
            continue

        try:
            if premium:
                if not api_key:
                    raise RuntimeError("premium=True but GEMINI api_key is empty")
                wav_bytes = _gemini_tts_wav_bytes_multi(
                    batch_turns=batch,
                    api_key=api_key,
                    model=gemini_model,
                    voice_a=voice_a,
                    voice_b=voice_b,
                )
            else:
                # Piper: render per speaker blocks inside batch, then concat to one wav
                with tempfile.TemporaryDirectory() as td:
                    td_p = Path(td)
                    inner_wavs: List[Path] = []

                    for j, t in enumerate(batch, start=1):
                        sp = t.get("speaker", "A")
                        tx = _norm_text(t.get("text", ""))
                        if not tx:
                            continue
                        v = pick_piper_voice(sp)
                        kb = _sha1_str(f"piper|{v}|" + tx)
                        inner_cached = cache_dir / f"{kb}.wav"
                        if inner_cached.exists() and inner_cached.stat().st_size > 2000:
                            inner_wavs.append(inner_cached)
                            continue

                        b = _piper_tts_wav_bytes(text=tx, voice_id=v, model_dir=model_dir)
                        tmp = td_p / f"turn_{j:04d}.wav"
                        tmp.write_bytes(b)
                        inner_cached.write_bytes(b)
                        inner_wavs.append(inner_cached)

                    if not inner_wavs:
                        raise RuntimeError("Piper produced no audio turns for this batch")

                    # concat inner wavs
                    tmp_concat = td_p / "batch.wav"
                    _concat_wavs_to_wav(inner_wavs, tmp_concat)
                    wav_bytes = tmp_concat.read_bytes()

            cached_wav.write_bytes(wav_bytes)
            wav_files.append(cached_wav)
            if silence_wav is not None:
                wav_files.append(silence_wav)

        except Exception as e:
            msg = str(e)
            if premium and fail_soft_on_quota and _is_quota_error(msg):
                last_quota_error = msg
                # mark and fallback to piper from now on
                try:
                    quota_marker.parent.mkdir(parents=True, exist_ok=True)
                    quota_marker.write_text(f"{time.time():.0f}\n{msg}\n", encoding="utf-8")
                except Exception:
                    pass
                premium = False
                provider_used = "piper"
                # retry this same batch via piper
                i -= 1
                continue
            raise

    # Remove trailing silence
    if silence_wav is not None and wav_files and wav_files[-1] == silence_wav:
        wav_files = wav_files[:-1]

    if not wav_files:
        raise RuntimeError("No wav files were generated.")

    # Final concat -> trim -> mp3
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        merged = td_p / "merged.wav"
        trimmed = td_p / "trimmed.wav"

        _concat_wavs_to_wav(wav_files, merged)
        _trim_silence_wav(merged, trimmed)
        _wav_to_mp3(trimmed, Path(mp3_path))
