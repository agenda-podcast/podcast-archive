import os
import shutil
import tempfile
import unittest
from io import BytesIO
import wave
from unittest import mock

from scripts import tts_generate


def _silence_wav_bytes(sample_rate: int = 8000, seconds: float = 0.1) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = int(sample_rate * seconds)
        wf.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class TTSSmokeTests(unittest.TestCase):
    def test_piper_generation(self):
        silence = _silence_wav_bytes()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            tts_generate, "_piper_tts_wav_bytes", return_value=silence
        ), mock.patch.object(tts_generate, "_gemini_tts_wav_bytes", return_value=silence):
            mp3_path = f"{td}/out.mp3"
            _, provider = tts_generate.tts_chunks_to_mp3(
                [{"speaker": "A", "text": "Hello"}, {"speaker": "B", "text": "World"}],
                mp3_path,
                premium=False,
                sample_rate=8000,
            )
            self.assertEqual(provider, "piper")
            self.assertTrue(shutil.which("ffmpeg"))
            self.assertTrue(os.path.exists(mp3_path))
            self.assertGreater(os.path.getsize(mp3_path), 0)

    def test_gemini_fallback_to_piper(self):
        silence = _silence_wav_bytes()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            tts_generate, "_piper_tts_wav_bytes", return_value=silence
        ), mock.patch.object(
            tts_generate, "_gemini_tts_wav_bytes", side_effect=RuntimeError("forced fail")
        ):
            mp3_path = f"{td}/out.mp3"
            _, provider = tts_generate.tts_chunks_to_mp3(
                [{"speaker": "A", "text": "Gemini text"}],
                mp3_path,
                premium=True,
                gemini_api_key="test",
                gemini_tts_model="gemini-2.0-flash",
                sample_rate=8000,
            )
            self.assertEqual(provider, "piper")
            self.assertTrue(os.path.exists(mp3_path))
            self.assertGreater(os.path.getsize(mp3_path), 0)


if __name__ == "__main__":
    unittest.main()
