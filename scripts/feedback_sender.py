#!/usr/bin/env python3
"""feedback_sender.py — Poll Telegram for intent feedback responses + send due check-ins.

Default one-shot mode supports manual/debug runs. Daemon mode is intended for a
persistent systemd service with Telegram long polling, so button taps are
acknowledged within seconds instead of waiting for a timer.

One-shot mode is two-phase:
  1. Poll getUpdates — process any callback_query taps (confirmed/rejected/deferred),
     write feedback_signal to feedback queue + delivery ledger.
  2. Send due messages — for pending entries where send_after <= now, send a Telegram
     message with inline keyboard, mark state=sent.

Deferred entries are re-queued at +24h (state reset to pending, send_after updated).

Usage:
    python3 scripts/feedback_sender.py
    python3 scripts/feedback_sender.py --daemon
    python3 scripts/feedback_sender.py --state-dir /tmp/test-state --verbose

Env vars:
    TELEGRAM_BOT_TOKEN   required
    TELEGRAM_CHAT_ID     required
    INTENT_STATE_DIR     state dir (default: ~/.local/state/journal-linker/intents)
    INTENT_FEEDBACK_POLL_TIMEOUT    long-poll timeout seconds (default: 25)
    INTENT_FEEDBACK_SEND_INTERVAL   due-message scan interval seconds (default: 60)
    INTENT_FEEDBACK_QUIET_START     quiet-hours start, local hour 0-23 (default: 22)
    INTENT_FEEDBACK_QUIET_END       quiet-hours end, local hour 0-23 (default: 8)
"""

import argparse
import html
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEEDBACK_QUEUE_FILENAME = "intent_feedback_queue.jsonl"
LEDGER_FILENAME = "intent_delivery_ledger.jsonl"
OFFSET_FILENAME = "telegram_update_offset.txt"
FEEDBACK_LEARNING_FILENAME = "intent_feedback_learning.json"
FEEDBACK_TAP_TRACE_FILENAME = "intent_feedback_tap_trace.jsonl"
DEFER_DELAY_SECS = 86400  # 24h re-queue on defer
DEFAULT_DEFER_LIMIT = 3
DEFAULT_MAX_PER_SOURCE_PER_RUN = 1

SURGICAL_EDIT_SYSTEM_PROMPT = (
    "You are making a minimal surgical edit to a markdown journal note. "
    "The user's reply provides a clarification or correction to something the system extracted. "
    "Apply ONLY the correction clearly implied — fix names, fill vague references like 'someone' "
    "if the reply makes them unambiguous. Do not rewrite, expand, or restructure. "
    "If the reply is ambiguous or you are not confident what to change, set changed to false "
    "and return the original text unchanged. "
    'Return JSON only: {"changed": true/false, "text": "<full corrected section text>"}'
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
THIRD_TAP_ACK = "Yes, we now have to record taps."
DUPLICATE_ACKS = [
    "Already recorded.",
    "Got this one already.",
    "Already got it.",
    "Logged the first tap.",
    "This one is already filed.",
    "First answer is still the one.",
    "Already handled.",
    "Yep, already saved.",
    "No change - already recorded.",
    "This tap is just a tap now.",
    "Still recorded from before.",
    "Already in the ledger.",
    "Button enthusiasm noted.",
    "Same answer is still saved.",
    "Nothing changed here.",
    "Already tucked away.",
    "The first tap won.",
    "Duplicate tap observed.",
    "Still counting only the first one.",
    "Already processed.",
    "Recorded earlier.",
    "No new signal from this tap.",
]

_verbose = False


def _log(tag: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}", flush=True)


def _vlog(tag: str, msg: str) -> None:
    if _verbose:
        _log(tag, msg)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _tg_request(token: str, method: str, payload: dict, request_timeout: int = 15) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}: {body[:300]}") from exc


def get_updates(token: str, offset: int, timeout: int = 0) -> list[dict]:
    result = _tg_request(token, "getUpdates", {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["callback_query", "message"],
    }, request_timeout=max(15, timeout + 10))
    return result.get("result", [])


