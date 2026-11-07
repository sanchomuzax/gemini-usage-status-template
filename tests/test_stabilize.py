from __future__ import annotations

import copy

from gemini_usage_status.stabilize import merge_samples


def with_fraction(summary, bucket_id, fraction=None, drop=False):
    data = copy.deepcopy(summary)
    for group in data["groups"]:
        for bucket in group["buckets"]:
            if bucket["bucketId"] == bucket_id:
                if drop:
                    bucket.pop("remainingFraction", None)
                elif fraction is not None:
                    bucket["remainingFraction"] = fraction
    return data


def fractions(summary):
    return {b["bucketId"]: b.get("remainingFraction") for g in summary["groups"] for b in g["buckets"]}


def test_median_of_three_ignores_one_outlier_either_way(summary_after_use):
    a = with_fraction(summary_after_use, "gemini-5h", 0.98)
    high = with_fraction(summary_after_use, "gemini-5h", 0.90)     # one replica far ahead
    low = with_fraction(summary_after_use, "gemini-5h", 0.999)     # one replica far behind
    assert fractions(merge_samples([a, high, a]))["gemini-5h"] == 0.98
    assert fractions(merge_samples([a, low, a]))["gemini-5h"] == 0.98


def test_a_lower_value_that_everyone_agrees_on_is_accepted(summary_after_use):
    # The backend does lower usage inside a window; a unanimous reading is the truth.
    lower = with_fraction(summary_after_use, "gemini-5h", 0.995)
    assert fractions(merge_samples([lower, lower, lower]))["gemini-5h"] == 0.995


def test_median_of_two_is_the_more_used(summary_after_use):
    a = with_fraction(summary_after_use, "gemini-5h", 0.98)
    b = with_fraction(summary_after_use, "gemini-5h", 0.97)
    assert fractions(merge_samples([a, b]))["gemini-5h"] == 0.97


def test_missing_fraction_counts_as_exhausted(summary_after_use):
    gone = with_fraction(summary_after_use, "gemini-weekly", drop=True)
    merged = merge_samples([gone, gone, summary_after_use])
    bucket = next(b for g in merged["groups"] for b in g["buckets"] if b["bucketId"] == "gemini-weekly")
    assert "remainingFraction" not in bucket


def test_invalid_fractions_are_ignored(summary_after_use):
    broken = with_fraction(summary_after_use, "gemini-5h", "garbage")
    assert fractions(merge_samples([broken, summary_after_use]))["gemini-5h"] == 0.9899333


def test_one_sample_is_that_sample(summary_after_use):
    assert merge_samples([summary_after_use]) == summary_after_use


def test_does_not_mutate(summary_after_use):
    before = copy.deepcopy(summary_after_use)
    merge_samples([summary_after_use, with_fraction(summary_after_use, "gemini-5h", 0.5)])
    assert summary_after_use == before


def test_skips_non_summaries(summary_after_use):
    assert merge_samples([None, summary_after_use, {"x": 1}]) == summary_after_use
    assert merge_samples([None]) is None
