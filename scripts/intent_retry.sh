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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_INTENTS_PY="${PROCESS_INTENTS_PY:-$ROOT/scripts/process_intents.py}"

job_log_init intent-retry .intent-job.lock intent-retry

job_log_header "Intent retry job" \
  echo "python: $PYTHON" \
  echo "script: $PROCESS_INTENTS_PY"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$PROCESS_INTENTS_PY" --retry 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
