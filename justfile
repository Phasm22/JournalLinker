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
