#!/usr/bin/env python3
"""Connectivity check for Telegram Bot API (getMe + optional getChat).

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment.
Use `just telegram-doctor` to load .env and ~/.config/journal-linker/env first."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _request(token: str, method: str, payload: dict) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    print("Journal Linker — Telegram")
    print("")

    if not token:
        print("  FAIL: TELEGRAM_BOT_TOKEN is not set")
        print("  Set it in .env or ~/.config/journal-linker/env (see systemd/journal-linker.env.example)")
        return 1

    try:
        r = _request(token, "getMe", {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        print(f"  FAIL: Telegram HTTP {exc.code}")
        if exc.code == 401:
            print("  Invalid or revoked bot token — get a new one from @BotFather")
        else:
            print(f"  {body}")
        return 1
    except OSError as exc:
        print(f"  FAIL: network error: {exc}")
        return 1

    if not r.get("ok"):
        print("  FAIL: getMe returned ok=false:", r)
        return 1

    u = r.get("result") or {}
    uname = u.get("username") or "?"
    fname = u.get("first_name") or ""
    print(f"  Bot API:   OK (getMe) — @{uname} {fname}".rstrip())

    if not chat_id:
        print("  Chat ID:   not set (TELEGRAM_CHAT_ID) — optional for token-only check")
        print("")
        print("  feedback_sender.py needs TELEGRAM_CHAT_ID to deliver messages.")
        return 0

    try:
        gc = _request(token, "getChat", {"chat_id": chat_id})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        print(f"  FAIL: getChat HTTP {exc.code}: {body}")
        return 2
    except OSError as exc:
        print(f"  FAIL: getChat network error: {exc}")
        return 2

    if not gc.get("ok"):
        print("  FAIL: getChat ok=false:", gc)
        return 2

    ch = gc.get("result") or {}
    title = ch.get("title") or ch.get("username") or ch.get("first_name") or "?"
    ctype = ch.get("type") or "?"
    print(f"  Chat ID:   OK (getChat) — {title} [{ctype}]")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
