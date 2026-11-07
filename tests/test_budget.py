from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest

import budget_check
from gemini_usage_status.budget import (
    PROFILES,
    brief_line,
    evaluate,
    group_figures,
    thresholds,
)
from gemini_usage_status.normalize import normalize_summary
from gemini_usage_status.publish import build_status


@pytest.fixture
def status(summary_after_use, captured_at):
    return build_status(normalize_summary(summary_after_use, now=captured_at), captured_at)


def with_bucket(status, bucket_id, used, resets_at=None):
    rows = [dict(r, percent_used=used, window_started=True, **({"resets_at": resets_at} if resets_at else {}))
            if r["bucket_id"] == bucket_id else r for r in status["limits"]]
    return {**status, "limits": rows}


def test_thresholds_profile_and_override():
    assert thresholds({})["WEEKLY_STOP"] == PROFILES["balanced"]["WEEKLY_STOP"]
    assert thresholds({"BUDGET_PROFILE": "conserve"})["WEEKLY_STOP"] == 50
    assert thresholds({"BUDGET_PROFILE": "conserve", "BUDGET_WEEKLY_STOP": "60"})["WEEKLY_STOP"] == 60
    assert thresholds({"BUDGET_PROFILE": "nonsense"})["WEEKLY_STOP"] == PROFILES["balanced"]["WEEKLY_STOP"]
    assert thresholds({"BUDGET_WEEKLY_STOP": "abc"})["WEEKLY_STOP"] == PROFILES["balanced"]["WEEKLY_STOP"]


def test_group_figures(status):
    fig = group_figures(status, "gemini")
    assert fig["session"] == 1.0 and fig["weekly"] == 1.0
    assert fig["weekly_started"] is True
    assert group_figures(status, "3p")["session"] == 0.0
    argon = group_figures(status, "argon")
    assert argon["session"] is None and argon["weekly"] == 1.0


def test_fresh_status_is_go(status, captured_at):
    result = evaluate(status, "gemini", thresholds({}), captured_at, source="live")
    assert result["verdict"] == "GO"
    assert result["weekly_burn_rate"] is None  # too early in the window


def test_session_caution_and_stop(status, captured_at):
    th = thresholds({})
    caution = evaluate(with_bucket(status, "gemini-5h", 75.0), "gemini", th, captured_at)
    assert caution["verdict"] == "CAUTION"
    stop = evaluate(with_bucket(status, "gemini-5h", 90.0), "gemini", th, captured_at)
    assert stop["verdict"] == "STOP"


def test_weekly_stop(status, captured_at):
    result = evaluate(with_bucket(status, "gemini-weekly", 95.0), "gemini", thresholds({}), captured_at)
    assert result["verdict"] == "STOP"
    assert "weekly window 95.0% used" in result["reasons"]


