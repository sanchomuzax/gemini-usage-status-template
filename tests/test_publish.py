from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from gemini_usage_status.collect import collect  # noqa: F401  (import check)
from gemini_usage_status.normalize import normalize_summary
from gemini_usage_status.publish import (
    append_history,
    build_status,
    commit_reasons,
    history_sample,
    quota_status_for,
    write_status,
)


@pytest.fixture
def limits(summary_after_use, captured_at):
    figures = normalize_summary(summary_after_use, now=captured_at)
    return {**figures, "subscription_type": "g1-pro-tier", "subscription_name": "Google AI Pro",
            "account": "someone", "agy_version": "1.5.3",
            "source_host": "daily-cloudcode-pa.googleapis.com", "token_refreshed": False}


@pytest.mark.parametrize("worst,expected", [
    (0, "ok"), (74.9, "ok"), (75, "warning"), (90, "critical"), (100, "exhausted"), (None, "unknown"),
])
def test_quota_status_thresholds(worst, expected):
    figures = {"max_percent_used": worst, "session_percent_used": 0.0, "weekly_percent_used": 0.0}
    assert quota_status_for(figures) == expected


def test_quota_status_error_wins():
    assert quota_status_for({"error": "x", "max_percent_used": 10}) == "error"


def test_build_status_shape(limits, captured_at):
    status = build_status(limits, captured_at)
    assert status["timestamp_utc"] == "2026-11-07T09:41:57Z"
    assert status["quota_status"] == "ok"
    assert status["session_percent_used"] == 1.0
    assert status["subscription_type"] == "g1-pro-tier"
    assert status["quota_error_detail"] == ""
    assert status["tokens_7d"] is None
    assert len(status["limits"]) == 5
    assert "null means unavailable, never zero" in status["note"]


def test_build_status_on_error_keeps_figures_null(captured_at):
    status = build_status({"error": "HTTP 403: nope"}, captured_at)
    assert status["quota_status"] == "error"
    assert status["quota_error_detail"] == "HTTP 403: nope"
    assert status["session_percent_used"] is None
    assert status["limits"] == []


def test_history_sample_carries_every_group(limits, captured_at):
    sample = history_sample(build_status(limits, captured_at))
    assert sample["t"] == "2026-11-07T09:41:57Z"
    assert sample["session"] == 1.0 and sample["weekly"] == 1.0
    assert sample["tp_session"] == 0.0 and sample["tp_weekly"] == 0.0
    assert sample["argon_weekly"] == 1.0
    assert sample["session_started"] is True
    assert sample["tokens_7d"] is None
    assert sample["weekly_resets_at"] == "2026-11-14T09:39:09Z"


def test_first_reading_is_committed(limits, captured_at):
    assert commit_reasons(None, build_status(limits, captured_at), captured_at) == ["no previous reading"]


def test_identical_reading_is_skipped(limits, captured_at):
    status = build_status(limits, captured_at)
    later = captured_at + timedelta(minutes=5)
    assert commit_reasons(status, build_status(limits, later), later) == []


def test_movement_of_one_point_is_committed(limits, captured_at):
    before = build_status(limits, captured_at)
    moved = copy.deepcopy(limits)
    moved["weekly_percent_used"] = 2.0
    later = captured_at + timedelta(minutes=5)
    reasons = commit_reasons(before, build_status(moved, later), later)
    assert reasons == ["weekly_percent_used 1.0 -> 2.0"]


def test_side_group_movement_is_committed(limits, captured_at):
    before = build_status(limits, captured_at)
    moved = copy.deepcopy(limits)
    moved["limits"] = [dict(r, percent_used=5.0) if r["bucket_id"] == "3p-5h" else r
                       for r in moved["limits"]]
    later = captured_at + timedelta(minutes=5)
    assert any("3p-5h" in r for r in commit_reasons(before, build_status(moved, later), later))


def test_status_change_and_error_are_committed(limits, captured_at):
    before = build_status(limits, captured_at)
    later = captured_at + timedelta(minutes=5)
    reasons = commit_reasons(before, build_status({"error": "boom"}, later), later)
    assert "status ok -> error" in reasons
    assert "quota error state changed" in reasons


def test_heartbeat_after_six_hours(limits, captured_at):
    before = build_status(limits, captured_at)
    later = captured_at + timedelta(hours=6, minutes=1)
    reasons = commit_reasons(before, build_status(limits, later), later)
    assert reasons and reasons[0].startswith("heartbeat")


def test_unreadable_previous_timestamp(limits, captured_at):
    before = dict(build_status(limits, captured_at), timestamp_utc="garbage")
    assert "previous timestamp unreadable" in commit_reasons(before, build_status(limits, captured_at), captured_at)


def test_write_and_append(tmp_path, limits, captured_at):
    status = build_status(limits, captured_at)
    path = tmp_path / "status.json"
    write_status(path, status)
    assert json.loads(path.read_text()) == status
    history = append_history(tmp_path / "history", status)
    append_history(tmp_path / "history", status)
    assert history.name == "2026-11.jsonl"
    lines = history.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["session"] == 1.0


def test_unknown_headline_figure_is_unknown_status(limits, captured_at):
    broken = dict(limits, session_percent_used=None)
    assert build_status(broken, captured_at)["quota_status"] == "unknown"


def test_slow_drift_is_published_once_it_adds_up(limits, captured_at):
    published = build_status(limits, captured_at)
    later = captured_at
    for step in range(1, 4):  # +0.4 per run, compared to the PUBLISHED reading
        later = later + timedelta(minutes=5)
        current = build_status(dict(limits, weekly_percent_used=1.0 + 0.4 * step), later)
        reasons = commit_reasons(published, current, later)
        if step < 3:
            assert reasons == []
    assert any(r.startswith("weekly_percent_used") for r in reasons)


def test_write_status_is_atomic(tmp_path, limits, captured_at):
    path = tmp_path / "status.json"
    write_status(path, build_status(limits, captured_at))
    assert not list(tmp_path.glob("*.tmp"))
