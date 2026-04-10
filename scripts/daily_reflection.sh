#!/bin/bash
# Journal Linker — structured entry for the daily ntfy reflection job.
# Runs as a poller; the Python script decides whether this tick is the send tick.
#
# Logs: ~/Library/Logs/JournalLinker/daily-reflection-YYYYMMDD-HHMMSS-PID.log
#       daily-reflection-latest.log -> that file

set -euo pipefail

if [[ -z "${HOME:-}" ]]; then
  export HOME
  HOME="$(cd ~ && pwd)"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
DAILY_REFLECTION_PY="${DAILY_REFLECTION_PY:-$ROOT/daily_reflection.py}"

LOG_DIR="${SCRIBE_JOB_LOG_DIR:-$HOME/Library/Logs/JournalLinker}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="/tmp/journal-linker-logs"
  mkdir -p "$LOG_DIR"
fi

LOCK_DIR="$LOG_DIR/.daily-reflection.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "daily_reflection: skipped — another job is running (lock: $LOCK_DIR)" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/daily-reflection-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/daily-reflection-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Daily reflection job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
  echo "python: $PYTHON"
  echo "script: $DAILY_REFLECTION_PY"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$DAILY_REFLECTION_PY" 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

{
  echo "end: $(ts)"
  echo "duration_sec: $DURATION"
  echo "exit_code: $EXIT"
  echo "=== done ==="
} | tee -a "$LOG_FILE"

ln -sf "$LOG_FILE" "$LATEST_LINK"
exit "$EXIT"
