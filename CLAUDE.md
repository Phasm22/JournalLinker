# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Prefer **`just`** from the repo root (`brew install just`) — see `justfile` and README “Commands (just)”.

```bash
just                    # list recipes
just scribe             # venv + Scribe.py
just scribe-paste       # macOS clipboard → Scribe
just scribe-job         # structured logs (same as launchd wrapper)
just weekly             # weekly insights
just test               # pytest
just doctor             # paths, venv, “what is this on my Mac”

# Run all tests (without just)
python3 -m pytest tests/

# Run a single test file
python3 -m pytest tests/test_scribe_learning.py

# Run a single test by name
python3 -m pytest tests/test_scribe_learning.py::TestScribeLearning::test_find_latest_modified_journal_note_selects_newest_valid_date_file

# Run Scribe (clipboard workflow, system python)
pbpaste | python3 Scribe.py

# Input priority (see `get_input_text` + `main`): argv, then non-empty stdin, then
# if stdin pipe was empty or input came from the clipboard and a journal note was
# resolved, the on-disk note body wins over clipboard (launchd-safe).

# Run weekly insights
python3 weekly_insights.py
```

Tests use `importlib` to load `Scribe.py` directly as a module (not a package), so test files import from `SCRIPT_PATH = Path(__file__).resolve().parents[1] / "Scribe.py"`.

## Environment

Create `.env` in the repo root:
```
SCRIBE_JOURNAL_DIR="/path/to/journal"
SCRIBE_MODEL="llama3.1:8b"   # optional
SCRIBE_CTX="8192"             # optional
```

`Scribe.py` calls `load_local_env()` at startup to read this file. Journal notes must be named `YYYY-MM-DD.md`.

## Architecture

**`Scribe.py`** — main entry point. Pipeline:
1. Parse CLI (`--model`, `--ctx`, `--journal-dir`, `--active-date`, `--active-file`, `--reset-learning`)
2. Resolve active journal context (`resolve_current_journal_context`) — from input header, `--active-date`/`--active-file`, then **today’s `YYYY-MM-DD.md` if that file exists**, else the newest modified dated note in the journal dir
3. Call local Ollama with a structured JSON prompt to get backlink candidates
4. Re-rank candidates via heuristics + reinforcement learning from `scribe_learning.json`
5. Insert `[[wikilinks]]` into the text (skipping existing links, frontmatter, portability tails)
6. Output to stdout; update learning state and nav links in journal files as side effects (Yesterday|Tomorrow point at the nearest **substantive** daily notes, skipping empty stubs; calendar ±1 day only if no neighbor on that side)

**`scribe_learning.json`** — per-term memory store. Each term tracks `success`/`failure` counts, `last_seen`/`last_success` dates, and context snippets. Ranking combines heuristic score, reinforcement score, recency decay (`exp(-lambda * days_since_success)`), semantic similarity, and burst boost from recent activity.

**`weekly_insights.py`** — standalone script that reads `scribe_learning.json` and journal entries for an ISO week, calls Ollama, and writes `<journal_dir>/Insights/Weekly Insight - YYYY-Www.md`. Idempotent on rerun.

**`archivist.py`** — separate utility that also uses Ollama and clipboard. Shares the same `SCRIBE_MODEL`/`SCRIBE_CTX` env vars pattern.

**`tests/`** — unittest-based. Fixtures live in `tests/fixtures/`. Tests use `tempfile.TemporaryDirectory` for isolation; no external services are called (Ollama calls are mocked).
