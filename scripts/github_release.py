import os
import subprocess
from pathlib import Path

def ensure_release_and_upload(repo: str, token: str, tag: str, files: list[Path]) -> dict:
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token

    # Ensure release exists
    # If not exists: create
    r = subprocess.run(["gh", "release", "view", tag, "--repo", repo], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.check_call(["gh", "release", "create", tag, "--repo", repo, "--title", tag, "--notes", "Automated daily overview"], env=env)

    urls = {}
    for f in files:
        f = Path(f)
        # upload (overwrite if exists)
        subprocess.check_call(["gh", "release", "upload", tag, str(f), "--repo", repo, "--clobber"], env=env)
        # deterministic download URL
        urls[f.name] = f"https://github.com/{repo}/releases/download/{tag}/{f.name}"
    return urls
