#!/usr/bin/env bash
# Run the analysis pipeline up to a reviewable plan. Never moves anything.
# Extra arguments are passed to every step, e.g.  ./run.sh --source ~/Downloads
# BUDGET=150 ./run.sh   makes long steps stop after 150 s and loop until done.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
BUDGET="${BUDGET:-0}"

step() {
  echo "== $1"
  "$PY" "$DIR/fo/$1.py" "${@:2}"
}

resumable() {  # exit code 3 = budget reached, run again
  local name="$1"; shift
  while true; do
    step "$name" --budget "$BUDGET" "$@"
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 3 ] || exit "$rc"
  done
}

step inventory "$@" || exit $?
step units "$@" || exit $?
resumable hash "$@"
step dupes "$@" || exit $?
step profile "$@" || exit $?
resumable classify "$@"
step rename "$@" || exit $?
step review summary "$@" || exit $?

cat <<EOF

Next:
  $PY $DIR/fo/review.py export --only review   # edit reports/plan.csv, then:
  $PY $DIR/fo/review.py import
  $PY $DIR/fo/apply.py                         # dry run
  $PY $DIR/fo/apply.py --apply                 # execute (undo with fo/undo.py --last --yes)
EOF
