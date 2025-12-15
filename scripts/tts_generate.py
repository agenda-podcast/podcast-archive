from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import wave
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_GAP_SECONDS = 0.35
DEFAULT_MAX_CHARS = 360
DEFAULT_SAMPLE_RATE = 22050


def find_piper_binary() -> str:
    """
    Locate the `piper` executable.

    Priority:
      1. Environment variable PIPER_BINARY (full path to binary)
      2. Look up 'piper' on PATH via shutil.which

    Raises:
      RuntimeError with actionable instructions if not found.
    Returns:
      The full path to the piper executable.
    """
    env_path = os.environ.get("PIPER_BINARY")
    if env_path:
        if os.path.isfile(env_path) and os.access(env_path, os.X_OK):
            return env_path
        raise RuntimeError(
            f"PIPER_BINARY is set to '{env_path}' but the file does not exist or is not executable."
        )

    piper_path = shutil.which("piper")
    if piper_path:
        return piper_path

    raise RuntimeError(
        textwrap.dedent(
            """\
            The 'piper' TTS binary was not found.

            Install or provide 'piper' and make sure it's executable and on PATH,
            or set the PIPER_BINARY environment variable to its full path.
            """
        )
    )


def _piper_tts_wav_bytes(text: str, voice: str, model_dir: Optional[str] = None) -> bytes:
    """
    Generate WAV bytes with piper.

    This function resolves the piper binary (via find_piper_binary()) and uses the full
    path when invoking subprocess to avoid FileNotFoundError. If piper exits with a
    non-zero status, a RuntimeError is raised and stderr is included to aid debugging.
    """
    piper_exe = find_piper_binary()

    cmd = [
        piper_exe,
        "--voice",
        voice,
    ]
    if model_dir:
        cmd.extend(["--model-dir", model_dir])

    try:
        proc = subprocess.run(
            cmd,
            input=(text.strip() + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as e:
        raise RuntimeError(f"Failed to execute piper binary at '{piper_exe}': {e}") from e

    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        max_len = 16 * 1024
        if len(stderr_text) > max_len:
            stderr_text = stderr_text[:max_len] + "\n...[truncated]"
        raise RuntimeError(f"piper failed (exit code {proc.returncode}). stderr:\n{stderr_text}")

    if not proc.stdout:
        raise RuntimeError("piper succeeded but returned no audio on stdout.")

    return proc.stdout


def _gemini_tts_wav_bytes(*, text: str, voice: Optional[str], api_key: str, model: str) -> bytes:
    try:
        import google.genai as genai  # type: ignore
    except Exception as e:
        raise RuntimeError(f"Gemini TTS unavailable (missing google-genai): {e}") from e

    client = genai.Client(api_key=api_key)
    cfg = {"response_mime_type": "audio/wav"}
    if voice:
        cfg["voice_name"] = voice

    try:
        resp = client.models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": [{"text": text}]}],
            generation_config=cfg,
        )
    except Exception as e:
        raise RuntimeError(f"Gemini TTS request failed: {e}") from e

    try:
        parts = resp.candidates[0].content.parts
        for p in parts:
            if hasattr(p, "inline_data") and getattr(p.inline_data, "data", None):
                return base64.b64decode(p.inline_data.data)
            if isinstance(p, dict) and p.get("inline_data", {}).get("data"):
                return base64.b64decode(p["inline_data"]["data"])
    except Exception:
        pass
    raise RuntimeError("Gemini TTS returned no audio payload.")


def _split_text(text: str, limit: int = DEFAULT_MAX_CHARS) -> List[str]:
    """
    Split text into chunks under the limit without breaking words if possible.
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= limit:
        return [t]

    parts: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for word in t.split():
        wlen = len(word) + (1 if cur else 0)
        if cur_len + wlen > limit and cur:
            parts.append(" ".join(cur).strip())
            cur = [word]
            cur_len = len(word)
        else:
            cur.append(word)
            cur_len += wlen
    if cur:
        parts.append(" ".join(cur).strip())
    return parts


def _resolve_sample_rate(sample_rate: Optional[int]) -> int:
    try:
        env_sr = int(os.getenv("AUDIO_SAMPLE_RATE", "").strip() or 0)
    except Exception:
        env_sr = 0
    return int(sample_rate or env_sr or DEFAULT_SAMPLE_RATE)


def _ensure_silence_wav(path: Path, seconds: float = DEFAULT_GAP_SECONDS, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Path:
    """
    Create a small silence wav for padding between utterances (cached on disk).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path

    n_frames = int(sample_rate * max(seconds, 0.0))
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return path


def _concat_wavs_to_mp3(wavs: List[Path], out_mp3: Path, sample_rate: int) -> None:
    """
    Concatenate wavs using ffmpeg concat demuxer and emit mp3.
    """
    if not wavs:
        raise RuntimeError("No audio chunks to concatenate.")

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"file '{w}'" for w in wavs]

    with tempfile.TemporaryDirectory() as td:
        concat_path = Path(td) / "concat.txt"
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-acodec",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(out_mp3),
        ]
        subprocess.check_call(cmd)


