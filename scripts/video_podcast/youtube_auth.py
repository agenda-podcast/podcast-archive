# ASCII-only. No ellipses. Keep <= 500 lines.

from __future__ import annotations

import os
from typing import List, Optional


def youtube_scopes() -> List[str]:
    return ["https://www.googleapis.com/auth/youtube.upload"]


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError("Missing required env var: %s" % name)
    return v


def build_credentials():
    # Imported lazily so the repo can run without YouTube deps unless enabled.
    from google.oauth2.credentials import Credentials

    client_id = require_env("YOUTUBE_CLIENT_ID")
    client_secret = require_env("YOUTUBE_CLIENT_SECRET")
    refresh_token = require_env("YOUTUBE_REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=youtube_scopes(),
    )
    return creds


def refresh_credentials_or_fail() -> Optional[str]:
    """Return an error code string if refresh fails, else None."""
    # Imported lazily so the repo can run without YouTube deps unless enabled.
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    creds = build_credentials()
    try:
        creds.refresh(Request())
        return None
    except RefreshError as e:
        msg = str(e)
        if "deleted_client" in msg:
            return "deleted_client"
        if "invalid_client" in msg:
            return "invalid_client"
        if "invalid_grant" in msg:
            return "invalid_grant"
        return "refresh_error"
