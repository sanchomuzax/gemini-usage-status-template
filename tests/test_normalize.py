from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from gemini_usage_status.normalize import (
    QuotaShapeError,
    normalize_summary,
    percent_used,
    tier_from_load,
    window_started,
)


def test_percent_used_rounds_to_one_decimal():
    assert percent_used(0.98998094) == 1.0
    assert percent_used(1) == 0.0
    assert percent_used(0) == 100.0
    assert percent_used(0.254) == 74.6


@pytest.mark.parametrize("bad", [None, "0.5", -0.1, 1.5, True])
def test_percent_used_rejects_nonsense(bad):
    assert percent_used(bad) is None


def test_headline_figures_come_from_the_gemini_group(summary_after_use, captured_at):
    result = normalize_summary(summary_after_use, now=captured_at)
    assert result["session_percent_used"] == 1.0
    assert result["weekly_percent_used"] == 1.0
    assert result["session_resets_at"] == "2026-11-07T14:39:09Z"
    assert result["weekly_resets_at"] == "2026-11-14T09:39:09Z"
    assert result["session_group"] == "gemini"
    assert result["weekly_group"] == "gemini"


def test_every_bucket_becomes_a_limit_row(summary_after_use, captured_at):
    rows = normalize_summary(summary_after_use, now=captured_at)["limits"]
    by_id = {row["bucket_id"]: row for row in rows}
    assert set(by_id) == {
        "gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h", "gemini-argon-wtu-weekly",
    }
    assert by_id["gemini-5h"]["kind"] == "session"
    assert by_id["gemini-weekly"]["kind"] == "weekly_all"
    assert by_id["gemini-argon-wtu-weekly"]["kind"] == "weekly_model"
    assert by_id["gemini-argon-wtu-weekly"]["group"] == "argon"
    assert by_id["3p-5h"]["group"] == "3p"
    assert by_id["3p-5h"]["window_duration_minutes"] == 300
    assert by_id["3p-weekly"]["window_duration_minutes"] == 10080
    assert by_id["gemini-weekly"]["percent_remaining"] == 99.0
    assert by_id["3p-5h"]["scope"] == "Claude and GPT models"


def test_max_is_the_worst_of_all_buckets(summary_after_use, captured_at):
    data = copy.deepcopy(summary_after_use)
    data["groups"][1]["buckets"][0]["remainingFraction"] = 0.4  # 3p-weekly 60%
    result = normalize_summary(data, now=captured_at)
    assert result["max_percent_used"] == 60.0
    assert result["weekly_percent_used"] == 1.0


def test_unused_bucket_is_marked_not_started(summary_after_use, captured_at):
    rows = normalize_summary(summary_after_use, now=captured_at)["limits"]
    by_id = {row["bucket_id"]: row for row in rows}
    assert by_id["3p-5h"]["window_started"] is False
    assert by_id["3p-weekly"]["window_started"] is False
    assert by_id["gemini-5h"]["window_started"] is True
    assert by_id["gemini-argon-wtu-weekly"]["window_started"] is True


def test_window_started_logic(captured_at):
    window = timedelta(hours=5)
    rolling = (captured_at + window).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert window_started(1, rolling, 300, captured_at) is False
    assert window_started(0.99, rolling, 300, captured_at) is True
    earlier = (captured_at + window - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert window_started(1, earlier, 300, captured_at) is True
    assert window_started(1, None, 300, captured_at) is None


def test_missing_gemini_group_is_a_shape_error(summary_after_use, captured_at):
    data = copy.deepcopy(summary_after_use)
    data["groups"] = [g for g in data["groups"] if g["displayName"] != "Gemini Models"]
    with pytest.raises(QuotaShapeError, match="gemini-5h"):
        normalize_summary(data, now=captured_at)


def test_empty_or_garbage_summary_is_a_shape_error(captured_at):
    for bad in ({}, {"groups": None}, {"groups": "x"}, [], None):
        with pytest.raises(QuotaShapeError):
            normalize_summary(bad, now=captured_at)


def test_unknown_bucket_is_kept_and_reported(summary_after_use, captured_at):
    data = copy.deepcopy(summary_after_use)
    data["groups"].append({
        "displayName": "Gemini 5 Something",
        "buckets": [{"bucketId": "gemini-next-daily", "window": "daily",
                     "resetTime": "2026-11-08T00:00:00Z", "remainingFraction": 0.5}],
    })
    result = normalize_summary(data, now=captured_at)
    row = next(r for r in result["limits"] if r["bucket_id"] == "gemini-next-daily")
    assert row["kind"] == "other"
    assert row["percent_used"] == 50.0
    assert row["window_duration_minutes"] is None
    assert result["unknown_buckets"] == ["gemini-next-daily"]
    assert result["max_percent_used"] == 50.0


def test_input_is_not_mutated(summary_after_use, captured_at):
    before = copy.deepcopy(summary_after_use)
    normalize_summary(summary_after_use, now=captured_at)
    assert summary_after_use == before


def test_bucket_with_broken_fraction_does_not_count_as_zero(summary_after_use, captured_at):
    data = copy.deepcopy(summary_after_use)
    data["groups"][0]["buckets"][1]["remainingFraction"] = None  # gemini-5h
    result = normalize_summary(data, now=captured_at)
    assert result["session_percent_used"] is None
    assert result["max_percent_used"] == 1.0


def test_tier_from_load_prefers_paid_tier(load_code_assist_pro):
    tier = tier_from_load(load_code_assist_pro)
    assert tier == {
        "subscription_type": "g1-pro-tier",
        "subscription_name": "Google AI Pro",
        "project": "example-project-123",
    }


def test_tier_from_load_without_paid_tier(load_code_assist_pro):
    data = {k: v for k, v in load_code_assist_pro.items() if k != "paidTier"}
    tier = tier_from_load(data)
    assert tier["subscription_type"] == "free-tier"
    assert tier["project"] == "example-project-123"


def test_tier_from_load_garbage():
    assert tier_from_load(None) == {
        "subscription_type": None, "subscription_name": None, "project": None,
    }


def test_missing_fraction_key_means_exhausted(summary_after_use, captured_at):
    # protojson omits zero-valued scalars: an exhausted bucket arrives without
    # remainingFraction at all. That is 100% used, flagged as inferred.
    data = copy.deepcopy(summary_after_use)
    del data["groups"][0]["buckets"][1]["remainingFraction"]  # gemini-5h
    result = normalize_summary(data, now=captured_at)
    row = next(r for r in result["limits"] if r["bucket_id"] == "gemini-5h")
    assert result["session_percent_used"] == 100.0
    assert row["percent_inferred"] is True
    assert result["max_percent_used"] == 100.0


def test_present_fraction_is_not_inferred(summary_after_use, captured_at):
    rows = normalize_summary(summary_after_use, now=captured_at)["limits"]
    assert all(row["percent_inferred"] is False for row in rows)


def test_window_started_is_unknown_when_fraction_unknown(captured_at):
    rolling = (captured_at + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert window_started(None, rolling, 300, captured_at) is None
    assert window_started(True, rolling, 300, captured_at) is None
