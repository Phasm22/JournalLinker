#!/usr/bin/env python3
"""Print Telegram intent feedback recorded in the delivery ledger (button taps).

Successful taps set feedback_signal and feedback_received_at on the ledger row.
The feedback_sender service must run (timer or manual) to process callbacks first."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LEDGER_FILENAME = "intent_delivery_ledger.jsonl"


def load_ledger(path: Path) -> dict[str, dict]:
    ledger: dict[str, dict] = {}
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Show recorded Telegram feedback (confirm/reject/defer)")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.getenv(
                "INTENT_STATE_DIR",
                str(Path.home() / ".local" / "state" / "journal-linker" / "intents"),
            )
        ),
    )
    args = parser.parse_args()
    path = args.state_dir / LEDGER_FILENAME

    print("Journal Linker — Telegram feedback status")
    print(f"  State dir: {args.state_dir}")
    print(f"  Ledger:    {path}")
    print("")

    ledger = load_ledger(path)
    if not ledger:
        print("  No ledger file or no entries yet.")
        print("  After you tap a button, run feedback_sender (or wait for the timer) to record it.")
        return 0

    rows: list[tuple[str, str, str, str]] = []
    for key, rec in ledger.items():
        sig = rec.get("feedback_signal")
        if sig is None:
            continue
        at = rec.get("feedback_received_at") or "?"
        title = (rec.get("title") or "")[:48]
        rows.append((at, key[:16], str(sig), title))

    rows.sort(reverse=True)

    if not rows:
        print("  No feedback_signal recorded yet (no button taps processed).")
        print("  In Telegram you should see a short toast (✓ Noted / ✗ Noted / → Snoozed)")
        print("  when feedback_sender processes the tap — up to one timer interval if the service is idle.")
        return 0

    print(f"  {'received_at':<26}  {'key16':<16}  signal      title")
    for at, k16, sig, title in rows[:30]:
        print(f"  {at:<26}  {k16:<16}  {sig:<10}  {title}")

    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