def send_message(token: str, chat_id: str, text: str, reply_markup: dict) -> dict:
    return _tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    })


def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    try:
        _tg_request(token, "answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
        })
    except Exception as exc:
        _vlog("callback", f"answerCallbackQuery failed (stale?): {exc}")


def edit_message_reply_markup(token: str, chat_id: str, message_id: int | str, reply_markup: dict | None = None) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    _tg_request(token, "editMessageReplyMarkup", payload)


def remove_callback_keyboard(token: str, callback_query: dict, entry: dict) -> None:
    """Best-effort removal of old inline buttons after a callback is accepted."""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id") or entry.get("telegram_chat_id", "")
    message_id = message.get("message_id") or entry.get("telegram_message_id", "")
    if not chat_id or not message_id:
        _vlog("callback", "cannot remove keyboard: missing chat_id or message_id")
        return
    try:
        edit_message_reply_markup(token, str(chat_id), message_id)
    except Exception as exc:
        _vlog("callback", f"editMessageReplyMarkup failed: {exc}")


# ---------------------------------------------------------------------------
# Queue + ledger I/O
# ---------------------------------------------------------------------------

def load_feedback_queue(state_dir: Path) -> list[dict]:
    path = state_dir / FEEDBACK_QUEUE_FILENAME
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def save_feedback_queue(state_dir: Path, entries: list[dict]) -> None:
    path = state_dir / FEEDBACK_QUEUE_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_ledger(state_dir: Path) -> dict:
    path = state_dir / LEDGER_FILENAME
    ledger: dict = {}
    if not path.exists():
        return ledger
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            key = record.get("claude_idempotency_key")
            if key:
                ledger[key] = record
        except json.JSONDecodeError:
            pass
    return ledger


def save_ledger(state_dir: Path, ledger: dict) -> None:
    path = state_dir / LEDGER_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            for record in ledger.values():
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_offset(state_dir: Path) -> int:
    path = state_dir / OFFSET_FILENAME
    if path.exists():
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    return 0


def save_offset(state_dir: Path, offset: int) -> None:
    (state_dir / OFFSET_FILENAME).write_text(str(offset), encoding="utf-8")


