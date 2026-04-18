#!/bin/bash
# Journal Linker — feedback sender (label: com.journal-linker.feedback).
# Intended as a persistent systemd service using Telegram long polling.
# Polls Telegram for callback responses and sends due check-in messages.
#
# Logs: $LOG_DIR/feedback-sender-YYYYMMDD-HHMMSS-PID.log
#       feedback-sender-latest.log -> that file (symlink)
#
# Env:
#   SCRIBE_JOB_LOG_DIR   override log directory
#   PYTHON               override Python binary
#   FEEDBACK_SENDER_PY   override script path

set -euo pipefail

if [[ -z "${HOME:-}" ]]; then
  export HOME
  HOME="$(cd ~ && pwd)"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
FEEDBACK_SENDER_PY="${FEEDBACK_SENDER_PY:-$ROOT/scripts/feedback_sender.py}"

LOG_DIR="${SCRIBE_JOB_LOG_DIR:-$HOME/.local/state/journal-linker/logs}"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="/tmp/journal-linker-logs"
  mkdir -p "$LOG_DIR"
fi

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/feedback-sender-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/feedback-sender-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Feedback sender job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
  echo "python: $PYTHON"
  echo "script: $FEEDBACK_SENDER_PY"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$FEEDBACK_SENDER_PY" "$@" 2>&1 | tee -a "$LOG_FILE"
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
