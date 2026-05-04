#!/usr/bin/env python3
"""Append one feedback-queue row with send_after in the past so the next feedback_sender run sends immediately.

Use this for a **manual** live end-to-end test with real Telegram credentials — no waiting for the intent pipeline.

Typical flow (two quick CLI runs; no long daemon):

  1. Stop the systemd feedback sender if it is running (only one getUpdates client per bot):
       systemctl --user stop journal-linker-feedback-sender.service

  2. Load env and enqueue:
       set -a && source ~/.config/journal-linker/journal-linker.env && set +a
       python3 scripts/telegram_enqueue_immediate_checkin.py --prompt "Live test — tap a button"

  3. Deliver the check-in + one short poll for inbound updates:
       python3 scripts/feedback_sender.py --verbose

  4. On your phone: tap Done / Skip / Later, add an emoji reaction, or reply (depending on what you are testing).

  5. Record the interaction:
       python3 scripts/feedback_sender.py --verbose

If nothing sends in step 3, you may be in **quiet hours** (see INTENT_FEEDBACK_QUIET_*). For a forced send anytime:

       INTENT_FEEDBACK_QUIET_START=0 INTENT_FEEDBACK_QUIET_END=0 python3 scripts/feedback_sender.py --verbose

Exit codes: 0 ok, 10 missing env/path.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Same bootstrap order as feedback_sender / telegram_smoke_test


def load_env_files() -> None:
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


FEEDBACK_QUEUE_FILENAME = "intent_feedback_queue.jsonl"


def telegram_button_labels_preview(intent_class: str, category: str) -> tuple[str, str, str]:
    """Must stay aligned with _button_labels() in scripts/feedback_sender.py."""
    ic = (intent_class or "").strip()
    cat = (category or "").strip().lower()
    if ic == "purchase_intent":
        return ("Got it", "Pass", "Later")
    if ic == "latent_interest":
        return ("Noted", "Dismiss", "Later")
    if cat == "reminder":
        return ("Done", "Dismiss", "Snooze")
    return ("Done", "Skip", "Later")


def main() -> int:
    load_env_files()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--prompt",
        default="Manual live test — choose Done / Skip / Later (or react 👍/👎).",
        help="Telegram message body (HTML-escaped by sender)",
    )
    parser.add_argument("--title", default="Manual Telegram E2E", help="Short title stored in queue + ledger")
    parser.add_argument(
        "--source-file",
        default="",
        help="Optional path to a real journal .md (enables reply→surgical edit path when OPENAI_API_KEY is set)",
    )
    parser.add_argument(
        "--intent-raw",
        default="manual live test",
        help="intent_raw field for learning / traces",
    )
    parser.add_argument(
        "--intent-class",
        default="task_intent",
        metavar="CLASS",
        help="Mirrors router envelope (default task_intent → Done/Skip/Later buttons). "
        "purchase_intent → Got it/Pass/Later; latent_interest → Noted/Dismiss/Later.",
    )
    parser.add_argument(
        "--category",
        default="task",
        metavar="CAT",
        help="reminder → Done/Dismiss/Snooze; otherwise combines with --intent-class for label choice.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Override INTENT_STATE_DIR (default: ~/.local/state/journal-linker/intents)",
    )
    parser.add_argument(
        "--tick",
        action="store_true",
        help="After enqueue, run feedback_sender once (same as: python3 scripts/feedback_sender.py)",
    )
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (env or journal-linker.env)", file=sys.stderr)
        return 10

    default_state = Path.home() / ".local" / "state" / "journal-linker" / "intents"
    state_dir = args.state_dir or Path(os.getenv("INTENT_STATE_DIR", str(default_state)))
    state_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    send_after = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    key = secrets.token_hex(32)

    entry = {
        "claude_idempotency_key": key,
        "intent_raw": args.intent_raw,
        "feedback_prompt": args.prompt,
        "title": args.title,
        "urgency": "today",
        "format": "note",
        "action": "notification",
        "category": args.category,
        "intent_class": args.intent_class,
        "source_file": args.source_file or "",
        "captured_at": now.isoformat(timespec="seconds"),
        "send_after": send_after,
        "timing_policy": "manual_immediate",
        "state": "pending",
        "telegram_message_id": None,
        "feedback_signal": None,
        "defer_count": 0,
        "expires_at": "",
    }

    queue_path = state_dir / FEEDBACK_QUEUE_FILENAME
    with open(queue_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    a, b, c = telegram_button_labels_preview(args.intent_class, args.category)
    print(f"Appended one pending row to {queue_path}")
    print(f"  claude_idempotency_key (prefix) = {key[:16]}…")
    print(f"  send_after = {send_after} (due immediately)")
    print(
        f"  Inline keyboard labels (confirm/reject/defer): {a!r} | {b!r} | {c!r} "
        f"(intent_class={args.intent_class!r}, category={args.category!r})"
    )
    print()
    if args.tick:
        sender = Path(__file__).resolve().parent / "feedback_sender.py"
        print(f"Running {sender} …")
        rc = subprocess.call([sys.executable, str(sender)], cwd=str(sender.parents[1]))
        print(f"feedback_sender exited {rc}")
        print()
    print("After you interact on Telegram, run again to record taps/reactions/replies:")
    print(f"  python3 {Path(__file__).resolve().parent / 'feedback_sender.py'} --verbose")
    print()
    print("If the message did not send, try disabling quiet hours for this run:")
    print("  INTENT_FEEDBACK_QUIET_START=0 INTENT_FEEDBACK_QUIET_END=0 python3 scripts/feedback_sender.py --verbose")

    return 0


if __name__ == "__main__":
    sys.exit(main())
