from __future__ import annotations

import copy

from gemini_usage_status.normalize import normalize_summary
from gemini_usage_status.stabilize import hold_within_window, merge_samples


def with_fraction(summary, bucket_id, fraction=None, reset=None, drop=False):
    data = copy.deepcopy(summary)
    for group in data["groups"]:
        for bucket in group["buckets"]:
            if bucket["bucketId"] == bucket_id:
                if drop:
                    bucket.pop("remainingFraction", None)
                elif fraction is not None:
                    bucket["remainingFraction"] = fraction
                if reset:
                    bucket["resetTime"] = reset
    return data


def fractions(summary):
    return {b["bucketId"]: b.get("remainingFraction") for g in summary["groups"] for b in g["buckets"]}


def test_merge_takes_the_most_used_reading_per_bucket(summary_after_use):
    stale = with_fraction(summary_after_use, "gemini-5h", 0.999)
    fresh = with_fraction(summary_after_use, "gemini-5h", 0.97)
    merged = merge_samples([stale, fresh, stale])
    assert fractions(merged)["gemini-5h"] == 0.97


def test_merge_treats_a_missing_fraction_as_exhausted(summary_after_use):
    exhausted = with_fraction(summary_after_use, "gemini-weekly", drop=True)
    merged = merge_samples([summary_after_use, exhausted])
    assert "remainingFraction" not in next(
        b for g in merged["groups"] for b in g["buckets"] if b["bucketId"] == "gemini-weekly")


def test_merge_ignores_invalid_fractions(summary_after_use):
    broken = with_fraction(summary_after_use, "gemini-5h", "garbage")
    assert fractions(merge_samples([broken, summary_after_use]))["gemini-5h"] == 0.9899333


def test_merge_of_one_sample_is_that_sample(summary_after_use):
    assert merge_samples([summary_after_use]) == summary_after_use


def test_merge_does_not_mutate(summary_after_use):
    before = copy.deepcopy(summary_after_use)
    merge_samples([summary_after_use, with_fraction(summary_after_use, "gemini-5h", 0.5)])
    assert summary_after_use == before


def test_merge_skips_non_summaries(summary_after_use):
    assert merge_samples([None, summary_after_use, {"x": 1}]) == summary_after_use
    assert merge_samples([None]) is None


def previous_status(summary, captured_at, **changes):
    figures = normalize_summary(summary, now=captured_at)
    rows = [dict(r, **changes.get(r["bucket_id"], {})) for r in figures["limits"]]
    return {"limits": rows}


def test_hold_keeps_a_higher_reading_of_the_same_window(summary_after_use, captured_at):
    previous = previous_status(summary_after_use, captured_at, **{"gemini-5h": {"percent_used": 2.0}})
    held = hold_within_window(summary_after_use, previous)
    bucket = next(b for g in held["groups"] for b in g["buckets"] if b["bucketId"] == "gemini-5h")
    assert bucket["remainingFraction"] == 0.98
    assert bucket["heldFromPrevious"] is True
    rows = normalize_summary(held, now=captured_at)["limits"]
    row = next(r for r in rows if r["bucket_id"] == "gemini-5h")
    assert row["percent_used"] == 2.0 and row["percent_held"] is True


def test_hold_does_not_cross_a_reset(summary_after_use, captured_at):
    previous = previous_status(summary_after_use, captured_at, **{
        "gemini-5h": {"percent_used": 60.0, "resets_at": "2026-11-07T09:00:00Z"}})
    held = hold_within_window(summary_after_use, previous)
    assert fractions(held)["gemini-5h"] == 0.9899333


def test_hold_never_lowers_a_reading(summary_after_use, captured_at):
    previous = previous_status(summary_after_use, captured_at, **{"gemini-5h": {"percent_used": 0.5}})
    assert fractions(hold_within_window(summary_after_use, previous))["gemini-5h"] == 0.9899333


def test_hold_ignores_unstarted_windows(summary_after_use, captured_at):
    # Unused buckets report a moving reset, so "same reset" means nothing there.
    previous = previous_status(summary_after_use, captured_at, **{
        "3p-5h": {"percent_used": 5.0, "window_started": False}})
    assert fractions(hold_within_window(summary_after_use, previous))["3p-5h"] == 1


def test_hold_without_previous_is_identity(summary_after_use):
    assert hold_within_window(summary_after_use, None) == summary_after_use
    assert hold_within_window(summary_after_use, {"limits": "x"}) == summary_after_use


def test_unheld_rows_say_so(summary_after_use, captured_at):
    rows = normalize_summary(summary_after_use, now=captured_at)["limits"]
    assert all(r["percent_held"] is False for r in rows)


def test_a_minute_of_countdown_wobble_is_the_same_window(summary_after_use, captured_at):
    previous = previous_status(summary_after_use, captured_at, **{
        "gemini-5h": {"percent_used": 2.0, "resets_at": "2026-11-07T14:40:00Z"}})
    held = hold_within_window(summary_after_use, previous)   # fixture says 14:39:09
    bucket = next(b for g in held["groups"] for b in g["buckets"] if b["bucketId"] == "gemini-5h")
    assert bucket["remainingFraction"] == 0.98
    assert bucket["resetTime"] == "2026-11-07T14:40:00Z"


def test_unparseable_previous_reset_is_not_the_same_window(summary_after_use, captured_at):
    previous = previous_status(summary_after_use, captured_at, **{
        "gemini-5h": {"percent_used": 9.0, "resets_at": "garbage"}})
    assert fractions(hold_within_window(summary_after_use, previous))["gemini-5h"] == 0.9899333
