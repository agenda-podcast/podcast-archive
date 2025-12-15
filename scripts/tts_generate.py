import subprocess
from pathlib import Path
from typing import List, Tuple

from google import genai

VOICE_MALE = "en-US-Neutral-2"
VOICE_FEMALE = "en-US-Neutral-1"
TURN_GAP_MS = 250


def parse_dialogue(script: str) -> List[Tuple[str, str]]:
    """
    Expected format:
      SPEAKER_A: ...
      SPEAKER_B: ...
    Returns list of (speaker, text) where speaker is 'male' or 'female'
    """
    turns: List[Tuple[str, str]] = []
    if not script:
        return turns

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("SPEAKER_A:"):
            txt = line.replace("SPEAKER_A:", "", 1).strip()
            if txt:
                turns.append(("male", txt))
        elif line.startswith("SPEAKER_B:"):
            txt = line.replace("SPEAKER_B:", "", 1).strip()
            if txt:
                turns.append(("female", txt))
        else:
            # Fallback: treat as SPEAKER_A line
            turns.append(("male", line))

    return turns


def synthesize_turn(client: genai.Client, text: str, voice_name: str) -> bytes:
    response = client.models.generate_speech(
        model="gemini-tts",
        contents=text,
        config={"voice": {"name": voice_name}},
    )
    return response.audio


def tts_to_mp3(script_text: str, mp3_path: Path, api_key: str):
    """
    Dialog TTS with 2 voices. Never fails due to missing tags:
    if dialogue not parseable, wraps whole text as SPEAKER_A.
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    wav_dir = mp3_path.parent / "_wav_parts"
    wav_dir.mkdir(exist_ok=True)

    client = genai.Client(api_key=api_key)

    turns = parse_dialogue(script_text)

    # HARDENING: never fail here
    if not turns:
        # If script_text is empty, generate a minimal placeholder
        fallback = script_text.strip() if script_text else ""
        if not fallback:
            fallback = "This is an automated overview. There was insufficient text to generate a full dialogue."
        turns = [("male", fallback)]

    wav_files: List[Path] = []

    for idx, (speaker, text) in enumerate(turns):
        voice = VOICE_MALE if speaker == "male" else VOICE_FEMALE

        wav_bytes = synthesize_turn(client, text, voice)
        wav_path = wav_dir / f"turn_{idx:04d}.wav"
        wav_path.write_bytes(wav_bytes)
        wav_files.append(wav_path)

        # Add small silence gap between turns
        gap_path = wav_dir / f"gap_{idx:04d}.wav"
        subprocess.check_call([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(TURN_GAP_MS / 1000),
            str(gap_path)
        ])
        wav_files.append(gap_path)

    concat_file = wav_dir / "concat.txt"
    concat_file.write_text(
        "\n".join([f"file '{p.absolute()}'" for p in wav_files]),
        encoding="utf-8"
    )

    final_wav = mp3_path.with_suffix(".wav")

    subprocess.check_call([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(final_wav)
    ])

    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", str(final_wav),
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(mp3_path)
    ])

    # Cleanup best-effort
    try:
        final_wav.unlink()
        for f in wav_files:
            f.unlink()
        concat_file.unlink()
        wav_dir.rmdir()
    except Exception:
        pass
