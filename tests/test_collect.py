from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gemini_usage_status.agyprint import UsageParseError, parse_usage_json
from gemini_usage_status.collect import collect

PANEL = (Path(__file__).parent / "fixtures" / "usage_print.json").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 7, 12, 44, 0, tzinfo=timezone.utc)


class FakeReader:
    def __init__(self, screens):
        self.screens = list(screens)
        self.reads = 0

    def read(self):
        self.reads += 1
        item = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if isinstance(item, Exception):
            raise item
        return parse_usage_json(item), {"email": "user@example.com", "plan": None,
                                        "agy_version": None}

    def version(self):
        return "1.5.3"


def run(tmp_path, screens, samples=1, previous=None):
    status = tmp_path / "status.json"
    if previous is not None:
        status.write_text(json.dumps(previous), encoding="utf-8")
    session = FakeReader(screens)
    result = collect(now=NOW, reader=session, status_path=status, samples=samples, sleep=lambda s: None)
    return result, session


def test_happy_path(tmp_path):
    result, _ = run(tmp_path, [PANEL])
    assert "error" not in result
    assert result["session_percent_used"] == 7.9
    assert result["weekly_percent_used"] == 3.3
    assert result["account"] == "user"
    assert result["agy_version"] == "1.5.3"
    assert result["source_host"] == "agy -p /usage"
    assert result["session_resets_at"] == "2026-11-07T19:39:09Z"


def test_three_samples_take_the_median(tmp_path):
    stray = PANEL.replace("0.9206092953681946", "0.5")
    result, session = run(tmp_path, [PANEL, stray, PANEL], samples=3)
    assert session.reads == 3
    assert result["session_percent_used"] == 7.9


def test_plan_survives_from_the_previous_status(tmp_path):
    result, _ = run(tmp_path, [PANEL], previous={"subscription_name": "Google AI Pro", "limits": []})
    assert result["subscription_name"] == "Google AI Pro"
    assert result["subscription_type"] == "google-ai-pro"


def test_a_lower_reading_is_not_held_up(tmp_path):
    first, _ = run(tmp_path, [PANEL])
    rows = [dict(r, percent_used=4.0) if r["bucket_id"] == "gemini-5h" else r for r in first["limits"]]
    result, _ = run(tmp_path, [PANEL], previous={"limits": rows})
    assert result["session_percent_used"] == 7.9


def test_sign_in_problem_is_an_error(tmp_path):
    result, _ = run(tmp_path, [UsageParseError("agy asks to sign in")])
    assert "sign in" in result["error"]


def test_one_stuck_reading_does_not_blank_the_run(tmp_path):
    stuck = UsageParseError("agy did not answer /usage in 20s")
    result, session = run(tmp_path, [PANEL, stuck, PANEL, PANEL], samples=3)
    assert "error" not in result
    assert session.reads == 4
    assert result["session_percent_used"] == 7.9


def test_a_few_good_readings_are_enough_when_the_rest_hang(tmp_path):
    stuck = UsageParseError("agy did not answer /usage in 20s")
    result, session = run(tmp_path, [stuck, stuck, stuck, stuck, PANEL], samples=3)
    assert session.reads == 5
    assert result["session_percent_used"] == 7.9


def test_all_readings_failing_is_an_error(tmp_path):
    stuck = UsageParseError("agy did not answer /usage in 20s")
    result, session = run(tmp_path, [stuck], samples=3)
    assert session.reads == 5
    assert "did not answer" in result["error"]


def test_unexpected_failure_never_escapes(tmp_path):
    result, _ = run(tmp_path, [RuntimeError("kaboom")])
    assert "kaboom" in result["error"]
