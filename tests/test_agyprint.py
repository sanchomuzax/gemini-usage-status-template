from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gemini_usage_status.agyprint import AgyPrint, UsageParseError, parse_usage_json

FIXTURE = (Path(__file__).parent / "fixtures" / "usage_print.json").read_text(encoding="utf-8")


def payload(**changes):
    data = json.loads(FIXTURE)
    data.update(changes)
    return json.dumps(data)


def buckets(summary):
    return {b["bucketId"]: b for g in summary["groups"] for b in g["buckets"]}


def test_parses_every_bucket_into_the_summary_shape():
    summary = parse_usage_json(FIXTURE)
    found = buckets(summary)
    assert set(found) == {"gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h",
                          "gemini-argon-wtu-weekly"}
    assert found["gemini-5h"] == {"bucketId": "gemini-5h", "window": "5h",
                                  "remainingFraction": 0.9206092953681946,
                                  "resetTime": "2026-11-07T19:39:09Z"}
    assert [g["displayName"] for g in summary["groups"]] == [
        "Gemini Models", "Claude and GPT models", "Gemini 4 Argon"]


def test_missing_fraction_stays_missing():
    data = json.loads(FIXTURE)
    del data["command"]["data"]["groups"][0]["buckets"][1]["remaining_fraction"]
    assert "remainingFraction" not in buckets(parse_usage_json(json.dumps(data)))["gemini-5h"]


def test_text_around_the_json_is_tolerated():
    assert buckets(parse_usage_json("note: something\n" + FIXTURE + "\n"))


@pytest.mark.parametrize("text, message", [
    ("", "no output"),
    ("not json at all", "no JSON"),
    (payload(status="ERROR", error="please sign in"), "please sign in"),
    (payload(num_turns=1), "model turn"),
    (payload(usage={"total_tokens": 12}), "model turn"),
    (payload(command={"name": "usage", "data": {"groups": []}}), "no quota buckets"),
    (payload(command=None), "no quota buckets"),
])
def test_anything_unexpected_is_an_error(text, message):
    with pytest.raises(UsageParseError, match=message):
        parse_usage_json(text)


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.kwargs = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_reader_runs_usage_in_plan_mode_and_parses_it(tmp_path):
    token = tmp_path / "token"
    token.write_text(json.dumps({"token": "SECRET", "id_token_email": "user@example.com"}))
    runner = Runner(done(stdout=FIXTURE))
    workdir = tmp_path / "work"
    summary, meta = AgyPrint("/bin/agy", runner=runner, token_file=token,
                             workdir=workdir).read()
    assert runner.calls[0] == ["/bin/agy", "--mode", "plan", "-p", "/usage",
                               "--output-format", "json"]
    assert runner.kwargs[0]["cwd"] == workdir and workdir.is_dir()
    assert buckets(summary)
    assert meta == {"email": "user@example.com", "plan": None, "agy_version": None}


def test_reader_falls_back_to_stderr_and_survives_a_missing_token_file(tmp_path):
    runner = Runner(done(stderr=FIXTURE))
    summary, meta = AgyPrint("/bin/agy", runner=runner, token_file=tmp_path / "x", workdir=tmp_path / "w").read()
    assert buckets(summary) and meta["email"] is None


def test_reader_reports_a_failed_run(tmp_path):
    runner = Runner(done(stderr="boom", code=2))
    with pytest.raises(UsageParseError, match="exit 2.*boom"):
        AgyPrint("/bin/agy", runner=runner, token_file=tmp_path / "x", workdir=tmp_path / "w").read()


def test_reader_reports_a_timeout(tmp_path):
    runner = Runner(subprocess.TimeoutExpired(["agy"], 60))
    with pytest.raises(UsageParseError, match="did not answer"):
        AgyPrint("/bin/agy", runner=runner, token_file=tmp_path / "x", workdir=tmp_path / "w").read()


def test_reader_reports_a_missing_binary(tmp_path):
    runner = Runner(FileNotFoundError("agy"))
    with pytest.raises(UsageParseError, match="not found"):
        AgyPrint("/bin/agy", runner=runner, token_file=tmp_path / "x", workdir=tmp_path / "w").read()


def test_version_is_read_once(tmp_path):
    runner = Runner(done(stdout="1.5.3\n"))
    reader = AgyPrint("/bin/agy", runner=runner, token_file=tmp_path / "x", workdir=tmp_path / "w")
    assert reader.version() == "1.5.3"
    assert reader.version() == "1.5.3"
    assert len(runner.calls) == 1


def test_version_failure_is_unknown(tmp_path):
    reader = AgyPrint("/bin/agy", runner=Runner(OSError("x")), token_file=tmp_path / "x", workdir=tmp_path / "w")
    assert reader.version() is None
