# -*- coding: utf-8 -*-
"""
tts_generate.py
Additions:
- find_piper_binary(): determine piper executable path (PIPER_BINARY env var preferred)
- _piper_tts_wav_bytes updated to use resolved piper path and raise helpful errors containing stderr
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from typing import Optional

# -------------------------
# IMPORTANT:
# - If your repo already has find_piper_binary/_piper_tts_wav_bytes implementations,
#   merge the logic below into the existing functions instead of duplicating.
# - Ensure real CLI flags used by your project for piper are preserved in cmd.
# -------------------------


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

            Quick options:
              - Download a prebuilt piper binary for your platform and place it on PATH, e.g. /usr/local/bin/piper.
              - In CI, download the release asset and chmod +x it; then set PIPER_BINARY to point at it.

            Example (Linux):
              curl -L -o /tmp/piper.tar.gz "<RELEASE_ASSET_URL>"
              tar -xzf /tmp/piper.tar.gz -C /tmp
              sudo mv /tmp/piper /usr/local/bin/piper && sudo chmod +x /usr/local/bin/piper

            Or set:
              export PIPER_BINARY=/path/to/piper
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

    # Build the command. Replace or extend these flags to match your project's expected piper args.
    cmd = [
        piper_exe,
        "--voice",
        voice,
    ]
    if model_dir:
        cmd.extend(["--model-dir", model_dir])

    # Send text on stdin and capture stdout/stderr.
    try:
        proc = subprocess.run(
            cmd,
            input=(text.strip() + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as e:
        # Exec-level errors (e.g., permission issues)
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

# The rest of the original tts_generate.py file should remain unchanged below.
# Integrate the above functions into the existing codebase where your code previously ran 'piper'.
