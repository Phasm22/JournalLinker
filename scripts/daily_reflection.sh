#!/bin/bash
# Journal Linker — structured entry for the daily ntfy reflection job.
# Runs as a poller; the Python script decides whether this tick is the send tick.
#
# Logs: ~/Library/Logs/JournalLinker/daily-reflection-YYYYMMDD-HHMMSS-PID.log
#       daily-reflection-latest.log -> that file

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
DAILY_REFLECTION_PY="${DAILY_REFLECTION_PY:-$ROOT/daily_reflection.py}"

job_log_init daily-reflection .daily-reflection.lock daily-reflection

job_log_header "Daily reflection job" \
  echo "python: $PYTHON" \
  echo "script: $DAILY_REFLECTION_PY"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$DAILY_REFLECTION_PY" 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
