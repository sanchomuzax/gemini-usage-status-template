"""Steady readings from a quota backend whose replicas disagree.

Measured 2026-11-07: twelve calls in one minute returned three different
values for the same 5-hour window (1.1%, 2.0%, 1.2% used), and one call an
hour after a request still answered as if nothing had been used. Some
replicas lag behind. Within one window usage only grows, so the most-used
answer is the freshest one: take the most-used of several samples, and never
let a window fall below what it already showed. Both are pure functions.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from .normalize import percent_used

# The /usage countdown has minute precision, so one window's reset time can
# wobble by a minute between readings. Within this much it is the same window.
SAME_WINDOW = timedelta(minutes=3)


def _same_window(old_reset, new_reset):
    try:
        old = datetime.fromisoformat(str(old_reset).replace("Z", "+00:00"))
        new = datetime.fromisoformat(str(new_reset).replace("Z", "+00:00"))
    except ValueError:
        return False
    return abs(old - new) <= SAME_WINDOW


def _fraction(bucket):
    """Remaining fraction for comparison; a missing key means none left."""
    if "remainingFraction" not in bucket:
        return 0.0
    value = bucket["remainingFraction"]
    return value if percent_used(value) is not None else None


def _buckets(summary):
    return [b for g in summary.get("groups") or [] if isinstance(g, dict)
            for b in g.get("buckets") or [] if isinstance(b, dict)]


def merge_samples(samples):
    """One summary with each bucket at its most-used valid reading."""
    valid = [s for s in samples if isinstance(s, dict) and isinstance(s.get("groups"), list)]
    if not valid:
        return None
    best = {}
    for sample in valid:
        for bucket in _buckets(sample):
            fraction = _fraction(bucket)
            current = best.get(bucket.get("bucketId"))
            if fraction is not None and (current is None or fraction < _fraction(current)):
                best[bucket.get("bucketId")] = bucket
    merged = copy.deepcopy(valid[-1])
    for group in merged.get("groups") or []:
        group["buckets"] = [copy.deepcopy(best.get(b.get("bucketId"), b))
                            for b in group.get("buckets") or []]
    return merged


def hold_within_window(summary, previous_status):
    """Raise a bucket to its previous reading when the window is the same."""
    rows = previous_status.get("limits") if isinstance(previous_status, dict) else None
    if not isinstance(rows, list):
        return summary
    before = {r.get("bucket_id"): r for r in rows if isinstance(r, dict)
              and r.get("window_started") is True and isinstance(r.get("percent_used"), (int, float))}
    held = copy.deepcopy(summary)
    for bucket in _buckets(held):
        old = before.get(bucket.get("bucketId"))
        fraction = _fraction(bucket)
        if old is None or fraction is None or not _same_window(old.get("resets_at"), bucket.get("resetTime")):
            continue
        bucket["resetTime"] = old["resets_at"]   # keep one stable deadline per window
        if old["percent_used"] > percent_used(fraction):
            bucket["remainingFraction"] = round(1 - old["percent_used"] / 100, 6)
            bucket["heldFromPrevious"] = True
    return held
