"""GO / CAUTION / STOP verdicts from a status reading. Pure: no I/O, no clock."""

from __future__ import annotations

from datetime import datetime, timedelta

PROFILES = {
    # Default: spend freely but stop before the window actually runs out.
    "balanced": {
        "SESSION_CAUTION": 70, "SESSION_STOP": 88, "WEEKLY_STOP": 92,
        "BURN_CAUTION": 1.4, "BURN_STOP": 2.0,
        "BURN_CAUTION_FLOOR": 10, "BURN_STOP_FLOOR": 35,
    },
    # "Use the whole allowance": only pull back at the very edge.
    "greedy": {
        "SESSION_CAUTION": 85, "SESSION_STOP": 96, "WEEKLY_STOP": 97,
        "BURN_CAUTION": 2.5, "BURN_STOP": 4.0,
        "BURN_CAUTION_FLOOR": 30, "BURN_STOP_FLOOR": 70,
    },
    # "Leave me half the week": protect a reserve, warn and stop early.
    "conserve": {
        "SESSION_CAUTION": 50, "SESSION_STOP": 70, "WEEKLY_STOP": 50,
        "BURN_CAUTION": 1.1, "BURN_STOP": 1.4,
        "BURN_CAUTION_FLOOR": 8, "BURN_STOP_FLOOR": 20,
    },
}

GROUPS = ("gemini", "3p", "argon")
WEEKLY_WINDOW = timedelta(days=7)
# Burn rate is noise in the first hours of a week; ~5 hours in it means something.
MIN_ELAPSED_FRACTION = 0.03
_ORDER = {"GO": 0, "CAUTION": 1, "STOP": 2}


def profile_name(environ):
    name = environ.get("BUDGET_PROFILE", "balanced").strip().lower()
    return name if name in PROFILES else "balanced"


def thresholds(environ):
    """Env override (BUDGET_<NAME>) > named profile > balanced default."""
    profile = PROFILES[profile_name(environ)]

    def pick(name):
        raw = environ.get(f"BUDGET_{name}")
        try:
            return float(raw) if raw is not None else float(profile[name])
        except ValueError:
            return float(profile[name])
    return {name: pick(name) for name in PROFILES["balanced"]}


def _parse(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def group_figures(status, group):
    rows = [r for r in status.get("limits") or [] if r.get("group") == group]
    session = next((r for r in rows if r.get("kind") == "session"), {})
    weekly = next((r for r in rows if r.get("kind") in ("weekly_all", "weekly_model")), {})
    return {
        "has_session": bool(session),
        "has_weekly": bool(weekly),
        "session": session.get("percent_used"),
        "session_resets_at": session.get("resets_at"),
        "weekly": weekly.get("percent_used"),
        "weekly_resets_at": weekly.get("resets_at"),
        "weekly_started": weekly.get("window_started"),
    }


def _expired(raw, now):
    end = _parse(raw) if raw else None
    return end is not None and end <= now


def _minutes_until(raw, now):
    end = _parse(raw) if raw else None
    return None if end is None else max(0, round((end - now).total_seconds() / 60))


def _burn(fig, now):
    weekly, end = fig["weekly"], _parse(fig["weekly_resets_at"]) if fig["weekly_resets_at"] else None
    if not isinstance(weekly, (int, float)) or end is None or fig["weekly_started"] is False:
        return None, None
    fraction = min(max((now - (end - WEEKLY_WINDOW)) / WEEKLY_WINDOW, 0.0), 1.0)
    if fraction < MIN_ELAPSED_FRACTION:
        return None, None
    return round(weekly / (fraction * 100), 2), round(weekly / fraction)


def _unknown(reason):
    return {"verdict": "UNKNOWN", "reasons": [reason], "session_percent_used": None,
            "weekly_percent_used": None, "weekly_burn_rate": None}


def evaluate(status, group, th, now, source="live", age=None):
    if status.get("quota_status") == "error" or status.get("quota_error_detail"):
        return _unknown(f"quota unreadable: {status.get('quota_error_detail') or 'error'}")
    fig = group_figures(status, group)
    if fig["session"] is None and fig["weekly"] is None:
        return _unknown(f"no figures for group '{group}'")

    session_stale = source == "cached" and _expired(fig["session_resets_at"], now)
    weekly_stale = source == "cached" and _expired(fig["weekly_resets_at"], now)
    session = None if session_stale else fig["session"]
    weekly = None if weekly_stale else fig["weekly"]
    burn, projected = _burn(fig, now) if not weekly_stale else (None, None)

    verdict, reasons = "GO", []

    def escalate(level, reason):
        nonlocal verdict
        verdict = level if _ORDER[level] > _ORDER[verdict] else verdict
        reasons.append(reason)

    if isinstance(session, (int, float)):
        if session >= th["SESSION_STOP"]:
            escalate("STOP", f"session window {session}% used")
        elif session >= th["SESSION_CAUTION"]:
            escalate("CAUTION", f"session window {session}% used")
    if isinstance(weekly, (int, float)) and weekly >= th["WEEKLY_STOP"]:
        escalate("STOP", f"weekly window {weekly}% used")
    if burn is not None:
        pace = f"burning {burn}x steady pace, on track for {projected}% by reset"
        if burn >= th["BURN_STOP"] and weekly >= th["BURN_STOP_FLOOR"]:
            escalate("STOP", pace)
        elif burn >= th["BURN_CAUTION"] and weekly >= th["BURN_CAUTION_FLOOR"]:
            escalate("CAUTION", pace)
    if session_stale:
        escalate("CAUTION", f"stale cache ({age}m old): its 5-hour window already reset, "
                            "session figure unusable")
    if weekly_stale:
        escalate("CAUTION", f"stale cache ({age}m old): its weekly window already reset, "
                            "weekly figure unusable")
    # A window that exists but could not be read is never a reason for GO.
    if fig["has_session"] and fig["session"] is None and not session_stale:
        escalate("CAUTION", "session figure unknown")
    if fig["has_weekly"] and fig["weekly"] is None and not weekly_stale:
        escalate("CAUTION", "weekly figure unknown")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "session_percent_used": session,
        "session_figure_stale": session_stale,
        "weekly_figure_stale": weekly_stale,
        "weekly_percent_used": weekly,
        "weekly_burn_rate": burn,
        "weekly_projected_at_reset": projected,
        "session_resets_in_minutes": _minutes_until(fig["session_resets_at"], now),
        "weekly_resets_in_minutes": _minutes_until(fig["weekly_resets_at"], now),
    }


def brief_line(result):
    bits = [result["verdict"]]
    if result.get("profile") and result["profile"] != "balanced":
        bits.append(f"[{result['profile']}]")
    if result.get("source") == "cached":
        age = result.get("data_age_minutes")
        bits.append(f"CACHED{f' {age}m old' if age is not None else ''}")
    elif result.get("source") == "live":
        bits.append("live")
    if result.get("account"):
        bits.append(f"acct {result['account']}")
    if result.get("group"):
        bits.append(result["group"])
    if result.get("session_percent_used") is not None:
        bits.append(f"session {result['session_percent_used']}%")
    elif result.get("session_figure_stale"):
        bits.append("session unknown (stale)")
    if result.get("weekly_percent_used") is not None:
        bits.append(f"weekly {result['weekly_percent_used']}%")
    if result.get("weekly_burn_rate") is not None:
        bits.append(f"burn {result['weekly_burn_rate']}x")
    if result.get("reasons"):
        bits.append("(" + "; ".join(result["reasons"]) + ")")
    return " | ".join(bits)
