# Journal Linker — local Obsidian journal wikilink helper (Scribe + weekly insights).
# Install the runner: brew install just   →   https://github.com/casey/just
#
# First-time: configure env vars (recommended: ~/.config/journal-linker/journal-linker.env) — see README.

set shell := ["/bin/bash", "-cu"]

root := justfile_directory()
py := root + "/ScribeVenv/bin/python3"
scribe := root + "/Scribe.py"

# List recipes (running `just` with no name does this)
default:
    @just --list

# Run Scribe with the project venv. Flags after `--`, e.g. `just scribe -- --active-date=2026-03-22`
scribe *ARGS:
    "{{py}}" "{{scribe}}" {{ARGS}}

# macOS: clipboard → Scribe → stdout
scribe-paste *ARGS:
    @pbpaste | "{{py}}" "{{scribe}}" {{ARGS}}

# Read today's note from disk, insert wikilinks, write back in-place
scribe-writeback *ARGS:
    @echo "" | "{{py}}" "{{scribe}}" --write-back {{ARGS}}

# Same as the launchd job: logs in ~/Library/Logs/JournalLinker/
scribe-job:
    "{{root}}/scripts/scheduled_run.sh"

# Set SCRIBE_JOURNAL_DIR in the LaunchAgent plist (safe for paths with apostrophes)
launchagent-journal p:
    "{{root}}/scripts/patch_launchagent_journal.sh" "{{p}}"

# Weekly insights note (uses the same env bootstrap as Scribe)
weekly:
    "{{py}}" "{{root}}/weekly_insights.py"

# Dry-run the daily Pushover reflection without sending
daily-reflection *ARGS:
    "{{py}}" "{{root}}/daily_reflection.py" --dry-run {{ARGS}}

# Real daily Pushover reflection run (same core path as the scheduled job)
daily-reflection-send *ARGS:
    "{{py}}" "{{root}}/daily_reflection.py" {{ARGS}}

# Tests only (Ollama mocked)
test:
    "{{py}}" -m pytest tests/

# Process a single voice recording (test/debug). e.g. `just voice ~/path/to/2026-04-04-1430.m4a`
voice FILE:
    "{{py}}" "{{root}}/scripts/process_voice.py" "{{FILE}}"

# Dry-run a single voice recording: transcribe and print callout block, no writes
voice-dry FILE:
    "{{py}}" "{{root}}/scripts/process_voice.py" --dry-run --verbose "{{FILE}}"

# Force re-process a single recording even if already marked .processed
voice-reprocess FILE:
    "{{py}}" "{{root}}/scripts/process_voice.py" --force "{{FILE}}"

# Force re-process ALL recordings in VoiceDrop (ignores .processed markers)
voice-reprocess-all:
    "{{py}}" "{{root}}/scripts/process_voice.py" --force

# Scan VoiceDrop folder and process all unprocessed recordings
voice-scan:
    "{{py}}" "{{root}}/scripts/process_voice.py"

# Install faster-whisper into the project venv
voice-install:
    "{{py}}" -m pip install faster-whisper

# Telegram Bot API: getMe + getChat. Sources repo .env, then XDG files (later overrides), then JOURNAL_LINKER_ENV_FILE.
telegram-doctor:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -f "$HOME/.config/journal-linker/env" ]] && . "$HOME/.config/journal-linker/env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/telegram_doctor.py"'

# Show Telegram button feedback recorded in intent_delivery_ledger.jsonl
feedback-status:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -f "$HOME/.config/journal-linker/env" ]] && . "$HOME/.config/journal-linker/env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/feedback_status.py"'

# Check voice pipeline health: faster-whisper, VoiceDrop dir, pending count
voice-doctor:
    @printf '%s\n' "Journal Linker — Voice Pipeline"
    @bash -c '"{{py}}" -c "import faster_whisper; print(\"  faster-whisper: OK (\" + faster_whisper.__version__ + \")\")" 2>/dev/null || echo "  faster-whisper: NOT INSTALLED  →  run: just voice-install"'
    @bash -c 'VOICEDROP="${SCRIBE_VOICEDROP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop}"; [[ -d "$VOICEDROP" ]] && echo "  VoiceDrop dir:  $VOICEDROP" || echo "  VoiceDrop dir:  NOT FOUND — create in Files app or mkdir -p \"$VOICEDROP\""'
    @bash -c 'VOICEDROP="${SCRIBE_VOICEDROP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop}"; TOTAL=$(ls "$VOICEDROP"/*.m4a 2>/dev/null | wc -l | tr -d " "); DONE=$(ls "$VOICEDROP"/*.m4a.processed 2>/dev/null | wc -l | tr -d " "); FAIL=$(ls "$VOICEDROP"/*.m4a.failed 2>/dev/null | wc -l | tr -d " "); PENDING=$((TOTAL - DONE - FAIL)); echo "  Recordings:     $TOTAL total  ✓ $DONE processed  ✗ $FAIL failed  ⧖ $PENDING pending"' 2>/dev/null || true
    @bash -c 'launchctl list 2>/dev/null | grep -q journal-linker.voice && echo "  LaunchAgent:    com.journal-linker.voice loaded" || echo "  LaunchAgent:    com.journal-linker.voice NOT loaded (see launchd/VoiceWatch.example.plist)"'
    @printf '%s\n' \
      "" \
      "  Voice logs:     ~/Library/Logs/JournalLinker/ (voice-*-PID.log, voice-latest.log)" \
      "  iOS setup:      docs/ios-setup.md"

# Run intent pipeline on a single note (debug / manual run)
intent FILE:
    "{{py}}" "{{root}}/scripts/process_intents.py" --file "{{FILE}}"

# Dry-run intent pipeline: show envelope + planned route without side effects
intent-dry FILE:
    "{{py}}" "{{root}}/scripts/process_intents.py" --file "{{FILE}}" --dry-run --verbose

# Replay transient intent failures from the retry queue
intent-retry:
    "{{py}}" "{{root}}/scripts/process_intents.py" --retry

# Reset intent ledger and state files (dev/debug; irreversible)
intent-reset-ledger:
    "{{py}}" "{{root}}/scripts/process_intents.py" --reset-ledger

# Prune intent ledger entries older than 30 days
intent-prune:
    "{{py}}" "{{root}}/scripts/process_intents.py" --prune-ledger --older-than 30d

# What this is, where it lives on disk, and where scheduled runs log
doctor:
    @printf '%s\n' \
      "Journal Linker — Scribe" \
      "" \
      "  What: suggests Obsidian [[wikilinks]] for journal text (local Ollama)." \
      "  Repo: {{root}}" \
      "  Python: {{py}}"
    @bash -c '[[ -x "{{py}}" ]] && echo "  Venv: OK" || echo "  Venv: missing (see README)"'
    @bash -c 'xdg="${XDG_CONFIG_HOME:-$HOME/.config}"; f="$xdg/journal-linker/journal-linker.env"; [[ -f "$f" ]] && echo "  user env: $f (present)" || echo "  user env: $f (missing)"'
    @bash -c '[[ -f "{{root}}/.env" ]] && echo "  repo .env: present (legacy; set JOURNAL_LINKER_DOTENV=1 to load)" || echo "  repo .env: missing"'
    @printf '%s\n' \
      "" \
      "  Scheduled job logs:  ~/Library/Logs/JournalLinker/ (scribe-*-PID.log, scribe-latest.log)" \
      "  launchd job name:    com.journal-linker.scribe" \
      "  More: README.md -> On your Mac"
