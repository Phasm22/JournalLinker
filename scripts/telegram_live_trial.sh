#!/usr/bin/env bash
# Live Telegram trial — run feedback_sender --daemon until a deadline or duration.
#
# Prereq: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment, e.g.
#   set -a && source ~/.config/journal-linker/journal-linker.env && set +a
#   ./scripts/telegram_live_trial.sh --minutes 90
#
# Optional:
#   TRIAL_SPIKE=1 (default) sets INTENT_TELEGRAM_REACTION_SPIKE=1 for extra spike JSONL.
#       Production emoji→ledger uses INTENT_REACTION_SIGNALS + the built-in/JSON map by default;
#       use INTENT_REACTION_SIGNALS=off for callbacks/replies only.
#   TRIAL_SKIP_CONFLICT_CHECK=1 — skip the exclusive bot check (you will get HTTP 409 if another
#       getUpdates client is still running).
#   Extra args pass through to feedback_sender.py (e.g. --verbose).
#
# Telegram allows only ONE active getUpdates long-poll per bot token. Stop the systemd feedback
# sender first if installed:
#   systemctl --user stop journal-linker-feedback-sender.service
#
# Requires: GNU date (Ubuntu), coreutils timeout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SPIKE="${TRIAL_SPIKE:-1}"
MINUTES=""
UNTIL=""
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes|-m) MINUTES="${2:?}"; shift 2 ;;
    --until|-u) UNTIL="${2:?}"; shift 2 ;;
    --help|-h)
      sed -n '1,40p' "$0"
      exit 0
      ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

if [[ -n "$MINUTES" && -n "$UNTIL" ]]; then
  echo "Use only one of --minutes or --until" >&2
  exit 2
fi

if [[ -z "$MINUTES" && -z "$UNTIL" ]]; then
  echo "Specify --minutes N or --until 'YYYY-MM-DD HH:MM:SS' (local time)" >&2
  exit 2
fi

if [[ "$SPIKE" == "1" || "$SPIKE" == "true" || "$SPIKE" == "yes" ]]; then
  export INTENT_TELEGRAM_REACTION_SPIKE=1
  echo "[trial] INTENT_TELEGRAM_REACTION_SPIKE=1 -> intent_feedback_reaction_spike.jsonl"
else
  unset INTENT_TELEGRAM_REACTION_SPIKE || true
  echo "[trial] reaction spike off (TRIAL_SPIKE unset or 0)"
fi

SECS=""
if [[ -n "$MINUTES" ]]; then
  SECS=$(( MINUTES * 60 ))
  echo "[trial] duration ${MINUTES} minutes (ends ~$(date -d "+${MINUTES} minutes" -Iseconds 2>/dev/null || date -Iseconds))"
else
  NOW=$(date +%s)
  END=$(date -d "$UNTIL" +%s)
  SECS=$(( END - NOW ))
  if [[ "$SECS" -le 0 ]]; then
    echo "[trial] --until is not in the future: $UNTIL" >&2
    exit 2
  fi
  echo "[trial] until $UNTIL ($(date -d "$UNTIL" -Iseconds))"
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[trial] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set." >&2
  echo "  Example: set -a && source ~/.config/journal-linker/journal-linker.env && set +a" >&2
  exit 10
fi

# Only one client may call getUpdates per bot; otherwise Telegram returns HTTP 409 Conflict.
trial_check_exclusive_feedback_sender() {
  if [[ "${TRIAL_SKIP_CONFLICT_CHECK:-}" == "1" ]]; then
    echo "[trial] skipping conflict check (TRIAL_SKIP_CONFLICT_CHECK=1)"
    return 0
  fi
  local unit="journal-linker-feedback-sender.service"
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
      echo "[trial] ERROR: ${unit} is active — it is already long-polling this bot (HTTP 409 if you run another)." >&2
      echo "  Run:  systemctl --user stop ${unit}" >&2
      echo "  Then re-run this trial. Override: TRIAL_SKIP_CONFLICT_CHECK=1 ./scripts/telegram_live_trial.sh ..." >&2
      exit 11
    fi
  fi
  if pgrep -f 'feedback_sender\.py' >/dev/null 2>&1; then
    echo "[trial] ERROR: feedback_sender.py already running (PIDs: $(pgrep -f 'feedback_sender\.py' | tr '\n' ' '))." >&2
    echo "  Stop those processes or: systemctl --user stop ${unit}" >&2
    echo "  Override: TRIAL_SKIP_CONFLICT_CHECK=1 ..." >&2
    exit 11
  fi
}

trial_check_exclusive_feedback_sender

STATE_DIR="${INTENT_STATE_DIR:-$HOME/.local/state/journal-linker/intents}"
echo "[trial] INTENT_STATE_DIR=$STATE_DIR"
echo "[trial] starting feedback_sender --daemon for ${SECS}s (Ctrl+C to stop early)"

# shellcheck disable=SC2086
exec timeout "${SECS}s" python3 "$ROOT/scripts/feedback_sender.py" --daemon "${EXTRA[@]}"
