```markdown
# podcast-archive

Copyright (c) Agenda Podcast
All rights reserved.
This repository and its code are owned by Agenda Podcast. Copying,
redistribution, or usage without explicit written permission is prohibited.

Overview
- scripts/sync.py: sync script to import and sanitize a remote RSS feed, download enclosures, and generate data/episodes.json and feed/rss.xml.
- .github/workflows/sync.yml: GitHub Actions workflow that runs hourly and on demand; uploads audio files to GitHub Releases and validates the feed.
- audio/: downloaded MP3 assets (ignored from git, uploaded to Releases).
- feed/rss.xml: sanitized feed (can be hosted on GitHub Pages).
- data/episodes.json: structured channel + episodes metadata.

Hosting the feed (GitHub Pages)
1. Enable GitHub Pages for this repository:
   - On GitHub go to Settings → Pages (or Settings → Pages & deploy).
   - Set source to the default branch (main) and the root or /docs folder as desired.
   - Save and note the published site URL (e.g. https://<owner>.github.io/<repo>/).

2. Set PUBLIC_URL repository secret to the feed's public URL:
   - e.g. https://<owner>.github.io/<repo>/feed/rss.xml
   - On GitHub: Settings → Secrets and variables → Actions → New repository secret
     - Name: PUBLIC_URL
     - Value: https://<owner>.github.io/<repo>/feed/rss.xml

3. Optionally set POSTER_URL secret to your hosted poster image URL.

Run locally
- python scripts/sync.py --download --public-url "https://example.org/podcast" --poster "https://example.org/podcast/poster.jpg"

Notes
- The workflow uploads downloaded audio to timestamped GitHub Releases (auto-archive-YYYYMMDDHHMMSS).
- The workflow includes a validate job that checks feed/rss.xml is well-formed, does not contain "buzzsprout", and that the channel title matches data/episodes.json (or EXPECTED_CHANNEL_TITLE if provided as a secret).
```