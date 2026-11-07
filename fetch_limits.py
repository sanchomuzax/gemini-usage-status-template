#!/usr/bin/env python3
"""Fetch real Google AI Pro (Antigravity) quota utilization.

Reads the Antigravity CLI's own /usage panel from a long-running `agy` in a
detached tmux session (started on demand). The CLI keeps its login fresh
there itself. No model turn is started, so a reading costs no quota.

Privacy: only quota figures, the plan and the account's short name are
emitted. Prints one JSON object and never raises.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gemini_usage_status.collect import collect  # noqa: E402


def main():
    try:
        result = collect()
    except Exception as error:  # noqa: BLE001 -- stdout must always be one JSON object
        result = {"error": f"unexpected failure: {type(error).__name__}: {error}"}
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
