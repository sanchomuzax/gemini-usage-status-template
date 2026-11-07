"""Read the quota from the Antigravity CLI's own /usage panel.

A long-running `agy` lives in a detached tmux session. The CLI keeps its own
login fresh there, exactly as the Codex monitor leans on `codex app-server`.
Every reading opens /usage, captures the screen, and closes the panel again.
No model turn is started, so a reading costs no quota.

The panel is turned into the same shape the quota summary endpoint returns,
so normalisation and the replica-flapping guard stay unchanged.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION = "gemini-usage-agy"
WAIT_SECONDS = 20
READY_SECONDS = 40
COMMAND = "/usage"
# Shown under the prompt once agy has finished starting.
READY_MARK = "? for shortcuts"

WINDOW_MINUTES = {"5h": 300, "weekly": 10080}

# Panel heading -> (group name as the HTTP summary spells it, bucket id prefix)
KNOWN_GROUPS = {
    "GEMINI MODELS": ("Gemini Models", "gemini"),
    "CLAUDE AND GPT MODELS": ("Claude and GPT models", "3p"),
    "GEMINI 4 ARGON": ("Gemini 4 Argon", "gemini-argon-wtu"),
}

_HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 &/.-]{3,}$")
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
_COUNTDOWN = re.compile(r"Refreshes in\s+(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?")
_BANNER_ACCOUNT = re.compile(r"(\S+@\S+)\s+\(([^)]+)\)")
_PANEL_ACCOUNT = re.compile(r"Account:\s*(\S+@\S+)")
_VERSION = re.compile(r"Antigravity CLI\s+(\d+\.\d+\.\d+)")
_SIGN_IN = re.compile(r"accounts\.google\.com|sign in|log in with google", re.IGNORECASE)


class UsageParseError(RuntimeError):
    pass


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(line):
    return line.strip().lstrip("│└├┌ ").strip()


def _window(label):
    lowered = label.lower()
    if "five hour" in lowered or "5-hour" in lowered or "5 hour" in lowered:
        return "5h"
    if "weekly" in lowered:
        return "weekly"
    return None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _reset(line, window, now):
    match = _COUNTDOWN.search(line)
    if match and any(match.groups()):
        days, hours, minutes = (int(x) if x else 0 for x in match.groups())
        return _iso(now.replace(second=0, microsecond=0) + timedelta(days=days, hours=hours, minutes=minutes))
    if "Quota available" in line:
        # An untouched window: the backend would report now + window length.
        return _iso(now.replace(microsecond=0) + timedelta(minutes=WINDOW_MINUTES[window]))
    return None


def parse_usage(text, now):
    """(summary, meta) from a captured /usage screen; raises UsageParseError."""
    lines = [_clean(line) for line in text.splitlines()]
    groups, current, pending = [], None, None
    for index, line in enumerate(lines):
        if _HEADING.match(line) and "%" not in line:
            name, prefix = KNOWN_GROUPS.get(line, (line.title(), _slug(line)))
            current = {"displayName": name, "buckets": [], "_prefix": prefix}
            groups.append(current)
            pending = None
            continue
        if current is None:
            continue
        window = _window(line) if "%" not in line and "Refreshes" not in line else None
        if window and ("Limit" in line):
            pending = {"window": window, "fraction": None}
            continue
        if pending and pending["fraction"] is None:
            match = _PERCENT.search(line)
            if match:
                pending["fraction"] = round(float(match.group(1)) / 100, 6)
            continue
        if pending and pending["fraction"] is not None:
            reset = _reset(line, pending["window"], now)
            if reset:
                suffix = "5h" if pending["window"] == "5h" else "weekly"
                current["buckets"].append({
                    "bucketId": f"{current['_prefix']}-{suffix}",
                    "window": pending["window"],
                    "remainingFraction": pending["fraction"],
                    "resetTime": reset,
                })
                pending = None
    found = [{k: v for k, v in g.items() if k != "_prefix"} for g in groups if g["buckets"]]
    if not found:
        if _SIGN_IN.search(text):
            raise UsageParseError("agy asks to sign in; run `tmux attach -t "
                                  f"{SESSION}` and sign in once")
        raise UsageParseError("no quota buckets on the /usage screen")
    banner = _BANNER_ACCOUNT.search(text)
    account = _PANEL_ACCOUNT.search(text)
    version = _VERSION.search(text)
    # The start banner (plan, version) scrolls away; the panel always names the account.
    meta = {
        "email": account.group(1) if account else (banner.group(1) if banner else None),
        "plan": banner.group(2) if banner else None,
        "agy_version": version.group(1) if version else None,
    }
    return {"groups": found}, meta


class AgySession:
    """The detached tmux session that keeps `agy` running and signed in."""

    def __init__(self, name=SESSION, agy_bin=None, runner=subprocess.run, sleep=time.sleep,
                 wait_seconds=WAIT_SECONDS):
        self.name = name
        self.agy_bin = agy_bin or str(Path.home() / ".local" / "bin" / "agy")
        self.run = runner
        self.sleep = sleep
        self.wait_seconds = wait_seconds

    def _tmux(self, *args):
        return self.run(["tmux", *args], capture_output=True, text=True, timeout=15)

    def ensure(self):
        """Start agy if needed, in plan mode, and wait until it is ready."""
        if self._tmux("has-session", "-t", self.name).returncode != 0:
            # Plan mode: should text ever reach the model, it cannot run anything.
            self._tmux("new-session", "-d", "-s", self.name, "-x", "200", "-y", "60",
                       "-c", str(Path.home()), self.agy_bin, "--mode", "plan")
        for _ in range(READY_SECONDS):
            if READY_MARK in self._screen():
                return
            self.sleep(1)
        raise UsageParseError(f"agy in tmux session {self.name} did not become ready")

    def _screen(self, history=0):
        args = ["capture-pane", "-p", "-t", self.name] + (["-S", f"-{history}"] if history else [])
        return self._tmux(*args).stdout or ""

    def _typed_exactly(self, text):
        """True if the input line reads exactly `text`.

        Typing a slash opens a suggestion list whose rows also start with '>'
        ("> /usage  View model quota usage"), so look for an exact line, never
        just the last '>' row.
        """
        return any(line.strip() == f"> {text}" for line in self._screen().splitlines())

    def _type_command(self):
        """Type /usage and press Enter only if the input line reads exactly that.

        Never sends Escape right before typing: the terminal reads ESC followed
        by '/' as Alt+/, the slash is lost, and the rest becomes a prompt.
        """
        self._tmux("send-keys", "-t", self.name, "C-u")
        self.sleep(0.5)
        self._tmux("send-keys", "-t", self.name, "-l", COMMAND)
        self.sleep(0.5)
        if not self._typed_exactly(COMMAND):
            self._tmux("send-keys", "-t", self.name, "C-u")
            raise UsageParseError("could not type /usage safely into agy; nothing was sent")
        self._tmux("send-keys", "-t", self.name, "Enter")

    def read_usage(self):
        """Open /usage, wait for the quota to render, capture, close."""
        self.ensure()
        self._type_command()
        waited = 0
        try:
            while waited <= self.wait_seconds:
                self.sleep(1)
                waited += 1
                screen = self._screen(history=120)
                if "Models & Quota" in screen and ("Refreshes in" in screen or "Quota available" in screen):
                    return screen
                if _SIGN_IN.search(screen) and "Models & Quota" not in screen:
                    return screen
            raise UsageParseError(f"agy /usage did not show the quota within {self.wait_seconds}s")
        finally:
            self._tmux("send-keys", "-t", self.name, "Escape")
