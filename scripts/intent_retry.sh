#!/bin/bash
# Journal Linker — intent retry (label: com.journal-linker.intent-retry).
# Runs on a schedule (e.g. every 15 minutes via systemd timer or launchd StartInterval).
# Replays transient-failed intent jobs using the persisted retry queue.
# Permanent failures are never re-attempted automatically.
#
# Logs: $LOG_DIR/intent-retry-YYYYMMDD-HHMMSS-PID.log
#       intent-retry-latest.log -> that file (symlink)
#
# Shares the same lock as intent_watcher.sh so runs don't overlap.

set -euo pipefail

if [[ -z "${HOME:-}" ]]; then
  export HOME
  HOME="$(cd ~ && pwd)"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_INTENTS_PY="${PROCESS_INTENTS_PY:-$ROOT/scripts/process_intents.py}"

LOG_DIR="${SCRIBE_JOB_LOG_DIR:-$HOME/.local/state/journal-linker/logs}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="/tmp/journal-linker-logs"
  mkdir -p "$LOG_DIR"
fi

# Share lock with intent_watcher so jobs don't overlap.
LOCK_DIR="$LOG_DIR/.intent-job.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "intent_retry: skipped — intent job is already running (lock: $LOCK_DIR)" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/intent-retry-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/intent-retry-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Intent retry job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$PROCESS_INTENTS_PY" --retry 2>&1 | tee -a "$LOG_FILE"
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
