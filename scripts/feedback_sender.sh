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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
FEEDBACK_SENDER_PY="${FEEDBACK_SENDER_PY:-$ROOT/scripts/feedback_sender.py}"

job_log_init feedback-sender .feedback-sender.lock feedback-sender

job_log_header "Feedback sender job" \
  echo "python: $PYTHON" \
  echo "script: $FEEDBACK_SENDER_PY"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$FEEDBACK_SENDER_PY" "$@" 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
