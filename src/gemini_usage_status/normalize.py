"""Turn the Antigravity quota summary into the shared status.json limit shape.

The summary endpoint answers with groups of buckets, each carrying the fraction
of the window still left and when the window resets. This module is pure: no
I/O, no clock, so every rule here is testable against captured responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# bucketId -> (group, kind). The headline figures are the Gemini group's,
# mirroring how the Claude and Codex monitors report their main model family.
KNOWN_BUCKETS = {
    "gemini-5h": ("gemini", "session"),
    "gemini-weekly": ("gemini", "weekly_all"),
    "3p-5h": ("3p", "session"),
    "3p-weekly": ("3p", "weekly_all"),
    "gemini-argon-wtu-weekly": ("argon", "weekly_model"),
}

WINDOW_MINUTES = {"5h": 300, "weekly": 10080}

SESSION_BUCKET = "gemini-5h"
WEEKLY_BUCKET = "gemini-weekly"

# An unused bucket reports a reset exactly one window after "now", recomputed
# on every call. This much slack absorbs clock skew and request latency.
ROLLING_TOLERANCE = timedelta(minutes=3)


class QuotaShapeError(ValueError):
    """The response did not look like a quota summary we understand."""


def percent_used(remaining_fraction):
    """1 - remaining, as a percentage with one decimal; None if unusable."""
    if isinstance(remaining_fraction, bool) or not isinstance(remaining_fraction, (int, float)):
        return None
    if not 0 <= remaining_fraction <= 1:
        return None
    return round((1 - float(remaining_fraction)) * 100, 1)


def _parse_time(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window_started(remaining_fraction, reset_time, window_minutes, now):
    """Whether the window has begun counting.

    Google anchors a window at its first use. Before that, the reset time is
    simply now + window length and moves with every call. A bucket that is both
    full and on that rolling reset has not started; anything else has.
    Returns None when it cannot tell.
    """
    reset = _parse_time(reset_time)
    if reset is None or window_minutes is None or percent_used(remaining_fraction) is None:
        return None
    if remaining_fraction < 1:
        return True
    rolling_reset = now + timedelta(minutes=window_minutes)
    return abs(reset - rolling_reset) > ROLLING_TOLERANCE


def _bucket_row(group_name, bucket, now):
    bucket_id = bucket.get("bucketId")
    group, kind = KNOWN_BUCKETS.get(bucket_id, (None, "other"))
    window = bucket.get("window")
    minutes = WINDOW_MINUTES.get(window)
    # protojson leaves zero-valued scalars out of the JSON, so an exhausted
    # bucket arrives with no remainingFraction at all. An explicit null or a
    # nonsense value stays unknown; only the absent key means zero left.
    inferred = "remainingFraction" not in bucket
    fraction = 0.0 if inferred else bucket.get("remainingFraction")
    used = percent_used(fraction)
    return {
        "kind": kind,
        "group": group,
        "bucket_id": bucket_id,
        "window": window,
        "percent_used": used,
        "percent_remaining": None if used is None else round(100 - used, 1),
        "percent_inferred": inferred,
        "resets_at": bucket.get("resetTime"),
        "scope": group_name,
        "window_duration_minutes": minutes,
        "window_started": window_started(fraction, bucket.get("resetTime"), minutes, now),
        "is_active": True,
    }


def _rows(summary, now):
    if not isinstance(summary, dict):
        raise QuotaShapeError("summary is not an object")
    groups = summary.get("groups")
    if not isinstance(groups, list) or not groups:
        raise QuotaShapeError("summary has no groups")
    return [
        _bucket_row(group.get("displayName"), bucket, now)
        for group in groups if isinstance(group, dict)
        for bucket in (group.get("buckets") or []) if isinstance(bucket, dict)
    ]


def normalize_summary(summary, now):
    """Reduce a retrieveUserQuotaSummary response to status.json fields."""
    rows = _rows(summary, now)
    by_id = {row["bucket_id"]: row for row in rows}
    missing = [b for b in (SESSION_BUCKET, WEEKLY_BUCKET) if b not in by_id]
    if missing:
        raise QuotaShapeError(f"missing headline buckets: {', '.join(missing)}")

    session, weekly = by_id[SESSION_BUCKET], by_id[WEEKLY_BUCKET]
    figures = [row["percent_used"] for row in rows if row["percent_used"] is not None]
    return {
        "session_percent_used": session["percent_used"],
        "weekly_percent_used": weekly["percent_used"],
        "max_percent_used": max(figures) if figures else None,
        "session_resets_at": session["resets_at"],
        "weekly_resets_at": weekly["resets_at"],
        "session_group": session["group"],
        "weekly_group": weekly["group"],
        "limits": rows,
        "unknown_buckets": [row["bucket_id"] for row in rows if row["kind"] == "other"],
    }
