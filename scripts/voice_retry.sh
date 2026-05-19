#!/bin/bash
# Journal Linker — timed voice retry (label: com.journal-linker.voice-retry).
# Runs every 15 minutes via launchd StartInterval.
# Re-processes .m4a files whose .failed marker says "kind: transient".
# Permanent failures (empty transcript, corrupt audio) are left alone.
#
# Logs: ~/Library/Logs/JournalLinker/voice-retry-YYYYMMDD-HHMMSS-PID.log
#       voice-retry-latest.log -> that file (symlink)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_VOICE_PY="${PROCESS_VOICE_PY:-$ROOT/scripts/process_voice.py}"

job_log_init voice-retry .voice-job.lock voice-retry

job_log_header "Voice retry job" \
  echo "python: $PYTHON" \
  echo "script: $PROCESS_VOICE_PY"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$PROCESS_VOICE_PY" --retry 2>&1 | tee -a "$LOG_FILE"
EXIT="${PIPESTATUS[0]}"
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
set -e

job_log_footer "$EXIT" "$DURATION"
exit "$EXIT"
