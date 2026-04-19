# Journal Linker

**Personal scripts and workflow** around a daily Obsidian journal. This repo is my working tree: paths, env layout, Python entrypoints, and launchd/systemd units wired to **my** vault and machines. It is not a polished product — if something here is useful, copy the pieces you need and retarget `SCRIBE_JOURNAL_DIR`, models, and schedulers to your setup.

Still, the shape is simple: **local-first**. Speak or write — entries get linked, learned from, and navigated with optional pushes (Pushover, etc.). Sync is whatever you use for the vault (iCloud, Dropbox, git); the automation assumes files show up locally.

One feedback loop, several moving parts:

- **Scribe** — inserts `[[wikilinks]]` into daily notes using a local Ollama model, re-ranked by a reinforcement learning store that tracks what links actually stuck
- **Echo** — transcribes iPhone voice recordings with Whisper, using that same learning store as vocabulary context so your project names and proper nouns land correctly
- **Weekly Insights** — reads the week's entries and the learning store, drafts a reflection note
- **Daily Reflection Push** — reads yesterday's entry, drafts a short reflection, and sends it once per day through Pushover

Related automation (not always in *this* repo clone) can live under `JOURNAL_LINKER_REPO` on disk — e.g. intent routing, Telegram feedback — but Scribe, voice, reflection, and weekly insights are anchored here.

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

1. Configure environment variables (recommended: **outside the git repo**):

```bash
install -d -m 700 ~/.config/journal-linker
${EDITOR:-nano} ~/.config/journal-linker/journal-linker.env
chmod 600 ~/.config/journal-linker/journal-linker.env
```

Put at least:

```bash
SCRIBE_JOURNAL_DIR="/path/to/your/daily-notes-folder"
# optional:
# SCRIBE_MODEL="llama3.1:8b"
# SCRIBE_CTX="8192"
# SCRIBE_WHISPER_MODEL="base.en"
```

Alternative locations (see `journal_linker_env.py`):

- `JOURNAL_LINKER_ENV_FILE=/path/to/file.env` (good for systemd `EnvironmentFile=`)
- Legacy dev: repo-root `.env` only if `JOURNAL_LINKER_DOTENV=1`

