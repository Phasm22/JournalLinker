#!/bin/bash
# Journal Linker — voice recording processor (label: com.journal-linker.voice).
# Scans the VoiceDrop iCloud Drive folder for unprocessed .m4a files,
# transcribes each with faster-whisper, and hands off to Scribe.py.
#
# Triggered by launchd WatchPaths on the VoiceDrop folder, or run manually.
#
# Logs: ~/Library/Logs/JournalLinker/voice-YYYYMMDD-HHMMSS-PID.log
#       voice-latest.log -> that file (symlink)
#
# Env: SCRIBE_JOB_LOG_DIR    — override log directory
#      SCRIBE_VOICEDROP_DIR  — override VoiceDrop watch folder
#      SCRIBE_WHISPER_MODEL  — faster-whisper model (default: base.en)
#      PYTHON / PROCESS_VOICE_PY — override binaries
#
# Stale lock (crash): rmdir "$LOG_DIR/.voice-job.lock"

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=job_log_lib.sh
source "$HERE/job_log_lib.sh"

ROOT="$(cd "$HERE/.." && pwd)"
JOURNAL_LINKER_ROOT="$ROOT"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
PROCESS_VOICE_PY="${PROCESS_VOICE_PY:-$ROOT/scripts/process_voice.py}"

job_log_init voice .voice-job.lock voice

job_log_header "Voice watcher job" \
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
