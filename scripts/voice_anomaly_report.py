#!/usr/bin/env python3
"""voice_anomaly_report.py — confusion report over voice_anomalies.jsonl.

The command stage logs every flagged wake-word attempt (transcription
artifacts, spelled-out letters, incomplete commands, low-confidence matches)
to ~/.local/state/journal-linker/intents/voice_anomalies.jsonl. This is the
equivalent of Rasa's errors.json / confusion-matrix report: instead of reading
the raw JSONL by hand, summarize which tags, mis-transcriptions, and routes
recur most.

Usage:
    python3 scripts/voice_anomaly_report.py            # default state-dir log
    python3 scripts/voice_anomaly_report.py --log FILE # a specific log
    python3 scripts/voice_anomaly_report.py --json     # machine-readable
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the pipeline's state-dir resolution + log filename so the report reads
# exactly what process_intents writes (honors INTENT_STATE_DIR).
from process_intents import VOICE_ANOMALY_LOG_FILENAME, get_state_dir

_UNKNOWN = "unknown"


def default_log_path() -> Path:
    return get_state_dir() / VOICE_ANOMALY_LOG_FILENAME


def load_records(log_path: Path) -> list[dict]:
    """Read anomaly records, tolerating blank and malformed lines."""
    records: list[dict] = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def summarize(records: list[dict]) -> dict:
    """Aggregate records into counts by tag, route, verb_form, corrected_term."""
    by_tag: Counter = Counter()
    by_route: Counter = Counter()
    by_verb_form: Counter = Counter()
    by_corrected_term: Counter = Counter()
    timestamps = []

    for rec in records:
        by_tag[rec.get("tag") or _UNKNOWN] += 1
        by_route[rec.get("route") or _UNKNOWN] += 1
        verb_form = rec.get("verb_form")
        if verb_form:
            by_verb_form[verb_form] += 1
        corrected = rec.get("corrected_term")
        if corrected:
            by_corrected_term[corrected] += 1
        ts = rec.get("timestamp")
        if ts:
            timestamps.append(ts)

    return {
        "total": len(records),
        "oldest": min(timestamps) if timestamps else None,
        "newest": max(timestamps) if timestamps else None,
        "by_tag": dict(by_tag.most_common()),
        "by_route": dict(by_route.most_common()),
        "by_verb_form": dict(by_verb_form.most_common()),
        "by_corrected_term": dict(by_corrected_term.most_common()),
    }


def _format_section(title: str, counts: dict) -> str:
    if not counts:
        return f"{title}:\n  (none)\n"
    width = max(len(str(k)) for k in counts)
    lines = [f"  {str(k):<{width}}  {v}" for k, v in counts.items()]
    return f"{title}:\n" + "\n".join(lines) + "\n"


def format_report(summary: dict, log_path: Path) -> str:
    parts = [
        f"Voice anomaly report — {log_path}",
        f"total: {summary['total']}"
        + (f"   ({summary['oldest']} … {summary['newest']})"
           if summary["oldest"] else ""),
        "",
        _format_section("By tag", summary["by_tag"]),
        _format_section("By route", summary["by_route"]),
        _format_section("By verb form (mis-transcribed activation words)",
                        summary["by_verb_form"]),
        _format_section("By corrected term (resolved ticker / correction)",
                        summary["by_corrected_term"]),
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, default=None,
                        help="Path to voice_anomalies.jsonl (default: state dir).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the summary as JSON instead of a table.")
    args = parser.parse_args(argv)

    log_path = args.log or default_log_path()
    if not log_path.exists():
        if args.json:
            print(json.dumps({"total": 0, "log": str(log_path)}))
        else:
            print(f"No anomaly log at {log_path} (nothing logged yet).")
        return 0

    summary = summarize(load_records(log_path))
    if args.json:
        print(json.dumps({**summary, "log": str(log_path)}, ensure_ascii=False))
    else:
        print(format_report(summary, log_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
