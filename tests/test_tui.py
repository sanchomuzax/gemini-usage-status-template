from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gemini_usage_status.normalize import normalize_summary
from gemini_usage_status.tui import AgySession, UsageParseError, parse_usage

PANEL = (Path(__file__).parent / "fixtures" / "usage_panel.txt").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 7, 12, 44, 0, tzinfo=timezone.utc)


def buckets(summary):
    return {b["bucketId"]: b for g in summary["groups"] for b in g["buckets"]}


def test_parses_every_bucket():
    summary, meta = parse_usage(PANEL, NOW)
    found = buckets(summary)
    assert set(found) == {"gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h", "gemini-argon-wtu-weekly"}
    assert found["gemini-weekly"]["remainingFraction"] == pytest.approx(0.9931)
    assert found["gemini-5h"]["remainingFraction"] == pytest.approx(0.9827)
    assert found["gemini-argon-wtu-weekly"]["remainingFraction"] == pytest.approx(0.9978)
    assert found["gemini-5h"]["window"] == "5h"
    assert found["gemini-weekly"]["window"] == "weekly"


def test_refresh_countdown_becomes_a_reset_time():
    found = buckets(parse_usage(PANEL, NOW)[0])
    assert found["gemini-5h"]["resetTime"] == "2026-11-07T14:39:00Z"          # 1h 55m
    assert found["gemini-weekly"]["resetTime"] == "2026-11-14T09:39:00Z"      # 164h 55m


def test_quota_available_is_an_unstarted_window():
    summary, _ = parse_usage(PANEL, NOW)
    rows = {r["bucket_id"]: r for r in normalize_summary(summary, now=NOW)["limits"]}
    assert rows["3p-5h"]["percent_used"] == 0.0
    assert rows["3p-5h"]["window_started"] is False
    assert rows["3p-weekly"]["window_started"] is False
    assert rows["gemini-5h"]["window_started"] is True


def test_group_names_match_the_http_shape():
    summary, _ = parse_usage(PANEL, NOW)
    assert [g["displayName"] for g in summary["groups"]] == [
        "Gemini Models", "Claude and GPT models", "Gemini 4 Argon"]


def test_account_from_the_panel_when_the_banner_scrolled_away():
    _, meta = parse_usage(PANEL, NOW)
    assert meta == {"email": "user@example.com", "plan": None, "agy_version": None}


def test_plan_and_version_from_a_visible_banner():
    banner = "  Antigravity CLI 1.5.3\n  user@example.com (Google AI Pro)\n"
    _, meta = parse_usage(banner + PANEL, NOW)
    assert meta == {"email": "user@example.com", "plan": "Google AI Pro", "agy_version": "1.5.3"}


def test_days_in_the_countdown():
    text = PANEL.replace("Refreshes in 164h 55m", "Refreshes in 6d 20h 55m", 1)
    found = buckets(parse_usage(text, NOW)[0])
    assert found["gemini-weekly"]["resetTime"] == "2026-11-14T09:39:00Z"


def test_unknown_group_is_kept():
    extra = "\nGEMINI 5 NOVA\n  Nova Weekly Limit\n    [███] 50.00%\n    Refreshes in 10h 0m\n"
    summary, _ = parse_usage(PANEL + extra, NOW)
    found = buckets(summary)
    assert found["gemini-5-nova-weekly"]["remainingFraction"] == pytest.approx(0.5)


def test_panel_without_buckets_is_an_error():
    with pytest.raises(UsageParseError):
        parse_usage("nothing to see", NOW)


def test_sign_in_screen_is_reported():
    with pytest.raises(UsageParseError, match="sign in"):
        parse_usage("Please sign in: https://accounts.google.com/o/oauth2/auth?x=1", NOW)


class FakeTmux:
    """A tiny agy: ready screen, an input line, and the panel after Enter."""

    def __init__(self, panels, alive=True, ready=True, echo=None):
        self.calls = []
        self.panels = list(panels)
        self.alive = alive
        self.ready = ready
        self.echo = echo          # what the input line shows after typing
        self.line = ""
        self.entered = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)

        class Done:
            returncode = 0
            stdout = ""
        done = Done()
        if args[:2] == ["tmux", "has-session"]:
            done.returncode = 0 if self.alive else 1
        elif args[:2] == ["tmux", "new-session"]:
            self.alive = True
        elif args[:2] == ["tmux", "send-keys"]:
            if args[-1] == "C-u":
                self.line = ""
            elif "-l" in args:
                self.line = self.echo if self.echo is not None else args[-1]
            elif args[-1] == "Enter":
                self.entered = True
        elif args[:2] == ["tmux", "capture-pane"]:
            if self.entered:
                done.stdout = self.panels.pop(0) if len(self.panels) > 1 else self.panels[0]
            else:
                suggestion = "\n> /usage  View model quota usage" if self.line.startswith("/") else ""
                done.stdout = ("? for shortcuts\n" if self.ready else "starting\n") + "> " + self.line + suggestion
        return done


def session(fake, **kwargs):
    return AgySession("s", "/bin/agy", runner=fake, sleep=lambda s: None, **kwargs)


def test_session_is_started_in_plan_mode_when_missing():
    fake = FakeTmux([PANEL], alive=False)
    session(fake).read_usage()
    started = next(c for c in fake.calls if c[:2] == ["tmux", "new-session"])
    assert started[-3:] == ["/bin/agy", "--mode", "plan"]


def test_read_usage_types_the_command_and_closes_the_panel():
    fake = FakeTmux(["busy", PANEL])
    text = session(fake).read_usage()
    assert "Models & Quota" in text
    sent = [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]
    assert ["tmux", "send-keys", "-t", "s", "-l", "/usage"] in sent
    assert sent[-1][-1] == "Escape"
    assert not any(c[:2] == ["tmux", "new-session"] for c in fake.calls)


def test_no_escape_before_typing():
    fake = FakeTmux([PANEL])
    session(fake).read_usage()
    sent = [c[-1] for c in fake.calls if c[:2] == ["tmux", "send-keys"]]
    assert sent.index("/usage") < sent.index("Escape")


def test_enter_is_never_pressed_when_the_line_is_wrong():
    fake = FakeTmux([PANEL], echo="usage")          # the slash got lost
    with pytest.raises(UsageParseError, match="nothing was sent"):
        session(fake).read_usage()
    assert not fake.entered


def test_waits_for_agy_to_be_ready():
    fake = FakeTmux([PANEL], ready=False)
    with pytest.raises(UsageParseError, match="did not become ready"):
        session(fake).read_usage()
    assert not fake.entered


def test_read_usage_gives_up_after_the_deadline():
    fake = FakeTmux(["still loading"])
    with pytest.raises(UsageParseError, match="did not show"):
        session(fake, wait_seconds=3).read_usage()
