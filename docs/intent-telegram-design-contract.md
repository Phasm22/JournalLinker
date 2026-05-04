# Intent pipeline — Telegram design contract

Single-user capture: **every interaction is signal**. This document locks **current behavior** for `feedback_sender.py`. Implementation: [`scripts/feedback_sender.py`](../scripts/feedback_sender.py); ledger / queue writers: [`scripts/process_intents.py`](../scripts/process_intents.py).

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

| Callback | Meaning | Ledger / queue |
|----------|---------|----------------|
| `confirm` | Positive acknowledgement | `feedback_signal=confirmed`, learning ++approved |
| `reject` | Dismiss / not doing | `feedback_signal=rejected`, learning ++suppressed pattern |
| `defer` | Snooze ~24h | `feedback_signal=deferred`, re-queue pending |

Tap trace: `intent_feedback_tap_trace.jsonl` with `interaction=callback`.

### Inbound — emoji reactions (`message_reaction`)

When **`INTENT_REACTION_SIGNALS`** is not off and the merged map is non-empty, `getUpdates` includes `message_reaction`. The **first** emoji in `new_reaction` that appears in the map wins.

- **Built-in map**: wide set of confirm / reject / defer emoji (see `DEFAULT_REACTION_SIGNAL_MAP` in `feedback_sender.py`). Disable with **`INTENT_REACTION_BUILTIN_MAP=off`** and supply only **`INTENT_REACTION_SIGNAL_MAP`** JSON.
- **Semantics**: Same state transitions as inline buttons (including defer cap → `expired_defer_limit`).
- **Audit**: `intent_feedback_reaction_audit.jsonl` records `accepted`, `duplicate`, `unmapped_emoji`, `wrong_chat`, `no_queue_entry`, `reaction_cleared`. Disable with **`INTENT_REACTION_AUDIT_LOG=off`**.
- **Tap trace**: `interaction=reaction`.

Optional **`INTENT_TELEGRAM_REACTION_SPIKE=1`**: additionally append sanitized rows to `intent_feedback_reaction_spike.jsonl` (debug stream; does not change semantics).

### Inbound — text replies (`process_message_updates`)

| Pattern | Behavior |
|---------|----------|
| Reply **threaded** to a check-in message | Surgical journal edit path (OpenAI) when configured; see `_apply_clarification`. |
| Free text, **one** unanswered check-in | Same surgical path for that check-in. |
| Free text, **zero or multiple** unanswered | Short helper reply; no patch. |
| **Reply trace** | `intent_feedback_reply_trace.jsonl` — sanitized `reply_text_preview`, `clarification_applied`, `reply_threaded`. `INTENT_TELEGRAM_REPLY_TRACE=off` disables. |
| **Reply learning** | **`INTENT_REPLY_LEARNING_MODE`** (default **`confirm`**): after clarification, if the entry was still `sent` without `feedback_signal`, apply the same transition as the matching button (`confirm` / `reject` / `defer`), merge ledger + learning, remove keyboard. **`off`** skips this (clarify-only). Tap trace `interaction=reply`. |

## Env reference (Telegram feedback)

| Env | Role |
|-----|------|
| `INTENT_REACTION_SIGNALS` | `off` — disable production reaction handling. |
| `INTENT_REACTION_BUILTIN_MAP` | `off` — omit built-in emoji map. |
| `INTENT_REACTION_SIGNAL_MAP` | JSON `{"emoji":"confirm|reject|defer",...}` — override / extend. |
| `INTENT_REACTION_AUDIT_LOG` | `off` — no `intent_feedback_reaction_audit.jsonl`. |
| `INTENT_REPLY_LEARNING_MODE` | `off` \| `confirm` \| `reject` \| `defer` (default `confirm`). |
| `INTENT_TELEGRAM_REPLY_TRACE` | `off` — no reply trace file. |
| `INTENT_TELEGRAM_REACTION_SPIKE` | `on` — extra spike JSONL stream. |

## Testing Telegram (quick reference)

1. **`scripts/telegram_smoke_test.py`** — `getMe` (+ optional `--send "text"`). Does **not** call `getUpdates`, so it will not conflict with `feedback_sender` long-poll.
2. **`scripts/telegram_live_trial.sh`** — time-boxed `feedback_sender.py --daemon`. Stop `journal-linker-feedback-sender.service` first to avoid HTTP 409.
3. **`pytest tests/test_feedback_sender.py`** — mocked Telegram API (offline CI).

## Live trial (time-boxed daemon)

- Script: [`scripts/telegram_live_trial.sh`](../scripts/telegram_live_trial.sh).
- **`TRIAL_SPIKE=1`** (default) sets `INTENT_TELEGRAM_REACTION_SPIKE=1` for the extra spike log. Production emoji handling stays governed by `INTENT_REACTION_SIGNALS` and the emoji map; use **`INTENT_REACTION_SIGNALS=off`** in the trial environment if you want callbacks + replies only.
- **HTTP 409** — only one `getUpdates` long-poll per bot token. Stop the systemd unit before a second daemon.

## Related

[`TECHNICAL.md`](../TECHNICAL.md) — digest/feedback modes, enrichment, Pushover filters.
