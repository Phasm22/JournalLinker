# systemd (user units) — Linux

Canonical timer + service definitions for journalLinker when **not** using macOS launchd.

## On-demand runtime (`pc-stacks`)

On TJ's Linux desktop, the **Telegram feedback sender** is cold at boot. Path units and timers stay enabled (low idle cost).

```bash
pc-stacks up journal       # journal-linker-feedback-sender.service
pc-stacks status
```

Job scripts source `scripts/ensure_ollama.sh` before Ollama/Whisper work. Index: [`/home/tj/bin/README.md`](/home/tj/bin/README.md). Traceability: PC Idle Quietdown plan (Cursor plans, Jul 2025).

## Why these timers look like this

- **`OnCalendar=*-*-* *:MM/30`** — wall-clock every 30 minutes (was 15 min pre–Idle Quietdown).

## Stagger

| Unit | Schedule | Purpose |
|------|----------|---------|
| `journal-linker-daily-reflection.timer` | minutes **:02, :32** (every 30m) | `daily_reflection.sh` — script decides whether to send |
| `journal-linker-voice-retry.timer` | minutes **:09, :39** (every 30m) | `voice_retry.sh` — retries transient voice failures |
| `journal-linker-intent-retry.timer` | every **30 min** after boot | `intent_retry.sh` |
| `journal-linker-nutrition-summary.timer` | **21:00** local | `nutrition_day_summary.sh` — one Pushover for the day's intake |

Seven minutes after each daily-reflection tick starts a voice-retry cycle, so Whisper / disk are less likely to pile onto the same moment as reflection.

## Install or refresh

```bash
REPO=/path/to/journalLinker   # e.g. ~/journalLinker
install -d -m 755 ~/.config/systemd/user
cp "$REPO/systemd/journal-linker-daily-reflection.service" \
   "$REPO/systemd/journal-linker-daily-reflection.timer" \
   "$REPO/systemd/journal-linker-voice-retry.service" \
   "$REPO/systemd/journal-linker-voice-retry.timer" \
   "$REPO/systemd/journal-linker-nutrition-summary.service" \
   "$REPO/systemd/journal-linker-nutrition-summary.timer" \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now journal-linker-daily-reflection.timer journal-linker-voice-retry.timer journal-linker-nutrition-summary.timer
systemctl --user list-timers 'journal-linker-daily-reflection*' 'journal-linker-voice-retry*'
```

Requires `JOURNAL_LINKER_REPO` and `journal-linker.env` as in your existing setup.
