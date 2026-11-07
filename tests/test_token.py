from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from gemini_usage_status.token import (
    TokenError,
    account_label,
    needs_refresh,
    parse_expiry,
    read_token,
)


def write_token(tmp_path, payload):
    path = tmp_path / "antigravity-oauth-token"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


GOOD = {
    "token": {
        "access_token": "ya29.test-access",
        "token_type": "Bearer",
        "refresh_token": "1//test-refresh",
        "expiry": "2026-11-07T11:37:07.506720288+01:00",
        "expires_in": 3599,
    },
    "auth_method": "consumer",
    "id_token_email": "someone@example.com",
}


def test_parse_expiry_handles_nanoseconds():
    assert parse_expiry("2026-11-07T11:37:07.506720288+01:00") == datetime(
        2026, 11, 7, 10, 37, 7, 506720, tzinfo=timezone.utc)


def test_parse_expiry_handles_z_and_plain():
    assert parse_expiry("2026-11-07T10:37:07Z") == datetime(2026, 11, 7, 10, 37, 7, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [None, "", "tomorrow", 42])
def test_parse_expiry_rejects_garbage(bad):
    assert parse_expiry(bad) is None


def test_read_token(tmp_path):
    info = read_token(write_token(tmp_path, GOOD))
    assert info.access_token == "ya29.test-access"
    assert info.expiry == datetime(2026, 11, 7, 10, 37, 7, 506720, tzinfo=timezone.utc)
    assert info.email == "someone@example.com"


def test_read_token_missing_file(tmp_path):
    with pytest.raises(TokenError, match="not found"):
        read_token(tmp_path / "nope")


def test_read_token_bad_json(tmp_path):
    path = tmp_path / "t"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TokenError, match="unreadable"):
        read_token(path)


def test_read_token_without_access_token(tmp_path):
    with pytest.raises(TokenError, match="no access_token"):
        read_token(write_token(tmp_path, {"token": {}}))


def test_token_error_never_contains_the_secret(tmp_path):
    payload = {"token": {"access_token": "", "refresh_token": "1//secret-value"}}
    with pytest.raises(TokenError) as caught:
        read_token(write_token(tmp_path, payload))
    assert "secret-value" not in str(caught.value)


def test_needs_refresh():
    now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
    info = read_token.__globals__["TokenInfo"]("x", now + timedelta(minutes=30), None)
    assert needs_refresh(info, now) is False
    assert needs_refresh(info, now + timedelta(minutes=26)) is True
    no_expiry = read_token.__globals__["TokenInfo"]("x", None, None)
    assert needs_refresh(no_expiry, now) is False  # rely on the 401 path instead


def test_account_label_is_the_local_part():
    assert account_label("someone@example.com") == "someone"
    assert account_label(None) is None
    assert account_label("weird") == "weird"
    assert account_label(123) is None
