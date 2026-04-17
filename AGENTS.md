# Repository Guidelines

## Project Structure & Module Organization

This repository is a local-first journal pipeline for an Obsidian vault. Core scripts live at the repo root:

- `Scribe.py` - wikilink insertion and learning-store updates
- `weekly_insights.py` - weekly reflection generation
- `daily_reflection.py` - day-behind reflection and push delivery
- `archivist.py` - standalone clipboard/Ollama helper

Supporting code lives in `scripts/`, with launchd and systemd examples in `launchd/` and `systemd/`. Documentation lives in `docs/`, and tests are under `tests/` with fixtures in `tests/fixtures/`.

## Build, Test, and Development Commands

Use `just` from the repo root:

- `just` - list available recipes
- `just scribe` - run the wikilink pipeline
- `just weekly` - generate the weekly insight note
- `just daily-reflection` - dry-run the Pushover reflection path
- `just test` - run the test suite with mocked Ollama calls
- `just doctor` - verify local paths, venv, and config

Direct test execution also works with `python3 -m pytest tests/`.

## Coding Style & Naming Conventions

This codebase is Python-first. Use 4-space indentation, `snake_case` for functions and files, and `Test...` classes with `test_...` methods. Keep scripts small and explicit; prefer clear control flow over abstraction. Daily note files should follow `YYYY-MM-DD.md`. Follow the existing ASCII-only style unless a file already contains Unicode.

## Testing Guidelines

Tests use `pytest` to run `unittest`-style cases. Keep tests isolated with `tempfile.TemporaryDirectory` and mock external services such as Ollama, Whisper, and Pushover. Add reusable fixtures to `tests/fixtures/` when needed. Name tests for behavior, not implementation details.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries like `Add ...`, `Rewrite ...`, or `Guard against ...`. Keep commit subjects focused and present-tense. PRs should include a brief description of the change, the command(s) used to verify it, and any user-facing behavior changes. If a change affects launchd, voice processing, or reflections, mention the relevant logs or config paths.

## Security & Configuration Tips

Do not commit secrets or machine-specific paths. Prefer `~/.config/journal-linker/journal-linker.env` (mode `0600`) or systemd `EnvironmentFile=` + `JOURNAL_LINKER_ENV_FILE=...` for secrets. Repo-root `.env` is supported only as an explicit dev escape hatch (`JOURNAL_LINKER_DOTENV=1`). Generated state such as `scribe_learning.json` is intentionally local and should stay out of version control.
