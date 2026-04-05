# Journal Linker (Scribe)

Local-first helper for an Obsidian-style vault: **`Scribe.py`** suggests `[[wikilinks]]` for daily journal notes using **Ollama** on your machine; **`weekly_insights.py`** can draft a weekly reflection note. No cloud API required.

## Prerequisites

- **[Ollama](https://ollama.com/)** installed and running, with a chat model pulled (for example `llama3.1:8b` — configurable via `.env`).
- **Python 3** and a project venv at `ScribeVenv/` (the `just` recipes use `./ScribeVenv/bin/python3`). Create the venv and install deps as you prefer, or follow [TECHNICAL.md](./TECHNICAL.md).

## Quick start

1. Create **`.env`** in the repo root:

   ```bash
   SCRIBE_JOURNAL_DIR="/path/to/your/daily-notes-folder"
   # optional:
   # SCRIBE_MODEL="llama3.1:8b"
   # SCRIBE_CTX="8192"
   ```

2. Daily notes must be named **`YYYY-MM-DD.md`** in that folder (or a layout your setup uses consistently with `SCRIBE_JOURNAL_DIR`).

3. Run Scribe (see [Commands (`just`)](#commands-just) below). Typical macOS flow: copy note body → `just scribe-paste` → paste stdout back into the note, or run against stdin / argv.

**Input and “active note” (short version):** Scribe takes text from **CLI args**, then **stdin**, then the **clipboard** if still empty. If stdin was an **empty pipe** or input came from the **clipboard** and a journal directory is configured, the **on-disk** body of the resolved daily note is used instead — so **launchd** and Shortcuts do not accidentally process stale clipboard text. Which file counts as “today” prefers **today’s `YYYY-MM-DD.md` if it exists**, otherwise the latest modified dated note. Override with `--active-date` or `--active-file`. Details: [TECHNICAL.md](./TECHNICAL.md).

## Commands (`just`)

Install [just](https://github.com/casey/just) (`brew install just`), then from this repo:

| Command | Meaning |
|--------|---------|
| `just` | List recipes |
| `just scribe` | Run Scribe (venv Python) |
| `just scribe-paste` | macOS: clipboard → Scribe → stdout |
| `just scribe -- --active-date=YYYY-MM-DD` | Forward flags to Scribe (note the `--`) |
| `just scribe-job` | Same wrapper the launchd template uses (timestamped logs under `~/Library/Logs/JournalLinker/`) |
| `just weekly` | Generate the weekly insights note |
| `just test` | Run pytest (Ollama mocked) |
| `just doctor` | Print paths, venv / `.env` hints, and where scheduled logs go |
| `just launchagent-journal "/path/to/daily-notes"` | Patch the LaunchAgent plist journal path (safe for paths with `'` — see below) |

## What it does

- Calls Ollama with a structured prompt, then **re-ranks** candidates with heuristics and **reinforcement** from `scribe_learning.json` (per-term success/failure, recency, optional local embeddings — see [TECHNICAL.md](./TECHNICAL.md)).
- Inserts `[[wikilinks]]` while skipping existing links, YAML frontmatter, and “portability” tails; avoids linkifying **fenced code blocks** and paragraphs that look like **shell transcripts or logs** (so launchd / `just` output in a note is not turned into links).
- Updates **Yesterday | Tomorrow** navigation in daily notes to point at the nearest **substantive** neighbors (skips empty stubs); uses calendar ±1 only when there is no neighbor on that side.
- **`weekly_insights.py`** reads the same learning file and that ISO week’s entries, then writes `Insights/Weekly Insight - YYYY-Www.md` (idempotent on rerun).

Daily layout reference: [JOURNAL_TEMPLATE.md](./JOURNAL_TEMPLATE.md).

## Without `just`

```bash
pbpaste | python3 Scribe.py
python3 weekly_insights.py
python3 -m pytest tests/
```

Full CLI flags and env vars: [TECHNICAL.md](./TECHNICAL.md).

## On your Mac

- **launchd:** Example plist: [launchd/Scribe.example.plist](./launchd/Scribe.example.plist). Job label **`com.journal-linker.scribe`** when installed as `~/Library/LaunchAgents/com.journal-linker.scribe.plist`. Check: `launchctl list | grep journal-linker`.
- **Paths with apostrophes** (e.g. `TJ's`): do not hand-edit with `PlistBuddy` for the journal path; use `./scripts/patch_launchagent_journal.sh "/your/path"` or `just launchagent-journal "/your/path"`.
- **Logs:** `~/Library/Logs/JournalLinker/` — per-run files `scribe-YYYYMMDD-HHMMSS-<pid>.log`, symlink `scribe-latest.log`. Only one `scribe-job` at a time (`.scribe-job.lock`); a second start exits quietly. Stale lock after a crash: `rmdir ~/Library/Logs/JournalLinker/.scribe-job.lock`. Tail the resolved file: `tail "$(readlink "$HOME/Library/Logs/JournalLinker/scribe-latest.log")"`.
- **LaunchAgent wrapper logs** (stdout/stderr from the plist’s wrapper): `/tmp/scribe.launchd.wrapper.*.log` as in the example plist.

## Voice pipeline (iPhone → journal)

Record on your iPhone; the Mac transcribes and appends to the day's note automatically.

**Quick start:**
1. Build the iOS Shortcut: **[docs/ios-setup.md](./docs/ios-setup.md)** — 3 actions, one-time setup.
2. Install faster-whisper: `just voice-install`
3. Install the LaunchAgent: copy `launchd/VoiceWatch.example.plist`, fill in your paths, load it.
4. Check everything: `just voice-doctor`

**How it works:** Each recording (`YYYY-MM-DD-HHmm.m4a`) saved to an iCloud Drive `VoiceDrop/` folder is picked up by a launchd `WatchPaths` agent. `scripts/process_voice.py` reads `scribe_learning.json`, ranks your known wikilink terms by success × recency, passes them as Whisper's `initial_prompt` (vocabulary bias), transcribes the audio, appends a `> [!voice] HH:MM` callout to the daily note, then calls `Scribe.py --write-back` so the full wikilink + learning feedback loop runs on the voice entry too.

**New `.env` vars for voice:**

| Variable | Default | Meaning |
|----------|---------|---------|
| `SCRIBE_VOICEDROP_DIR` | `~/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop` | iCloud folder to watch |
| `SCRIBE_WHISPER_MODEL` | `base.en` | faster-whisper model (`base.en`, `small.en`, `medium.en`) |
| `SCRIBE_NIGHT_CUTOFF` | `4` | Hour (0–23) before which a recording is attributed to the previous day |

**New `just` recipes:**

| Command | Meaning |
|---------|---------|
| `just voice-install` | `pip install faster-whisper` into the venv |
| `just voice FILE` | Process a single `.m4a` file |
| `just voice-dry FILE` | Dry-run: transcribe + print callout, no writes |
| `just voice-scan` | Process all pending files in VoiceDrop |
| `just voice-doctor` | Check faster-whisper, VoiceDrop dir, pending count, LaunchAgent |

**Voice pipeline on your Mac:**

1. Copy the plist: `cp launchd/VoiceWatch.example.plist ~/Library/LaunchAgents/com.journal-linker.voice.plist`
2. Edit it — replace `REPLACE_WITH_YOUR_JOURNAL_PATH` and `REPLACE_WITH_REPO_PATH` / `REPLACE_WITH_USERNAME`.
3. Load it: `launchctl load ~/Library/LaunchAgents/com.journal-linker.voice.plist`
4. Verify: `launchctl list | grep journal-linker.voice`
5. Logs: `~/Library/Logs/JournalLinker/voice-*.log`, symlink `voice-latest.log`.
6. Stale lock after crash: `rmdir ~/Library/Logs/JournalLinker/.voice-job.lock`

---

## Repo layout (high level)

| Path | Role |
|------|------|
| `Scribe.py` | Main wikilink pipeline |
| `weekly_insights.py` | Weekly note generator |
| `archivist.py` | Separate Ollama + clipboard utility (shared env pattern) |
| `scripts/process_voice.py` | Voice-to-journal bridge (faster-whisper + Scribe handoff) |
| `scripts/voice_watcher.sh` | launchd wrapper for voice processing |
| `docs/ios-setup.md` | iOS Shortcut build instructions |
| `scribe_learning.json` | Local learning store (often gitignored — see `.gitignore`) |
| `tests/` | Pytest; Ollama mocked |

Contributors / AI assistants: see [CLAUDE.md](./CLAUDE.md).
