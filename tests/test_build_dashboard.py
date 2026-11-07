from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DASHES = tuple(chr(code) for code in (0x2014, 0x2013, 0x2212))  # em, en, unicode minus


def load_builder():
    spec = importlib.util.spec_from_file_location("build_dashboard", ROOT / "build_dashboard.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_row(stamp: str = "2026-05-04T00:00:00Z", **overrides) -> dict:
    row = {
        "t": stamp,
        "quota_status": "ok",
        "session": 5.0,
        "weekly": 20.0,
        "max": 20.0,
        "tokens_7d": None,
        "account": "demo-account",
        "session_started": True,
        "weekly_started": True,
        "tp_session": 3.0,
        "tp_weekly": 10.0,
        "argon_weekly": 2.0,
    }
    row.update(overrides)
    return row


def write_history(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def payload_of(html: str) -> dict:
    raw = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)


def build_payload(module, rows: list[dict]) -> dict:
    prepared = [{**r, "_dt": module.parse_ts(r["t"])} for r in rows]
    return module.build_payload(prepared, 2026, 5)


def test_generated_report_declares_utf8_and_antigravity_title(tmp_path) -> None:
    module = load_builder()
    history = write_history(tmp_path / "2026-05.jsonl", [make_row()])

    assert module.main([str(history)]) == 0
    html = history.with_suffix(".html").read_text(encoding="utf-8")

    assert html.startswith('<!doctype html>\n<meta charset="utf-8">')
    assert "<title>Antigravity Quota - May 2026</title>" in html
    assert "Codex" not in html


def test_report_has_no_dashes_that_the_house_style_forbids(tmp_path) -> None:
    module = load_builder()
    history = write_history(tmp_path / "2026-05.jsonl", [make_row()])
    module.main([str(history)])
    html = history.with_suffix(".html").read_text(encoding="utf-8")

    for dash in FORBIDDEN_DASHES:
        assert dash not in html
        assert dash not in (ROOT / "build_dashboard.py").read_text(encoding="utf-8")


def test_series_carries_the_claude_gpt_and_argon_fields() -> None:
    module = load_builder()
    payload = build_payload(module, [
        make_row("2026-05-04T00:00:00Z"),
        make_row("2026-05-04T00:05:00Z", tp_session=40.5, tp_weekly=12.0, argon_weekly=3.5,
                 session_started=False, weekly_started=True),
    ])

    first, second = payload["series"]
    assert (first["ts"], first["tw"], first["ag"]) == (3.0, 10.0, 2.0)
    assert (second["ts"], second["tw"], second["ag"]) == (40.5, 12.0, 3.5)
    assert first["ss"] is True and second["ss"] is False
    assert second["ws"] is True
    assert "k" not in first  # no token series: Antigravity gives no token counts
    assert payload["meta"]["hasExtra"] is True


def test_session_is_always_available_not_marked_unexposed() -> None:
    module = load_builder()
    payload = build_payload(module, [make_row(session=7.5, max=20.0)])
    session_kpi = next(k for k in payload["kpis"] if k["key"] == "sessionPeak")

    assert payload["meta"]["hasSession"] is True
    assert session_kpi["value"] == 7.5
    assert session_kpi["note"]["type"] == "peakTime"


def test_tp_kpi_reports_peaks_of_claude_gpt_and_argon() -> None:
    module = load_builder()
    payload = build_payload(module, [
        make_row("2026-05-04T00:00:00Z", tp_session=10.0, tp_weekly=5.0, argon_weekly=1.0),
        make_row("2026-05-04T00:05:00Z", tp_session=60.0, tp_weekly=25.5, argon_weekly=4.0),
        make_row("2026-05-04T00:10:00Z", tp_session=20.0, tp_weekly=15.0, argon_weekly=2.0),
    ])
    kpi = next(k for k in payload["kpis"] if k["key"] == "tpPeak")

    assert not any(k["key"] == "tokensAvg" for k in payload["kpis"])
    assert kpi["value"] == 25.5
    assert kpi["note"] == {"type": "tpArgon", "tpSession": 60.0, "argon": 4.0}


def test_missing_extra_buckets_degrade_to_no_data() -> None:
    module = load_builder()
    payload = build_payload(module, [
        make_row(tp_session=None, tp_weekly=None, argon_weekly=None),
    ])
    kpi = next(k for k in payload["kpis"] if k["key"] == "tpPeak")

    assert payload["meta"]["hasExtra"] is False
    assert kpi["value"] is None
    assert kpi["note"] == {"type": "noData"}


def test_rows_without_the_new_fields_still_build() -> None:
    module = load_builder()
    legacy = {"t": "2026-05-04T00:00:00Z", "quota_status": "ok", "session": 1.0,
              "weekly": 2.0, "max": 2.0, "tokens_7d": None}
    payload = build_payload(module, [legacy])

    assert payload["series"][0]["ts"] is None
    assert payload["meta"]["hasExtra"] is False


def test_saturation_episode_names_the_window_that_peaked() -> None:
    module = load_builder()
    payload = build_payload(module, [
        make_row("2026-05-04T00:00:00Z"),
        make_row("2026-05-04T00:05:00Z", quota_status="critical", tp_session=93.0, max=93.0),
        make_row("2026-05-04T00:10:00Z", quota_status="exhausted", tp_session=100.0, max=100.0),
        make_row("2026-05-04T00:15:00Z"),
    ])

    (episode,) = payload["episodes"]
    assert episode["peak"] == 100.0
    assert episode["hasExhausted"] is True
    assert episode["at"]["tpSession"] == 100.0
    assert episode["at"]["session"] == 5.0
    assert episode["steps"][-1]["cls"] == "reset"


def test_error_blocks_do_not_mention_token_logs() -> None:
    module = load_builder()
    payload = build_payload(module, [
        make_row("2026-05-04T00:00:00Z"),
        make_row("2026-05-04T00:05:00Z", quota_status="error", session=None, weekly=None, max=None,
                 tp_session=None, tp_weekly=None, argon_weekly=None),
        make_row("2026-05-04T00:10:00Z", quota_status="error", session=None, weekly=None, max=None,
                 tp_session=None, tp_weekly=None, argon_weekly=None),
        make_row("2026-05-04T00:15:00Z"),
    ])

    (block,) = payload["errorBlocks"]
    assert block["count"] == 2
    assert "logAlive" not in block
    html = module.render(payload)
    assert "logAlive" not in html and "tokens_7d" not in html


def test_both_languages_cover_the_new_labels(tmp_path) -> None:
    module = load_builder()
    history = write_history(tmp_path / "2026-05.jsonl", [make_row()])
    module.main([str(history)])
    html = history.with_suffix(".html").read_text(encoding="utf-8")

    for text in (
        "Antigravity (Google AI Pro) quota",       # EN eyebrow
        "Antigravity (Google AI Pro) kvóta",       # HU eyebrow
        "Claude/GPT and Argon quotas over time",   # EN extra chart
        "Claude/GPT és Argon keret az időben",     # HU extra chart
        "no token counts",
        "tokenszámot nem",
    ):
        assert text in html


def test_the_line_breaks_where_the_active_account_changes(tmp_path) -> None:
    """Two accounts are two quotas; joining them draws a cliff that is not a
    change in consumption. The series marks the switch so the chart breaks."""
    module = load_builder()
    history = write_history(tmp_path / "2026-05.jsonl", [
        make_row(stamp, weekly=weekly, max=weekly, account=account)
        for stamp, weekly, account in (
            ("2026-05-04T00:00:00Z", 67.0, "account-a"),
            ("2026-05-04T00:05:00Z", 67.0, "account-a"),
            ("2026-05-04T00:10:00Z", 53.0, "account-b"),
            ("2026-05-04T00:15:00Z", 53.0, "account-b"),
        )
    ])
    out = tmp_path / "report.html"

    module.main([str(history), "-o", str(out)])
    series = payload_of(out.read_text(encoding="utf-8"))["series"]

    assert [point["b"] for point in series] == [0, 0, 1, 0]
    assert [point["a"] for point in series] == ["account-a", "account-a", "account-b", "account-b"]


def test_a_single_sample_history_builds(tmp_path) -> None:
    """The very first run of the monitor has exactly one row."""
    module = load_builder()
    history = write_history(tmp_path / "2026-11.jsonl", [make_row("2026-11-07T09:47:58Z")])

    assert module.main([str(history)]) == 0
    payload = payload_of(history.with_suffix(".html").read_text(encoding="utf-8"))
    assert payload["meta"]["n"] == 1


def test_sample_generator_is_pinned_and_matches_the_committed_file(tmp_path) -> None:
    sample = ROOT / "sample"
    before = (sample / "2026-05.jsonl").read_bytes()

    result = subprocess.run([sys.executable, "-I", str(sample / "make_sample.py")],
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert (sample / "2026-05.jsonl").read_bytes() == before
    rows = [json.loads(line) for line in before.decode("utf-8").splitlines()]
    assert all(r["tokens_7d"] is None for r in rows)
    assert {"ok", "warning", "critical", "exhausted", "error"} <= {r["quota_status"] for r in rows}
    assert re.fullmatch(r"[0-9a-f]{64}", re.search(r'EXPECTED_SHA256 = "([0-9a-f]+)"',
                        (sample / "make_sample.py").read_text()).group(1))
