# Journal Linker — local Obsidian journal wikilink helper (Scribe + weekly insights).
# Install the runner: brew install just   →   https://github.com/casey/just
#
# First-time: create `.env` in this repo (SCRIBE_JOURNAL_DIR=...) — see README.

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

# Weekly insights note (uses .env for journal path)
weekly:
    "{{py}}" "{{root}}/weekly_insights.py"

# Tests only (Ollama mocked)
test:
    "{{py}}" -m pytest tests/

# Process a single voice recording (test/debug). e.g. `just voice ~/path/to/2026-04-04-1430.m4a`
voice FILE:
    "{{py}}" "{{root}}/scripts/process_voice.py" "{{FILE}}"

# Dry-run a single voice recording: transcribe and print callout block, no writes
voice-dry FILE:
    "{{py}}" "{{root}}/scripts/process_voice.py" --dry-run --verbose "{{FILE}}"

# Scan VoiceDrop folder and process all unprocessed recordings
voice-scan:
    "{{py}}" "{{root}}/scripts/process_voice.py"

# Install faster-whisper into the project venv
voice-install:
    "{{py}}" -m pip install faster-whisper

# Check voice pipeline health: faster-whisper, VoiceDrop dir, pending count
voice-doctor:
    @printf '%s\n' "Journal Linker — Voice Pipeline"
    @bash -c '"{{py}}" -c "import faster_whisper; print(\"  faster-whisper: OK (\" + faster_whisper.__version__ + \")\")" 2>/dev/null || echo "  faster-whisper: NOT INSTALLED  →  run: just voice-install"'
    @bash -c 'VOICEDROP="${SCRIBE_VOICEDROP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop}"; [[ -d "$VOICEDROP" ]] && echo "  VoiceDrop dir:  $VOICEDROP" || echo "  VoiceDrop dir:  NOT FOUND — create in Files app or mkdir -p \"$VOICEDROP\""'
    @bash -c 'VOICEDROP="${SCRIBE_VOICEDROP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop}"; TOTAL=$(ls "$VOICEDROP"/*.m4a 2>/dev/null | wc -l | tr -d " "); DONE=$(ls "$VOICEDROP"/*.processed 2>/dev/null | wc -l | tr -d " "); PENDING=$((TOTAL - DONE)); echo "  Recordings:     $TOTAL total, $PENDING pending"' 2>/dev/null || true
    @bash -c 'launchctl list 2>/dev/null | grep -q journal-linker.voice && echo "  LaunchAgent:    com.journal-linker.voice loaded" || echo "  LaunchAgent:    com.journal-linker.voice NOT loaded (see launchd/VoiceWatch.example.plist)"'
    @printf '%s\n' \
      "" \
      "  Voice logs:     ~/Library/Logs/JournalLinker/ (voice-*-PID.log, voice-latest.log)" \
      "  iOS setup:      docs/ios-setup.md"

# What this is, where it lives on disk, and where scheduled runs log
doctor:
    @printf '%s\n' \
      "Journal Linker — Scribe" \
      "" \
      "  What: suggests Obsidian [[wikilinks]] for journal text (local Ollama)." \
      "  Repo: {{root}}" \
      "  Python: {{py}}"
    @bash -c '[[ -x "{{py}}" ]] && echo "  Venv: OK" || echo "  Venv: missing (see README)"'
    @bash -c '[[ -f "{{root}}/.env" ]] && echo "  .env: present" || echo "  .env: missing"'
    @printf '%s\n' \
      "" \
      "  Scheduled job logs:  ~/Library/Logs/JournalLinker/ (scribe-*-PID.log, scribe-latest.log)" \
      "  launchd job name:    com.journal-linker.scribe" \
      "  More: README.md -> On your Mac"
