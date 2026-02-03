from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .model import Episode, parse_episodes
from .repo_state import load_state


@dataclass(frozen=True)
class QueueDecision:
    action: str  # 'render' | 'upload' | 'none'
    guid: Optional[str]
    title: Optional[str]
    reason: str


def _read_status_csv(p: Path) -> List[Dict[str, str]]:
    if not p.exists():
        raise FileNotFoundError(f"status.csv not found: {p}")
    rows: List[Dict[str, str]] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({k: (v or "").strip() for k, v in r.items()})
    return rows


def decide_next(repo_root: Path, status_csv: Path) -> QueueDecision:
    """Return what to do next.

    Priority is ALWAYS: render backlog first. Upload only when every episode in
    episodes.json has a rendered video.

    "generated" is determined by data/video-data/state.json (processed entries).
    "uploaded" is determined ONLY by presence of YouTube video_id in state.
    """

    episodes_path = repo_root / "data" / "episodes.json"
    state_path = repo_root / "data" / "video-data" / "state.json"

    if episodes_path.exists() and state_path.exists():
        eps: List[Episode] = parse_episodes(episodes_path)
        st = load_state(state_path)
        processed = st.get("processed") or {}

        # Render backlog first: first episode not in processed is next to render.
        for ep in eps:
            if ep.guid not in processed:
                return QueueDecision(
                    action="render",
                    guid=ep.guid,
                    title=ep.title,
                    reason=f"pending_count>=1 processed={len(processed)}",
                )

        # All rendered: upload next lacking youtube id.
        for ep in eps:
            rec = processed.get(ep.guid) or {}
            y = rec.get("youtube") or {}
            vid = (y.get("video_id") or "").strip()
            if vid == "":
                return QueueDecision(
                    action="upload",
                    guid=ep.guid,
                    title=ep.title,
                    reason="all_rendered=1 not_uploaded>=1",
                )

        return QueueDecision(action="none", guid=None, title=None, reason="all_done=1")

    # Fallback: use status.csv only.
    rows = _read_status_csv(status_csv)
    pending = [r for r in rows if (r.get("status") or "").upper() != "RENDERED"]
    rendered = [r for r in rows if (r.get("status") or "").upper() == "RENDERED"]
    not_uploaded = [r for r in rendered if (r.get("youtube_video_id") or "").strip() == ""]

    if pending:
        r0 = pending[0]
        return QueueDecision(
            action="render",
            guid=r0.get("guid") or None,
            title=r0.get("title") or None,
            reason=f"pending_count={len(pending)} rendered_count={len(rendered)}",
        )

    if not_uploaded:
        r0 = not_uploaded[0]
        return QueueDecision(
            action="upload",
            guid=r0.get("guid") or None,
            title=r0.get("title") or None,
            reason=f"not_uploaded_count={len(not_uploaded)} rendered_count={len(rendered)}",
        )

    return QueueDecision(action="none", guid=None, title=None, reason=f"all_rendered={len(rendered)}")


def _write_github_outputs(dec: QueueDecision) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    p = Path(out_path)
    lines = [
        f"action={dec.action}",
        f"guid={dec.guid or ''}",
        f"title={dec.title or ''}",
        f"reason={dec.reason}",
    ]
    with p.open("a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    status_csv = repo_root / "data" / "video-data" / "status.csv"

    dec = decide_next(repo_root, status_csv)
    print(
        "[queue] action={a} guid={g} title={t} reason={r}".format(
            a=dec.action,
            g=dec.guid or "",
            t=(dec.title or "").replace("\n", " ")[:120],
            r=dec.reason,
        )
    )
    _write_github_outputs(dec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
