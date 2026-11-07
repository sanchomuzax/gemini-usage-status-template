# gemini-usage-status

<!-- TEMPLATE_BANNER_START -->
> **This is the shareable template.** Every measurement in `status.json`,
> `history/` and `sample/` is deterministic dummy data, never a real account's
> usage. Create a **private** repository from this template for your own
> monitor, because its future history will contain your usage figures.
<!-- TEMPLATE_BANNER_END -->

Automated monitor for the **Google AI Pro quota in Antigravity** (the `agy` CLI,
the Antigravity app and SDK share it). A cron job runs every 5 minutes,
refreshes [`status.json`](status.json), and publishes it when the reading
changes meaningfully. It is the third sibling of
[claude-usage-status](https://github.com/sanchomuzax/claude-usage-status) and
[codex-usage-status](https://github.com/sanchomuzax/codex-usage-status), and
writes the same `status.json` / history contract.

## Why Antigravity and not Gemini CLI

Since 2026-06-18 Google no longer serves free, AI Pro or AI Ultra logins in the
open-source Gemini CLI; those accounts moved to the Antigravity CLI. An old
Gemini CLI login still refreshes, but the server answers `UNSUPPORTED_CLIENT`
and every quota call returns 403. This monitor therefore reads the quota through
the Antigravity CLI's login.

The Gemini **API key** (AI Studio, `generativelanguage.googleapis.com`) is a
different allowance entirely and is not measured here.

## Quick start

You need an always-on machine with the Antigravity CLI installed and signed in:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash   # installs ~/.local/bin/agy
agy            # first run prints a sign-in URL; paste the code back
```

When starting from the public template, click **Use this template, Create a new
repository**, and make the new repository **private**: its history will hold
your own usage figures. Clone it onto the always-on machine, then check the
monitor and schedule it:

```sh
./gemini-usage-check.sh
cat status.json
crontab -e
*/5 * * * * ~/Documents/gemini-usage-status/gemini-usage-check.sh >/dev/null 2>&1
```

Needs `tmux`. If `quota_status` is `error`, read `quota_error_detail`; to see
what agy shows, `tmux attach -t gemini-usage-agy` (detach with Ctrl+B, D).

## What it does

1. **Session.** A long-running `agy` lives in a detached tmux session
   (`gemini-usage-agy`), started on demand in plan mode. The Antigravity CLI keeps
   its own login fresh there, the way the Codex monitor leans on
   `codex app-server`. You never have to use `agy` yourself.
2. **Quota.** Each reading types `/usage` into that session, captures the
   "Models & Quota" panel, and closes it again: every model group's 5-hour and
   weekly window with the remaining percentage and the time until it refreshes.
   No model turn is started, so a reading costs no quota. Enter is pressed only
   when the input line reads exactly `/usage`; otherwise nothing is sent.
3. **Publish.** `record_status.py` writes `status.json`, appends to
   `history/YYYY-MM.jsonl`, and the runner commits and pushes on a meaningful
   change (1 point of movement, a status change, or a 6-hour heartbeat measured
   from the last published reading).

### Measured quirks (2026-11-07)

- **`agy` does not write a renewed token back to its file** outside an
  interactive start, so reading the token file and calling the quota API
  directly stops working an hour after sign-in. Reading `/usage` from a running
  `agy` has no such limit.
- **A window starts at its first use.** Before that, `resetTime` is simply
  "now + window length" and moves with every reading. `window_started: false`
  marks that state; it is not a reset.
- **`currentTier` says `free-tier` even on AI Pro.** The subscription is in
  `paidTier`.
- **Replicas disagree.** Twelve calls in one minute returned 1.1%, 2.0% and
  1.2% for the same 5-hour window, and one call an hour after a request still
  showed nothing used. Within a window usage only grows, so each reading takes
  the most-used of three samples two seconds apart, and a window never falls
  below its previous reading for the same reset time (`percent_held: true`).

### One assumption (not yet observed)

The response looks like protojson (an untouched bucket reports `1`, not `1.0`),
and protojson leaves zero-valued fields out. An exhausted bucket would then
arrive **without** `remainingFraction`. The monitor reads a missing key as 100%
used and marks the row `percent_inferred: true`; an explicit `null` or a value
outside 0..1 stays unknown. If a headline window cannot be read at all,
`quota_status` is `unknown` and `budget_check.py` never answers GO.

## What you get

| field | meaning |
|---|---|
| `session_percent_used` | Gemini group, 5-hour window |
| `weekly_percent_used` | Gemini group, 7-day window |
| `max_percent_used` | the worst of every bucket, all groups |
| `limits[]` | every bucket: `gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly` (Claude and GPT inside Antigravity), `gemini-argon-wtu-weekly` (separate Gemini 4 Argon cap) |
| `limits[].window_started` | false while the window has not begun counting |
| `unknown_buckets` | bucket ids Google added that this monitor does not know yet; they are still listed in `limits[]` |
| `subscription_type` | `paidTier.id`, e.g. `g1-pro-tier` |
| `quota_status` | `ok` <75%, `warning` >=75%, `critical` >=90%, `exhausted` 100%, `error`, `unknown` |
| `tokens_7d` | always `null`: Antigravity reports no token counts |

Percentages carry one decimal: a single small request moved the 5-hour window
by about one point.

## Spending strategy (`budget_check.py`)

Same verdicts and profiles as the siblings (`balanced`, `greedy`, `conserve`,
per-threshold `BUDGET_*` overrides). Pick the model group with `--group`:

```sh
python3 budget_check.py --brief                 # Gemini Flash / Pro
python3 budget_check.py --brief --group 3p      # Claude and GPT in Antigravity
python3 budget_check.py --brief --group argon   # Gemini 4 Argon weekly cap
```

See [ORCHESTRATOR_PROMPT.md](ORCHESTRATOR_PROMPT.md) for the agent-side rules.

## Risks

The quota endpoint is undocumented and the monitor presents itself as the
Antigravity CLI. It only reads (about 288 calls a day, no model calls), but
Google may change the host, the User-Agent rule or the response shape at any
time. When that happens the reading becomes `error` or `unknown`, never a
wrong number, and the downstream dashboards show the gap.

## Tests

```sh
python3 -m pytest -q
```
