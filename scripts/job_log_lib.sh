# shellcheck shell=bash
# Shared logging helpers for journalLinker supervised job wrappers.
# Source from wrapper scripts: source "$(dirname "${BASH_SOURCE[0]}")/job_log_lib.sh"

job_log_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

job_log_resolve_python() {
  local root="${JOURNAL_LINKER_ROOT:-}"
  if [[ -z "$root" ]]; then
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
    root="$(cd "$here/.." && pwd)"
  fi
  JOURNAL_LINKER_ROOT="$root"
  export PYTHONPATH="${JOURNAL_LINKER_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
  JOB_LOG_PYTHON="${PYTHON:-$root/ScribeVenv/bin/python3}"
  if [[ ! -x "$JOB_LOG_PYTHON" ]]; then
    JOB_LOG_PYTHON="${PYTHON:-python3}"
  fi
}

job_log_init() {
  local service="$1"
  local lock_name="$2"
  local log_basename="$3"

  job_log_resolve_python
  JOURNAL_LINKER_SERVICE="$service"

  if [[ -z "${HOME:-}" ]]; then
    export HOME
    HOME="$(cd ~ && pwd)"
  fi

  LOG_DIR="${SCRIBE_JOB_LOG_DIR:-}"
  if [[ -z "$LOG_DIR" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      LOG_DIR="$HOME/Library/Logs/JournalLinker"
    else
      # Linux systemd default (flat dir). Do not use .../logs — that tree is stale on some hosts.
      LOG_DIR="$HOME/.local/state/journal-linker"
    fi
  fi
  if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
    LOG_DIR="/tmp/journal-linker-logs"
    mkdir -p "$LOG_DIR"
  fi

  LOCK_DIR="$LOG_DIR/$lock_name"
  RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
  LOG_FILE="$LOG_DIR/${log_basename}-$RUN_ID.log"
  LATEST_LINK="$LOG_DIR/${log_basename}-latest.log"
  JOURNAL_LINKER_JOB_PAYLOAD_FILE="$LOG_DIR/.payload-${log_basename}-$RUN_ID.json"
  export JOURNAL_LINKER_SERVICE JOURNAL_LINKER_JOB_PAYLOAD_FILE

  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "${service}: skipped — another job is running (lock: $LOCK_DIR). If stuck: rmdir \"$LOCK_DIR\"" >&2
    job_log_lock_skip
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM HUP
}

job_log_header() {
  local title="$1"
  shift
  {
    echo "=== $title $RUN_ID ==="
    echo "log_file: $LOG_FILE"
    echo "start: $(job_log_ts)"
    "$@"
  } | tee "$LOG_FILE"
}

job_log_emit_event() {
  if [[ -z "${JOB_LOG_PYTHON:-}" ]]; then
    job_log_resolve_python
  fi
  local exit_code="$1"
  local duration="$2"
  local skipped="${3:-}"
  local skip_reason="${4:-}"
  local -a args=(
    finalize
    --service "$JOURNAL_LINKER_SERVICE"
    --run-id "$RUN_ID"
    --exit-code "$exit_code"
    --duration-sec "$duration"
    --payload-file "${JOURNAL_LINKER_JOB_PAYLOAD_FILE:-}"
  )
  if [[ -n "$skipped" ]]; then
    args+=(--skipped)
    if [[ -n "$skip_reason" ]]; then
      args+=(--skip-reason "$skip_reason")
    fi
  fi
  (cd "$JOURNAL_LINKER_ROOT" && "$JOB_LOG_PYTHON" -m journal_linker_telemetry "${args[@]}") 2>&1 | tee -a "${LOG_FILE:-/dev/stderr}"
}

job_log_lock_skip() {
  job_log_emit_event 0 0 1 lock_held
}

job_log_footer() {
  local exit_code="${1:-0}"
  local duration="${2:-0}"
  local extra_skip_reason="${3:-}"
  if [[ -n "$extra_skip_reason" ]]; then
    job_log_emit_event "$exit_code" "$duration" 1 "$extra_skip_reason"
  else
    job_log_emit_event "$exit_code" "$duration"
  fi
  {
    echo "end: $(job_log_ts)"
    echo "duration_sec: $duration"
    echo "exit_code: $exit_code"
    echo "log_file: $LOG_FILE"
    if [[ -n "${LATEST_LINK:-}" ]]; then
      echo "latest_symlink: $LATEST_LINK -> $(basename "$LOG_FILE")"
    fi
    echo "=== done ==="
  } | tee -a "$LOG_FILE"
  ln -sf "$LOG_FILE" "$LATEST_LINK"
}
