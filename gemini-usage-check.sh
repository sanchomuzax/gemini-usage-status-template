#!/usr/bin/env bash
#
# Google AI Pro (Antigravity) usage monitor: reads the real quota through the
# Antigravity CLI's own login, writes status.json and history, and pushes the
# reading to GitHub when it changed meaningfully.
#
# Designed to be run unattended from cron. It never exits non-zero on failure:
# a failure IS the signal, and gets recorded in status.json.
#
# Usage: gemini-usage-check.sh

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${REPO_DIR}/run.log"
LOCK_FILE="${REPO_DIR}/.run.lock"

# cron gives a minimal PATH; agy lives in ~/.local/bin by default.
export PATH="${HOME}/.local/bin:${HOME}/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG_FILE}"
}

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || { log "no Python interpreter (${PYTHON_BIN})"; exit 0; }

# A token renewal through agy can take a while; never stack runs on top of it.
if command -v flock >/dev/null 2>&1; then
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    log "skipped: the previous run is still going"
    exit 0
  fi
else
  log "warning: flock not installed, running without the overlap guard"
fi

regenerate_dashboard() {
  local month history_file builder
  builder="${REPO_DIR}/build_dashboard.py"
  [[ -f "${builder}" ]] || return 0
  month="$(date -u +%Y-%m)"
  history_file="${REPO_DIR}/history/${month}.jsonl"
  [[ -f "${history_file}" ]] || return 0
  if "${PYTHON_BIN}" "${builder}" "${history_file}" \
       -o "${REPO_DIR}/history/${month}.html" >>"${LOG_FILE}" 2>&1; then
    log "dashboard regenerated (history/${month}.html)"
  else
    log "dashboard regeneration failed (see above)"
  fi
}

# --- 1. read the quota (bounded: a hung call must not pile up runs) -----------
# The collector's own timeouts add up to well under this, so it normally ends
# with a specific error first; this is the last-resort guard.
limits_json="$(timeout 240 "${PYTHON_BIN}" "${REPO_DIR}/fetch_limits.py" 2>>"${LOG_FILE}")"

# --- 2. write status.json + history, decide whether to publish ----------------
decision="$(printf '%s' "${limits_json}" | "${PYTHON_BIN}" "${REPO_DIR}/record_status.py" 2>>"${LOG_FILE}")"
decision_line="$(printf '%s' "${decision}" | tail -n 1)"

# --- 3. commit and push ---------------------------------------------------------
cd "${REPO_DIR}" || { log "cannot cd to ${REPO_DIR}"; exit 0; }

if [[ "${decision_line}" != COMMIT* ]]; then
  log "local update only (${decision_line:-no decision})"
else
  reason="${decision_line#COMMIT }"
  regenerate_dashboard
  month_dashboard="history/$(date -u +%Y-%m).html"

  summary="$("${PYTHON_BIN}" -c "
import json
d = json.load(open('status.json'))
label = lambda v: f'{v:g}%' if isinstance(v, (int, float)) else 'unavailable'
print(f\"{d['quota_status']} session={label(d.get('session_percent_used'))} weekly={label(d.get('weekly_percent_used'))}\")
" 2>/dev/null || echo "update")"

  # Only ever the data this run produced: a pathspec-limited add and commit, so
  # nothing a human happened to stage is swept into an automated data commit.
  publish_paths=(status.json history)
  [[ -f "${month_dashboard}" ]] && publish_paths+=("${month_dashboard}")
  git add -- "${publish_paths[@]}" 2>/dev/null
  if [[ -z "$(git diff --cached --name-only -- "${publish_paths[@]}")" ]]; then
    log "nothing staged despite decision: ${reason}"
  # [skip ci]: data, not code. GitHub bills every job as a whole minute, and
  # this script may push every five minutes.
  elif git commit -q -m "chore: usage status $(date -u +%Y-%m-%dT%H:%MZ) (${summary}) [skip ci]" \
         -m "${reason}" -- "${publish_paths[@]}"; then
    # The next readings are compared against this one, not the local file.
    "${PYTHON_BIN}" "${REPO_DIR}/record_status.py" --published 2>>"${LOG_FILE}" \
      || log "could not record the published reading"
    log "committed: ${summary} [${reason}]"
  else
    log "commit failed"
  fi
fi

# --- 4. push whatever is not on the remote yet ----------------------------------
# Separate from the commit decision: a push that failed earlier must go out on
# the next run, not wait for the next meaningful change.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  ahead="$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
else
  ahead="unknown"   # no upstream yet: the first push sets it
fi
if [[ "${ahead}" != "0" ]]; then
  if git push -q -u origin HEAD 2>>"${LOG_FILE}"; then
    log "pushed (${ahead} commit(s) ahead)"
  else
    log "push failed (${ahead} commit(s) ahead); will retry next run"
  fi
fi

# Keep the log from growing without bound.
if [[ -f "${LOG_FILE}" ]] && [[ "$(wc -l <"${LOG_FILE}")" -gt 2000 ]]; then
  tail -n 500 "${LOG_FILE}" >"${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "${LOG_FILE}"
fi

exit 0