def _tts_provider_to_bytes(
    *,
    provider: str,
    text: str,
    voice: str,
    model_dir: Optional[str],
    gemini_model: Optional[str],
    gemini_api_key: Optional[str],
) -> bytes:
    if provider == "piper":
        return _piper_tts_wav_bytes(text, voice=voice, model_dir=model_dir)
    if provider == "gemini":
        if not gemini_api_key or not gemini_model:
            raise RuntimeError("Gemini TTS requires GEMINI_API_KEY and gemini_model.")
        return _gemini_tts_wav_bytes(text=text, voice=voice, api_key=gemini_api_key, model=gemini_model)
    raise RuntimeError(f"Unsupported TTS provider: {provider}")


def tts_chunks_to_mp3(
    chunks: Iterable[Dict[str, str]],
    mp3_path: str,
    *,
    premium: bool = False,
    gemini_api_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
    gemini_tts_model: Optional[str] = None,
    gemini_voice_a: Optional[str] = None,
    gemini_voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
    piper_model_dir: Optional[str] = None,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_chars: int = DEFAULT_MAX_CHARS,
    sample_rate: Optional[int] = None,
) -> tuple[str, str]:
    """
    Convert a list of dialogue chunks to an MP3 file.

    chunks: iterable of {"speaker": "A"/"B", "text": "..."}
    premium: if True, attempt premium provider (Gemini); falls back to Piper on errors/misconfig.
    Returns:
      (mp3_path, provider_used)
    """
    cache_dir = Path("outputs/_tts_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Voice selection
    p_voice_a = piper_voice_a or os.environ.get("PIPER_VOICE_A") or "en_US-ryan-medium"
    p_voice_b = piper_voice_b or os.environ.get("PIPER_VOICE_B") or "en_US-amy-medium"
    g_voice_a = gemini_voice_a or os.environ.get("GEMINI_TTS_VOICE_A") or p_voice_a
    g_voice_b = gemini_voice_b or os.environ.get("GEMINI_TTS_VOICE_B") or p_voice_b

    provider_requested = "gemini" if (premium and gemini_api_key and (gemini_tts_model or gemini_model)) else "piper"
    provider_used = provider_requested

    sr = _resolve_sample_rate(sample_rate)
    ms_gap = int(round(gap_seconds * 1000))
    silence_wav = _ensure_silence_wav(cache_dir / f"silence_{ms_gap}ms_{sr}hz.wav", seconds=gap_seconds, sample_rate=sr)
    wav_paths: List[Path] = []

    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            continue
        speaker = str(raw_chunk.get("speaker", "A") or "A").strip().upper()
        text = str(raw_chunk.get("text") or "").strip()
        if not text:
            continue

        voice_g = g_voice_a if speaker == "A" else g_voice_b
        voice_p = p_voice_a if speaker == "A" else p_voice_b

        for part in _split_text(text, max_chars):
            provider_for_chunk = provider_requested
            voice_for_chunk = voice_p if provider_for_chunk == "piper" else voice_g
            audio_bytes = None
            if provider_for_chunk == "piper" and provider_used != "piper":
                provider_used = "piper"
            if audio_bytes is None:
                try:
                    audio_bytes = _tts_provider_to_bytes(
                        provider=provider_for_chunk,
                        text=part,
                        voice=voice_for_chunk,
                        model_dir=piper_model_dir,
                        gemini_model=gemini_tts_model or gemini_model,
                        gemini_api_key=gemini_api_key,
                    )
                except Exception:
                    if provider_for_chunk == "gemini":
                        provider_used = "piper"
                        provider_for_chunk = "piper"
                        voice_for_chunk = voice_p
                        audio_bytes = _tts_provider_to_bytes(
                            provider="piper",
                            text=part,
                            voice=voice_for_chunk,
                            model_dir=piper_model_dir,
                            gemini_model=None,
                            gemini_api_key=None,
                        )
                    else:
                        raise

            key = hashlib.sha256(
                f"{provider_for_chunk}|{voice_for_chunk}|{part}|{piper_model_dir or ''}|{sr}".encode("utf-8")
            ).hexdigest()
            wav_path = cache_dir / f"{key}.wav"
            if not wav_path.exists():
                wav_path.write_bytes(audio_bytes)
            wav_paths.append(wav_path)
            if gap_seconds > 0:
                wav_paths.append(silence_wav)

    if wav_paths and wav_paths[-1] == silence_wav:
        wav_paths = wav_paths[:-1]

    _concat_wavs_to_mp3(wav_paths, Path(mp3_path), sample_rate=sr)
    return mp3_path, provider_used
