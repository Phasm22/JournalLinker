#!/bin/bash
# Journal Linker — intent watcher (label: com.journal-linker.intent).
# Triggered by a filesystem watcher (inotifywait / launchd WatchPaths) when
# the journal directory changes, or run manually after editing a note.
#
# Logs: $LOG_DIR/intent-YYYYMMDD-HHMMSS-PID.log
#       intent-latest.log -> that file (symlink)
#
# Env:
#   SCRIBE_JOB_LOG_DIR   override log directory
#   INTENT_NOTE_FILE     if set, process this specific note instead of scan
#   PYTHON               override Python binary
#   PROCESS_INTENTS_PY   override script path
#
# Stale lock: rmdir "$LOG_DIR/.intent-job.lock"

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

LOCK_DIR="$LOG_DIR/.intent-job.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "intent_watcher: skipped — another job is running (lock: $LOCK_DIR). If stuck: rmdir \"$LOCK_DIR\"" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/intent-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/intent-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Intent watcher job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
  echo "python: $PYTHON"
  echo "script: $PROCESS_INTENTS_PY"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)

# Resolve which note to process.
# Priority: explicit INTENT_NOTE_FILE > most recently modified dated note (last 10 min).
# If no fresh note found, exit clean — retries are the timer's job, not the path watcher's.
NOTE_FILE="${INTENT_NOTE_FILE:-}"
if [[ -z "$NOTE_FILE" && -n "${SCRIBE_JOURNAL_DIR:-}" && -d "${SCRIBE_JOURNAL_DIR}" ]]; then
  NOTE_FILE=$(find "${SCRIBE_JOURNAL_DIR}" -maxdepth 1 -name "????-??-??.md" -mmin -10 \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
fi

if [[ -n "$NOTE_FILE" ]]; then
  echo "note: $NOTE_FILE" | tee -a "$LOG_FILE"
  "$PYTHON" "$PROCESS_INTENTS_PY" --file "$NOTE_FILE" 2>&1 | tee -a "$LOG_FILE"
  EXIT="${PIPESTATUS[0]}"
else
  echo "no recently modified note found — exiting (retries handled by timer)" | tee -a "$LOG_FILE"
  EXIT=0
fi
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
