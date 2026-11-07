#!/usr/bin/env python3
"""Generate a synthetic demo history file for the dashboard.

This is a *sample* dataset shipped with the repo so newcomers can run
``build_dashboard.py`` and see a populated dashboard before collecting any of
their own data. It is entirely made up: a seeded simulation, not real usage,
and it carries no real account data.

    python3 sample/make_sample.py            # writes sample/2026-05.jsonl

The row format mirrors the Antigravity (Google AI Pro) monitor's history:
the Gemini 5-hour (``session``) and weekly (``weekly``) windows, the
"Claude and GPT models" group (``tp_session`` / ``tp_weekly``) and the
separate Gemini 4 Argon weekly bucket (``argon_weekly``). A window only starts
at its first use (``*_started``); until then its reset time keeps moving.
Antigravity gives no token counts, so ``tokens_7d`` is always null. The data
has evening-weighted bursts, a Claude/GPT saturation episode and one outage.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260504
EXPECTED_SHA256 = "3e396b78e83ac3c55c9434e1b7defb867046e38386df528cc325737e083c6ed6"
START = datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
DAYS = 14
STEP_MIN = 5
SESSION_WIN = timedelta(hours=5)
WEEKLY_WIN = timedelta(days=7)
ACCOUNT = "demo-account"

# Days with heavy Claude/GPT use (day 8 is the engineered saturation episode)
HEAVY_DAYS = {2: 1.3, 3: 1.3, 8: 3.0, 11: 1.3}

# Engineered, clearly-labelled synthetic event:
ERROR_START = START + timedelta(days=9, hours=12, minutes=20)
ERROR_END = START + timedelta(days=9, hours=15, minutes=55)


def diurnal(hour: float) -> float:
    """Activity weight over the day: quiet nights, a late-afternoon/evening peak."""
    evening = math.exp(-((hour - 18) ** 2) / (2 * 3.1 ** 2))
    morning = 0.30 * math.exp(-((hour - 9) ** 2) / (2 * 1.7 ** 2))
    return evening + morning


class Window:
    """A usage window that starts at its first use and resets after `length`."""

    def __init__(self, length: timedelta) -> None:
        self.length = length
        self.start: datetime | None = None
        self.value = 0.0

    def advance(self, now: datetime, used: float) -> None:
        if self.start is not None and now >= self.start + self.length:
            self.start, self.value = None, 0.0
        if used > 0:
            if self.start is None:
                self.start = now
            self.value = min(100.0, self.value + used)

    @property
    def started(self) -> bool:
        return self.start is not None

    def resets_at(self, now: datetime) -> datetime:
        return (self.start or now) + self.length


def status_for(worst: float) -> str:
    if worst >= 100:
        return "exhausted"
    if worst >= 90:
        return "critical"
    if worst >= 75:
        return "warning"
    return "ok"


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    rng = random.Random(SEED)
    out = Path(__file__).with_name("2026-05.jsonl")

    gem_s, gem_w = Window(SESSION_WIN), Window(WEEKLY_WIN)
    tp_s, tp_w = Window(SESSION_WIN), Window(WEEKLY_WIN)
    argon = Window(WEEKLY_WIN)
    lines = []

    for i in range(DAYS * 24 * 60 // STEP_MIN):
        t = START + timedelta(minutes=i * STEP_MIN)
        day = (t - START).days
        weight = diurnal(t.hour + t.minute / 60.0)
        day_factor = 0.55 + 0.4 * ((rng.random() + math.sin(day)) % 1.0)

        # Gemini: busy most evenings
        active = rng.random() < (0.06 + 0.55 * min(weight, 1.0))
        burst = weight * day_factor * (rng.uniform(1.4, 2.9) if active else 0.0)
        gem_s.advance(t, burst * rng.uniform(0.9, 1.4))
        gem_w.advance(t, burst * rng.uniform(0.12, 0.18))

        # Claude/GPT: rarer, heavier bursts, concentrated on a few days
        heavy_day = day in HEAVY_DAYS
        tp_active = rng.random() < (0.02 + (0.30 if heavy_day else 0.05) * min(weight, 1.0))
        tp_burst = (rng.uniform(1.5, 4.0) if tp_active else 0.0) * HEAVY_DAYS.get(day, 1.0)
        tp_s.advance(t, tp_burst)
        tp_w.advance(t, tp_burst * rng.uniform(0.10, 0.16))

        # Argon: occasional use, slow weekly growth
        argon.advance(t, rng.uniform(0.3, 1.1) if rng.random() < 0.012 * min(weight * 3, 1.0) else 0.0)

        if ERROR_START <= t <= ERROR_END:
            row = {
                "t": stamp(t), "quota_status": "error",
                "session": None, "weekly": None, "max": None, "tokens_7d": None,
                "account": ACCOUNT,
            }
        else:
            values = {
                "session": round(gem_s.value, 1), "weekly": round(gem_w.value, 1),
                "tp_session": round(tp_s.value, 1), "tp_weekly": round(tp_w.value, 1),
                "argon_weekly": round(argon.value, 1),
            }
            worst = max(values.values())
            row = {
                "t": stamp(t), "quota_status": status_for(worst),
                "session": values["session"], "weekly": values["weekly"], "max": worst,
                "tokens_7d": None, "account": ACCOUNT,
                "session_resets_at": stamp(gem_s.resets_at(t)),
                "weekly_resets_at": stamp(gem_w.resets_at(t)),
                "session_started": gem_s.started, "weekly_started": gem_w.started,
                "tp_session": values["tp_session"], "tp_weekly": values["tp_weekly"],
                "argon_weekly": values["argon_weekly"],
            }
        lines.append(json.dumps(row))

    payload = ("\n".join(lines) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError(f"synthetic fixture changed unexpectedly: {digest}")
    out.write_bytes(payload)
    print(f"wrote {len(lines)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
