#!/bin/bash
# Manual Scribe run — same venv + Scribe.py as launchd, but no job lock and no
# timestamped log (use scripts/scheduled_run.sh for that).
#
# Usage:
#   ./scripts/scribe.sh [--active-date=YYYY-MM-DD] [text...]
#   pbpaste | ./scripts/scribe.sh
#   echo "" | ./scripts/scribe.sh --write-back   # reads today's note, writes wikilinks back in-place
#
# --write-back: when input resolves to the on-disk journal file (empty stdin pipe),
#   write the processed output back to the file instead of only printing to stdout.
#   Use this in Shortcuts / launchd so you don't need a separate "write to file" step.
#
# Override model/context: set SCRIBE_MODEL / SCRIBE_CTX in the environment, or rely
# on ~/.config/journal-linker/journal-linker.env (see journal_linker_env.py).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/ScribeVenv/bin/python3}"
SCRIBE_PY="${SCRIBE_PY:-$ROOT/Scribe.py}"

export SCRIBE_MODEL="${SCRIBE_MODEL:-llama3.1:8b}"
export SCRIBE_CTX="${SCRIBE_CTX:-8192}"

exec "$PYTHON" "$SCRIBE_PY" "$@"
