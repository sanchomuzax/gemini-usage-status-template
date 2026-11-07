from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gemini_usage_status.collect import collect
from gemini_usage_status.tui import UsageParseError

PANEL = (Path(__file__).parent / "fixtures" / "usage_panel.txt").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 7, 12, 44, 0, tzinfo=timezone.utc)


class FakeSession:
    def __init__(self, screens):
        self.screens = list(screens)
        self.reads = 0

    def read_usage(self):
        self.reads += 1
        item = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if isinstance(item, Exception):
            raise item
        return item


def run(tmp_path, screens, samples=1, previous=None):
    status = tmp_path / "status.json"
    if previous is not None:
        status.write_text(json.dumps(previous), encoding="utf-8")
    session = FakeSession(screens)
    result = collect(now=NOW, session=session, status_path=status, samples=samples, sleep=lambda s: None)
    return result, session


def test_happy_path(tmp_path):
    result, _ = run(tmp_path, [PANEL])
    assert "error" not in result
    assert result["session_percent_used"] == 1.7
    assert result["weekly_percent_used"] == 0.7
    assert result["account"] == "user"
    assert result["source_host"] == "agy /usage"
    assert result["session_resets_at"] == "2026-11-07T14:39:00Z"


def test_three_samples_take_the_median(tmp_path):
    stray = PANEL.replace("98.27%", "90.00%")
    result, session = run(tmp_path, [PANEL, stray, PANEL], samples=3)
    assert session.reads == 3
    assert result["session_percent_used"] == 1.7


def test_plan_survives_from_the_previous_status(tmp_path):
    result, _ = run(tmp_path, [PANEL], previous={"subscription_name": "Google AI Pro", "limits": []})
    assert result["subscription_name"] == "Google AI Pro"
    assert result["subscription_type"] == "google-ai-pro"


def test_a_lower_reading_is_not_held_up(tmp_path):
    first, _ = run(tmp_path, [PANEL])
    rows = [dict(r, percent_used=4.0) if r["bucket_id"] == "gemini-5h" else r for r in first["limits"]]
    result, _ = run(tmp_path, [PANEL], previous={"limits": rows})
    assert result["session_percent_used"] == 1.7


def test_sign_in_problem_is_an_error(tmp_path):
    result, _ = run(tmp_path, [UsageParseError("agy asks to sign in")])
    assert "sign in" in result["error"]


def test_unexpected_failure_never_escapes(tmp_path):
    result, _ = run(tmp_path, [RuntimeError("kaboom")])
    assert "kaboom" in result["error"]
