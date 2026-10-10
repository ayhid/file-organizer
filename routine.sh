#!/usr/bin/env bash
# Periodic routine: scan + plan + review export. NEVER moves, deletes or installs anything.
#
#   FO_SOURCE   folder to organise                      (default ~/Downloads)
#   FO_HOME     state, personal rules, reports, logs    (default $FO_SOURCE/.file-organizer)
#   FO_TARGET   organised tree root                     (default ~/Documents/Organized)
#   MAX_SECONDS stop after this many seconds and exit 3 so the caller runs it again (0 = no limit)
#
# Exit codes: 0 plan ready, 3 time budget reached (run again, it resumes), other = error.
# Last line of output is "ROUTINE: DONE" or "ROUTINE: RESUME".
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
FO_SOURCE="${FO_SOURCE:-$HOME/Downloads}"
FO_HOME="${FO_HOME:-$FO_SOURCE/.file-organizer}"
FO_TARGET="${FO_TARGET:-$HOME/Documents/Organized}"
MAX_SECONDS="${MAX_SECONDS:-0}"
START=$SECONDS

mkdir -p "$FO_HOME/reports" "$FO_HOME/logs"
ARGS=(--source "$FO_SOURCE" --target "$FO_TARGET" --state "$FO_HOME/state.db"
      --local-rules "$FO_HOME/rules.local.toml")

remaining() {  # seconds left, minus a 15 s safety margin; 0 means unlimited
  if [ "$MAX_SECONDS" -le 0 ]; then echo 0; return; fi
  local left=$(( MAX_SECONDS - (SECONDS - START) - 15 ))
  [ "$left" -lt 1 ] && left=1
  echo "$left"
}

out_of_time() {
  [ "$MAX_SECONDS" -gt 0 ] && [ $(( SECONDS - START )) -ge $(( MAX_SECONDS - 15 )) ]
}

resume() { echo "ROUTINE: RESUME"; exit 3; }

step() {
  out_of_time && resume
  echo "== $1"
  "$PY" "$DIR/fo/$1.py" "${ARGS[@]}" "${@:2}"
  local rc=$?
  [ "$rc" -eq 0 ] || { echo "ROUTINE: ERROR in $1 (exit $rc)"; exit "$rc"; }
}

resumable() {
  while true; do
    out_of_time && resume
    echo "== $1"
    "$PY" "$DIR/fo/$1.py" "${ARGS[@]}" --budget "$(remaining)"
    local rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 3 ] || { echo "ROUTINE: ERROR in $1 (exit $rc)"; exit "$rc"; }
  done
}

step inventory
step units
resumable hash
step dupes
step profile
resumable classify
step rename
step review summary
step review export --only review --file "$FO_HOME/reports/review.csv"
step review export --only all --file "$FO_HOME/reports/plan.csv"
echo "ROUTINE: DONE"
