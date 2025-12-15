# -*- coding: utf-8 -*-
"""
run_topic.py
Wrap calls to TTS generation with helpful error handling so CI logs show actionable messages.
"""

from __future__ import annotations

import sys
import logging

# Import the tts function(s) used in this script.
# Replace or adjust the import below to match your existing codebase.
try:
    from scripts.tts_generate import tts_chunks_to_mp3  # example; adjust to your actual function
except Exception:
    # If your project imports differently, keep the existing import lines instead of this block.
    pass

# -------------------------
# IMPORTANT:
# - Replace the `# ----- BEGIN TTS CALL -----` placeholder with your actual TTS invocation.
# - Do not remove other important logic; only wrap the TTS call in try/except as shown.
# -------------------------

def main() -> None:
    # existing setup logic should remain before this try/except
    try:
        # ----- BEGIN TTS CALL -----
        # Example placeholder call; REPLACE with your real invocation such as:
        #   tts_chunks_to_mp3(...actual arguments...)
        #
        # Example:
        # tts_chunks_to_mp3(chunks, voice=voice, model_dir=model_dir)
        #
        # If your script runs many steps, wrap the specific step that invokes piper.
        pass
        # ----- END TTS CALL -----
    except RuntimeError as e:
        logging.error("TTS failure: %s", e)
        print("Error: TTS step failed. See logs for details.", file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # rest of main flow continues here...
    # sys.exit(0) or return


if __name__ == "__main__":
    main()
