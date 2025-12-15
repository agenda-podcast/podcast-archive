import os
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

from google import genai
from google.genai import types

TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip()

VOICE_A = os.environ.get("VOICE_A", "Kore").strip()
VOICE_B = os.environ.get("VOICE_B", "Puck").strip()

PCM_RATE = 24000
PCM_CHANNELS = 1
PCM_FORMAT = "s16le"

# Conservative chunk limit. Output audio truncates around ~655s. 1
# We chunk by text size so that each part stays well below that.
MAX_CHARS_PER_CHUNK = int(os.environ.get("TTS_MAX_CHARS_PER_CHUNK", "3200"))
TURN_GAP_MS = int(os.environ.get("TTS_TURN_GAP_MS", "200"))


def parse_dialogue(script: str) -> List[Tuple[str, str]]:
    turns: List[Tuple[str, str]] = []
    if not script:
        return turns

    for raw in script.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("SPEAKER_A:"):
            txt = line.split("SPEAKER_A:", 1)[1].strip()
            if txt:
                turns.append(("SPEAKER_A", txt))
        elif line.startswith("SPEAKER_B:"):
            txt = line.split("SPEAKER_B:", 1)[1].strip()
            if txt:
                turns.append(("SPEAKER_B", txt))
        else:
            turns.append(("SPEAKER_A", line))
    return turns


def _normalize_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s


def _chunk_turns(turns: List[Tuple[str, str]], max_chars: int) -> List[List[Tuple[str, str]]]:
    """
    Chunk by approximate character length of transcript.
    Keeps speaker tags to preserve multi-speaker mapping.
    """
    chunks: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    cur_len = 0

    for spk, txt in turns:
        txt = _normalize_text(txt)
        if not txt:
            continue

        line = f"{spk}: {txt}\n"
        line_len = len(line)

        # If a single line is huge, hard-split it (avoid empty chunk)
        if line_len > max_chars:
            # Flush current
            if cur:
                chunks.append(cur)
                cur = []
                cur_len = 0
            # Split by sentences-ish
            parts = re.split(r"(?<=[.!?])\s+", txt)
            sub: List[Tuple[str, str]] = []
            sub_len = 0
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                l = f"{spk}: {p}\n"
                if sub_len + len(l) > max_chars and sub:
                    chunks.append(sub)
                    sub = []
                    sub_len = 0
                sub.append((spk, p))
                sub_len += len(l)
            if sub:
                chunks.append(sub)
            continue

        if cur_len + line_len > max_chars and cur:
            chunks.append(cur)
            cur = []
            cur_len = 0

        cur.append((spk, txt))
        cur_len += line_len

    if cur:
        chunks.append(cur)

    return chunks if chunks else [[("SPEAKER_A", "This is an automated overview.")]]


def _build_prompt(turns: List[Tuple[str, str]]) -> str:
    lines = [f"{spk}: {_normalize_text(txt)}" for spk, txt in turns if _normalize_text(txt)]
    transcript = "\n".join(lines).strip() or "SPEAKER_A: This is an automated overview."

    return (
        "TTS the following conversation between SPEAKER_A and SPEAKER_B.\n"
        "Style: professional podcast delivery; natural pacing; do not add extra words.\n"
        "Transcript:\n"
        f"{transcript}"
    )


def _tts_chunk_to_pcm(client: genai.Client, prompt: str) -> bytes:
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[
                    types.SpeakerVoiceConfig(
                        speaker="SPEAKER_A",
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_A)
                        ),
                    ),
                    types.SpeakerVoiceConfig(
                        speaker="SPEAKER_B",
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_B)
                        ),
                    ),
                ]
            )
        ),
    )

    resp = client.models.generate_content(model=TTS_MODEL, contents=prompt, config=cfg)

    try:
        return resp.candidates[0].content.parts[0].inline_data.data
    except Exception as e:
        raise RuntimeError(f"TTS returned no audio data: {e}")


