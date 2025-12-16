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