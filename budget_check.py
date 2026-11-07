#!/usr/bin/env python3
"""Decide whether it is safe to start or continue expensive Antigravity work.

Prints a JSON verdict (or one line with --brief) and exits with:
  0 = GO       start anything
  1 = CAUTION  small/sequential work only, no large fan-outs
  2 = STOP     do not start new work; checkpoint and wind down
  3 = UNKNOWN  quota could not be read (fail open, but say so)

Usage:
  python3 budget_check.py [--brief] [--group gemini|3p|argon]

--group picks the model group the work runs on: gemini (default, Flash/Pro),
3p (Claude and GPT inside Antigravity) or argon (Gemini 4 Argon weekly cap).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR / "src"))

from gemini_usage_status.budget import GROUPS, brief_line, evaluate, profile_name, thresholds  # noqa: E402
from gemini_usage_status.publish import build_status  # noqa: E402

CACHE_MAX_AGE_MINUTES = 90
_CODES = {"GO": 0, "CAUTION": 1, "STOP": 2, "UNKNOWN": 3}


def live_status():
    """A fresh reading through fetch_limits.py, or None when it fails."""
    try:
        done = subprocess.run([sys.executable, str(REPO_DIR / "fetch_limits.py")],
                              capture_output=True, text=True, timeout=240)
        data = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    return build_status(data, datetime.now(timezone.utc))


def cached_status(repo_dir):
    try:
        data = json.loads((Path(repo_dir) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _age_minutes(status, now):
    try:
        stamp = datetime.strptime(status["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return round((now - stamp).total_seconds() / 60)


def _group(argv):
    """The requested group; None for an unknown one (never a silent default)."""
    for index, arg in enumerate(argv):
        if arg.startswith("--group="):
            value = arg.split("=", 1)[1]
            return value if value in GROUPS else None
        if arg == "--group":
            value = argv[index + 1] if index + 1 < len(argv) else ""
            return value if value in GROUPS else None
    return "gemini"


def main(argv=None, repo_dir=REPO_DIR, now=None, environ=None, out=sys.stdout):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    try:
        result = _verdict(argv, repo_dir, now or datetime.now(timezone.utc), environ)
    except Exception as error:  # noqa: BLE001 -- a crash would exit 1, which reads as CAUTION
        result = {"verdict": "UNKNOWN", "reasons": [f"budget check failed: {type(error).__name__}: {error}"]}
    if "--brief" in argv:
        print(brief_line(result), file=out)
    else:
        print(json.dumps(result, indent=2), file=out)
    return _CODES[result["verdict"]]


def _verdict(argv, repo_dir, now, environ):
    group = _group(argv)
    if group is None:
        return {"verdict": "UNKNOWN", "reasons": [f"unknown --group; use one of {', '.join(GROUPS)}"]}

    status, source, age = live_status(), "live", 0
    if status is None:
        status, source = cached_status(repo_dir), "cached"
        age = _age_minutes(status, now) if status else None

    if status is None:
        result = {"verdict": "UNKNOWN", "reasons": ["no live quota and no readable status.json; "
                                                    "check that agy is signed in"]}
    elif source == "cached" and (age is None or age > CACHE_MAX_AGE_MINUTES):
        result = {"verdict": "UNKNOWN", "reasons": [f"live read failed and the cached reading is "
                                                    f"{age}m old; check that agy is signed in"]}
    else:
        result = evaluate(status, group, thresholds(environ), now, source=source, age=age)

    return {**result, "source": source if status else None, "data_age_minutes": age,
            "group": group, "account": (status or {}).get("account"),
            "profile": profile_name(environ)}


if __name__ == "__main__":
    sys.exit(main())