def tts_to_mp3(script_text: str, mp3_path: Path, api_key: str) -> None:
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    turns = parse_dialogue(script_text)
    if not turns:
        turns = [("SPEAKER_A", "This is an automated overview based on publicly available reporting.")]

    chunks = _chunk_turns(turns, MAX_CHARS_PER_CHUNK)

    client = genai.Client(api_key=api_key)

    pcm_dir = mp3_path.parent / "_pcm_parts"
    pcm_dir.mkdir(exist_ok=True)

    pcm_files: List[Path] = []

    for i, chunk_turns in enumerate(chunks):
        prompt = _build_prompt(chunk_turns)
        pcm_bytes = _tts_chunk_to_pcm(client, prompt)

        pcm_path = pcm_dir / f"part_{i:03d}.pcm"
        pcm_path.write_bytes(pcm_bytes)
        pcm_files.append(pcm_path)

        # Optional small pause between chunks to avoid “hard cuts”
        if TURN_GAP_MS > 0:
            gap_pcm = pcm_dir / f"gap_{i:03d}.pcm"
            subprocess.check_call([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r={PCM_RATE}:cl=mono",
                "-t", str(TURN_GAP_MS / 1000),
                "-f", PCM_FORMAT,
                "-ar", str(PCM_RATE),
                "-ac", "1",
                str(gap_pcm),
            ])
            pcm_files.append(gap_pcm)

    # Concatenate PCM parts
    concat_pcm = mp3_path.with_suffix(".pcm")
    with open(concat_pcm, "wb") as out:
        for p in pcm_files:
            out.write(p.read_bytes())

    wav_path = mp3_path.with_suffix(".wav")

    # PCM -> WAV
    subprocess.check_call([
        "ffmpeg", "-y",
        "-f", PCM_FORMAT,
        "-ar", str(PCM_RATE),
        "-ac", str(PCM_CHANNELS),
        "-i", str(concat_pcm),
        str(wav_path),
    ])

    # WAV -> MP3
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", str(wav_path),
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(mp3_path),
    ])

    # Cleanup best-effort
    try:
        concat_pcm.unlink()
        wav_path.unlink()
        for p in pcm_files:
            try:
                p.unlink()
            except Exception:
                pass
        pcm_dir.rmdir()
    except Exception:
        pass        f"{transcript}"
    )


def tts_to_mp3(script_text: str, mp3_path: Path, api_key: str) -> None:
    """
    Generate multi-speaker audio using Gemini TTS via generate_content(AUDIO),
    then convert to MP3.
    """
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)

    turns = parse_dialogue(script_text)

    # Hard fallback: never empty
    if not turns:
        turns = [
            ("SPEAKER_A", "This is an automated overview based on publicly available reporting."),
            ("SPEAKER_B", "We were unable to parse the script into a dialogue. Please check script generation."),
        ]

    prompt = build_multispeaker_prompt(turns)

    # Multi-speaker voice config (2 speakers)
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[
                    types.SpeakerVoiceConfig(
                        speaker="SPEAKER_A",
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_A)
                        ),
                    ),
                    types.SpeakerVoiceConfig(
                        speaker="SPEAKER_B",
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_B)
                        ),
                    ),
                ]
            )
        ),
    )

    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=prompt,
        config=cfg,
    )

    # Extract raw PCM bytes from inline_data
    # Docs show: response.candidates[0].content.parts[0].inline_data.data 1
    try:
        pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
    except Exception as e:
        raise RuntimeError(f"TTS returned no audio data. Response parse failed: {e}")

    # Write PCM then convert -> WAV -> MP3
    pcm_path = mp3_path.with_suffix(".pcm")
    wav_path = mp3_path.with_suffix(".wav")

    pcm_path.write_bytes(pcm_bytes)

    # PCM -> WAV
    subprocess.check_call([
        "ffmpeg", "-y",
        "-f", PCM_FORMAT,
        "-ar", str(PCM_RATE),
        "-ac", str(PCM_CHANNELS),
        "-i", str(pcm_path),
        str(wav_path),
    ])

    # WAV -> MP3
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", str(wav_path),
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(mp3_path),
    ])

    # Cleanup best-effort
    try:
        pcm_path.unlink()
        wav_path.unlink()
    except Exception:
        pass
