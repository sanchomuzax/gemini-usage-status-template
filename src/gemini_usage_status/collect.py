"""One quota reading, end to end. Never raises: failures come back as `error`."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import exclusive_lock
from .normalize import QuotaShapeError, normalize_summary
from .stabilize import merge_samples
from .tui import AgySession, UsageParseError, parse_usage

REPO_DIR = Path(__file__).resolve().parents[2]
SOURCE = "agy /usage"

# The backend's replicas disagree (see stabilize.py); the median of three looks
# a couple of seconds apart outvotes a single stray answer.
QUOTA_SAMPLES = 3
SAMPLE_GAP_SECONDS = 2


def _previous_status(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _account(email):
    return email.split("@", 1)[0] if isinstance(email, str) and "@" in email else None


def _slug(plan):
    return re.sub(r"[^a-z0-9]+", "-", plan.lower()).strip("-") if isinstance(plan, str) else None


def _describe(error):
    if isinstance(error, UsageParseError):
        return str(error)
    if isinstance(error, QuotaShapeError):
        return f"unexpected quota shape: {error}"
    return f"unexpected failure: {type(error).__name__}: {error}"


def _read(session, status_path, samples, sleep, now):
    readings, meta, observed = [], {}, None
    for index in range(samples):
        if index:
            sleep(SAMPLE_GAP_SECONDS)
        observed = now or datetime.now(timezone.utc)
        summary, found = parse_usage(session.read_usage(), observed)
        readings.append(summary)
        meta = {**meta, **{k: v for k, v in found.items() if v}}
    previous = _previous_status(status_path) or {}
    figures = normalize_summary(merge_samples(readings), now=observed)
    plan = meta.get("plan") or previous.get("subscription_name")
    return {
        **figures,
        "subscription_type": _slug(plan),
        "subscription_name": plan,
        "account": _account(meta.get("email")),
        "agy_version": meta.get("agy_version") or previous.get("agy_version"),
        "source_host": SOURCE,
    }


def collect(*, now=None, session=None, status_path=None, samples=QUOTA_SAMPLES, sleep=time.sleep):
    status_path = Path(status_path or REPO_DIR / "status.json")
    try:
        with exclusive_lock(status_path.with_name(".collect.lock")):
            return _read(session or AgySession(), status_path, max(1, samples), sleep, now)
    except Exception as error:  # noqa: BLE001 -- the contract is "never raise"
        return {"error": _describe(error)}
