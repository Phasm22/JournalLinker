# Service Intake Flow — journalLinker

This intake process determines whether a service is ready to be monitored. The goal is to define, per service, what healthy looks like **before** any dashboard or alerting logic is built. Outputs feed directly into the service registry and log contract — nothing gets defined in the monitoring app that was not answered here first.

**Scope:** Scheduled and supervised jobs in this repo (`scripts/*.sh`, `systemd/`, `launchd/`). Manual-only CLIs are listed under [Deferred](#deferred--manual-only).

**Log roots (platform-dependent):**

| Platform | Default `SCRIBE_JOB_LOG_DIR` | Fallback |
|----------|------------------------------|----------|
| macOS (launchd) | `~/Library/Logs/JournalLinker/` | `/tmp/journal-linker-logs` |
| Linux (systemd) | `~/.local/state/journal-linker/` | `/tmp/journal-linker-logs` |

**Audits and monitoring** must use `$SCRIBE_JOB_LOG_DIR` from `~/.config/journal-linker/journal-linker.env`, or `journalctl --user -u 'journal-linker-*'`. Do **not** treat `~/Library/Logs/JournalLinker/` (macOS-only) or `~/.local/state/journal-linker/logs/` (stale subdir from an older default) as the live Linux log root.

**Telemetry gaps (post-deploy):** `scribe` and `intent-watcher` need one run after telemetry landed to produce `JOURNAL_LINKER_EVENT=` in file logs; timers are active if journald shows May-dated runs.

**Shared wrapper contract** (all `scripts/*_*.sh` job wrappers):

- Timestamped log: `{service}-{YYYYMMDD-HHMMSS-PID}.log`
- Symlink: `{service}-latest.log` → that run
- Header/footer: `start`, `end`, `duration_sec`, `exit_code`, `log_file`
- Overlap: `mkdir` lock per service family; concurrent run exits **0** with skip message
- Stale lock after crash: `rmdir` the lock dir documented per service

---

## Service registry (summary)

| Service ID | Unit / label | Trigger | Gate | Max silence |
|------------|--------------|---------|------|-------------|
| `scribe` | `journal-linker-scribe.timer` / `com.journal-linker.scribe` | Calendar 03:17 local | Pass | 36 h |
| `voice-watcher` | `journal-linker-voice-watcher.path` or `voice-poll.timer` / `com.journal-linker.voice` | PathChanged / 2 min poll | Pass | 24 h (if recordings expected) |
| `voice-retry` | `journal-linker-voice-retry.timer` / `com.journal-linker.voice-retry` | Every 15 min (:09/:24/:39/:54) | Pass | 6 h (when transient backlog) |
| `daily-reflection` | `journal-linker-daily-reflection.timer` / `com.journal-linker.daily-reflection` | Every 15 min (:02/:17/:32/:47) | Pass | 26 h (missed send day) |
| `intent-watcher` | `journal-linker-intent-watcher.path` / `com.journal-linker.intent` | PathChanged on journal dir | Pass | N/A (event-driven) |
| `intent-retry` | `journal-linker-intent-retry.timer` | Every 15 min (boot+active) | Pass | 2 h (stuck transient queue) |
| `feedback-sender` | `journal-linker-feedback-sender.service` | Continuous (long-poll) | Pass | 5 min (daemon down) |
| `weekly-insights` | — | Manual (`just weekly`) | **Deferred** | — |
| `archivist` | — | Manual | **Deferred** | — |

---

## Stage 1 — Gate (pass all 4 or stop)

| # | Criterion | How to apply here |
|---|-----------|-------------------|
| 1 | Defined trigger (schedule / file event / continuous) | See registry |
| 2 | Observable success | See per-service Stage 2 |
| 3 | Structured output (or small patch) | Wrapper logs + Python stderr; intent has stable exit codes |
| 4 | Maximum acceptable silence | See registry column |

---

## `scribe` — nightly wikilink write-back

**Stage 1:** Pass (all four).

| Gate question | Answer |
|---------------|--------|
| Trigger? | **Schedule** — `OnCalendar=*-*-* 03:17:00` (systemd) / launchd `StartCalendarInterval` 03:17 |
| Observable success? | Exit 0; journal note updated (`--write-back`); learning store touched; log ends with `=== done ===` |
| Structured output? | **Yes** — wrapper envelope + Scribe stdout/stderr in log file |
| Max silence? | **36 hours** — one missed nightly is warning; two consecutive is alert |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Runs local Ollama over the active daily note and writes `[[wikilinks]]` plus Yesterday/Tomorrow nav back to disk.
- **Triggers:** `scripts/scheduled_run.sh` via timer/launchd; manual: `just scribe-job`, `just scribe-writeback`.

**Execution**

- **Frequency / silence:** Once per calendar day ~03:17 local; silence window 36 h.
- **Max duration (hung):** **45 min** — typical run is minutes; Ollama stall or huge note.

**Health**

- **Success:** Exit 0; log shows Scribe completion; target `YYYY-MM-DD.md` mtime advances when content changed.
- **Silent failure risk?** **Low** — non-zero exit or empty write-back with errors in log; lock skip exits 0 but prints skip (not a failed Scribe run).
- **Failure (outside):** Non-zero exit; Ollama connection errors in log; stale `.scribe-job.lock` (skip loop).

**Alerting**

- **Consecutive failures:** Alert on **2** consecutive non-zero exits (not lock-skip).
- **Missed window:** **Warning** at 36 h without a successful run log; **alert** at 48 h.
- **Downstream:** Stale wikilinks and nav until next success; voice pipeline may still append raw callouts.

**Logging**

- **Where / format:** `$SCRIBE_JOB_LOG_DIR/scribe-*.log` (see table above); text, header + tee’d stdout.
- **Tells what happened?** **Mostly** — exit code and duration yes; link count requires reading Scribe output lines.
- **Broken today:** Check `scribe-latest.log`, `just doctor`, Ollama up, `SCRIBE_JOURNAL_DIR` set.

**Dashboard**

- **At a glance:** Last success time, last exit code, hours since last success.

**Exit codes (Scribe.py):** `0` ok, `1` runtime error, `2` usage/config.

**Lock:** `$LOG_DIR/.scribe-job.lock`

---

## `voice-watcher` — VoiceDrop → journal callout

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **File event** — `PathChanged` on VoiceDrop (systemd path unit or launchd `WatchPaths`); optional **poll** `journal-linker-voice-poll.timer` every 2 min if inotify/sync is flaky |
| Observable success? | New `.m4a` → `.m4a.processed` sidecar; voice callout in daily note; log `[voice] marked processed` |
| Structured output? | **Yes** — wrapper log + `[voice]` stderr lines |
| Max silence? | **24 h** only when user expects recordings; otherwise N/A (event-driven) |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Transcribes new voice memos with faster-whisper, appends a `> [!voice]` block to the journal, then runs Scribe write-back for that date.
- **Triggers:** Folder change; `scripts/voice_watcher.sh` → `process_voice.py` (scan mode with `--retry` so new files and transient failures are both eligible).

**Execution**

- **Frequency:** On each new/synced recording (bursty).
- **Max duration (hung):** **20 min** per recording (Whisper + Scribe); multi-file scan proportionally longer.

**Health**

- **Success:** `.processed` marker; non-empty transcript; Scribe exit 0 inside pipeline.
- **Silent failure risk?** **Medium** — iCloud placeholder skipped (retries later); empty transcript → permanent `.failed`; watcher can exit 0 with "0 to process".
- **Failure (outside):** `.m4a.failed` with reason; permanent vs transient in marker; `just voice-doctor` counts pending/failed.

**Alerting**

- **Consecutive failures:** Alert if **same file** permanent-fails or **3+** transient failures in 24 h.
- **Missed window:** **Warning** if pending count > 0 for **> 2 h** after file appeared locally.
- **Downstream:** No voice text in journal; Scribe not run for that entry.

**Logging**

- **Where:** `$SCRIBE_JOB_LOG_DIR/voice-*.log`, `voice-latest.log`.
- **Tells what happened?** **Yes** — per-file processing, transcript word count, failure kind.
- **Broken today:** `just voice-doctor`, latest log, VoiceDrop path, faster-whisper installed.

**Dashboard**

- **At a glance:** Pending / failed / processed counts; age of oldest pending file.

**Exit codes:** `0` success or nothing to do; `1` errors (config, processing failures in batch).

**Lock:** `$LOG_DIR/.voice-job.lock` (shared with voice-retry)

---

## `voice-retry` — transient voice failures

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **Schedule** — every 15 min at :09, :24, :39, :54 |
| Observable success? | Transient `.failed` cleared → `.processed` or new failure reason in log |
| Structured output? | **Yes** — same as voice-watcher |
| Max silence? | **6 h** while transient backlog exists; N/A when queue empty |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Re-attempts voice files marked `kind: transient` (Ollama down, I/O) without touching permanent failures.
- **Triggers:** `scripts/voice_retry.sh` → `process_voice.py --retry`.

**Execution**

- **Frequency:** Every 15 min; shares lock with voice-watcher.
- **Max duration:** **20 min** per run (same as watcher).

**Health**

- **Success:** Transient queue drains; exit 0.
- **Silent failure risk?** **Low** when queue empty (exit 0, short log); **medium** if Ollama stays down (repeated transient).
- **Failure:** Growing transient backlog; permanent failures only visible via `voice-doctor`.

**Alerting**

- **Consecutive:** Alert if transient backlog **unchanged** after **6** retry ticks (~90 min).
- **Missed window:** N/A when no transient files.
- **Downstream:** Same as voice-watcher.

**Logging**

- **Where:** `voice-retry-*.log`, `voice-retry-latest.log`.
- **Broken today:** `just voice-doctor`, compare pending transient list to log.

**Dashboard**

- **At a glance:** Count of transient-failed files; oldest transient failure age.

---

## `daily-reflection` — day-behind Pushover reflection

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **Schedule** — poller every 15 min; script picks one random send instant inside window |
| Observable success? | One Pushover per reflected day; state file records send; log shows send or explicit skip |
| Structured output? | **Yes** — wrapper log; optional `daily_reflection_state.json` |
| Max silence? | **26 h** after end of send window without send or documented skip |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Once per day, sends a short Pushover reflection on yesterday's journal entry using Ollama + learning store context.
- **Triggers:** `scripts/daily_reflection.sh` → `daily_reflection.py`; window default **16:00–21:00** local (`DAILY_REFLECTION_WINDOW_START/END`).

**Execution**

- **Frequency:** Poll every 15 min; **at most one send** per calendar day.
- **Max duration:** **10 min** per tick (Ollama + HTTP).

**Health**

- **Success:** Pushover API 200; state updated; or **clean skip** (thin/missing yesterday note, outside window, already sent) with exit 0.
- **Silent failure risk?** **Medium** — skip paths look healthy; missed window if poller never runs.
- **Failure:** Exit 1 (send/API/model); exit 2 (config).

**Alerting**

- **Consecutive:** Alert on **2** failed send attempts on the chosen send day (not skip).
- **Missed window:** **Alert** if no send and no logged skip by **26 h** after window end.
- **Downstream:** User misses reflection notification only.

**Logging**

- **Where:** `daily-reflection-*.log`, `daily-reflection-latest.log`.
- **Broken today:** Latest log, `daily_reflection_state.json`, Pushover creds, `just daily-reflection` dry-run.

**Dashboard**

- **At a glance:** Last send date; today's status (sent / skipped / pending window / failed).

**Lock:** `$LOG_DIR/.daily-reflection.lock`

---

## `intent-watcher` — journal change → intent pipeline

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **File event** — `PathChanged` on `SCRIBE_JOURNAL_DIR` |
| Observable success? | `process_intents.py` exit 0/0-skip; ledger/delivery updates under `INTENT_STATE_DIR` |
| Structured output? | **Yes** — wrapper log + stable exit codes (see below) |
| Max silence? | **N/A** (event-driven); rely on intent-retry for recovery |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** On journal edits, runs gate (local Ollama) → route (OpenAI) → deliver (Pushover, cortex, digest) for actionable intents.
- **Triggers:** `scripts/intent_watcher.sh`; processes note modified in last **10 min** or `INTENT_NOTE_FILE`.

**Execution**

- **Frequency:** Bursty on save; may exit 0 with "no recently modified note" (timer handles retry).
- **Max duration:** **15 min** per note (gate + routing + delivery).

**Health**

- **Success:** Exit 0 (including no intents / already delivered); ledger rows `succeeded`.
- **Silent failure risk?** **Medium** — path fires but note older than 10 min → watcher no-ops; intent-retry must catch.
- **Failure:** Exit 10 permanent; 20/30/40 transient; 50 partial.

**Alerting**

- **Consecutive:** Alert on **2** permanent (10) failures for same note fingerprint; transient: alert if retry queue age **> 2 h**.
- **Missed window:** N/A.
- **Downstream:** No push/cortex for captured intents; user may not see reminders.

**Logging**

- **Where:** `intent-*.log`, `intent-latest.log` (Linux default state dir for intents: `~/.local/state/journal-linker/intents/`).
- **Broken today:** `intent-latest.log`, `just handoff-check`, exit code, ledger tail.

**Dashboard**

- **At a glance:** Last run exit code; retry queue depth; last delivery status.

**Exit codes:** `0` ok/skip, `10` permanent, `20` gate, `30` routing, `40` delivery, `50` partial.

**Lock:** `$LOG_DIR/.intent-job.lock` (shared with intent-retry)

---

## `intent-retry` — transient intent delivery

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **Schedule** — every 15 min (`OnBootSec=5min` + `OnUnitActiveSec=15min`) |
| Observable success? | Retry queue drains; exit 0 |
| Structured output? | **Yes** |
| Max silence? | **2 h** with non-empty transient retry queue |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Replays `process_intents.py --retry` for transient gate/routing/delivery failures.
- **Triggers:** `scripts/intent_retry.sh`.

**Execution**

- **Frequency:** Every 15 min; shares intent lock.
- **Max duration:** **15 min**.

**Health**

- **Success:** Queue empty or entries marked succeeded; worst exit 0.
- **Silent failure risk?** **Low** if timer enabled; **high** if only path watcher and timer off.
- **Failure:** Stuck exit 20/30/40/50; growing ledger in-flight rows.

**Alerting**

- **Consecutive:** Alert after **8** failed retry ticks (~2 h) with same queue head.
- **Downstream:** Same as intent-watcher.

**Logging**

- **Where:** `intent-retry-*.log`, `intent-retry-latest.log`.

**Dashboard**

- **At a glance:** Retry queue size; oldest queued intent age; last worst exit code.

---

## `feedback-sender` — Telegram feedback long-poll

**Stage 1:** Pass.

| Gate question | Answer |
|---------------|--------|
| Trigger? | **Continuous** — systemd `Type=simple` `Restart=always`; `feedback_sender.py --daemon` |
| Observable success? | Process running; `getUpdates` loop active; callbacks written to ledger |
| Structured output? | **Yes** — `health.probe` while daemon runs; `job.completed` on process exit |
| Max silence? | **5 min** process down (RestartSec=10 should recover sooner) |

### Stage 2 — Questionnaire

**Identity**

- **One sentence:** Long-polls Telegram for intent feedback button presses and sends scheduled check-in messages.
- **Triggers:** `scripts/feedback_sender.sh --daemon`; requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

**Execution**

- **Frequency:** Continuous; restarts every 10 s on failure.
- **Max duration:** N/A (long-lived); **stuck** if no `health.probe` line for **> 30 min** (default emit every 300 s via `INTENT_FEEDBACK_HEARTBEAT_SEC`).

**Health**

- **Success:** `systemctl --user is-active journal-linker-feedback-sender`; `just telegram-doctor` OK; only one poller per token (HTTP 409 if duplicated).
- **Silent failure risk?** **High** without systemd — daemon exit stops feedback until restart.
- **Failure:** Crash loop in journal; 409 conflict; invalid token.

**Alerting**

- **Consecutive:** Alert if service **inactive** for **> 5 min**.
- **Missed window:** Warning if unanswered sent check-ins exceed `INTENT_FEEDBACK_MAX_UNANSWERED` (see `just handoff-check`).
- **Downstream:** No feedback capture; suppression learning stale.

**Logging**

- **Where:** `feedback-sender-*.log` per process start; `feedback-sender-latest.log`.
- **Broken today:** `systemctl --user status`, latest log, `just telegram-doctor`, ensure single instance.

**Dashboard**

- **At a glance:** Active/inactive; restart count in last hour; unanswered check-in count.

---

## Deferred — manual only

### `weekly-insights`

| Gate | Status |
|------|--------|
| 1 Trigger | **No** — manual / external cron not defined in repo |
| 2–4 | Not evaluated until scheduled |

**Blockers:** Add timer or document external schedule; define success as `Insights/Weekly Insight - YYYY-Www.md` mtime for target week.

**When enabled:** Weekly after ISO week close; silence **8 days**; success = note written or logged skip (sparse week).

---

### `archivist`

| Gate | Status |
|------|--------|
| All | **Deferred** — interactive clipboard helper, no supervisor |

---

## External dependencies (not owned by this repo)

Monitor separately; journalLinker degrades without them:

| Dependency | Health check | Affects |
|------------|----------------|---------|
| Ollama | Model pull; API reachable | scribe, intent gate, daily-reflection, voice→Scribe |
| OpenAI API | Key valid | intent routing |
| Pushover | `pnotify` / API | daily-reflection, intent delivery |
| llmLibrarian MCP | `just status` → `/healthz` | intent enrichment (graceful off) |
| faster-whisper | `just voice-doctor` | voice pipeline |
| Dropbox/iCloud sync | File fully local | voice-watcher placeholders |

---

## Log contract (registry fields)

Every supervised job log includes a **machine-readable event** immediately before `=== done ===`:

```text
JOURNAL_LINKER_EVENT={"event":"job.completed","service":"scribe",...}
```

Grep pattern: `^JOURNAL_LINKER_EVENT=`

Long-running **`feedback-sender`** also emits periodic probes while the daemon runs:

```text
JOURNAL_LINKER_EVENT={"event":"health.probe","service":"feedback-sender",...}
```

Implementation: [`journal_linker_telemetry.py`](journal_linker_telemetry.py) (emit/finalize), [`scripts/job_log_lib.sh`](scripts/job_log_lib.sh) (wrapper finalize), per-entrypoint `write_job_payload` via `JOURNAL_LINKER_JOB_PAYLOAD_FILE`.

### `job.completed` (required fields)

| Field | Source |
|-------|--------|
| `event` | Always `job.completed` |
| `service` | Wrapper (`scribe`, `voice`, `intent-watcher`, …) |
| `run_id` | `YYYYMMDD-HHMMSS-PID` from wrapper |
| `exit_code` | Python exit (wrapper finalize) |
| `duration_sec` | Wrapper measured |
| `skipped` | `true` on lock contention or shell-only skip |
| `skip_reason` | e.g. `lock_held`, `no_recent_note` |

Human-readable header/footer (`start`, `end`, `duration_sec`, `exit_code` text lines) remain for tailing.

### `job.completed` service extensions

| Service | Extra JSON fields |
|---------|-------------------|
| `scribe` | `active_file`, `write_back`, `links_added`, `ollama_sec` |
| `daily-reflection` | `outcome` (`sent` \| `skipped` \| `failed` \| `dry_run`), `reason`, `reflection_date`, `target_send_at`, `sent` |
| `voice`, `voice-retry` | `items_processed`, `items_failed`, `items_skipped_placeholder`, `items_skipped_already`, `items_pending_at_start` |
| `intent-watcher`, `intent-retry` | `intents_gated`, `intents_delivered`, `intents_suppressed`, `intents_failed`, `retry_queue_in`, `retry_queue_out`, `worst_exit_code` |
| `feedback-sender` (one-shot) | `updates_received`, `feedback_queue_pending`, `poll_offset` |

### `health.probe` (feedback-sender daemon)

| Field | Meaning |
|-------|---------|
| `ts` | ISO timestamp |
| `uptime_sec` | Monotonic uptime |
| `poll_offset` | Telegram `getUpdates` offset |
| `updates_last_cycle` | Updates handled since previous probe |
| `feedback_queue_pending` | Queue rows in `pending` state |

Default interval: **300 s** (`INTENT_FEEDBACK_HEARTBEAT_SEC`).

---

## Intent exit code reference (machine contract)

| Code | Meaning | Retry? |
|------|---------|--------|
| 0 | Success or clean skip | No |
| 10 | Permanent | No |
| 20 | Gate (Ollama) transient | Yes |
| 30 | Routing transient | Yes |
| 40 | Delivery transient | Yes |
| 50 | Partial delivery | Yes (delivery only) |

---

*Generated from repo state: `systemd/`, `launchd/`, `scripts/*_*.sh`, and entrypoint docstrings. Update this file when triggers, windows, or exit contracts change.*
