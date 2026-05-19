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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
SCRIBE_PY="${SCRIBE_PY:-$ROOT/Scribe.py}"

job_log_init scribe .scribe-job.lock scribe

job_log_header "Scribe job" \
  echo "python: $PYTHON" \
  echo "script: $SCRIBE_PY"

set +e
START_EPOCH=$(date +%s)
echo "" | "$PYTHON" "$SCRIBE_PY" --write-back 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
