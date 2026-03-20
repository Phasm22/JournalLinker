# Scribe

Scribe is a local-first Obsidian helper that adds helpful `[[wikilinks]]` to journal notes.

Use it when you want a quick pass over a daily entry, or when you want a weekly reflection without leaving your own files.

## Quick Start

1. Copy your note text.
2. Run `Scribe.py`.
3. Paste the result back into Obsidian.

## What It Does

- Helps surface useful links in a journal entry.
- Learns from recent edits over time.
- Writes a weekly reflection from your journal.
- Works locally with your existing files.

This repo includes the daily note starter template in [JOURNAL_TEMPLATE.md](./JOURNAL_TEMPLATE.md).

## Basic Use

```bash
pbpaste | python3 Scribe.py
python3 weekly_insights.py
```

If you want the setup details, see [TECHNICAL.md](./TECHNICAL.md).
