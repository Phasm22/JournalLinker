# Journal Linker

A local-first knowledge pipeline for an Obsidian vault. Speak or write — entries get linked, learned from, and navigated automatically. No cloud required beyond iCloud for sync.

Three components, one feedback loop:

- **Scribe** — inserts `[[wikilinks]]` into daily notes using a local Ollama model, re-ranked by a reinforcement learning store that tracks what links actually stuck
- **Echo** — transcribes iPhone voice recordings with Whisper, using that same learning store as vocabulary context so your project names and proper nouns land correctly
- **Weekly Insights** — reads the week's entries and the learning store, drafts a reflection note

---

## How the feedback loop works

Every entry — typed or spoken — passes through the same pipeline:

1. **Vocab injection upstream:** the learning store's top-ranked terms (success × recency) are passed as Whisper's `initial_prompt` before transcription, so the model already knows your vocabulary
2. **Wikilink suggestion:** Ollama proposes candidates; Scribe re-ranks them using heuristics, semantic similarity, and per-term reinforcement scores
3. **State update downstream:** confirmed links feed back into `scribe_learning.json` — success count, recency, and context snippets — which improves both future suggestions and future transcriptions

Voice entries are first-class. A recording made at 11 PM is attributed to that day's note (configurable 4 AM rollover). The transcript lands as a `> [!voice] HH:MM` callout, then Scribe's full pipeline runs on it.

---

## Prerequisites

