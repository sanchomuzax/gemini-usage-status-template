from __future__ import annotations

import io
import json

import fetch_limits


def test_main_prints_the_collector_result_as_json(monkeypatch):
    monkeypatch.setattr(fetch_limits, "collect", lambda: {"error": "token file not found"})
    out = io.StringIO()
    monkeypatch.setattr(fetch_limits.sys, "stdout", out)
    assert fetch_limits.main() == 0
    assert json.loads(out.getvalue()) == {"error": "token file not found"}


def test_main_survives_an_escaping_exception(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(fetch_limits, "collect", boom)
    out = io.StringIO()
    monkeypatch.setattr(fetch_limits.sys, "stdout", out)
    assert fetch_limits.main() == 0
    assert "kaboom" in json.loads(out.getvalue())["error"]
