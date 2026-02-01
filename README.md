```markdown
# podcast-archive

Copyright (c) Agenda Podcast
All rights reserved.
This repository and its code are owned by Agenda Podcast. Copying,
redistribution, or usage without explicit written permission is prohibited.

## Overview

This repository provides a simple RSS feed archiving and sanitization system:

- **scripts/sync.py**: Imports and sanitizes a remote RSS feed (e.g., from Buzzsprout), downloads audio enclosures, and generates clean outputs
- **.github/workflows/sync.yml**: GitHub Actions workflow that runs hourly and on demand
- **feed/rss.xml**: Sanitized RSS feed (can be hosted on GitHub Pages)
- **data/episodes.json**: Structured episode metadata

## How It Works

1. Fetches episodes from the source RSS feed
2. Downloads audio files to temporary storage
3. Uploads audio files to GitHub Releases for permanent hosting
4. Generates a sanitized RSS feed with rewritten audio URLs
5. Commits the updated feed and metadata

## Setup

### Required Secrets

Configure these in GitHub: Settings → Secrets and variables → Actions

- **RSS**: Source RSS feed URL (required)
- **PODCAST_TITLE**: Custom podcast title (optional, defaults to "Agenda")
- **PODCAST_LINK**: Podcast website URL (optional)
- **PODCAST_DESCRIPTION**: Feed description (optional)
- **PODCAST_IMAGE**: Podcast artwork URL (optional)
- **ITUNES_CATEGORY**: iTunes category (optional, defaults to "News")
- **ITUNES_SUBCATEGORY**: iTunes subcategory (optional)

### GitHub Pages Hosting

1. Enable GitHub Pages:
   - Go to Settings → Pages
   - Set source to main branch, root directory
   - Note your published URL: `https://<owner>.github.io/<repo>/`

2. Your feed will be available at: `https://<owner>.github.io/<repo>/feed/rss.xml`

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run sync (requires environment variables)
export RSS="https://feeds.buzzsprout.com/..."
export REPO="owner/repo"
export GITHUB_TOKEN="your_token"
python scripts/sync.py
```

## Notes

- Audio files are uploaded to GitHub Releases (tag: `audio-archive`)
- The workflow runs hourly via cron schedule
- All references to the source feed provider (e.g., "buzzsprout") are removed from output
- Episode GUIDs remain stable across runs to prevent duplicates
```

## Video Podcast Renderer (B-roll + RSS audio)

This repo can render 16:9 video episodes by mixing stock B-roll (Pexels + Pixabay) with the episode audio,
then publish MP4s and per-episode manifests to GitHub Releases.

### Inputs
- Items source: `data/episodes.json` (full episode list, produced by `scripts/sync.py`)
- Per-episode audio URL: `audio_url` inside `data/episodes.json`

### State and outputs in the repo
- Render state and status: `data/video-data/`
  - `state.json`: processed GUIDs (used to render only new episodes)
  - `status.csv`: full list with PENDING/RENDERED
- Video RSS feed: `feed/video_podcast.xml`
  - Enclosure URLs point to release assets under tag `video-podcast`.

### GitHub Actions workflow (manual)
Workflow: `.github/workflows/render_video_podcast.yml` (workflow_dispatch)

It uses the repository secrets (no Actions environment).

Required repository secrets:
- `PEXELS_API_KEY`
- `PIXABAY_API_KEY`

Releases used:
- Tag `video-podcast`, title `video podcast` (MP4 assets)
- Tag `video-podcast-manifests`, title `video podcast manifests` (manifest JSON assets)

Note: Git tags cannot contain spaces, so the release tags use hyphens.

### How it works
- Renders only episodes whose `guid` is not yet present in `data/video-data/state.json`
- Each episode:
  - downloads audio
  - searches for related stock videos
  - cuts random 15s clips (deterministic per guid)
  - concatenates clips to match audio duration
  - adds intro and outro from `data/raw_2_1440p_crf15_aac256.mp4`
  - overlays a static frame PNG on top of the full video from `data/video_frame.png`
  - muxes audio so the episode audio starts after the intro and ends before the outro
  - writes a per-episode manifest JSON listing clip provenance

### Required local assets for rendering
Two assets are expected to exist in the repository:

- `data/raw_2_1440p_crf15_aac256.mp4` (intro/outro)
- `data/video_frame.png` (overlay frame)

This repo includes small placeholder files at these paths so the pipeline can run end-to-end.
Replace them with your real intro/outro and frame assets.

### YouTube upload
The workflow can optionally upload rendered MP4s to YouTube.

#### Required secrets in repository secrets
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

#### Get a refresh token (one-time, on your laptop)
1) Create a Google Cloud project and enable the YouTube Data API.
2) Create OAuth client credentials and copy the client id and client secret.
3) Install dependencies locally:

```bash
pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
```

4) Run the helper to get a refresh token:

```bash
python -m scripts.video_podcast.youtube_oauth_local \
  --client-id "YOUR_CLIENT_ID" \
  --client-secret "YOUR_CLIENT_SECRET" \
  --out youtube_refresh_token.json
```

5) Add the refresh token to repository secrets as `YOUTUBE_REFRESH_TOKEN`.

#### Run upload in Actions
In the manual workflow `Render Video Podcast`, set `upload_to_youtube` to `true`.
The workflow will upload any rendered episodes that do not yet have a YouTube video id stored in `data/video-data/state.json`.
