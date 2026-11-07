"""Read the OAuth token the Antigravity CLI (agy) keeps on disk.

The monitor never refreshes the token itself: that would need the CLI's OAuth
client secret. When the token is about to expire, the collector lets agy renew
it (see agy.py). Error messages never include token material.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fsutil import display_path

DEFAULT_TOKEN_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

# Renew this long before expiry so a slow run cannot outlive its token.
REFRESH_MARGIN = timedelta(minutes=5)

_FRACTION = re.compile(r"(\.\d{6})\d+")


class TokenError(RuntimeError):
    """The token file is missing or unusable."""


@dataclass(frozen=True)
class TokenInfo:
    access_token: str
    expiry: datetime | None
    email: str | None


def parse_expiry(raw):
    """ISO 8601 with up to nanoseconds (Go writes nine digits) -> aware UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    text = _FRACTION.sub(r"\1", raw.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_token(path=DEFAULT_TOKEN_PATH):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TokenError(f"token file not found: {display_path(path)}") from error
    except (OSError, ValueError) as error:
        raise TokenError(f"token file unreadable: {display_path(path)} ({type(error).__name__})") from error

    token = payload.get("token") if isinstance(payload, dict) else None
    access = token.get("access_token") if isinstance(token, dict) else None
    if not access:
        raise TokenError(f"no access_token in {display_path(path)}")
    return TokenInfo(
        access_token=access,
        expiry=parse_expiry(token.get("expiry")),
        email=payload.get("id_token_email"),
    )


def needs_refresh(info, now, margin=REFRESH_MARGIN):
    """True when the token is about to expire.

    A token without a readable expiry is used as is: running agy on every
    reading would cost a minute each time, and a real expiry shows up as a 401,
    which triggers one renewal.
    """
    return info.expiry is not None and info.expiry - now <= margin


def account_label(email):
    """Short account name for the published status: the e-mail's local part."""
    if not isinstance(email, str) or not email:
        return None
    return email.split("@", 1)[0]
