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
"""

import argparse
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
DEFER_DELAY_SECS = 86400  # 24h re-queue on defer
DEFAULT_DEFER_LIMIT = 3
DEFAULT_MAX_PER_SOURCE_PER_RUN = 1

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

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
        "allowed_updates": ["callback_query"],
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
        if not matched:
            _vlog("callback", f"no queue entry found for key16={key16!r}")
            answer_callback(token, cq_id, "Already processed or not found.")
            continue

        ikey = matched["claude_idempotency_key"]
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

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

        answer_callback(token, cq_id, {
            "confirmed": "✓ Noted",
            "rejected": "✗ Noted",
            "deferred": "→ Snoozed 24h",
            "expired_defer_limit": "Expired after too many defers",
        }.get(signal_label, "OK"))

    if learning_changed and state_dir is not None and learning is not None:
        save_feedback_learning(state_dir, learning)
    return entries, ledger


def send_due_messages(
    entries: list[dict],
    token: str,
    chat_id: str,
) -> list[dict]:
    """Send Telegram messages for pending entries past their send_after time."""
    now = datetime.now(timezone.utc)
    max_per_source = _env_int("INTENT_FEEDBACK_MAX_PER_SOURCE_PER_RUN", DEFAULT_MAX_PER_SOURCE_PER_RUN)
    sent_by_source: dict[str, int] = {}
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

        source_file = str(entry.get("source_file") or "")
        if source_file and sent_by_source.get(source_file, 0) >= max_per_source:
            _vlog("sender", f"source send cap reached for {source_file!r}")
            continue

        key16 = entry["claude_idempotency_key"][:16]
        title = entry.get("title", "Intent")
        prompt = entry.get("feedback_prompt", title)
        category = entry.get("category", "")
        intent_class = entry.get("intent_class", "")

        text = (
            f"📋 <b>Intent check-in</b>\n\n"
            f"{prompt}\n"
            f"<i>({title} · {category}"
            f"{' · ' + intent_class if intent_class else ''})</i>"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "Done",  "callback_data": f"confirm:{key16}"},
                {"text": "Skip",  "callback_data": f"reject:{key16}"},
                {"text": "Later", "callback_data": f"defer:{key16}"},
            ]]
        }

        try:
            result = send_message(token, chat_id, text, reply_markup)
            msg_id = result.get("result", {}).get("message_id")
            entry["state"] = "sent"
            entry["telegram_message_id"] = msg_id
            if source_file:
                sent_by_source[source_file] = sent_by_source.get(source_file, 0) + 1
            _log("sender", f"sent message_id={msg_id} for {title!r}")
        except Exception as exc:
            _log("sender", f"ERROR sending for {title!r}: {exc}")

    return entries


def _process_updates_for_state(
    state_dir: Path,
    token: str,
    updates: list[dict],
) -> None:
    if not updates:
        return
    entries = load_feedback_queue(state_dir)
    ledger = load_ledger(state_dir)
    entries, ledger = process_callback_updates(updates, entries, ledger, token, state_dir=state_dir)
    save_ledger(state_dir, ledger)
    save_feedback_queue(state_dir, entries)
    new_offset = max(u["update_id"] for u in updates) + 1
    save_offset(state_dir, new_offset)
    _vlog("updates", f"new offset={new_offset}")


def _send_due_for_state(state_dir: Path, token: str, chat_id: str) -> None:
    entries = load_feedback_queue(state_dir)
    entries = send_due_messages(entries, token, chat_id)
    save_feedback_queue(state_dir, entries)


def run(state_dir: Path, token: str, chat_id: str, poll_timeout: int = 0) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)

    offset = load_offset(state_dir)
    _vlog("updates", f"polling getUpdates offset={offset}")

    updates = get_updates(token, offset, timeout=poll_timeout)
    _vlog("updates", f"received {len(updates)} updates")

    _process_updates_for_state(state_dir, token, updates)
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
                _process_updates_for_state(state_dir, token, updates)

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
