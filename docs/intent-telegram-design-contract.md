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

## Related env vars

See [`TECHNICAL.md`](../TECHNICAL.md): `INTENT_FEEDBACK_MODE`, `INTENT_DIGEST_MODE`, Telegram pressure knobs, and `INTENT_TELEGRAM_REACTION_SPIKE`.
