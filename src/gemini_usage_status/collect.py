"""One quota reading, end to end. Never raises: failures come back as `error`."""

from __future__ import annotations

import json
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agy import agy_version, find_agy, refresh_via_agy
from .client import HOST, ApiError, CodeAssistClient, user_agent
from .fsutil import atomic_write_text, exclusive_lock
from .normalize import QuotaShapeError, normalize_summary, tier_from_load
from .stabilize import hold_within_window, merge_samples
from .token import DEFAULT_TOKEN_PATH, TokenError, account_label, needs_refresh, read_token

# Project, subscription and CLI version barely change; asking once a day keeps
# the per-run cost down to the quota samples.
STATE_MAX_AGE = timedelta(hours=24)

# The backend's replicas disagree (see stabilize.py); a few samples a couple of
# seconds apart make it likely that at least one answer is current.
QUOTA_SAMPLES = 3
SAMPLE_GAP_SECONDS = 2

HINT_403 = ("the server rejected the call; if agy itself still works, Google "
            "probably changed the required User-Agent or host")
HINT_SIGN_IN = "run `agy` once interactively to sign in again"


class CollectError(RuntimeError):
    pass


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _state_fresh(state, now, account):
    """Usable without asking again: recent, complete, and for this account."""
    try:
        fetched = datetime.fromisoformat(state["fetched_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return False
    if fetched.tzinfo is None:
        return False
    return bool(state.get("project")) and state.get("account") == account \
        and now - fetched < STATE_MAX_AGE


def _save_state(path, state):
    try:
        atomic_write_text(path, json.dumps(state, indent=2) + "\n")
    except OSError:
        pass  # a missing cache only costs one extra call next run


class _Session:
    """Token handling shared by every call in one run."""

    def __init__(self, token_path, agy_bin, runner, now):
        self.token_path = token_path
        self.agy_bin = agy_bin
        self.runner = runner
        self.now = now
        self.token = read_token(token_path)
        self._initial_token = self.token.access_token

    @property
    def refreshed(self):
        return self.token.access_token != self._initial_token

    @property
    def account(self):
        return account_label(self.token.email)

    def refresh(self):
        """Let agy renew the token; returns agy's failure detail ('' on success)."""
        ok, detail = refresh_via_agy(self.agy_bin, runner=self.runner)
        self.token = read_token(self.token_path)
        if self.token.expiry is not None and self.token.expiry <= self.now:
            raise CollectError(f"token expired and agy did not renew it ({detail or 'no detail'}); "
                               f"{HINT_SIGN_IN}")
        return "" if ok else detail

    def ensure_fresh(self):
        if needs_refresh(self.token, self.now):
            self.refresh()


class _Reader:
    def __init__(self, session, state_path, client_factory, system, machine, now):
        self.session = session
        self.state_path = state_path
        self.client_factory = client_factory
        self.system = system
        self.machine = machine
        self.now = now

    def client(self, version):
        return self.client_factory(self.session.token.access_token,
                                   user_agent(version, self.system, self.machine))

    def state(self, force=False):
        """(state, fetched_now). Cached unless forced, stale, or another account."""
        cached = _load_state(self.state_path)
        if not force and _state_fresh(cached, self.now, self.session.account):
            return cached, False
        version = agy_version(self.session.agy_bin, runner=self.session.runner) \
            or cached.get("agy_version")
        try:
            tier = tier_from_load(self.client(version).load_code_assist())
        except ApiError as error:
            if cached.get("project") and cached.get("account") == self.session.account:
                return cached, False  # stale but usable; next run asks again
            raise CollectError(f"loadCodeAssist failed: {error}") from error
        if not tier["project"]:
            if cached.get("project") and cached.get("account") == self.session.account:
                return cached, False
            raise CollectError("loadCodeAssist returned no project")
        fresh = {**tier, "account": self.session.account, "agy_version": version,
                 "fetched_at": _iso(self.now)}
        _save_state(self.state_path, fresh)
        return fresh, True

    def extra_samples(self, state, count, sleep):
        """Further readings for the merge; a failed extra sample is just skipped."""
        samples = []
        for _ in range(count):
            sleep(SAMPLE_GAP_SECONDS)
            try:
                samples.append(self.client(state.get("agy_version")).post(
                    "retrieveUserQuotaSummary", {"project": state["project"]}))
            except ApiError:
                continue
        return samples

    def summary(self, state, fetched_now):
        """The quota summary, with one renewal on 401 and one state refresh on 403."""
        def ask(current):
            return self.client(current.get("agy_version")).post(
                "retrieveUserQuotaSummary", {"project": current["project"]})
        try:
            return ask(state), state
        except ApiError as error:
            if error.status == 403 and not fetched_now:
                # A cached project or CLI version may be what the server objects to.
                fresh, _ = self.state(force=True)
                return ask(fresh), fresh
            if error.status != 401:
                raise
        detail = self.session.refresh()
        try:
            return ask(state), state
        except ApiError as error:
            if error.status == 401:
                raise CollectError(f"{error}: the token was refused even after agy ran "
                                   f"({detail or 'agy reported success'}); {HINT_SIGN_IN}") from error
            raise


def _describe(error):
    if isinstance(error, TokenError):
        return f"{error}; sign in with `agy` (Antigravity CLI)"
    if isinstance(error, ApiError) and error.status == 403:
        return f"{error} -- {HINT_403}"
    if isinstance(error, QuotaShapeError):
        return f"unexpected quota shape: {error}"
    if isinstance(error, (CollectError, ApiError)):
        return str(error)
    return f"unexpected failure: {type(error).__name__}: {error}"


def _previous_status(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read(now, token_path, state_path, agy_bin, runner, client_factory, system, machine, clock,
          samples, sleep):
    session = _Session(token_path, agy_bin, runner, now)
    session.ensure_fresh()
    reader = _Reader(session, state_path, client_factory, system, machine, now)
    state, fetched_now = reader.state()
    response, state = reader.summary(state, fetched_now)
    merged = merge_samples([response] + reader.extra_samples(state, samples - 1, sleep))
    steady = hold_within_window(merged, _previous_status(state_path.with_name("status.json")))
    # Unused windows report "now + window" as their reset, so compare against
    # the moment the answer arrived, not the start of the run.
    figures = normalize_summary(steady, now=clock())
    return {
        **figures,
        "subscription_type": state.get("subscription_type"),
        "subscription_name": state.get("subscription_name"),
        "account": session.account,
        "agy_version": state.get("agy_version"),
        "source_host": HOST,
        "token_refreshed": session.refreshed,
    }


def collect(*, now=None, token_path=DEFAULT_TOKEN_PATH, state_path=None, agy_bin=None,
            runner=subprocess.run, client_factory=CodeAssistClient,
            system=None, machine=None, samples=QUOTA_SAMPLES, sleep=time.sleep):
    fixed = now
    now = now or datetime.now(timezone.utc)
    state_path = Path(state_path or Path(__file__).resolve().parents[2] / ".state.json")
    try:
        with exclusive_lock(state_path.with_name(".collect.lock")):
            return _read(now, token_path, state_path, agy_bin or find_agy(), runner,
                         client_factory, system or platform.system(), machine or platform.machine(),
                         clock=lambda: fixed or datetime.now(timezone.utc),
                         samples=max(1, samples), sleep=sleep)
    except Exception as error:  # noqa: BLE001 -- the contract is "never raise"
        return {"error": _describe(error)}