- **[Ollama](https://ollama.com/)** running locally with a chat model pulled (e.g. `llama3.1:8b`)
- **Python 3** with a venv at `ScribeVenv/` (`just` recipes use it automatically)
- **faster-whisper** for voice: `just voice-install`

---

## Quick start

1. Create **`.env`** in the repo root:

   ```bash
   SCRIBE_JOURNAL_DIR="/path/to/your/daily-notes-folder"
   # optional:
   # SCRIBE_MODEL="llama3.1:8b"
   # SCRIBE_CTX="8192"
   # SCRIBE_WHISPER_MODEL="base.en"
   ```

2. Daily notes must be named **`YYYY-MM-DD.md`**.

3. Run `just` to list all recipes, or see [Commands](#commands-just) below.

---

## Commands (`just`)

Install [just](https://github.com/casey/just) (`brew install just`).

**Scribe**

| Command | Meaning |
|---------|---------|
| `just scribe` | Run Scribe against today's note (venv Python) |
| `just scribe-paste` | macOS: clipboard → Scribe → stdout |
| `just scribe-writeback` | Read today's note from disk, insert wikilinks, write back in-place |
| `just scribe-job` | Same wrapper the launchd agent uses (timestamped logs) |
| `just weekly` | Generate the weekly insights note |
| `just test` | Run pytest (Ollama mocked) |
| `just doctor` | Paths, venv, `.env`, and log locations |

**Echo (voice)**

| Command | Meaning |
|---------|---------|
| `just voice-install` | Install faster-whisper into the venv |
| `just voice FILE` | Process a single recording |
| `just voice-dry FILE` | Dry-run: transcribe and print callout, no writes |
| `just voice-scan` | Process all pending recordings in VoiceDrop |
| `just voice-reprocess FILE` | Force re-process a specific file (clears `.failed` / `.processed`) |
| `just voice-reprocess-all` | Force re-process everything in VoiceDrop |
| `just voice-doctor` | Check faster-whisper, VoiceDrop dir, state counts, LaunchAgent |

---

## What Scribe does

- Sends note text to Ollama with a structured prompt; receives candidate link substrings
- Re-ranks candidates using heuristics (name-like terms, frequency, position) + reinforcement scores from `scribe_learning.json` (success/failure counts, recency decay, semantic similarity, burst boost from recent activity)
- Inserts `[[wikilinks]]` into the body, skipping existing links, YAML frontmatter, fenced code blocks, and paragraphs that look like shell output or logs
- Updates `← Yesterday | Tomorrow →` navigation to point at the nearest **substantive** daily notes — blank template stubs are skipped; nav falls back to calendar ±1 only when no real neighbor exists
- Writes a run report and updates the learning store after every pass

---

## What Echo does

- An iOS Shortcut saves dated `.m4a` recordings (`YYYY-MM-DD-HHmm.m4a`) to an iCloud Drive `VoiceDrop/` folder
- A launchd `WatchPaths` agent wakes `voice_watcher.sh` when the folder changes; the shell wrapper handles deduplication via a mkdir lock, then delegates entirely to `process_voice.py`
- `process_voice.py` checks the file is fully synced (not an iCloud placeholder), then runs the pipeline:
  - Reads `scribe_learning.json`, scores terms by `success_count × exp(-λ × days)`, builds a Whisper `initial_prompt` from the top-ranked terms (~150 tokens)
  - Transcribes with `faster-whisper base.en` (local, no API)
  - Appends a `> [!voice] HH:MM` callout to the target daily note
  - Calls `Scribe.py --write-back --active-date=DATE` so the full wikilink + learning pipeline runs on the voice entry
- State is tracked per file: `.processed` (success), `.failed` (error with reason string — surfaced by `voice-doctor`, skipped on retry until `--force`)

iOS setup: **[docs/ios-setup.md](./docs/ios-setup.md)**

---

## On your Mac

**Nightly Scribe job** (`com.journal-linker.scribe`)
- Plist: [launchd/Scribe.example.plist](./launchd/Scribe.example.plist)
- Runs `scheduled_run.sh` on a calendar interval; passes `--write-back` so the nightly pass saves linked output back to the active note
- Logs: `~/Library/Logs/JournalLinker/scribe-*.log`, symlink `scribe-latest.log`
- Lock: `.scribe-job.lock` (stale after crash: `rmdir ~/Library/Logs/JournalLinker/.scribe-job.lock`)

**Voice watcher** (`com.journal-linker.voice`)
- Plist: [launchd/VoiceWatch.example.plist](./launchd/VoiceWatch.example.plist)
- Install: copy plist to `~/Library/LaunchAgents/`, fill in paths, `launchctl load`
- Uses `WatchPaths` as the trigger edge only — all logic lives in Python
- Logs: `~/Library/Logs/JournalLinker/voice-*.log`, symlink `voice-latest.log`

**Verify both agents:** `launchctl list | grep journal-linker`

---

## Env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `SCRIBE_JOURNAL_DIR` | — | Path to daily notes folder (required) |
| `SCRIBE_MODEL` | `llama3.1:8b` | Ollama model |
| `SCRIBE_CTX` | `8192` | Ollama context window |
| `SCRIBE_WHISPER_MODEL` | `base.en` | faster-whisper model (`base.en`, `small.en`, `medium.en`) |
| `SCRIBE_VOICEDROP_DIR` | `~/…/iCloud Drive/VoiceDrop` | Folder Echo watches for recordings |
| `SCRIBE_NIGHT_CUTOFF` | `4` | Hour (0–23) before which a recording is attributed to the previous calendar day |

---

## Repo layout

| Path | Role |
|------|------|
| `Scribe.py` | Wikilink pipeline + learning store |
| `weekly_insights.py` | Weekly reflection note generator |
| `archivist.py` | Standalone Ollama + clipboard utility |
| `scripts/process_voice.py` | Echo: voice-to-journal bridge |
| `scripts/voice_watcher.sh` | launchd wrapper for Echo |
| `scripts/scheduled_run.sh` | launchd wrapper for Scribe |
| `launchd/` | Example plists for both agents |
| `docs/ios-setup.md` | iOS Shortcut build guide (UTS#35 date format, troubleshooting) |
| `scribe_learning.json` | Per-term learning store (gitignored) |
| `JOURNAL_TEMPLATE.md` | Obsidian Templater daily note template |
| `tests/` | Pytest suite; Ollama mocked |

Contributors / AI assistants: see [CLAUDE.md](./CLAUDE.md).
