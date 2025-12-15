from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import textwrap
import wave
from pathlib import Path
from typing import Dict, Iterable, List, Optional

DEFAULT_GAP_SECONDS = 0.35
DEFAULT_MAX_CHARS = 360


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


def _ensure_silence_wav(path: Path, seconds: float = DEFAULT_GAP_SECONDS, sample_rate: int = 22050) -> Path:
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


def _concat_wavs_to_mp3(wavs: List[Path], out_mp3: Path) -> None:
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
            "22050",
            str(out_mp3),
        ]
        subprocess.check_call(cmd)


def _tts_provider_to_bytes(
    *,
    provider: str,
    text: str,
    voice: str,
    model_dir: Optional[str],
) -> bytes:
    """
    Currently only Piper is supported for audio generation. Premium (Gemini) requests are
    transparently routed to Piper until a Gemini TTS endpoint is available.
    """
    if provider == "piper":
        return _piper_tts_wav_bytes(text, voice=voice, model_dir=model_dir)
    if provider == "gemini":
        return _piper_tts_wav_bytes(text, voice=voice, model_dir=model_dir)
    raise RuntimeError(f"Unsupported TTS provider: {provider}")


def tts_chunks_to_mp3(
    chunks: Iterable[Dict[str, str]],
    mp3_path: str,
    *,
    premium: bool = False,
    gemini_api_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
    gemini_voice_a: Optional[str] = None,
    gemini_voice_b: Optional[str] = None,
    piper_voice_a: Optional[str] = None,
    piper_voice_b: Optional[str] = None,
    piper_model_dir: Optional[str] = None,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """
    Convert a list of dialogue chunks to an MP3 file.

    chunks: iterable of {"speaker": "A"/"B", "text": "..."}
    premium: if True, attempt premium provider (Gemini); falls back to Piper on errors/misconfig.
    """
    cache_dir = Path("outputs/_tts_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Voice selection
    p_voice_a = piper_voice_a or os.environ.get("PIPER_VOICE_A") or "en_US-ryan-medium"
    p_voice_b = piper_voice_b or os.environ.get("PIPER_VOICE_B") or "en_US-amy-medium"
    g_voice_a = gemini_voice_a or os.environ.get("GEMINI_TTS_VOICE_A") or p_voice_a
    g_voice_b = gemini_voice_b or os.environ.get("GEMINI_TTS_VOICE_B") or p_voice_b

    provider = "gemini" if premium and gemini_api_key else "piper"

    ms_gap = int(round(gap_seconds * 1000))
    silence_wav = _ensure_silence_wav(cache_dir / f"silence_{ms_gap}ms.wav", seconds=gap_seconds)
    wav_paths: List[Path] = []

    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            continue
        speaker = str(raw_chunk.get("speaker", "A") or "A").strip().upper()
        text = str(raw_chunk.get("text") or "").strip()
        if not text:
            continue

        voice = g_voice_a if speaker == "A" else g_voice_b
        if provider == "piper":
            voice = p_voice_a if speaker == "A" else p_voice_b

        for part in _split_text(text, max_chars):
            key = hashlib.sha256(
                f"{provider}|{voice}|{part}|{piper_model_dir or ''}".encode("utf-8")
            ).hexdigest()
            wav_path = cache_dir / f"{key}.wav"
            if not wav_path.exists():
                audio_bytes = _tts_provider_to_bytes(provider=provider, text=part, voice=voice, model_dir=piper_model_dir)
                wav_path.write_bytes(audio_bytes)
            wav_paths.append(wav_path)
            if gap_seconds > 0:
                wav_paths.append(silence_wav)

    if wav_paths and wav_paths[-1] == silence_wav:
        wav_paths = wav_paths[:-1]

    _concat_wavs_to_mp3(wav_paths, Path(mp3_path))
    return mp3_path
