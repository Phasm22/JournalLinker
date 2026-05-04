# systemd (user units) — Linux

Canonical timer + service definitions for journalLinker when **not** using macOS launchd.

## Why these timers look like this

- **`OnCalendar=*-*-* *:MM/15`** — wall-clock every 15 minutes.  
  Do **not** use `OnBootSec=` + `OnUnitActiveSec=` for this poller pattern: on some systemd/user-session setups the timer ends up with **`NextElapseUSecMonotonic=infinity`** and never schedules real **`NEXT`** times.

## Stagger

| Unit | Schedule | Purpose |
|------|----------|---------|
| `journal-linker-daily-reflection.timer` | minutes **:02, :17, :32, :47** | `daily_reflection.sh` — script decides whether to send |
| `journal-linker-voice-retry.timer` | minutes **:09, :24, :39, :54** | `voice_retry.sh` — retries transient voice failures |

Seven minutes after each daily-reflection tick starts a voice-retry cycle, so Whisper / disk are less likely to pile onto the same moment as reflection.

## Install or refresh

```bash
REPO=/path/to/journalLinker   # e.g. ~/journalLinker
install -d -m 755 ~/.config/systemd/user
cp "$REPO/systemd/journal-linker-daily-reflection.service" \
   "$REPO/systemd/journal-linker-daily-reflection.timer" \
   "$REPO/systemd/journal-linker-voice-retry.service" \
   "$REPO/systemd/journal-linker-voice-retry.timer" \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now journal-linker-daily-reflection.timer journal-linker-voice-retry.timer
systemctl --user list-timers 'journal-linker-daily-reflection*' 'journal-linker-voice-retry*'
```

Requires `JOURNAL_LINKER_REPO` and `journal-linker.env` as in your existing setup.
