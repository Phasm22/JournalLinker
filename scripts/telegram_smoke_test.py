#!/usr/bin/env python3
"""telegram_smoke_test.py — Safe Telegram API checks without running the feedback daemon.

Use this to verify TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID before enabling the trial script or systemd.

What it does (default):
  1. getMe — proves the token is valid.
  2. Optional --send TEXT — one sendMessage to your chat (plain text, no inline keyboard).

What it does NOT do:
  - Call getUpdates (that competes with feedback_sender long-poll and can cause HTTP 409 or consume updates).
  - Start a daemon.

For full long-poll testing after stopping the systemd unit, use scripts/telegram_live_trial.sh.

Exit codes: 0 ok, 10 missing env, 11 API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def load_env_files() -> None:
    """Same bootstrap order as feedback_sender.py main()."""
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


def tg_request(token: str, method: str, payload: dict, *, timeout: int = 30) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}: {body[:400]}") from exc


def run_smoke(
    *,
    send_text: str | None,
    quiet: bool,
) -> int:
    load_env_files()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID (env or journal-linker.env)", file=sys.stderr)
        return 10

    try:
        me = tg_request(token, "getMe", {})
    except Exception as exc:
        print(f"getMe failed: {exc}", file=sys.stderr)
        return 11

    if not me.get("ok"):
        print(f"getMe not ok: {me}", file=sys.stderr)
        return 11
    user = (me.get("result") or {}).get("username") or "(no username)"
    bot_id = (me.get("result") or {}).get("id")
    if not quiet:
        print(f"OK getMe: bot @{user} id={bot_id}")

    if send_text:
        try:
            sent = tg_request(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": send_text},
            )
        except Exception as exc:
            print(f"sendMessage failed: {exc}", file=sys.stderr)
            return 11
        if not sent.get("ok"):
            print(f"sendMessage not ok: {sent}", file=sys.stderr)
            return 11
        mid = (sent.get("result") or {}).get("message_id")
        if not quiet:
            print(f"OK sendMessage: chat_id={chat_id} message_id={mid}")

    if not quiet:
        print("Smoke test passed. (No getUpdates — avoids conflicting with feedback_sender.)")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram API smoke test (getMe + optional sendMessage).")
    parser.add_argument(
        "--send",
        metavar="TEXT",
        default=None,
        help="If set, send this plain-text message to TELEGRAM_CHAT_ID",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    args = parser.parse_args(argv)
    return run_smoke(send_text=args.send, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
