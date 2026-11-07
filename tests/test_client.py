from __future__ import annotations

import io
import json
import urllib.error

import pytest

from gemini_usage_status.client import ApiError, CodeAssistClient, user_agent


def test_user_agent_mirrors_the_installed_cli():
    assert user_agent("1.5.3", "Linux", "aarch64") == (
        "antigravity/cli/1.5.3 (aidev_client; os_type=linux; arch=arm64)")
    assert user_agent("2.0.0", "Darwin", "x86_64") == (
        "antigravity/cli/2.0.0 (aidev_client; os_type=darwin; arch=amd64)")


def test_user_agent_falls_back_when_version_unknown():
    assert user_agent(None, "Linux", "aarch64").startswith("antigravity/cli/1.5.3 ")


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(responses, seen):
    def opener(request, timeout):
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())
    return opener


def http_error(code, message):
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def test_post_sends_bearer_user_agent_and_json():
    seen = []
    client = CodeAssistClient("tok", "UA/1", opener=make_opener([{"ok": 1}], seen))
    assert client.post("retrieveUserQuotaSummary", {"project": "p"}) == {"ok": 1}
    request = seen[0]
    assert request.full_url == (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary")
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("User-agent") == "UA/1"
    assert json.loads(request.data) == {"project": "p"}
    assert request.get_method() == "POST"


def test_http_error_becomes_api_error_with_server_message():
    client = CodeAssistClient("tok", "UA", opener=make_opener([http_error(403, "no license")], []))
    with pytest.raises(ApiError) as caught:
        client.post("loadCodeAssist", {})
    assert caught.value.status == 403
    assert "no license" in str(caught.value)
    assert "tok" not in str(caught.value)


def test_network_error_becomes_api_error():
    client = CodeAssistClient("tok", "UA", opener=make_opener(
        [urllib.error.URLError("dns"), urllib.error.URLError("dns")], []), sleep=lambda s: None)
    with pytest.raises(ApiError) as caught:
        client.post("loadCodeAssist", {})
    assert caught.value.status is None


def test_non_json_body_becomes_api_error():
    def opener(request, timeout):
        return FakeResponse(b"<html>")
    with pytest.raises(ApiError, match="not JSON"):
        CodeAssistClient("tok", "UA", opener=opener, sleep=lambda s: None).post("loadCodeAssist", {})


def test_load_code_assist_sends_antigravity_metadata():
    seen = []
    client = CodeAssistClient("tok", "UA", opener=make_opener([{}], seen))
    client.load_code_assist()
    assert json.loads(seen[0].data)["metadata"]["ideType"] == "ANTIGRAVITY"


def test_transient_errors_are_retried_once():
    seen, sleeps = [], []
    client = CodeAssistClient("tok", "UA", opener=make_opener([http_error(503, "busy"), {"ok": 1}], seen),
                              sleep=sleeps.append)
    assert client.post("loadCodeAssist", {}) == {"ok": 1}
    assert len(seen) == 2 and sleeps


def test_permanent_errors_are_not_retried():
    seen = []
    client = CodeAssistClient("tok", "UA", opener=make_opener([http_error(403, "no")], seen),
                              sleep=lambda s: None)
    with pytest.raises(ApiError):
        client.post("loadCodeAssist", {})
    assert len(seen) == 1


def test_second_transient_error_is_raised():
    client = CodeAssistClient("tok", "UA", opener=make_opener(
        [urllib.error.URLError("dns"), urllib.error.URLError("dns")], []), sleep=lambda s: None)
    with pytest.raises(ApiError):
        client.post("loadCodeAssist", {})
