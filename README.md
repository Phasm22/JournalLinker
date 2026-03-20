# Scribe

`Scribe.py` is a local-first Obsidian helper that suggests and inserts `[[wikilinks]]` into a journal entry.

It is designed for a clipboard workflow (for example, macOS Shortcuts keybind):
1. Copy note text.
2. Run `Scribe.py`.
3. Paste the output back into Obsidian.

## What It Does

- Accepts input from argv, piped stdin, or clipboard (`pbpaste` fallback).
- Calls local Ollama with a structured JSON prompt for backlink candidates.
- Re-ranks candidates with lightweight heuristics.
- Inserts links while avoiding existing `[[...]]`, frontmatter, and portability tail handling.

## Environment

Create or update `.env` in this directory:

```bash
SCRIBE_JOURNAL_DIR="/path/to/your/journal"
```

`Scribe.py` automatically reads this local `.env` file at runtime.

Optional variables:

```bash
SCRIBE_MODEL="llama3.1:8b"
SCRIBE_CTX="8192"
```

## Temporal Memory Loop (One Day Feedback + Decay + Burst)

Scribe now supports passive learning from your actual edits with temporal relevance:

1. On each run, Scribe resolves the current note date from:
   - the input note header/nav when present,
   - otherwise the newest modified `YYYY-MM-DD.md` in `SCRIBE_JOURNAL_DIR`.
2. It checks yesterday's note in `SCRIBE_JOURNAL_DIR`.
3. It compares yesterday's suggested terms against links that remain in yesterday's current file.
4. Terms that survived are boosted; terms removed are down-ranked.
5. The learner stores per-term memory (`success/failure counts`, `last seen/success date`, and compact context snippets).
6. Ranking now combines:
   - heuristic score (frequency/name-like/generic penalties/model order),
   - reinforcement from prior usefulness,
   - recency decay (`exp(-lambda * days_since_success)`),
   - semantic similarity between current context and stored contexts,
   - burst boost from recent wikilink activity (`last 3 days`, capped at 3 days).
7. Today's run is saved and used as feedback tomorrow.
8. If your note has `[[...|Yesterday]] | [[...|Tomorrow]]`, Scribe auto-syncs those targets from nearby existing journal entries in `SCRIBE_JOURNAL_DIR` (with +/-1 day fallback when neighbors do not exist).

Learning memory is saved to:

- `scribe_learning.json` (next to `Scribe.py`)

## CLI

Basic:

```bash
pbpaste | python3 Scribe.py
```

With explicit journal folder:

```bash
pbpaste | python3 Scribe.py --journal-dir "/path/to/your/journal"
```

Optional active-entry overrides:

```bash
pbpaste | python3 Scribe.py --active-date 2026-03-11
pbpaste | python3 Scribe.py --active-file "/path/to/your/journal/2026-03-11.md"
```

Reset learned weights:

```bash
python3 Scribe.py --reset-learning
```

## Weekly Insight Runner

Generate or refresh one weekly insight note with no interaction:

```bash
python3 weekly_insights.py
```

Optional:

```bash
python3 weekly_insights.py --journal-dir "/path/to/journal"
python3 weekly_insights.py --week 2026-W10
python3 weekly_insights.py --learning-file "/path/to/scribe_learning.json"
```

Behavior:

- Uses ISO week boundaries (`Mon-Sun`).
- Writes to `<journal_dir>/Insights/Weekly Insight - YYYY-Www.md`.
- Overwrites the same week's file on rerun (idempotent refresh).
- Includes sections:
  - `Top Active Topics This Week`
  - `Rising Topics`
  - `Cooling Topics`
  - `Evidence` with date links like `[[2026-03-08]]`.
- Prints output path and counts for Automator/AppleScript/shell logs.

## Notes

- Daily note files should be named `YYYY-MM-DD.md` in the configured journal folder.
- If learning context is unavailable (missing date, missing file, missing env), Scribe still runs normally.
- Navigation links are automatically corrected to nearest existing journal dates when `SCRIBE_JOURNAL_DIR` is set, which helps when you skip days.
- For body-only Shortcut input, Scribe updates nav links in the resolved active note file (newest modified note by default).
- Scribe also updates the previous existing journal note so its `Tomorrow` points at the latest note.
