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

# launchd often omits HOME; without it ~/Library/… paths break.
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

LOCK_DIR="$LOG_DIR/.voice-job.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "voice_watcher: skipped — another job is running (lock: $LOCK_DIR). If stuck: rmdir \"$LOCK_DIR\"" >&2
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/voice-$RUN_ID.log"
LATEST_LINK="$LOG_DIR/voice-latest.log"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

{
  echo "=== Voice watcher job $RUN_ID ==="
  echo "log_file: $LOG_FILE"
  echo "start: $(ts)"
  echo "python: $PYTHON"
  echo "script: $PROCESS_VOICE_PY"
} | tee "$LOG_FILE"

set +e
START_EPOCH=$(date +%s)
"$PYTHON" "$PROCESS_VOICE_PY" 2>&1 | tee -a "$LOG_FILE"
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
