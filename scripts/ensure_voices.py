import hashlib
import subprocess
from pathlib import Path

BASE = Path("assets/piper")
BASE.mkdir(parents=True, exist_ok=True)

VOICES = {
    "en-US-male": "en_US-ryan-high.onnx",
    "en-US-female": "en_US-amy-medium.onnx",
}

VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/{file}"

def sha256(p: Path):
    h = hashlib.sha256()
    with p.open("rb") as f:
        h.update(f.read())
    return h.hexdigest()

for name, fname in VOICES.items():
    target = BASE / fname
    if target.exists():
        print(f"[OK] Voice exists: {fname}")
        continue

    url = VOICE_URL.format(file=fname)
    print(f"[DL] {fname}")
    subprocess.check_call(["curl", "-L", "-o", str(target), url])
