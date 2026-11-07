#!/usr/bin/env python3
"""Record one reading: write status.json, append history, decide on a commit.

Reads fetch_limits.py output from stdin. The last line printed is the decision
the runner script acts on: "COMMIT <reasons>" or "SKIP unchanged".

The comparison is against .last_published.json, which the runner refreshes
with `record_status.py --published` after a successful commit.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR / "src"))

from gemini_usage_status.fsutil import atomic_write_text  # noqa: E402
from gemini_usage_status.publish import (  # noqa: E402
    append_history,
    build_status,
    commit_reasons,
    write_status,
)


def _parse(text):
    if not text.strip():
        return {"error": "fetch_limits.py produced no output"}
    try:
        data = json.loads(text)
    except ValueError as error:
        return {"error": f"unparseable fetch_limits.py output: {error}"}
    return data if isinstance(data, dict) else {"error": "fetch_limits.py output is not an object"}


PUBLISHED_FILE = ".last_published.json"


def _previous(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_published(repo_dir=REPO_DIR):
    """Remember the reading that just went into a commit."""
    text = (Path(repo_dir) / "status.json").read_text(encoding="utf-8")
    atomic_write_text(Path(repo_dir) / PUBLISHED_FILE, text)


def main(limits_text, repo_dir=REPO_DIR, now=None, out=sys.stdout):
    now = now or datetime.now(timezone.utc)
    status_path = Path(repo_dir) / "status.json"
    status = build_status(_parse(limits_text), now)
    reasons = commit_reasons(_previous(Path(repo_dir) / PUBLISHED_FILE), status, now)
    write_status(status_path, status)
    try:
        append_history(Path(repo_dir) / "history", status)
    except OSError as error:
        print(f"warning: cannot append history: {error}", file=out)
    print("COMMIT " + "; ".join(reasons) if reasons else "SKIP unchanged", file=out)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--published"]:
        mark_published()
        sys.exit(0)
    sys.exit(main(sys.stdin.read()))
