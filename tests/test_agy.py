from __future__ import annotations

import subprocess

from gemini_usage_status.agy import agy_version, refresh_via_agy


class Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_agy_version_parses_output():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return Done("1.5.3\n")
    assert agy_version("/bin/agy", runner=runner) == "1.5.3"
    assert calls == [["/bin/agy", "--version"]]


def test_agy_version_tolerates_prefix():
    assert agy_version("agy", runner=lambda a, **k: Done("agy version 2.0.1 (abc)\n")) == "2.0.1"


def test_agy_version_failures_return_none():
    def missing(args, **kwargs):
        raise FileNotFoundError(args[0])

    def hung(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)
    assert agy_version("agy", runner=missing) is None
    assert agy_version("agy", runner=hung) is None
    assert agy_version("agy", runner=lambda a, **k: Done("garbage")) is None


def test_refresh_runs_agy_models_without_stdin():
    seen = {}

    def runner(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Done("Fetching available models...\n")
    assert refresh_via_agy("/bin/agy", runner=runner) == (True, "")
    assert seen["args"] == ["/bin/agy", "models"]
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert seen["kwargs"]["timeout"] > 0


def test_refresh_failure_is_reported():
    ok, detail = refresh_via_agy("agy", runner=lambda a, **k: Done("boom", returncode=1))
    assert ok is False
    assert "exit 1" in detail

    def missing(args, **kwargs):
        raise FileNotFoundError(args[0])
    ok, detail = refresh_via_agy("agy", runner=missing)
    assert ok is False
    assert "not found" in detail
