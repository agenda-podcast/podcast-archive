#!/usr/bin/env python3
# ASCII-only. No ellipses. Keep <= 500 lines.

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict


def _scopes():
    return ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--out", default="youtube_refresh_token.json")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as e:
        print("Missing google-auth-oauthlib. Install: pip install google-auth-oauthlib", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 2

    client_config: Dict[str, Any] = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=_scopes())
    creds = flow.run_local_server(port=0)
    if not creds or not creds.refresh_token:
        print("No refresh token returned. Ensure consent prompt is shown.", file=sys.stderr)
        return 2

    out = {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": creds.refresh_token,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")

    print("Wrote refresh token to %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
