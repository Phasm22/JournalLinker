#!/bin/bash
# Journal Linker — structured entry for launchd (label: com.journal-linker.scribe).
# Timestamped logs; not a replacement for Shortcuts notifications.
#
# Logs: ~/Library/Logs/JournalLinker/scribe-YYYYMMDD-HHMMSS-PID.log
#       scribe-latest.log -> that file (symlink).
# PID + a mkdir lock prevent two runs from interleaving into one log.
#
# Env: SCRIBE_JOB_LOG_DIR to override log directory.
#      PYTHON / SCRIBE_PY to override binaries (defaults: repo venv + Scribe.py).
# Stale lock (crash): rmdir "$LOG_DIR/.scribe-job.lock"

set -euo pipefail

# launchd often omits HOME; without it, $HOME/Library/... breaks and no logs appear.
if [[ -z "${HOME:-}" ]]; then
  export HOME
  HOME="$(cd ~ && pwd)"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
SCRIBE_PY="${SCRIBE_PY:-$ROOT/Scribe.py}"

LOG_DIR="${SCRIBE_JOB_LOG_DIR:-$HOME/Library/Logs/JournalLinker}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="/tmp/journal-linker-logs"
  mkdir -p "$LOG_DIR"
fi

LOCK_DIR="$LOG_DIR/.scribe-job.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "scribe scheduled_run: skipped — another job is running (lock: $LOCK_DIR). If stuck: rmdir \"$LOCK_DIR\"" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/scribe-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/scribe-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Scribe job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
  echo "python: $PYTHON"
  echo "script: $SCRIBE_PY"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$SCRIBE_PY" 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

{
  echo "end: $(ts)"
  echo "duration_sec: $DURATION"
  echo "exit_code: $EXIT"
  echo "log_file: $LOG_FILE"
  echo "latest_symlink: $LATEST_LINK -> $(basename "$LOG_FILE")"
  echo "=== done ==="
} | tee -a "$LOG_FILE"

ln -sf "$LOG_FILE" "$LATEST_LINK"

exit "$EXIT"