def load_feedback_learning(state_dir: Path) -> dict:
    path = state_dir / FEEDBACK_LEARNING_FILENAME
    if not path.exists():
        return {"approved": {}, "suppressed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"approved": {}, "suppressed": {}}
        data.setdefault("approved", {})
        data.setdefault("suppressed", {})
        return data
    except Exception:
        return {"approved": {}, "suppressed": {}}


def save_feedback_learning(state_dir: Path, learning: dict) -> None:
    path = state_dir / FEEDBACK_LEARNING_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(learning, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _tap_trace_path(state_dir: Path) -> Path:
    return state_dir / FEEDBACK_TAP_TRACE_FILENAME


def count_tap_traces(state_dir: Path, key: str) -> int:
    path = _tap_trace_path(state_dir)
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("claude_idempotency_key") == key or record.get("key16") == key[:16]:
            count += 1
    return count


def append_tap_trace(state_dir: Path, record: dict) -> None:
    path = _tap_trace_path(state_dir)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record_feedback_learning(learning: dict, entry: dict, signal_label: str, at: str) -> None:
    intent_class = str(entry.get("intent_class") or "unknown")
    phrase = str(entry.get("feedback_prompt") or entry.get("title") or "").strip()
    intent_raw = str(entry.get("intent_raw") or "").strip()
    bucket_name = "approved" if signal_label == "confirmed" else "suppressed"
    if signal_label not in {"confirmed", "rejected"}:
        return

    bucket = learning.setdefault(bucket_name, {}).setdefault(intent_class, {
        "count": 0,
        "examples": [],
        "phrases": {},
    })
    bucket["count"] = int(bucket.get("count", 0)) + 1
    if phrase:
        phrases = bucket.setdefault("phrases", {})
        phrases[phrase] = int(phrases.get(phrase, 0)) + 1
    examples = bucket.setdefault("examples", [])
    examples.append({
        "at": at,
        "title": entry.get("title", ""),
        "phrase": phrase,
        "intent_raw": intent_raw,
        "action": entry.get("action", ""),
    })
    del examples[:-20]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _in_quiet_hours() -> bool:
    start = _env_int("INTENT_FEEDBACK_QUIET_START", 22)
    end = _env_int("INTENT_FEEDBACK_QUIET_END", 8)
    h = datetime.now().hour  # local time
    if start > end:  # wraps midnight
        return h >= start or h < end
    return start <= h < end


def _next_quiet_end() -> datetime:
    end_hour = _env_int("INTENT_FEEDBACK_QUIET_END", 8)
    now_local = datetime.now()
    candidate = now_local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def _cortex_path_for_entry(entry: dict, ledger: dict) -> str | None:
    ikey = entry.get("claude_idempotency_key", "")
    for attempt in ledger.get(ikey, {}).get("delivery_attempts", []):
        cortex = attempt.get("results", {}).get("cortex", {})
        if cortex.get("ok") and cortex.get("path"):
            return cortex["path"]
    return None


def _callback_message_id(callback_query: dict) -> int | str:
    message = callback_query.get("message") or {}
    return message.get("message_id", "")


def _is_duplicate_callback(entry: dict, callback_query: dict) -> bool:
    state = str(entry.get("state") or "")
    if state in {"responded", "expired"}:
        return True
    if state == "pending" and entry.get("feedback_signal"):
        return True
    if state == "sent" and entry.get("feedback_signal"):
        stored_message_id = entry.get("telegram_message_id")
        callback_message_id = _callback_message_id(callback_query)
        if stored_message_id and callback_message_id and str(stored_message_id) != str(callback_message_id):
            return True
    return False


def _duplicate_ack(tap_number: int) -> str:
    if tap_number == 3:
        return THIRD_TAP_ACK
    return DUPLICATE_ACKS[tap_number % len(DUPLICATE_ACKS)]


def _record_tap(
    state_dir: Path | None,
    entry: dict | None,
    callback_query: dict,
    key16: str,
    action: str,
    accepted: bool,
    prior_state: str,
    resulting_state: str,
    prior_signal: str,
    resulting_signal: str,
    acknowledgement: str,
    tap_number: int,
    at: str,
) -> None:
    if state_dir is None:
        return
    message = callback_query.get("message") or {}
    record = {
        "at": at,
        "claude_idempotency_key": entry.get("claude_idempotency_key", "") if entry else "",
        "key16": key16,
        "intent_class": str(entry.get("intent_class") or "") if entry else "",
        "action": action,
        "accepted": accepted,
        "prior_state": prior_state,
        "resulting_state": resulting_state,
        "prior_signal": prior_signal,
        "resulting_signal": resulting_signal,
        "callback_query_id": callback_query.get("id", ""),
        "telegram_message_id": message.get("message_id", entry.get("telegram_message_id", "") if entry else ""),
        "acknowledgement": acknowledgement,
        "tap_number": tap_number,
    }
    append_tap_trace(state_dir, record)


def _button_labels(entry: dict) -> tuple[str, str, str]:
    intent_class = str(entry.get("intent_class") or "")
    category = str(entry.get("category") or "")
    if intent_class == "purchase_intent":
        return ("Got it", "Pass", "Later")
    if intent_class == "latent_interest":
        return ("Noted", "Dismiss", "Later")
    if category == "reminder":
        return ("Done", "Dismiss", "Snooze")
    return ("Done", "Skip", "Later")


def build_feedback_message_text(entry: dict) -> str:
    title = str(entry.get("title", "Intent") or "Intent")
    prompt = str(entry.get("feedback_prompt", title) or title)
    category = str(entry.get("category", "") or "").strip()
    meta = title if not category else f"{title} · {category}"
    return f"{html.escape(prompt)}\n<i>({html.escape(meta)})</i>"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_callback_updates(
    updates: list[dict],
    entries: list[dict],
    ledger: dict,
    token: str,
    state_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    """Apply callback taps to queue entries and ledger. Returns updated (entries, ledger)."""
    learning = load_feedback_learning(state_dir) if state_dir is not None else None
    learning_changed = False
    defer_limit = _env_int("INTENT_FEEDBACK_DEFER_LIMIT", DEFAULT_DEFER_LIMIT)

    for update in updates:
        cq = update.get("callback_query", {})
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        if not data or ":" not in data:
            _vlog("callback", f"ignoring update with no usable callback_data: {data!r}")
            continue

        action, key16 = data.split(":", 1)
        action = action.strip().lower()
        if action not in ("confirm", "reject", "defer"):
            _vlog("callback", f"unknown action {action!r}")
            continue

        # Find matching queue entry
        matched = next(
            (e for e in entries if e.get("claude_idempotency_key", "").startswith(key16)),
            None,
        )
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not matched:
            _vlog("callback", f"no queue entry found for key16={key16!r}")
            ack = "Already processed or not found."
            answer_callback(token, cq_id, ack)
            _record_tap(
                state_dir, None, cq, key16, action, False,
                "", "", "", "", ack, 1, now_iso,
            )
            continue

        ikey = matched["claude_idempotency_key"]
        prior_state = str(matched.get("state", "") or "")
        prior_signal = str(matched.get("feedback_signal", "") or "")
        previous_taps = count_tap_traces(state_dir, ikey) if state_dir is not None else int(matched.get("tap_count") or 0)
        tap_number = previous_taps + 1
        matched["tap_count"] = tap_number

        if _is_duplicate_callback(matched, cq):
            ack = _duplicate_ack(tap_number)
            answer_callback(token, cq_id, ack)
            _record_tap(
                state_dir, matched, cq, key16, action, False,
                prior_state, prior_state, prior_signal, prior_signal, ack, tap_number, now_iso,
            )
            _log("callback", f"duplicate tap key={key16} action={action} tap_number={tap_number}")
            continue

        if action == "defer":
            defer_count = int(matched.get("defer_count") or 0) + 1
            matched["defer_count"] = defer_count
            if defer_count > defer_limit:
                matched["state"] = "expired"
                matched["feedback_signal"] = "expired_defer_limit"
                matched["expires_at"] = now_iso
                matched["telegram_message_id"] = None
                _log("callback", f"expired key={key16} after defer_count={defer_count}")
                signal_label = "expired_defer_limit"
            else:
                new_send_after = (
                    datetime.now(timezone.utc) + timedelta(seconds=DEFER_DELAY_SECS)
                ).isoformat(timespec="seconds")
                matched["state"] = "pending"
                matched["send_after"] = new_send_after
                matched["telegram_message_id"] = None
                matched["feedback_signal"] = "deferred"
                _log("callback", f"deferred key={key16} count={defer_count} new send_after={new_send_after}")
                signal_label = "deferred"
        else:
            signal_label = "confirmed" if action == "confirm" else "rejected"
            matched["state"] = "responded"
            matched["feedback_signal"] = signal_label
            _log("callback", f"{signal_label} key={key16}")
            if learning is not None:
                _record_feedback_learning(learning, matched, signal_label, now_iso)
                learning_changed = True

        # Write feedback_signal to ledger entry (record it even for defer)
        if ikey in ledger:
            ledger[ikey]["feedback_signal"] = signal_label
            ledger[ikey]["feedback_received_at"] = now_iso
            ledger[ikey]["title"] = matched.get("title", ledger[ikey].get("title", ""))
            ledger[ikey]["category"] = matched.get("category", ledger[ikey].get("category", ""))
            ledger[ikey]["urgency"] = matched.get("urgency", ledger[ikey].get("urgency", ""))
            ledger[ikey]["intent_class"] = matched.get("intent_class", ledger[ikey].get("intent_class", ""))
            ledger[ikey]["action"] = matched.get("action", ledger[ikey].get("action", ""))
            ledger[ikey]["defer_count"] = matched.get("defer_count", ledger[ikey].get("defer_count", 0))
        else:
            _vlog("callback", f"ledger entry not found for {ikey[:16]} — signal recorded in queue only")

        ack = {
            "confirmed": "✓ Noted",
            "rejected": "✗ Noted",
            "deferred": "→ Snoozed 24h",
            "expired_defer_limit": "Expired after too many defers",
        }.get(signal_label, "OK")
        answer_callback(token, cq_id, ack)
        remove_callback_keyboard(token, cq, matched)
        _record_tap(
            state_dir, matched, cq, key16, action, True,
            prior_state, str(matched.get("state", "") or ""),
            prior_signal, signal_label, ack, tap_number, now_iso,
        )

    if learning_changed and state_dir is not None and learning is not None:
        save_feedback_learning(state_dir, learning)
    return entries, ledger


def send_due_messages(
    entries: list[dict],
    token: str,
    chat_id: str,
    state_dir: Path | None = None,
) -> list[dict]:
    """Send Telegram messages for pending entries past their send_after time."""
    now = datetime.now(timezone.utc)
    max_per_source = _env_int("INTENT_FEEDBACK_MAX_PER_SOURCE_PER_RUN", DEFAULT_MAX_PER_SOURCE_PER_RUN)
    sent_by_source: dict[str, int] = {}
    ledger = load_ledger(state_dir) if state_dir is not None else {}
    for entry in entries:
        if entry.get("state") != "pending":
            continue
        send_after_str = entry.get("send_after", "")
        try:
            send_after = datetime.fromisoformat(send_after_str)
            if send_after.tzinfo is None:
                send_after = send_after.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            _log("sender", f"invalid send_after {send_after_str!r} — skipping entry")
            continue

        if now < send_after:
            _vlog("sender", f"not yet due: {entry.get('title','?')!r} send_after={send_after_str}")
            continue

        # Suppress if the cortex note was deleted from Obsidian
        cortex_path = _cortex_path_for_entry(entry, ledger)
        if cortex_path and not Path(cortex_path).exists():
            entry["state"] = "expired"
            entry["feedback_signal"] = "cortex_deleted"
            _log("sender", f"cortex deleted, suppressing: {entry.get('title','?')!r}")
            continue

        # Respect quiet hours — reschedule to next quiet-end rather than sending now
        if _in_quiet_hours():
            next_end = _next_quiet_end()
            next_end_utc = next_end.astimezone(timezone.utc).isoformat(timespec="seconds")
            entry["send_after"] = next_end_utc
            _log("sender", f"quiet hours: rescheduled {entry.get('title','?')!r} to {next_end_utc}")
            continue

        source_file = str(entry.get("source_file") or "")
        if source_file and sent_by_source.get(source_file, 0) >= max_per_source:
            _vlog("sender", f"source send cap reached for {source_file!r}")
            continue

        key16 = entry["claude_idempotency_key"][:16]
        title = entry.get("title", "Intent")
        text = build_feedback_message_text(entry)
        confirm_label, reject_label, defer_label = _button_labels(entry)
        reply_markup = {
            "inline_keyboard": [[
                {"text": confirm_label, "callback_data": f"confirm:{key16}"},
                {"text": reject_label,  "callback_data": f"reject:{key16}"},
                {"text": defer_label,   "callback_data": f"defer:{key16}"},
            ]]
        }

        try:
            result = send_message(token, chat_id, text, reply_markup)
            msg_id = result.get("result", {}).get("message_id")
            entry["state"] = "sent"
            entry["telegram_message_id"] = msg_id
            entry["telegram_chat_id"] = chat_id
            if source_file:
                sent_by_source[source_file] = sent_by_source.get(source_file, 0) + 1
            _log("sender", f"sent message_id={msg_id} for {title!r}")
        except Exception as exc:
            _log("sender", f"ERROR sending for {title!r}: {exc}")

    return entries


def _extract_journal_section(source_file: Path, intent_raw: str, context_lines: int = 40) -> str:
    text = source_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    needle = intent_raw[:50].lower()
    for i, line in enumerate(lines):
        if needle in line.lower():
            start = max(0, i - context_lines // 2)
            end = min(len(lines), i + context_lines // 2)
            return "\n".join(lines[start:end])
    return "\n".join(lines[-context_lines:])


def _openai_surgical_edit(api_key: str, journal_section: str, intent_raw: str, user_reply: str) -> dict:
    try:
        import openai  # type: ignore
    except ImportError:
        raise RuntimeError("openai package not available")
    client = openai.OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2048,
        temperature=0,
        messages=[
            {"role": "system", "content": SURGICAL_EDIT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "journal_section": journal_section,
                "intent_raw": intent_raw,
                "user_reply": user_reply,
            }, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    raw = completion.choices[0].message.content or ""
    result = json.loads(raw)
    if not isinstance(result, dict) or "changed" not in result or "text" not in result:
        raise ValueError(f"unexpected response shape: {raw[:200]}")
    return result


def _send_plain(token: str, chat_id: str, text: str) -> None:
    try:
        _tg_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as exc:
        _vlog("clarify", f"sendMessage failed: {exc}")


def _apply_clarification(entry: dict, reply_text: str, token: str, chat_id: str) -> None:
    source_file = entry.get("source_file", "")
    if not source_file:
        return
    path = Path(source_file)
    if not path.exists() or path.suffix != ".md":
        _vlog("clarify", f"source file missing or not .md: {path}")
        return
    if path.stat().st_size > 100_000:
        _vlog("clarify", f"source file too large: {path}")
        return

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        _log("clarify", "OPENAI_API_KEY not set, skipping surgical edit")
        return

    intent_raw = str(entry.get("intent_raw") or entry.get("title") or "")
    section = _extract_journal_section(path, intent_raw)

    try:
        result = _openai_surgical_edit(api_key, section, intent_raw, reply_text)
    except Exception as exc:
        _log("clarify", f"OpenAI call failed: {exc}")
        _send_plain(token, chat_id, "⚠️ Couldn't apply edit right now.")
        return

    if not result.get("changed"):
        _log("clarify", "model returned changed=false")
        _send_plain(token, chat_id, "Nothing to change.")
        return

    new_section = str(result["text"])
    original_text = path.read_text(encoding="utf-8")
    new_text = original_text.replace(section, new_section, 1)
    if new_text == original_text:
        _log("clarify", "section not found verbatim, no edit applied")
        _send_plain(token, chat_id, "✏️ Couldn't locate the exact section to patch.")
        return

    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        _log("clarify", "failed to write updated note")
        _send_plain(token, chat_id, "⚠️ Couldn't write the update.")
        return

    _log("clarify", f"surgical edit applied to {path.name}")
    _send_plain(token, chat_id, "✏️ Updated.")
    entry["clarification_reply"] = reply_text
    entry["clarification_applied"] = True


def process_message_updates(
    updates: list[dict],
    entries: list[dict],
    token: str,
    chat_id: str,
) -> list[dict]:
    for update in updates:
        msg = update.get("message")
        if not msg:
            continue
        reply_to = msg.get("reply_to_message")
        if not reply_to:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        reply_to_id = reply_to.get("message_id")
        matched = next(
            (e for e in entries if e.get("telegram_message_id") == reply_to_id),
            None,
        )
        if not matched:
            _vlog("clarify", f"no queue entry for reply_to_message_id={reply_to_id}")
            continue
        _log("clarify", f"reply to msg_id={reply_to_id}: {text[:80]!r}")
        _apply_clarification(matched, text, token, chat_id)
    return entries


def _process_updates_for_state(
    state_dir: Path,
    token: str,
    updates: list[dict],
    chat_id: str = "",
) -> None:
    if not updates:
        return
    entries = load_feedback_queue(state_dir)
    ledger = load_ledger(state_dir)
    entries, ledger = process_callback_updates(updates, entries, ledger, token, state_dir=state_dir)
    if chat_id:
        entries = process_message_updates(updates, entries, token, chat_id)
    save_ledger(state_dir, ledger)
    save_feedback_queue(state_dir, entries)
    new_offset = max(u["update_id"] for u in updates) + 1
    save_offset(state_dir, new_offset)
    _vlog("updates", f"new offset={new_offset}")


def _send_due_for_state(state_dir: Path, token: str, chat_id: str) -> None:
    entries = load_feedback_queue(state_dir)
    entries = send_due_messages(entries, token, chat_id, state_dir=state_dir)
    save_feedback_queue(state_dir, entries)


def run(state_dir: Path, token: str, chat_id: str, poll_timeout: int = 0) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)

    offset = load_offset(state_dir)
    _vlog("updates", f"polling getUpdates offset={offset}")

    updates = get_updates(token, offset, timeout=poll_timeout)
    _vlog("updates", f"received {len(updates)} updates")

    _process_updates_for_state(state_dir, token, updates, chat_id=chat_id)
    _send_due_for_state(state_dir, token, chat_id)

    return 0


def run_daemon(
    state_dir: Path,
    token: str,
    chat_id: str,
    poll_timeout: int,
    send_interval: int,
) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    _log(
        "daemon",
        f"starting long polling poll_timeout={poll_timeout}s send_interval={send_interval}s state_dir={state_dir}",
    )
    next_send_at = 0.0
    while True:
        try:
            offset = load_offset(state_dir)
            _vlog("updates", f"long polling getUpdates offset={offset} timeout={poll_timeout}")
            updates = get_updates(token, offset, timeout=poll_timeout)
            if updates:
                _log("updates", f"received {len(updates)} update(s)")
                _process_updates_for_state(state_dir, token, updates, chat_id=chat_id)

            now = time.monotonic()
            if now >= next_send_at:
                _send_due_for_state(state_dir, token, chat_id)
                next_send_at = now + send_interval
        except KeyboardInterrupt:
            _log("daemon", "stopping on keyboard interrupt")
            return 0
        except Exception as exc:
            _log("daemon", f"ERROR: {exc}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    global _verbose

    parser = argparse.ArgumentParser(description="Send due intent feedback prompts via Telegram")
    parser.add_argument("--state-dir", help="Override state directory")
    parser.add_argument("--daemon", action="store_true", help="Run forever with Telegram long polling")
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=_env_int("INTENT_FEEDBACK_POLL_TIMEOUT", 25),
        help="Telegram getUpdates long-poll timeout in seconds for --daemon (default: 25)",
    )
    parser.add_argument(
        "--send-interval",
        type=int,
        default=_env_int("INTENT_FEEDBACK_SEND_INTERVAL", 60),
        help="Seconds between due-message scans in --daemon mode (default: 60)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _verbose = args.verbose

    env_path = Path.home() / ".config" / "journal-linker" / "journal-linker.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")

    root_env = Path(__file__).resolve().parents[1] / ".env"
    if root_env.exists():
        for line in root_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        _log("error", "TELEGRAM_BOT_TOKEN is not set")
        return 10
    if not chat_id:
        _log("error", "TELEGRAM_CHAT_ID is not set")
        return 10

    default_state = Path.home() / ".local" / "state" / "journal-linker" / "intents"
    state_dir = Path(args.state_dir) if args.state_dir else Path(
        os.getenv("INTENT_STATE_DIR", str(default_state))
    )

    try:
        if args.daemon:
            return run_daemon(
                state_dir,
                token,
                chat_id,
                poll_timeout=max(1, args.poll_timeout),
                send_interval=max(1, args.send_interval),
            )
        return run(state_dir, token, chat_id)
    except Exception as exc:
        _log("error", f"unhandled exception: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
