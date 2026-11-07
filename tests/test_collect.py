from __future__ import annotations

import json
from datetime import timedelta

import pytest

from gemini_usage_status.client import ApiError
from gemini_usage_status.collect import STATE_MAX_AGE, collect


class FakeClient:
    """Stands in for CodeAssistClient; scripted per method."""

    def __init__(self, token, ua, script, log):
        self.token = token
        self.ua = ua
        self._script = script
        self._log = log

    def load_code_assist(self):
        self._log.append(("loadCodeAssist", self.token, self.ua))
        return self._next("loadCodeAssist")

    def post(self, method, payload):
        self._log.append((method, self.token, payload))
        return self._next(method)

    def _next(self, method):
        item = self._script[method].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def env(tmp_path, captured_at, summary_after_use, load_code_assist_pro):
    token_path = tmp_path / "antigravity-oauth-token"
    state_path = tmp_path / ".state.json"

    def write_token(access="ya29.a", expiry=captured_at + timedelta(minutes=50)):
        token_path.write_text(json.dumps({
            "token": {"access_token": access, "expiry": expiry.isoformat()},
            "id_token_email": "someone@example.com",
        }), encoding="utf-8")

    write_token()
    log = []
    script = {
        "loadCodeAssist": [load_code_assist_pro],
        "retrieveUserQuotaSummary": [summary_after_use],
    }
    runner_calls = []

    def runner(args, **kwargs):
        runner_calls.append(args)

        class Done:
            returncode = 0
            stdout = "1.5.3\n" if args[-1] == "--version" else "Fetching available models...\n"
        if args[-1] == "models" and env_state.get("on_refresh"):
            env_state["on_refresh"]()
        return Done()

    env_state = {}

    def run(**overrides):
        return collect(
            now=overrides.get("now", captured_at),
            token_path=token_path,
            state_path=state_path,
            agy_bin="/fake/agy",
            runner=runner,
            client_factory=lambda token, ua: FakeClient(token, ua, script, log),
        )

    return {
        "run": run, "log": log, "script": script, "runner_calls": runner_calls,
        "write_token": write_token, "state_path": state_path, "env_state": env_state,
        "token_path": token_path,
    }


def test_happy_path(env):
    result = env["run"]()
    assert "error" not in result
    assert result["session_percent_used"] == 1.0
    assert result["weekly_percent_used"] == 1.0
    assert result["subscription_type"] == "g1-pro-tier"
    assert result["account"] == "someone"
    assert result["agy_version"] == "1.5.3"
    assert result["source_host"] == "daily-cloudcode-pa.googleapis.com"
    assert result["token_refreshed"] is False
    summary_call = next(c for c in env["log"] if c[0] == "retrieveUserQuotaSummary")
    assert summary_call[2] == {"project": "example-project-123"}
    assert all("ya29" not in json.dumps(v) for v in result.values())


def test_user_agent_uses_the_installed_version(env):
    env["run"]()
    ua = env["log"][0][2]
    assert ua.startswith("antigravity/cli/1.5.3 (aidev_client;")


def test_state_is_cached_between_runs(env, summary_after_use):
    env["run"]()
    env["script"]["retrieveUserQuotaSummary"].append(summary_after_use)
    env["run"]()
    assert [c[0] for c in env["log"]].count("loadCodeAssist") == 1
    state = json.loads(env["state_path"].read_text())
    assert state["project"] == "example-project-123"
    assert "access_token" not in json.dumps(state)


def test_stale_state_is_refreshed(env, captured_at, summary_after_use, load_code_assist_pro):
    env["run"]()
    env["script"]["retrieveUserQuotaSummary"].append(summary_after_use)
    env["script"]["loadCodeAssist"].append(load_code_assist_pro)
    later = captured_at + STATE_MAX_AGE + timedelta(minutes=1)
    env["write_token"](expiry=later + timedelta(minutes=50))
    env["run"](now=later)
    assert [c[0] for c in env["log"]].count("loadCodeAssist") == 2


def test_corrupt_state_file_is_ignored(env):
    env["state_path"].write_text("{broken", encoding="utf-8")
    result = env["run"]()
    assert "error" not in result


def test_expiring_token_triggers_agy_refresh(env, captured_at):
    env["write_token"](expiry=captured_at + timedelta(minutes=2))
    env["env_state"]["on_refresh"] = lambda: env["write_token"](
        access="ya29.b", expiry=captured_at + timedelta(minutes=60))
    result = env["run"]()
    assert ["/fake/agy", "models"] in env["runner_calls"]
    assert result["token_refreshed"] is True
    assert env["log"][-1][1] == "ya29.b"


def test_refresh_that_does_not_help_is_an_error(env, captured_at):
    env["write_token"](expiry=captured_at - timedelta(minutes=5))
    result = env["run"]()
    assert "token expired" in result["error"]
    assert not any(c[0] == "retrieveUserQuotaSummary" for c in env["log"])


