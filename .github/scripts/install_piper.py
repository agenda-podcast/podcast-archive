#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_piper.py

Downloads and installs the piper TTS binary from the latest GitHub release.
Designed for CI environments to automatically provision the piper executable.

Usage:
    python .github/scripts/install_piper.py

Exits with non-zero code on failure with helpful error messages.
Installs piper to ./tools/piper and makes it executable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def main() -> None:
    """
    Query GitHub API for latest rhasspy/piper release, download appropriate asset,
    extract if needed, and install to tools/piper.
    """
    api_url = "https://api.github.com/repos/rhasspy/piper/releases/latest"
    
    print(f"Fetching: {api_url}")
    try:
        with urllib.request.urlopen(api_url, timeout=30) as response:
            data = json.load(response)
    except Exception as e:
        print(f"ERROR: Failed to fetch release data from GitHub API: {e}", file=sys.stderr)
        sys.exit(1)

    assets = data.get("assets", [])
    if not assets:
        print("ERROR: No assets found in latest release", file=sys.stderr)
        sys.exit(2)

    # Prefer linux + amd64 assets
    candidates = []
    for asset in assets:
        name = asset.get("name", "").lower()
        url = asset.get("browser_download_url")
        if not url:
            continue
        
        # Check for linux and amd64/x86_64/x86 architecture
        if "linux" in name and ("amd64" in name or "x86_64" in name or "x86" in name):
            candidates.append((name, url))

    # Fallback to first available asset if no linux+amd64 match
    if not candidates and assets:
        print("WARNING: No linux+amd64 asset found, using first available asset")
        candidates = [(a.get("name", ""), a.get("browser_download_url")) 
                     for a in assets if a.get("browser_download_url")]

    if not candidates:
        asset_names = [a.get("name", "N/A") for a in assets]
        print(f"ERROR: No suitable release asset found. Available assets: {asset_names}", file=sys.stderr)
        sys.exit(2)

    asset_name, download_url = candidates[0]
    print(f"Selected asset: {asset_name}")
    print(f"Download URL: {download_url}")

    # Download to temporary directory
    tmpdir = tempfile.mkdtemp(prefix="piper-install-")
    archive_path = os.path.join(tmpdir, "asset")
    
    print(f"Downloading to {archive_path}")
    try:
        subprocess.check_call(
            ["curl", "-L", "-o", archive_path, download_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to download asset: {e}", file=sys.stderr)
        sys.exit(3)
    except FileNotFoundError:
        print("ERROR: 'curl' command not found. Please install curl.", file=sys.stderr)
        sys.exit(3)

    # Try to extract if it's an archive
    extracted = try_extract_archive(archive_path, tmpdir)
    
    # Locate piper executable
    piper_path = find_piper_executable(tmpdir, archive_path, extracted)
    
    if not piper_path:
        print("ERROR: Failed to locate 'piper' executable in downloaded asset.", file=sys.stderr)
        print(f"Contents of {tmpdir}:")
        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                print(f"  {os.path.join(root, f)}")
        sys.exit(4)

    # Install to tools/piper
    tools_dir = Path.cwd() / "tools"
    tools_dir.mkdir(exist_ok=True)
    
    final_path = tools_dir / "piper"
    shutil.copy(piper_path, final_path)
    os.chmod(final_path, 0o755)
    
    print(f"SUCCESS: Installed piper to {final_path}")
    print(f"Piper location: {final_path.absolute()}")
    
    # Verify it's executable
    if not os.access(final_path, os.X_OK):
        print(f"WARNING: Piper binary at {final_path} may not be executable", file=sys.stderr)
    
    # Clean up temp directory
    try:
        shutil.rmtree(tmpdir)
    except Exception:
        pass  # Best effort cleanup


def try_extract_archive(archive_path: str, dest_dir: str) -> bool:
    """
    Attempt to extract the archive. Returns True if extraction succeeded.
    """
    try:
        if tarfile.is_tarfile(archive_path):
            print(f"Extracting tarball: {archive_path}")
            with tarfile.open(archive_path) as tf:
                tf.extractall(dest_dir)
            return True
    except Exception as e:
        print(f"Warning: Failed to extract as tarfile: {e}")

    try:
        if zipfile.is_zipfile(archive_path):
            print(f"Extracting zip: {archive_path}")
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
            return True
    except Exception as e:
        print(f"Warning: Failed to extract as zipfile: {e}")

    return False


def find_piper_executable(tmpdir: str, archive_path: str, was_extracted: bool) -> str | None:
    """
    Search for the piper executable in the extracted files or treat the
    downloaded file as the binary itself.
    """
    if was_extracted:
        # Search for a file named 'piper'
        for root, dirs, files in os.walk(tmpdir):
            if "piper" in files:
                candidate = os.path.join(root, "piper")
                print(f"Found piper executable: {candidate}")
                return candidate
        
        # If no file named 'piper', look for any executable file
        for root, dirs, files in os.walk(tmpdir):
            for filename in files:
                filepath = os.path.join(root, filename)
                if os.path.isfile(filepath) and os.access(filepath, os.X_OK):
                    print(f"Found executable file (fallback): {filepath}")
                    return filepath
    else:
        # Treat the downloaded file as the binary itself
        print("Archive not extracted, treating downloaded file as piper binary")
        candidate = os.path.join(tmpdir, "piper.bin")
        shutil.move(archive_path, candidate)
        os.chmod(candidate, 0o755)
        return candidate

    return None


if __name__ == "__main__":
    main()