def test_burn_rate_stop_late_in_the_week(status, captured_at):
    # Two days into the week (reset 5 days out) with 70% gone: ~2.45x pace.
    resets = (captured_at + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = with_bucket(status, "gemini-weekly", 70.0, resets_at=resets)
    result = evaluate(data, "gemini", thresholds({}), captured_at)
    assert result["verdict"] == "STOP"
    assert result["weekly_burn_rate"] == 2.45
    assert result["weekly_projected_at_reset"] == 245


def test_unstarted_weekly_window_has_no_burn_rate(status, captured_at):
    result = evaluate(status, "3p", thresholds({}), captured_at)
    assert result["weekly_burn_rate"] is None
    assert result["verdict"] == "GO"


def test_error_status_is_unknown(captured_at):
    status = build_status({"error": "HTTP 403: nope"}, captured_at)
    result = evaluate(status, "gemini", thresholds({}), captured_at)
    assert result["verdict"] == "UNKNOWN"
    assert "HTTP 403" in result["reasons"][0]


def test_cached_status_with_expired_session_is_caution(status, captured_at):
    later = captured_at + timedelta(hours=6)
    result = evaluate(status, "gemini", thresholds({}), later, source="cached", age=360)
    assert result["session_percent_used"] is None
    assert result["verdict"] == "CAUTION"


def test_brief_line(status, captured_at):
    result = evaluate(status, "gemini", thresholds({}), captured_at, source="live")
    line = brief_line({**result, "profile": "balanced", "source": "live", "account": "someone",
                       "group": "gemini"})
    assert line == "GO | live | acct someone | gemini | session 1.0% | weekly 1.0%"
    cached = brief_line({**result, "profile": "conserve", "source": "cached", "data_age_minutes": 12,
                         "group": "3p", "account": None})
    assert cached.startswith("GO | [conserve] | CACHED 12m old | 3p |")


def test_cli_uses_cache_when_live_fails(tmp_path, status, monkeypatch, captured_at):
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(budget_check, "live_status", lambda: None)
    out = io.StringIO()
    code = budget_check.main(["--brief"], repo_dir=tmp_path, now=captured_at + timedelta(minutes=10),
                             environ={}, out=out)
    assert code == 0
    assert "CACHED 10m old" in out.getvalue()


def test_cli_too_old_cache_is_unknown(tmp_path, status, monkeypatch, captured_at):
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(budget_check, "live_status", lambda: None)
    out = io.StringIO()
    code = budget_check.main([], repo_dir=tmp_path, now=captured_at + timedelta(hours=3), environ={}, out=out)
    assert code == 3
    assert json.loads(out.getvalue())["verdict"] == "UNKNOWN"


def test_cli_live_and_group_flag(tmp_path, status, monkeypatch, captured_at):
    monkeypatch.setattr(budget_check, "live_status", lambda: status)
    out = io.StringIO()
    code = budget_check.main(["--brief", "--group", "3p"], repo_dir=tmp_path, now=captured_at,
                             environ={}, out=out)
    assert code == 0
    assert "| live |" in out.getvalue() and "| 3p |" in out.getvalue()


def test_cli_no_data_at_all(tmp_path, monkeypatch, captured_at):
    monkeypatch.setattr(budget_check, "live_status", lambda: None)
    out = io.StringIO()
    assert budget_check.main(["--brief"], repo_dir=tmp_path, now=captured_at, environ={}, out=out) == 3
    assert out.getvalue().startswith("UNKNOWN")


def test_unknown_session_figure_is_caution(status, captured_at):
    data = with_bucket(status, "gemini-5h", None)
    data = {**data, "limits": [dict(r, percent_used=None) if r["bucket_id"] == "gemini-5h" else r
                               for r in data["limits"]]}
    result = evaluate(data, "gemini", thresholds({}), captured_at)
    assert result["verdict"] == "CAUTION"
    assert any("unknown" in r for r in result["reasons"])


def test_cached_status_with_expired_weekly_drops_the_weekly(status, captured_at):
    resets = (captured_at + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = with_bucket(status, "gemini-weekly", 95.0, resets_at=resets)
    later = captured_at + timedelta(minutes=45)
    result = evaluate(data, "gemini", thresholds({}), later, source="cached", age=45)
    assert result["weekly_percent_used"] is None
    assert result["verdict"] != "STOP"


def test_cli_unknown_group_is_unknown(tmp_path, status, monkeypatch, captured_at):
    monkeypatch.setattr(budget_check, "live_status", lambda: status)
    out = io.StringIO()
    assert budget_check.main(["--brief", "--group", "claude"], repo_dir=tmp_path, now=captured_at,
                             environ={}, out=out) == 3
    assert out.getvalue().startswith("UNKNOWN")


def test_cli_accepts_equals_form(tmp_path, status, monkeypatch, captured_at):
    monkeypatch.setattr(budget_check, "live_status", lambda: status)
    out = io.StringIO()
    budget_check.main(["--brief", "--group=3p"], repo_dir=tmp_path, now=captured_at, environ={}, out=out)
    assert "| 3p |" in out.getvalue()


def test_cli_corrupt_cache_is_unknown_not_a_crash(tmp_path, monkeypatch, captured_at):
    (tmp_path / "status.json").write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setattr(budget_check, "live_status", lambda: None)
    out = io.StringIO()
    assert budget_check.main(["--brief"], repo_dir=tmp_path, now=captured_at, environ={}, out=out) == 3


def test_live_status_parses_fetch_output(monkeypatch, summary_after_use, captured_at):
    payload = json.dumps(normalize_summary(summary_after_use, now=captured_at))

    class Done:
        stdout = payload
    monkeypatch.setattr(budget_check.subprocess, "run", lambda *a, **k: Done())
    assert budget_check.live_status()["session_percent_used"] == 1.0
    Done.stdout = json.dumps({"error": "x"})
    assert budget_check.live_status() is None
    Done.stdout = "garbage"
    assert budget_check.live_status() is None
