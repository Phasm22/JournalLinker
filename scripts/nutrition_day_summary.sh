#!/bin/bash
# Journal Linker — end-of-day nutrition Pushover.
# Reinforces the previous closed journal day, then sends today's intake list.
#
# Logs: ~/.local/state/journal-linker/nutrition-summary-YYYYMMDD-HHMMSS-PID.log
#       nutrition-summary-latest.log -> that file

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
NUTRITION_PY="${NUTRITION_PY:-$ROOT/scripts/nutrition_ledger.py}"

job_log_init nutrition-summary .nutrition-summary.lock nutrition-summary

job_log_header "Nutrition day summary" \
  echo "python: $PYTHON" \
  echo "script: $NUTRITION_PY"

# shellcheck source=ensure_ollama.sh
source "$HERE/ensure_ollama.sh" || true

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$NUTRITION_PY" --summary 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
