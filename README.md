# Scribe

`Scribe.py` is a local-first Obsidian helper that suggests and inserts `[[wikilinks]]` into a journal entry.

It is designed for a clipboard workflow (for example, macOS Shortcuts keybind):
1. Copy note text.
2. Run `Scribe.py`.
3. Paste the output back into Obsidian.

## What It Does

- Accepts input from argv, piped stdin, or clipboard (`pbpaste` fallback).
- Calls local Ollama with a structured JSON prompt for backlink candidates.
- Re-ranks candidates with lightweight heuristics plus cached local embeddings for semantic similarity.
- Inserts links while avoiding existing `[[...]]`, frontmatter, and portability tail handling.

## Tunables

### Environment Variables

Create or update `.env` in this directory:

```bash
SCRIBE_JOURNAL_DIR="/path/to/your/journal"
```

`Scribe.py` automatically reads this local `.env` file at runtime.

Optional variables:

```bash
SCRIBE_MODEL="llama3.1:8b"
SCRIBE_CTX="8192"
SCRIBE_EMBED_MODEL="all-minilm"
SCRIBE_EMBED_KEEP_ALIVE="5m"
SCRIBE_EMBED_CACHE_MAX_ITEMS="512"
```

### `Scribe.py` CLI

```bash
python3 Scribe.py [text]
python3 Scribe.py --journal-dir "/path/to/your/journal"
python3 Scribe.py --model "llama3.1:8b"
python3 Scribe.py --ctx 8192
python3 Scribe.py --active-date 2026-03-11
python3 Scribe.py --active-file "/path/to/your/journal/2026-03-11.md"
python3 Scribe.py --reset-learning
```

Knobs:

- `--model` overrides the Ollama chat model used for backlink suggestions.
- `--ctx` overrides the Ollama context window.
- `--journal-dir` points at the Obsidian daily note folder.
- `--active-date` and `--active-file` override how the current note is resolved.
- `--reset-learning` clears `scribe_learning.json` before the run.

### `weekly_insights.py` CLI

```bash
python3 weekly_insights.py --journal-dir "/path/to/journal"
python3 weekly_insights.py --learning-file "/path/to/scribe_learning.json"
python3 weekly_insights.py --week 2026-W10
python3 weekly_insights.py --update-vault-map
```

Knobs:

- `--journal-dir` points at the Obsidian daily note folder.
- `--learning-file` selects the shared learning and embedding cache file.
- `--week` selects the ISO week to summarize.
- `--update-vault-map` runs `vault_mapper.py` after writing the insight note.

### `vault_mapper.py` CLI

```bash
python3 vault_mapper.py --journal-dir "/path/to/journal"
python3 vault_mapper.py --learning-file "/path/to/scribe_learning.json"
python3 vault_mapper.py --output-dir "/path/to/output"
python3 vault_mapper.py --min-cooccurrence 3
```

Knobs:

- `--journal-dir` points at the Obsidian daily note folder.
- `--learning-file` selects the shared learning file used for strength scoring.
- `--output-dir` changes where the markdown and JSON outputs are written.
- `--min-cooccurrence` controls the minimum link co-occurrence threshold for edges.

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
   - cached local embeddings for a second semantic signal when available,
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
- Builds a compact semantic summary from cached local embeddings so the model can see which entries sit closest to the week’s center of gravity.
- Includes sections:
  - `Top Active Topics This Week`
  - `Rising Topics`
  - `Cooling Topics`
  - `Evidence` with date links like `[[2026-03-08]]`.
- Prints output path and counts for Automator/AppleScript/shell logs.

## Notes

- Daily note files should be named `YYYY-MM-DD.md` in the configured journal folder.
- If learning context is unavailable (missing date, missing file, missing env), Scribe still runs normally.
- The embedding cache lives in `scribe_learning.json` next to the existing learning data, so repeated runs can reuse vectors instead of recomputing them.
- Navigation links are automatically corrected to nearest existing journal dates when `SCRIBE_JOURNAL_DIR` is set, which helps when you skip days.
- For body-only Shortcut input, Scribe updates nav links in the resolved active note file (newest modified note by default).
- Scribe also updates the previous existing journal note so its `Tomorrow` points at the latest note.
