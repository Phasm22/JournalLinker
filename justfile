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

# Run pytest under coverage and print the line-by-line report
coverage:
    "{{py}}" -m coverage run -m pytest tests/
    "{{py}}" -m coverage report --show-missing

# Generate an HTML coverage report in htmlcov/
coverage-html:
    "{{py}}" -m coverage run -m pytest tests/
    "{{py}}" -m coverage html
    @printf '%s\n' "Coverage HTML written to htmlcov/index.html"

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

# Install the MCP client used for llmLibrarian intent enrichment
intent-mcp-install:
    "{{py}}" -m pip install mcp

# Smoke test: initialize → tools/list → tools/call against the live MCP server
intent-mcp-smoke *ARGS:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/mcp_smoke.py" {{ARGS}}'

# Stack health: /healthz, llmlib watcher services, pal silo status, enrichment mode
status:
    @printf '%s\n' "Journal Linker — Stack Health"
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; \
      host="${LLMLIBRARIAN_MCP_HOST:-127.0.0.1}"; \
      port="${LLMLIBRARIAN_MCP_PORT:-8765}"; \
      url="http://$host:$port/healthz"; \
      if curl -sf --max-time 2 "$url" >/dev/null 2>&1; then \
        printf "  %-34s %s\n" "llmLibrarian /healthz" "OK  ($url)"; \
      else \
        printf "  %-34s %s\n" "llmLibrarian /healthz" "DOWN  ($url)"; \
      fi; \
      running=$(systemctl --user list-units --type=service --state=running 2>/dev/null | grep -c "llmlibrarian-watch" || true); \
      total=$(systemctl --user list-unit-files --type=service 2>/dev/null | grep -c "llmlibrarian-watch" || true); \
      if [[ "${running:-0}" -ge 1 ]]; then \
        printf "  %-34s %s\n" "llmlib watcher services" "OK  ($running/$total running)"; \
      else \
        printf "  %-34s %s\n" "llmlib watcher services" "DOWN  (0/$total running)"; \
      fi; \
      palbin="${LLMLIBRARIAN_REPO:-$HOME/Desktop/llmLibrarian}/.venv/bin/pal"; \
      if [[ -x "$palbin" ]]; then \
        palout=$("$palbin" ls --status 2>&1); \
        if echo "$palout" | grep -q "No action needed"; then \
          printf "  %-34s %s\n" "pal ls --status" "OK"; \
        else \
          printf "  %-34s %s\n" "pal ls --status" "ACTION NEEDED"; \
          echo "$palout" | sed "s/^/      /"; \
        fi; \
      else \
        printf "  %-34s %s\n" "pal ls --status" "not found ($palbin)"; \
      fi; \
      mode="${INTENT_ENRICHMENT_MODE:-llmlib}"; \
      if [[ "$mode" == "off" ]]; then \
        printf "  %-34s %s\n" "INTENT_ENRICHMENT_MODE" "WARNING: off (enrichment disabled)"; \
      else \
        printf "  %-34s %s\n" "INTENT_ENRICHMENT_MODE" "OK  ($mode)"; \
      fi'

# Cross-project handoff quickcheck: status + queue pressure + actionable repairs
handoff-check:
    @printf '%s\n' "Journal Linker — Handoff Quickcheck"
    @just status
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; \
      state_dir="${INTENT_STATE_DIR:-$HOME/.local/state/journal-linker/intents}"; \
      max_unanswered="${INTENT_FEEDBACK_MAX_UNANSWERED:-3}"; \
      ledger="$state_dir/intent_delivery_ledger.jsonl"; \
      if [[ -f "$ledger" ]]; then \
        unanswered=$("{{py}}" -c "import json, pathlib; p=pathlib.Path(r'''$ledger'''); rows=[json.loads(l) for l in p.read_text(encoding='utf-8', errors='ignore').splitlines() if l.strip().startswith('{')]; print(sum(1 for r in rows if str(r.get('delivery_status','')).strip().lower()=='sent' and r.get('feedback_signal') in (None,'','none')))" 2>/dev/null || echo 0); \
        printf "  %-34s %s\n" "unanswered sent check-ins" "$unanswered (cap=$max_unanswered)"; \
      else \
        printf "  %-34s %s\n" "intent_delivery_ledger.jsonl" "missing ($ledger)"; \
      fi; \
      mode="${INTENT_ENRICHMENT_MODE:-llmlib}"; \
      silo="${LLMLIBRARIAN_MCP_SILO:-<all-available>}"; \
      printf "  %-34s %s\n" "llmLibrarian silo target" "$silo"; \
      printf "%s\n" ""; \
      printf "%s\n" "If llmLibrarian returns inconsistent results:"; \
      printf "%s\n" "  llmli repair tjs-pc-7f8e4e9d"; \
      printf "%s\n" "  llmli add --full /home/tj/Documents/twin-brain/TJ\\'s\\ PC"; \
      if [[ "$mode" != "off" ]]; then \
        printf "%s\n" ""; \
        printf "%s\n" "Enrichment is enabled; if context is unexpectedly thin, verify cortex is indexed in llmLibrarian excludes/archetypes."; \
      fi'

# Telegram Bot API: getMe + getChat. Sources repo .env, then XDG files (later overrides), then JOURNAL_LINKER_ENV_FILE.
telegram-doctor:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/telegram_doctor.py"'

# Show Telegram button feedback recorded in intent_delivery_ledger.jsonl
feedback-status:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/feedback_status.py"'

# Poll Telegram for feedback responses and send any due check-in messages
feedback-sender *ARGS:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/feedback_sender.py" {{ARGS}}'

# Run Telegram feedback sender as a foreground long-polling daemon
feedback-daemon *ARGS:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/feedback_sender.py" --daemon {{ARGS}}'

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

# Build markdown snapshot from intent_delivery_ledger.jsonl for llmLibrarian indexing
intent-ledger-snapshot *ARGS:
    @bash -c 'set -a; \
      [[ -f "{{root}}/.env" ]] && . "{{root}}/.env"; \
      [[ -f "$HOME/.config/journal-linker/journal-linker.env" ]] && . "$HOME/.config/journal-linker/journal-linker.env"; \
      [[ -n "${JOURNAL_LINKER_ENV_FILE:-}" && -f "${JOURNAL_LINKER_ENV_FILE}" ]] && . "${JOURNAL_LINKER_ENV_FILE}"; \
      set +a; "{{py}}" "{{root}}/scripts/intent_ledger_snapshot.py" {{ARGS}}'

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
