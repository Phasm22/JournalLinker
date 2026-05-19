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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_INTENTS_PY="${PROCESS_INTENTS_PY:-$ROOT/scripts/process_intents.py}"

job_log_init intent-watcher .intent-job.lock intent

job_log_header "Intent watcher job" \
  echo "python: $PYTHON" \
  echo "script: $PROCESS_INTENTS_PY"

set +e
START_EPOCH=$(date +%s)
SKIP_REASON=""

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
  SKIP_REASON=no_recent_note
fi
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION" "$SKIP_REASON"
exit "$EXIT"
