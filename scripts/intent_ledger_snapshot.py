#!/usr/bin/env python3
"""Build a markdown snapshot from intent_delivery_ledger.jsonl for indexing."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _week_label(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _build_markdown(rows: list[dict], generated_at: str) -> str:
    by_week_signal: dict[str, Counter] = defaultdict(Counter)
    by_week_class: dict[str, Counter] = defaultdict(Counter)
    recent: list[dict] = []

    for row in rows:
        ts = _parse_iso(row.get("feedback_received_at") or row.get("journal_timestamp") or "")
        if ts is None:
            continue
        week = _week_label(ts)
        signal = str(row.get("feedback_signal") or "none").strip().lower() or "none"
        intent_class = str(row.get("intent_class") or "unknown").strip().lower() or "unknown"
        by_week_signal[week][signal] += 1
        by_week_class[week][intent_class] += 1
        if signal in {"confirmed", "rejected", "deferred"}:
            recent.append(
                {
                    "timestamp": ts.isoformat(timespec="seconds"),
                    "week": week,
                    "signal": signal,
                    "intent_class": intent_class,
                    "title": str(row.get("title") or row.get("intent_raw") or "").strip(),
                    "source_path": str(row.get("source_path") or ""),
                }
            )

    recent.sort(key=lambda item: item["timestamp"], reverse=True)
    weeks = sorted(set(by_week_signal.keys()) | set(by_week_class.keys()), reverse=True)

    lines = [
        "# Intent Ledger Snapshot",
        "",
        f"Generated at: {generated_at}",
        "",
        "Source: intent_delivery_ledger.jsonl",
        "",
        "## Weekly Feedback Signals",
        "",
    ]
    for week in weeks:
        lines.append(f"### {week}")
        counts = by_week_signal.get(week, Counter())
        if not counts:
            lines.append("- no signal rows")
        else:
            for key, value in sorted(counts.items()):
                lines.append(f"- {key}: {value}")
        lines.append("")

    lines.extend(["## Weekly Intent Classes", ""])
    for week in weeks:
        lines.append(f"### {week}")
        counts = by_week_class.get(week, Counter())
        if not counts:
            lines.append("- no class rows")
        else:
            for key, value in sorted(counts.items()):
                lines.append(f"- {key}: {value}")
        lines.append("")

    lines.extend(["## Recent Feedback Events", ""])
    for row in recent[:100]:
        lines.append(
            f"- {row['timestamp']} | {row['signal']} | {row['intent_class']} | "
            f"{row['title']} | {row['source_path']}"
        )
    if not recent:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate markdown snapshot from intent delivery ledger.")
    parser.add_argument(
        "--state-dir",
        default=os.getenv("INTENT_STATE_DIR", os.path.expanduser("~/.local/state/journal-linker/intents")),
        help="Intent state directory containing intent_delivery_ledger.jsonl",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output file path (defaults to <state-dir>/intent_ledger_snapshot.md)",
    )
    args = parser.parse_args()

    state_dir = Path(args.state_dir).expanduser()
    ledger_path = state_dir / "intent_delivery_ledger.jsonl"
    out_path = Path(args.output).expanduser() if args.output else state_dir / "intent_ledger_snapshot.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = _load_rows(ledger_path)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_path.write_text(_build_markdown(rows, generated_at), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
