"""Read the quota with `agy -p /usage --output-format json`.

The Antigravity CLI answers its own slash commands in print mode without a
model turn (documented under "Headless mode"): the reply carries the quota
groups as JSON, `num_turns` is 0 and no tokens are counted. agy refreshes its
own login for the run, so nothing here touches the OAuth token.

The reply is turned into the shape the quota summary endpoint returns, so
normalisation and the replica-flapping guard stay unchanged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

COMMAND = ["--mode", "plan", "-p", "/usage", "--output-format", "json"]
# A healthy reply takes 2-4 s. Google's quota call occasionally never answers and
# agy waits for it forever (seen 2026-11-07), so give up early and try again.
TIMEOUT_SECONDS = 20
# agy treats its working directory as a workspace and watches every file in it,
# so it runs from an empty directory of its own.
WORKDIR = Path.home() / ".cache" / "gemini-usage-status" / "agy-workdir"
TOKEN_FILE = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


class UsageParseError(RuntimeError):
    pass


def _json_object(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise UsageParseError("no JSON in the agy reply")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as error:
        raise UsageParseError(f"no JSON in the agy reply ({error})") from error


def _bucket(raw):
    bucket = {"bucketId": raw.get("id"), "window": raw.get("window")}
    if "remaining_fraction" in raw:
        bucket["remainingFraction"] = raw["remaining_fraction"]
    bucket["resetTime"] = raw.get("reset_time")
    return bucket


def parse_usage_json(text):
    """Quota summary from the print-mode reply; raises UsageParseError."""
    if not text or not text.strip():
        raise UsageParseError("no output from agy")
    reply = _json_object(text)
    if reply.get("status") != "SUCCESS":
        detail = reply.get("error") or reply.get("response") or reply.get("status")
        raise UsageParseError(f"agy /usage failed: {str(detail)[:300]}")
    # The whole point is that a reading costs nothing. If agy ever hands
    # "/usage" to the model instead, stop loudly rather than burn quota.
    tokens = (reply.get("usage") or {}).get("total_tokens", 0)
    if reply.get("num_turns", 0) != 0 or tokens:
        raise UsageParseError("agy ran a model turn for /usage; refusing to use it")
    data = (reply.get("command") or {}).get("data") or {}
    groups = [{"displayName": g.get("name"), "buckets": [_bucket(b) for b in g.get("buckets") or []]}
              for g in data.get("groups") or []]
    groups = [g for g in groups if g["buckets"]]
    if not groups:
        raise UsageParseError("no quota buckets in the agy /usage reply")
    return {"groups": groups}


class AgyPrint:
    """One /usage reading per `read()`, plus the account e-mail and CLI version."""

    def __init__(self, agy_bin=None, *, runner=subprocess.run, token_file=TOKEN_FILE,
                 workdir=WORKDIR):
        self.agy = agy_bin or shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
        self.run = runner
        self.token_file = Path(token_file)
        self.workdir = Path(workdir)
        self._version = ...

    def _email(self):
        # Only the e-mail field is read; the token itself is agy's business.
        try:
            value = json.loads(self.token_file.read_text(encoding="utf-8")).get("id_token_email")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and "@" in value else None

    def version(self):
        if self._version is ...:
            try:
                out = self.run([self.agy, "--version"], capture_output=True, text=True,
                               timeout=15).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                out = ""
            self._version = out.splitlines()[0] if out else None
        return self._version

    def read(self):
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            done = self.run([self.agy, *COMMAND], capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS, cwd=self.workdir)
        except FileNotFoundError as error:
            raise UsageParseError(f"agy not found at {self.agy}") from error
        except subprocess.TimeoutExpired as error:
            raise UsageParseError(f"agy did not answer /usage in {TIMEOUT_SECONDS}s") from error
        if done.returncode != 0:
            raise UsageParseError(f"agy /usage exit {done.returncode}: "
                                  f"{(done.stderr or done.stdout or '').strip()[:300]}")
        text = done.stdout if (done.stdout or "").strip() else done.stderr
        meta = {"email": self._email(), "plan": None, "agy_version": None}
        return parse_usage_json(text), meta
