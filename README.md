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
SCRIBE_JOURNAL_DIR="/Users/tjm4/Documents/TJ's Think Tank/much thinks"
```

Optional variables:

```bash
SCRIBE_MODEL="llama3.1:8b"
SCRIBE_CTX="8192"
```

## Yesterday-Learning Loop (One Day Behind)

Scribe now supports passive learning from your actual edits:

1. On each run, Scribe detects the current note date from `# Daily Log - YYYY-MM-DD`.
2. It checks yesterday's note in `SCRIBE_JOURNAL_DIR`.
3. It compares yesterday's suggested terms against links that remain in yesterday's current file.
4. Terms that survived are boosted; terms removed are down-ranked.
5. Today's run is saved and used as feedback tomorrow.

Learning memory is saved to:

- `scribe_learning.json` (next to `Scribe.py`)

## CLI

Basic:

```bash
pbpaste | python3 Scribe.py
```

With explicit journal folder:

```bash
pbpaste | python3 Scribe.py --journal-dir "/Users/tjm4/Documents/TJ's Think Tank/much thinks"
```

Reset learned weights:

```bash
python3 Scribe.py --reset-learning
```

## Notes

- Daily note files should be named `YYYY-MM-DD.md` in the configured journal folder.
- If learning context is unavailable (missing date, missing file, missing env), Scribe still runs normally.
# JournalLinker
# JournalLinker
