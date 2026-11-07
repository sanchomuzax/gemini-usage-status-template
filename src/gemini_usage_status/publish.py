"""Shape a reading into status.json and history, and decide what to publish.

Same contract as the Claude and Codex monitors, so the downstream tools
(budget checks, the Grafana ingest) read all three the same way.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import atomic_write_text

CHANGE_THRESHOLD = 1   # percentage points
HEARTBEAT_HOURS = 6    # publish at least this often regardless

NOTE = (
    "Percentages are REAL, from the Antigravity CLI's own quota report "
    "(`agy -p /usage`, no model turn) -- the same figures its /usage panel shows. "
    "session = Gemini 5-hour window, weekly = Gemini 7-day window; limits[] also "
    "holds the Claude/GPT group (3p) and the separate Gemini 4 Argon weekly cap. "
    "A window starts at its first use: window_started false means nothing has "
    "been used and resets_at still moves with each reading. Antigravity reports "
    "no token counts, so tokens_7d is null. null means unavailable, never zero."
)

_HEADLINES = ("session_percent_used", "weekly_percent_used", "max_percent_used")
_SIDE_BUCKETS = {"tp_session": "3p-5h", "tp_weekly": "3p-weekly", "argon_weekly": "gemini-argon-wtu-weekly"}


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def quota_status_for(limits):
    if limits.get("error"):
        return "error"
    worst = limits.get("max_percent_used")
    # A headline window we could not read means we do not know, even if every
    # other bucket looks fine: "ok" would quietly turn that unknown into a zero.
    headlines = (limits.get("session_percent_used"), limits.get("weekly_percent_used"))
    if not isinstance(worst, (int, float)) or any(not isinstance(v, (int, float)) for v in headlines):
        return "unknown"
    if worst >= 100:
        return "exhausted"
    if worst >= 90:
        return "critical"
    if worst >= 75:
        return "warning"
    return "ok"


def build_status(limits, now):
    get = limits.get
    return {
        "timestamp_utc": _iso(now),
        "quota_status": quota_status_for(limits),
        "session_percent_used": get("session_percent_used"),
        "weekly_percent_used": get("weekly_percent_used"),
        "max_percent_used": get("max_percent_used"),
        "session_resets_at": get("session_resets_at"),
        "weekly_resets_at": get("weekly_resets_at"),
        "session_group": get("session_group"),
        "weekly_group": get("weekly_group"),
        "account": get("account"),
        "limits": get("limits") or [],
        "unknown_buckets": get("unknown_buckets") or [],
        "subscription_type": get("subscription_type"),
        "subscription_name": get("subscription_name"),
        "agy_version": get("agy_version"),
        "source_host": get("source_host"),
        "token_refreshed": get("token_refreshed"),
        "quota_error_detail": get("error") or "",
        "tokens_7d": None,
        "tokens_today": None,
        "note": NOTE,
    }


def _bucket(status, bucket_id):
    return next((row for row in status.get("limits") or [] if row.get("bucket_id") == bucket_id), {})


def history_sample(status):
    sides = {key: _bucket(status, bid).get("percent_used") for key, bid in _SIDE_BUCKETS.items()}
    return {
        "t": status["timestamp_utc"],
        "quota_status": status["quota_status"],
        "session": status["session_percent_used"],
        "weekly": status["weekly_percent_used"],
        "max": status["max_percent_used"],
        "tokens_7d": None,
        "account": status["account"],
        "session_resets_at": status["session_resets_at"],
        "weekly_resets_at": status["weekly_resets_at"],
        "session_started": _bucket(status, "gemini-5h").get("window_started"),
        "weekly_started": _bucket(status, "gemini-weekly").get("window_started"),
        **sides,
    }


def _moved(old, new):
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return old != new
    return abs(new - old) >= CHANGE_THRESHOLD


def _bucket_figures(status):
    return {row.get("bucket_id"): row.get("percent_used") for row in status.get("limits") or []}


def _heartbeat(previous, now):
    try:
        stamp = datetime.strptime(previous["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return ["previous timestamp unreadable"]
    age = (now - stamp.replace(tzinfo=timezone.utc)).total_seconds() / 3600
    return [f"heartbeat ({age:.1f}h since last publish)"] if age >= HEARTBEAT_HOURS else []


def commit_reasons(previous, status, now):
    """Why this reading deserves a commit; empty means keep it local.

    `previous` must be the last PUBLISHED reading, not the last local one: the
    local status.json is rewritten every run, and comparing against it would
    hide any drift slower than the threshold per run and starve the heartbeat.
    """
    if previous is None:
        return ["no previous reading"]
    reasons = []
    if previous.get("quota_status") != status["quota_status"]:
        reasons.append(f"status {previous.get('quota_status')} -> {status['quota_status']}")
    reasons += [f"{field} {previous.get(field)} -> {status.get(field)}"
                for field in _HEADLINES if _moved(previous.get(field), status.get(field))]
    old_buckets, new_buckets = _bucket_figures(previous), _bucket_figures(status)
    reasons += [f"{bid} {old_buckets.get(bid)} -> {value}"
                for bid, value in new_buckets.items()
                if bid not in ("gemini-5h", "gemini-weekly") and _moved(old_buckets.get(bid), value)]
    if bool(previous.get("quota_error_detail")) != bool(status["quota_error_detail"]):
        reasons.append("quota error state changed")
    if previous.get("account") != status["account"]:
        reasons.append(f"account {previous.get('account')} -> {status['account']}")
    return reasons + _heartbeat(previous, now)


def write_status(path, status):
    atomic_write_text(path, json.dumps(status, indent=2) + "\n")


def append_history(history_dir, status):
    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (status["timestamp_utc"][:7] + ".jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_sample(status)) + "\n")
    return path
