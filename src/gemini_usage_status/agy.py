"""The few things the monitor asks of the Antigravity CLI itself.

`agy models` lists models with one read-only call and, as a side effect, lets
the CLI renew its own OAuth token. It starts no model turn and costs no quota.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Together with the HTTP timeouts these stay well inside the runner's overall
# limit, so a hang is reported with its cause instead of being killed silently.
VERSION_TIMEOUT = 10
REFRESH_TIMEOUT = 45

_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def find_agy():
    """agy on PATH, else the installer's default location."""
    return shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")


def agy_version(agy_bin, runner=subprocess.run):
    try:
        done = runner([agy_bin, "--version"], capture_output=True, text=True,
                      timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(done.stdout or "")
    return match.group(1) if match else None


def refresh_via_agy(agy_bin, runner=subprocess.run):
    """Run `agy models`; returns (ok, detail)."""
    try:
        done = runner([agy_bin, "models"], capture_output=True, text=True,
                      timeout=REFRESH_TIMEOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, f"agy not found at {agy_bin}"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"agy models failed: {type(error).__name__}"
    if done.returncode != 0:
        return False, f"agy models: exit {done.returncode}"
    return True, ""