2. Daily notes must be named `**YYYY-MM-DD.md**`.
3. Run `just` to list all recipes, or see [Commands](#commands-just) below.

---

## Commands (`just`)

Install [just](https://github.com/casey/just) (`brew install just`).

**Scribe**


| Command                 | Meaning                                                            |
| ----------------------- | ------------------------------------------------------------------ |
| `just scribe`           | Run Scribe against today's note (venv Python)                      |
| `just scribe-paste`     | macOS: clipboard → Scribe → stdout                                 |
| `just scribe-writeback` | Read today's note from disk, insert wikilinks, write back in-place |
| `just scribe-job`       | Same wrapper the launchd agent uses (timestamped logs)             |
| `just weekly`           | Generate the weekly insights note                                  |
| `just daily-reflection` | Dry-run the day-behind Pushover reflection and print the notification |
| `just daily-reflection-send` | Run the real Pushover delivery path manually                 |
| `just intent-mcp-install` | Install the MCP client used for llmLibrarian intent enrichment |
| `just test`             | Run pytest (Ollama mocked)                                         |
| `just coverage`         | Run pytest under coverage and print missing lines                  |
| `just doctor`           | Paths, venv, env config, and log locations                         |


**Echo (voice)**


| Command                     | Meaning                                                            |
| --------------------------- | ------------------------------------------------------------------ |
| `just voice-install`        | Install faster-whisper into the venv                               |
| `just voice FILE`           | Process a single recording                                         |
| `just voice-dry FILE`       | Dry-run: transcribe and print callout, no writes                   |
| `just voice-scan`           | Process all pending recordings in VoiceDrop                        |
| `just voice-reprocess FILE` | Force re-process a specific file (clears `.failed` / `.processed`) |
| `just voice-reprocess-all`  | Force re-process everything in VoiceDrop                           |
| `just voice-doctor`         | Check faster-whisper, VoiceDrop dir, state counts, LaunchAgent     |


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

**Daily reflection push** (`com.journal-linker.daily-reflection`)

- Plist: [launchd/DailyReflection.example.plist](./launchd/DailyReflection.example.plist)
- Polls every 15 minutes; `daily_reflection.py` chooses one deterministic random send time between the configured local window bounds and sends once when that tick arrives
- Sends a short day-behind reflection to Pushover; skips silently when yesterday's note is missing or too thin
- Logs: `~/Library/Logs/JournalLinker/daily-reflection-*.log`, symlink `daily-reflection-latest.log`

**Verify both agents:** `launchctl list | grep journal-linker`

---

## On Linux (systemd)

User units and env templates live under [`systemd/`](./systemd/). Typical flow:

- Copy or symlink unit files into `~/.config/systemd/user/`, set `JOURNAL_LINKER_REPO` to this repo’s path in `~/.config/journal-linker/journal-linker.env`, then `systemctl --user daemon-reload`.
- Enable `.path` / `.timer` units for watcher or scheduled jobs. Enable `journal-linker-feedback-sender.service` directly; it is a persistent Telegram long-polling daemon, not a timer.
- Logs often go to `~/.local/state/journal-linker/` when `SCRIBE_JOB_LOG_DIR` is set that way (see `just doctor`).
- After amending commits or changing remotes: `git fetch origin` before `git push --force-with-lease` so the lease matches GitHub.

VoiceDrop may be a Dropbox folder instead of iCloud; override `SCRIBE_VOICEDROP_DIR` in the env file.

---

## Env vars


| Variable               | Default                      | Meaning                                                                         |
| ---------------------- | ---------------------------- | ------------------------------------------------------------------------------- |
| `SCRIBE_JOURNAL_DIR`   | —                            | Path to daily notes folder (required)                                           |
| `SCRIBE_MODEL`         | `llama3.1:8b`                | Ollama model                                                                    |
| `SCRIBE_CTX`           | `8192`                       | Ollama context window                                                           |
| `SCRIBE_WHISPER_MODEL` | `base.en`                    | faster-whisper model (`base.en`, `small.en`, `medium.en`)                       |
| `SCRIBE_VOICEDROP_DIR` | `~/…/iCloud Drive/VoiceDrop` | Folder Echo watches for recordings                                              |
| `SCRIBE_NIGHT_CUTOFF`  | `4`                          | Hour (0–23) before which a recording is attributed to the previous calendar day |
| `SCRIBE_PUSHOVER_SERVER`   | `https://api.pushover.net` | Pushover API server                                                             |
| `SCRIBE_PUSHOVER_APP_TOKEN`| —                          | Pushover application API token                                                  |
| `SCRIBE_PUSHOVER_USER_KEY` | —                          | Pushover recipient user key                                                     |
| `SCRIBE_PUSHOVER_DEVICE`   | —                          | Optional device name to target a specific device                                |
| `SCRIBE_PUSHOVER_PRIORITY` | `0`                        | Pushover priority                                                               |
| `SCRIBE_DAILY_REFLECTION_WINDOW_START` | `16:00`      | Local start of the random reflection send window                                 |
| `SCRIBE_DAILY_REFLECTION_WINDOW_END`   | `21:00`      | Local end of the random reflection send window                                   |

For compatibility, `daily_reflection.py` also accepts `PUSHOVER_TOKEN` and `PUSHOVER_KEY` as aliases for the app token and user key.


---

## Repo layout


| Path                       | Role                                                           |
| -------------------------- | -------------------------------------------------------------- |
| `Scribe.py`                | Wikilink pipeline + learning store                             |
| `weekly_insights.py`       | Weekly reflection note generator                               |
| `daily_reflection.py`      | Day-behind Pushover reflection generator + sender              |
| `archivist.py`             | Standalone Ollama + clipboard utility                          |
| `scripts/process_voice.py` | Echo: voice-to-journal bridge                                  |
| `scripts/voice_watcher.sh` | launchd wrapper for Echo                                       |
| `scripts/scheduled_run.sh` | launchd wrapper for Scribe                                     |
| `scripts/daily_reflection.sh` | launchd/systemd wrapper for Pushover reflection polling    |
| `launchd/`                 | Example plists (macOS)                                         |
| `systemd/`                 | Example user units + `journal-linker.env.example` (Linux)      |
| `docs/ios-setup.md`        | iOS Shortcut build guide (UTS#35 date format, troubleshooting) |
| `scribe_learning.json`     | Per-term learning store (gitignored)                           |
| `JOURNAL_TEMPLATE.md`      | Obsidian Templater daily note template                         |
| `tests/`                   | Pytest suite; Ollama mocked                                    |


Contributors / AI assistants: see [CLAUDE.md](./CLAUDE.md). For deeper config and CLI notes, see [TECHNICAL.md](./TECHNICAL.md).
