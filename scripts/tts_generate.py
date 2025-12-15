import subprocess
from pathlib import Path
from typing import List, Tuple

from google import genai

# -------------------------------------------------
# CONFIG — voices close to NotebookLM deep dive
# -------------------------------------------------
# Эти имена соответствуют нейтральным news-style голосам Gemini TTS.
# Они стабильны и официально поддерживаются.
VOICE_MALE = "en-US-Neutral-2"
VOICE_FEMALE = "en-US-Neutral-1"

# Small pause between dialogue turns (ms)
TURN_GAP_MS = 250


def parse_dialogue(script: str) -> List[Tuple[str, str]]:
    """
    Parse script into dialogue turns.

    Expected format:
      SPEAKER_A: text...
      SPEAKER_B: text...

    Returns list of (voice, text)
    """
    turns: List[Tuple[str, str]] = []

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("SPEAKER_A:"):
            turns.append(("male", line.replace("SPEAKER_A:", "", 1).strip()))
        elif line.startswith("SPEAKER_B:"):
            turns.append(("female", line.replace("SPEAKER_B:", "", 1).strip()))
        else:
            # Fallback: narrator → male
            turns.append(("male", line))

    return turns


def synthesize_turn(client: genai.Client, text: str, voice_name: str) -> bytes:
    """
    Generate WAV audio bytes for a single turn.
    """
    response = client.models.generate_speech(
        model="gemini-tts",
        contents=text,
        config={
            "voice": {
                "name": voice_name
            }
        }
    )
    return response.audio


def tts_to_mp3(script_text: str, mp3_path: Path, api_key: str):
    """
    Generate dialog-style TTS with 2 voices (male/female),
    merge into a single MP3.

    Output:
      mp3_path
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    wav_dir = mp3_path.parent / "_wav_parts"
    wav_dir.mkdir(exist_ok=True)

    client = genai.Client(api_key=api_key)

    turns = parse_dialogue(script_text)
    if not turns:
        raise RuntimeError("No dialogue turns parsed from script.")

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
            "-i", f"anullsrc=r=24000:cl=mono",
            "-t", str(TURN_GAP_MS / 1000),
            str(gap_path)
        ])
        wav_files.append(gap_path)

    # Concatenate WAVs
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

    # Convert to MP3 (high quality, podcast-ready)
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", str(final_wav),
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(mp3_path)
    ])

    # Cleanup
    try:
        final_wav.unlink()
        for f in wav_files:
            f.unlink()
        concat_file.unlink()
        wav_dir.rmdir()
    except Exception:
        pass
