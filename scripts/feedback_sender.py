#!/usr/bin/env python3
"""feedback_sender.py — Poll Telegram for intent feedback responses + send due check-ins.

Run on a timer (every 30 min). Two-phase each run:
  1. Poll getUpdates — process any callback_query taps (confirmed/rejected/deferred),
     write feedback_signal to feedback queue + delivery ledger.
  2. Send due messages — for pending entries where send_after <= now, send a Telegram
     message with inline keyboard, mark state=sent.

Deferred entries are re-queued at +24h (state reset to pending, send_after updated).

Usage:
    python3 scripts/feedback_sender.py
    python3 scripts/feedback_sender.py --state-dir /tmp/test-state --verbose

Env vars:
    TELEGRAM_BOT_TOKEN   required
    TELEGRAM_CHAT_ID     required
    INTENT_STATE_DIR     state dir (default: ~/.local/state/journal-linker/intents)
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEEDBACK_QUEUE_FILENAME = "intent_feedback_queue.jsonl"
LEDGER_FILENAME = "intent_delivery_ledger.jsonl"
OFFSET_FILENAME = "telegram_update_offset.txt"
DEFER_DELAY_SECS = 86400  # 24h re-queue on defer

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

def _tg_request(token: str, method: str, payload: dict) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}: {body[:300]}") from exc


def get_updates(token: str, offset: int) -> list[dict]:
    result = _tg_request(token, "getUpdates", {
        "offset": offset,
        "timeout": 0,
        "allowed_updates": ["callback_query"],
    })
    return result.get("result", [])


def send_message(token: str, chat_id: str, text: str, reply_markup: dict) -> dict:
    return _tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    })


def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    _tg_request(token, "answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


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


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_callback_updates(
    updates: list[dict],
    entries: list[dict],
    ledger: dict,
    token: str,
) -> tuple[list[dict], dict]:
    """Apply callback taps to queue entries and ledger. Returns updated (entries, ledger)."""
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
            new_send_after = (
                datetime.now(timezone.utc) + timedelta(seconds=DEFER_DELAY_SECS)
            ).isoformat(timespec="seconds")
            matched["state"] = "pending"
            matched["send_after"] = new_send_after
            matched["telegram_message_id"] = None
            _log("callback", f"deferred key={key16} new send_after={new_send_after}")
            signal_label = "deferred"
        else:
            signal_label = "confirmed" if action == "confirm" else "rejected"
            matched["state"] = "responded"
            matched["feedback_signal"] = signal_label
            _log("callback", f"{signal_label} key={key16}")

        # Write feedback_signal to ledger entry (record it even for defer)
        if ikey in ledger:
            ledger[ikey]["feedback_signal"] = signal_label
            ledger[ikey]["feedback_received_at"] = now_iso
        else:
            _vlog("callback", f"ledger entry not found for {ikey[:16]} — signal recorded in queue only")

        answer_callback(token, cq_id, {"confirmed": "✓ Noted", "rejected": "✗ Noted", "deferred": "→ Snoozed 24h"}.get(signal_label, "OK"))

    return entries, ledger


def send_due_messages(
    entries: list[dict],
    token: str,
    chat_id: str,
) -> list[dict]:
    """Send Telegram messages for pending entries past their send_after time."""
    now = datetime.now(timezone.utc)
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

        key16 = entry["claude_idempotency_key"][:16]
        title = entry.get("title", "Intent")
        prompt = entry.get("feedback_prompt", title)
        category = entry.get("category", "")

        text = (
            f"📋 <b>Intent check-in</b>\n\n"
            f"{prompt}\n"
            f"<i>({title} · {category})</i>"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✓ Confirmed", "callback_data": f"confirm:{key16}"},
                {"text": "✗ Rejected",  "callback_data": f"reject:{key16}"},
                {"text": "→ Deferred",  "callback_data": f"defer:{key16}"},
            ]]
        }

        try:
            result = send_message(token, chat_id, text, reply_markup)
            msg_id = result.get("result", {}).get("message_id")
            entry["state"] = "sent"
            entry["telegram_message_id"] = msg_id
            _log("sender", f"sent message_id={msg_id} for {title!r}")
        except Exception as exc:
            _log("sender", f"ERROR sending for {title!r}: {exc}")

    return entries


def run(state_dir: Path, token: str, chat_id: str) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)

    entries = load_feedback_queue(state_dir)
    _vlog("queue", f"loaded {len(entries)} entries")

    offset = load_offset(state_dir)
    _vlog("updates", f"polling getUpdates offset={offset}")

    updates = get_updates(token, offset)
    _vlog("updates", f"received {len(updates)} updates")

    if updates:
        ledger = load_ledger(state_dir)
        entries, ledger = process_callback_updates(updates, entries, ledger, token)
        save_ledger(state_dir, ledger)
        new_offset = max(u["update_id"] for u in updates) + 1
        save_offset(state_dir, new_offset)
        _vlog("updates", f"new offset={new_offset}")

    entries = send_due_messages(entries, token, chat_id)
    save_feedback_queue(state_dir, entries)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    global _verbose

    parser = argparse.ArgumentParser(description="Send due intent feedback prompts via Telegram")
    parser.add_argument("--state-dir", help="Override state directory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _verbose = args.verbose

    env_path = Path.home() / ".config" / "journal-linker" / "env"
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
        return run(state_dir, token, chat_id)
    except Exception as exc:
        _log("error", f"unhandled exception: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
