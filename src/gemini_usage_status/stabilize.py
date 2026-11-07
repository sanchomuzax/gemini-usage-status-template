"""Steady readings from a quota backend whose replicas disagree.

Measured 2026-11-07: calls seconds apart returned different values for the
same window (1.1%, 2.0%, 1.2% used), and usage also went DOWN inside a window
and stayed down (2.7% to a unanimous 1.9%). So neither "take the highest" nor
"never let a window fall" is right. Each reading takes several samples and
keeps, per bucket, the median: one replica far ahead or far behind is
outvoted, a change every sample agrees on is followed.
"""

from __future__ import annotations

import copy

from .normalize import percent_used


def _fraction(bucket):
    """Remaining fraction for comparison; a missing key means none left."""
    if "remainingFraction" not in bucket:
        return 0.0
    value = bucket["remainingFraction"]
    return value if percent_used(value) is not None else None


def _buckets(summary):
    return [b for g in summary.get("groups") or [] if isinstance(g, dict)
            for b in g.get("buckets") or [] if isinstance(b, dict)]


def _median_bucket(candidates):
    """The bucket whose fraction is the median; with an even count, the more used."""
    ordered = sorted(candidates, key=_fraction)
    return ordered[(len(ordered) - 1) // 2]


def merge_samples(samples):
    """One summary with each bucket at its median valid reading."""
    valid = [s for s in samples if isinstance(s, dict) and isinstance(s.get("groups"), list)]
    if not valid:
        return None
    seen = {}
    for sample in valid:
        for bucket in _buckets(sample):
            if _fraction(bucket) is not None:
                seen.setdefault(bucket.get("bucketId"), []).append(bucket)
    merged = copy.deepcopy(valid[-1])
    for group in merged.get("groups") or []:
        group["buckets"] = [copy.deepcopy(_median_bucket(seen[b.get("bucketId")]))
                            if b.get("bucketId") in seen else b
                            for b in group.get("buckets") or []]
    return merged
