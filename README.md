# Journal Linker (Scribe)

**Journal Linker** is this repo’s name on disk: a local-first Obsidian helper that adds useful `[[wikilinks]]` to daily journal notes (`Scribe.py`), with optional weekly synthesis (`weekly_insights.py`). Everything stays on your machine (Ollama).

Use it for a quick pass on a daily entry, or for a weekly reflection, without leaving your vault.

## Quick Start

1. Configure `.env` in the repo root (`SCRIBE_JOURNAL_DIR="/path/to/vault/daily-notes"` — see [TECHNICAL.md](./TECHNICAL.md)).
2. Copy note text (or rely on empty input + latest `YYYY-MM-DD.md` — see `Scribe.py` behavior).
3. Run Scribe, paste stdout back into Obsidian if you used the clipboard workflow.

## Commands (`just`)

Install [just](https://github.com/casey/just) (`brew install just`), then from this repo:

| Command | Meaning |
|--------|---------|
| `just` | List recipes |
| `just scribe` | Run Scribe (venv Python) |
| `just scribe-paste` | macOS: clipboard → Scribe → stdout |
| `just scribe -- --active-date=YYYY-MM-DD` | Scribe with CLI flags (note the `--`) |
| `just scribe-job` | Same wrapper the launchd template uses (timestamped logs) |
| `just weekly` | Weekly insights note |
| `just test` | Pytest |
| `just doctor` | Explain “what is this” + paths + venv + log locations |

## What It Does

- Suggests links in a journal entry and inserts `[[wikilinks]]`.
- Learns from recent runs (`scribe_learning.json`).
- Can write a weekly reflection into your vault.
- Works locally with your existing files.

Daily template reference: [JOURNAL_TEMPLATE.md](./JOURNAL_TEMPLATE.md).

## On your Mac

These names show up when you wonder *what is this thing*:

- **launchd** (scheduled runs): job label **`com.journal-linker.scribe`** if you use [launchd/Scribe.example.plist](./launchd/Scribe.example.plist) as `~/Library/LaunchAgents/com.journal-linker.scribe.plist`. Check with `launchctl list | grep journal-linker`.
- **Journal path in the plist:** do not use `PlistBuddy` for paths containing `'` (e.g. `TJ's`); use `./scripts/patch_launchagent_journal.sh "/your/path"` or `just launchagent-journal "/your/path"`.
- **Logs**: `~/Library/Logs/JournalLinker/` — per-run files `scribe-YYYYMMDD-HHMMSS-<pid>.log`, symlink `scribe-latest.log` (updated after each run). Only one job runs at a time (`.scribe-job.lock`); a second `kickstart` exits quietly. Stale lock after a crash: `rmdir ~/Library/Logs/JournalLinker/.scribe-job.lock`. Old logs from before PID/locking may look merged — safe to delete `scribe-*.log` files you do not need. To tail the resolved file: `tail "$(readlink "$HOME/Library/Logs/JournalLinker/scribe-latest.log")"`.
- **LaunchAgent stdout/stderr** (wrapper only): `/tmp/scribe.launchd.wrapper.*.log` as set in the example plist.

## Basic use without `just`

```bash
pbpaste | ./ScribeVenv/bin/python3 Scribe.py
./ScribeVenv/bin/python3 weekly_insights.py
```

More detail: [TECHNICAL.md](./TECHNICAL.md).
