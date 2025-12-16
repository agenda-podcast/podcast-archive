import tempfile
import unittest
from pathlib import Path

from scripts.run_topic import _require_gemini_key, _validate_outputs


class RunTopicValidationTests(unittest.TestCase):
    def test_require_gemini_key_rejects_empty(self):
        with self.assertRaises(RuntimeError):
            _require_gemini_key("topic-01", "")

    def test_validate_outputs_requires_audio(self):
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "out.mp3"
            mp4 = Path(td) / "out.mp4"
            with self.assertRaises(RuntimeError):
                _validate_outputs("topic-01", audio_ok=mp3.exists(), video_ok=mp4.exists(), video_enabled=True)

    def test_validate_outputs_requires_video_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "out.mp3"
            mp4 = Path(td) / "out.mp4"
            mp3.write_bytes(b"ok")
            with self.assertRaises(RuntimeError):
                _validate_outputs(
                    "topic-01",
                    audio_ok=mp3.exists() and mp3.stat().st_size > 0,
                    video_ok=mp4.exists(),
                    video_enabled=True,
                )

    def test_validate_outputs_allows_missing_video_when_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "out.mp3"
            mp4 = Path(td) / "out.mp4"
            mp3.write_bytes(b"ok")
            # should not raise because video is disabled
            _validate_outputs(
                "topic-01",
                audio_ok=mp3.exists() and mp3.stat().st_size > 0,
                video_ok=mp4.exists(),
                video_enabled=False,
            )


if __name__ == "__main__":
    unittest.main()
