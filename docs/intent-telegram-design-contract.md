# Intent pipeline — Telegram design contract

Single-user capture: **every interaction is signal**. This document locks **current behavior**, **target behavior** (Sprint 3+), and **open decisions**. Implementation lives in [`scripts/feedback_sender.py`](../scripts/feedback_sender.py) and callers in [`scripts/process_intents.py`](../scripts/process_intents.py).

## Surfaces

| Surface | Transport | Purpose |
|---------|-----------|---------|
| Cortex notes | Obsidian markdown | Durable capture (primary product). |
| Feedback queue JSONL | Local disk | Pending/sent Telegram check-in rows. |
| Delivery ledger JSONL | Local disk | Idempotent delivery + `feedback_signal` echo. |
| Learning JSON | `intent_feedback_learning.json` | Approved/rejected tallies + suppression patterns (routing hints). |

## Current behavior (as shipped)

### Outbound check-in

- **Trigger**: Router emits non-empty `feedback_prompt` and `urgency != low`, and `INTENT_FEEDBACK_MODE` is not off (`process_intents`).
- **Payload**: HTML message body = `feedback_prompt` (title not shown separately in Telegram body).
- **Inline keyboard**: Always three callbacks — `confirm` / `reject` / `defer`, displayed labels from `_button_labels()` (intent_class + category).

### Inbound — inline buttons

| Callback | Meaning today | Ledger / queue |
|----------|----------------|----------------|
| `confirm` | Positive acknowledgement | `feedback_signal=confirmed`, learning ++approved |
| `reject` | Dismiss / not doing | `feedback_signal=rejected`, learning ++suppressed pattern |
| `defer` | Snooze ~24h | `feedback_signal=deferred`, re-queue pending |

### Inbound — text replies (`process_message_updates`)

| Pattern | Behavior today |
|---------|----------------|
| Reply **threaded** to a check-in message | Text routed to **surgical journal edit** (OpenAI) against source daily note — **not** written to `intent_feedback_learning` as confirm/reject. |
| Free text, **one** unanswered check-in | Same surgical path for that check-in. |
| Free text, **zero or multiple** unanswered | Short helper reply; no patch. |
| **Reply trace** (Sprint 3) | After each clarifying path, one JSON line in `intent_feedback_reply_trace.jsonl` (key, reply preview, `clarification_applied`, `reply_threaded`). Set `INTENT_TELEGRAM_REPLY_TRACE=off` to disable. Does not yet increment button-style learning. |

### Spike only (optional)

| Env | Behavior |
|-----|----------|
| `INTENT_TELEGRAM_REACTION_SPIKE=1` | `getUpdates` subscribes to `message_reaction`; raw events append to `intent_feedback_reaction_spike.jsonl` for inspection. **No** ledger or learning mutation. |

## Target behavior (Sprint 3 — not all implemented yet)

These are **design intents**; track implementation in PRs/issues.

1. **Reply as signal**: Optionally record structured engagement (e.g. “ack only” vs “edit note”) and feed **learning** without forcing every reply to be a surgical edit.
2. **Reactions**: Map chosen emoji → discrete signals (`confirmed` / `dismiss` / `defer`) once Bot API events are trusted in your chat type.
3. **Prompt ↔ control alignment**: Either constrain `feedback_prompt` to match button semantics, or replace buttons with reply/reaction-only flows for non-binary prompts.
4. **Design contract tests**: Unit tests that assert handler → ledger field mapping for each inbound type.

## Open decisions (resolve before Sprint 3 coding)

- **Emoji map** for reactions (👍/👎/⏰ etc.) vs intent_class.
- Whether surgical edit remains **opt-in** (prefix/command) vs default on reply.
- Supergroups vs private chat: reaction availability differs; document your chat id mode.

## Testing Telegram (quick reference)

1. **`scripts/telegram_smoke_test.py`** — `getMe` (+ optional `--send "text"`). Does **not** call `getUpdates`, so it will not conflict with `feedback_sender` long-poll.
2. **`scripts/telegram_live_trial.sh`** — time-boxed `feedback_sender.py --daemon` for live buttons/replies/reactions. Stop `journal-linker-feedback-sender.service` first to avoid HTTP 409.
3. **`pytest tests/test_feedback_sender.py`** — mocked Telegram API (offline CI).

## Live trial (time-boxed daemon)

Use a **deadline or duration** instead of leaving the daemon running indefinitely while you experiment.

- Script: [`scripts/telegram_live_trial.sh`](../scripts/telegram_live_trial.sh) wraps `timeout` + `feedback_sender.py --daemon`.
- Prereq: load Telegram vars first, e.g. `set -a && source ~/.config/journal-linker/journal-linker.env && set +a`.
- Examples:
  - `./scripts/telegram_live_trial.sh --minutes 45`
  - `./scripts/telegram_live_trial.sh --until "2026-05-04 22:00:00"`
  - `TRIAL_SPIKE=0 ./scripts/telegram_live_trial.sh --minutes 30` — daemon only, no `message_reaction` subscription.

Default **`TRIAL_SPIKE=1`** turns on `INTENT_TELEGRAM_REACTION_SPIKE` so reactions append to `intent_feedback_reaction_spike.jsonl` for inspection.

**HTTP 409 Conflict (`getUpdates`)** — Telegram allows **only one** active long-poll client per bot token. If `journal-linker-feedback-sender.service` (or another `feedback_sender.py --daemon`) is running, stop it before a trial:

`systemctl --user stop journal-linker-feedback-sender.service`

The trial script refuses to start if that unit is active or another `feedback_sender.py` is running (override with `TRIAL_SKIP_CONFLICT_CHECK=1` only if you know what you are doing).

**Container?** Usually unnecessary for this solo bot: the trial script + env file give an isolated *time window* without Docker overhead. Use a container only if you want a throwaway token/chat pair.

## Related env vars

See [`TECHNICAL.md`](../TECHNICAL.md): `INTENT_FEEDBACK_MODE`, `INTENT_DIGEST_MODE`, Telegram pressure knobs, and `INTENT_TELEGRAM_REACTION_SPIKE`.
