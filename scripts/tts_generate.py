import os
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

from google import genai
from google.genai import types


# Gemini TTS model (supports multispeaker)
# See: gemini-2.5-flash-preview-tts / gemini-2.5-pro-preview-tts
TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip()

# Prebuilt voices (Gemini TTS "voice_name" values)
# Defaults are examples from Google docs; you can override via env.
VOICE_A = os.environ.get("VOICE_A", "Kore").strip()
VOICE_B = os.environ.get("VOICE_B", "Puck").strip()

# Output audio format assumptions per docs for TTS PCM
PCM_RATE = 24000
PCM_CHANNELS = 1
PCM_FORMAT = "s16le"  # 16-bit signed little-endian


def parse_dialogue(script: str) -> List[Tuple[str, str]]:
    """
    Parse lines:
      SPEAKER_A: ...
      SPEAKER_B: ...
    Returns list of (speaker_name, text) where speaker_name is 'SPEAKER_A' or 'SPEAKER_B'.
    Any untagged non-empty line is treated as SPEAKER_A.
    """
    turns: List[Tuple[str, str]] = []
    if not script:
        return turns

    for raw_line in script.splitlines():
        line = raw_line.strip()
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
            # Fallback: treat as SPEAKER_A text
            turns.append(("SPEAKER_A", line))

    return turns


def build_multispeaker_prompt(turns: List[Tuple[str, str]]) -> str:
    """
    Build a conversation transcript the TTS model can recite.
    """
    # Keep it clean and predictable
    lines = []
    for spk, txt in turns:
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        lines.append(f"{spk}: {txt}")

    transcript = "\n".join(lines).strip()
    if not transcript:
        transcript = "SPEAKER_A: This is an automated overview."

    # Director-style instruction + transcript
    return (
        "TTS the following conversation between SPEAKER_A and SPEAKER_B.\n"
        "Style: clear, professional podcast delivery; natural pacing; crisp enunciation.\n"
        "SPEAKER_A should sound like a male host; SPEAKER_B like a female host.\n"
        "Transcript:\n"
        f"{transcript}"
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