def test_401_triggers_one_refresh_and_retry(env, captured_at, summary_after_use):
    env["script"]["retrieveUserQuotaSummary"] = [ApiError(401, "expired"), summary_after_use]
    env["env_state"]["on_refresh"] = lambda: env["write_token"](
        access="ya29.c", expiry=captured_at + timedelta(minutes=60))
    result = env["run"]()
    assert "error" not in result
    assert result["token_refreshed"] is True


def test_403_is_an_error_with_hint(env):
    env["script"]["retrieveUserQuotaSummary"] = [ApiError(403, "The caller does not have permission")]
    result = env["run"]()
    assert "403" in result["error"]
    assert "User-Agent" in result["error"]


def test_missing_token_file(env):
    env["token_path"].unlink()
    result = env["run"]()
    assert "not found" in result["error"]
    assert "agy" in result["error"]


def test_shape_error_is_reported(env):
    env["script"]["retrieveUserQuotaSummary"] = [{"groups": []}]
    result = env["run"]()
    assert "unexpected quota shape" in result["error"]


def test_load_code_assist_failure_without_cached_project(env):
    env["script"]["loadCodeAssist"] = [ApiError(None, "network down")]
    result = env["run"]()
    assert "loadCodeAssist" in result["error"]


def test_unexpected_exception_never_escapes(env):
    env["script"]["retrieveUserQuotaSummary"] = [RuntimeError("kaboom")]
    result = env["run"]()
    assert "kaboom" in result["error"]


def test_state_of_another_account_is_not_reused(env, summary_after_use, load_code_assist_pro):
    env["run"]()
    state = json.loads(env["state_path"].read_text())
    env["state_path"].write_text(json.dumps(dict(state, account="someone-else")), encoding="utf-8")
    env["script"]["retrieveUserQuotaSummary"].append(summary_after_use)
    env["script"]["loadCodeAssist"].append(load_code_assist_pro)
    env["run"]()
    assert [c[0] for c in env["log"]].count("loadCodeAssist") == 2


def test_403_with_cached_state_refreshes_state_and_retries(env, summary_after_use, load_code_assist_pro):
    env["run"]()
    env["script"]["retrieveUserQuotaSummary"] = [ApiError(403, "denied"), summary_after_use]
    env["script"]["loadCodeAssist"].append(load_code_assist_pro)
    result = env["run"]()
    assert "error" not in result
    assert [c[0] for c in env["log"]].count("loadCodeAssist") == 2


def test_second_401_tells_the_user_to_sign_in(env, captured_at):
    env["script"]["retrieveUserQuotaSummary"] = [ApiError(401, "expired"), ApiError(401, "expired")]
    result = env["run"]()
    assert "401" in result["error"] and "agy" in result["error"]


def test_refresh_flag_only_when_the_token_changed(env, captured_at, summary_after_use):
    env["script"]["retrieveUserQuotaSummary"] = [ApiError(401, "expired"), summary_after_use]
    result = env["run"]()  # agy ran, but the token file stayed the same
    assert result["token_refreshed"] is False


def test_token_without_expiry_does_not_run_agy_every_time(env):
    env["token_path"].write_text(json.dumps({"token": {"access_token": "ya29.a"}}), encoding="utf-8")
    result = env["run"]()
    assert "error" not in result
    assert ["/fake/agy", "models"] not in env["runner_calls"]


def test_non_string_email_does_not_break_the_contract(env):
    env["token_path"].write_text(json.dumps({"token": {"access_token": "ya29.a"},
                                             "id_token_email": 123}), encoding="utf-8")
    result = env["run"]()
    assert result["account"] is None


def test_project_less_answer_falls_back_to_cached_state(env, captured_at, summary_after_use):
    env["run"]()
    later = captured_at + STATE_MAX_AGE + timedelta(minutes=1)
    env["write_token"](expiry=later + timedelta(minutes=50))
    env["script"]["loadCodeAssist"].append({"currentTier": {"id": "free-tier"}})
    env["script"]["retrieveUserQuotaSummary"].append(summary_after_use)
    assert "error" not in env["run"](now=later)


def test_project_less_answer_without_cache_is_an_error(env):
    env["script"]["loadCodeAssist"] = [{"currentTier": {"id": "free-tier"}}]
    assert "no project" in env["run"]()["error"]


def test_naive_state_timestamp_is_treated_as_stale(env):
    env["state_path"].write_text(json.dumps({"project": "p", "fetched_at": "2026-11-07T09:00:00",
                                             "account": "someone"}), encoding="utf-8")
    assert "error" not in env["run"]()


def test_error_detail_hides_the_home_directory():
    from pathlib import Path

    from gemini_usage_status.fsutil import display_path
    assert display_path(Path.home() / ".gemini" / "x") == "~/.gemini/x"
    assert display_path("/srv/x") == "/srv/x"


def test_state_file_is_written_atomically(env):
    env["run"]()
    assert not list(env["state_path"].parent.glob("*.tmp"))
