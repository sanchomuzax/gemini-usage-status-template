from __future__ import annotations

import io
import json
from datetime import timedelta

import record_status
from gemini_usage_status.normalize import normalize_summary


def run(tmp_path, limits_text, now):
    out = io.StringIO()
    code = record_status.main(limits_text, repo_dir=tmp_path, now=now, out=out)
    return code, out.getvalue().strip().splitlines()[-1]


def test_first_run_writes_and_commits(tmp_path, summary_after_use, captured_at):
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    code, decision = run(tmp_path, limits, captured_at)
    assert code == 0
    assert decision == "COMMIT no previous reading"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["quota_status"] == "ok"
    assert (tmp_path / "history" / "2026-11.jsonl").exists()


def test_second_identical_run_skips(tmp_path, summary_after_use, captured_at):
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    run(tmp_path, limits, captured_at)
    record_status.mark_published(tmp_path)
    _, decision = run(tmp_path, limits, captured_at + timedelta(minutes=5))
    assert decision == "SKIP unchanged"
    assert len((tmp_path / "history" / "2026-11.jsonl").read_text().splitlines()) == 2


def test_garbage_input_is_recorded_as_error(tmp_path, captured_at):
    _, decision = run(tmp_path, "not json", captured_at)
    assert decision.startswith("COMMIT")
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["quota_status"] == "error"
    assert "unparseable" in status["quota_error_detail"]


def test_empty_input_is_an_error(tmp_path, captured_at):
    run(tmp_path, "", captured_at)
    status = json.loads((tmp_path / "status.json").read_text())
    assert "no output" in status["quota_error_detail"]


def test_corrupt_previous_status_counts_as_first(tmp_path, summary_after_use, captured_at):
    (tmp_path / "status.json").write_text("{", encoding="utf-8")
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    _, decision = run(tmp_path, limits, captured_at)
    assert decision == "COMMIT no previous reading"


def test_comparison_is_against_the_last_published_reading(tmp_path, summary_after_use, captured_at):
    import copy
    base = normalize_summary(summary_after_use, now=captured_at)
    run(tmp_path, json.dumps(base), captured_at)
    record_status.mark_published(tmp_path)
    decisions = []
    for step in range(1, 4):
        drift = copy.deepcopy(base)
        drift["weekly_percent_used"] = 1.0 + 0.4 * step
        decisions.append(run(tmp_path, json.dumps(drift), captured_at + timedelta(minutes=5 * step))[1])
    assert decisions[0] == decisions[1] == "SKIP unchanged"
    assert decisions[2].startswith("COMMIT weekly_percent_used 1.0 -> 2.2")


def test_heartbeat_fires_six_hours_after_publishing(tmp_path, summary_after_use, captured_at):
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    run(tmp_path, limits, captured_at)
    record_status.mark_published(tmp_path)
    for minutes in range(5, 360, 5):
        run(tmp_path, limits, captured_at + timedelta(minutes=minutes))
    _, decision = run(tmp_path, limits, captured_at + timedelta(hours=6, minutes=1))
    assert decision.startswith("COMMIT heartbeat")


def test_unpublished_previous_status_counts_as_first(tmp_path, summary_after_use, captured_at):
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    run(tmp_path, limits, captured_at)
    _, decision = run(tmp_path, limits, captured_at + timedelta(minutes=5))
    assert decision == "COMMIT no previous reading"


def test_non_object_published_file_is_ignored(tmp_path, summary_after_use, captured_at):
    (tmp_path / ".last_published.json").write_text("[1, 2]", encoding="utf-8")
    limits = json.dumps(normalize_summary(summary_after_use, now=captured_at))
    assert run(tmp_path, limits, captured_at)[1] == "COMMIT no previous reading"


def test_mark_published_copies_status(tmp_path, summary_after_use, captured_at):
    run(tmp_path, json.dumps(normalize_summary(summary_after_use, now=captured_at)), captured_at)
    record_status.mark_published(tmp_path)
    assert (tmp_path / ".last_published.json").read_text() == (tmp_path / "status.json").read_text()
