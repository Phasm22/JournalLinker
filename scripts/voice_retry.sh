#!/bin/bash
# Journal Linker — timed voice retry (label: com.journal-linker.voice-retry).
# Runs every 15 minutes via launchd StartInterval.
# Re-processes .m4a files whose .failed marker says "kind: transient".
# Permanent failures (empty transcript, corrupt audio) are left alone.
#
# Logs: ~/Library/Logs/JournalLinker/voice-retry-YYYYMMDD-HHMMSS-PID.log
#       voice-retry-latest.log -> that file (symlink)

set -euo pipefail

if [[ -z "${HOME:-}" ]]; then
  export HOME
  HOME="$(cd ~ && pwd)"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_VOICE_PY="${PROCESS_VOICE_PY:-$ROOT/scripts/process_voice.py}"

LOG_DIR="${SCRIBE_JOB_LOG_DIR:-$HOME/Library/Logs/JournalLinker}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="/tmp/journal-linker-logs"
  mkdir -p "$LOG_DIR"
fi

# Share lock with voice_watcher so transcription jobs don't overlap.
LOCK_DIR="$LOG_DIR/.voice-job.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "voice_retry: skipped — voice job is already running (lock: $LOCK_DIR)" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/voice-retry-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/voice-retry-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Voice retry job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$PROCESS_VOICE_PY" --retry 2>&1 | tee -a "$LOG_FILE"
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
