# Technical Notes

This file collects the setup, configuration, and behavior details that are useful for maintenance or deeper customization.

## Environment Variables

Create a local `.env` file next to the scripts.

```bash
SCRIBE_JOURNAL_DIR="/path/to/your/journal"
SCRIBE_MODEL="llama3.1:8b"
SCRIBE_CTX="8192"
SCRIBE_EMBED_MODEL="all-minilm"
SCRIBE_EMBED_KEEP_ALIVE="5m"
SCRIBE_EMBED_CACHE_MAX_ITEMS="512"
SCRIBE_NTFY_TOPIC="your-topic"
SCRIBE_DAILY_REFLECTION_WINDOW_START="16:00"
SCRIBE_DAILY_REFLECTION_WINDOW_END="21:00"
```

## `Scribe.py` CLI

```bash
python3 Scribe.py [text]
python3 Scribe.py --journal-dir "/path/to/your/journal"
python3 Scribe.py --model "llama3.1:8b"
python3 Scribe.py --ctx 8192
python3 Scribe.py --active-date 2026-03-11
python3 Scribe.py --active-file "/path/to/your/journal/2026-03-11.md"
python3 Scribe.py --reset-learning
```

- `--model` overrides the Ollama chat model used for backlink suggestions.
- `--ctx` overrides the Ollama context window.
- `--journal-dir` points at the Obsidian daily note folder.
- `--active-date` and `--active-file` override how the current note is resolved.
- `--reset-learning` clears `scribe_learning.json` before the run.

## `weekly_insights.py` CLI

```bash
python3 weekly_insights.py --journal-dir "/path/to/journal"
python3 weekly_insights.py --learning-file "/path/to/scribe_learning.json"
python3 weekly_insights.py --week 2026-W10
python3 weekly_insights.py --update-vault-map
```

- `--journal-dir` points at the Obsidian daily note folder.
- `--learning-file` selects the shared learning and embedding cache file.
- `--week` selects the ISO week to summarize.
- `--update-vault-map` runs `vault_mapper.py` after writing the insight note.

## `daily_reflection.py` CLI

```bash
python3 daily_reflection.py --journal-dir "/path/to/journal"
python3 daily_reflection.py --dry-run
python3 daily_reflection.py --date 2026-04-08
python3 daily_reflection.py --force-send
```

- `--date` overrides the reflected day; default is yesterday in local time.
- `--dry-run` prints the ntfy payload without sending it.
- `--force-send` bypasses the sent-state check for manual testing.

## `vault_mapper.py` CLI

```bash
python3 vault_mapper.py --journal-dir "/path/to/journal"
python3 vault_mapper.py --learning-file "/path/to/scribe_learning.json"
python3 vault_mapper.py --output-dir "/path/to/output"
python3 vault_mapper.py --min-cooccurrence 3
```

- `--journal-dir` points at the Obsidian daily note folder.
- `--learning-file` selects the shared learning file used for strength scoring.
- `--output-dir` changes where the markdown and JSON outputs are written.
- `--min-cooccurrence` controls the minimum link co-occurrence threshold for edges.

## Behavior

- Scribe reads input from argv, stdin, or clipboard.
- It uses Ollama for candidate generation.
- It ranks candidates with heuristics plus optional local embeddings.
- It writes and reuses learning data in `scribe_learning.json`.
- Weekly insights use ISO week boundaries and write to `Insights/Weekly Insight - YYYY-Www.md`.
- Daily reflection push reads only the previous day's note, computes a deterministic random send time within the configured local window, and sends to ntfy at most once per day.
- The embedding cache lives in the same learning file so repeated runs can reuse vectors.

## Notes

- Daily note files should be named `YYYY-MM-DD.md`.
- If learning context is unavailable, Scribe still runs normally.
- Navigation links are corrected to nearby existing journal dates when `SCRIBE_JOURNAL_DIR` is set.
- For body-only Shortcut input, Scribe updates nav links in the resolved active note file by default.
- Scribe also updates the previous existing journal note so its `Tomorrow` points at the latest note.
