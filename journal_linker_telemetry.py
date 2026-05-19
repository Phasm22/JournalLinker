"""Structured job telemetry for supervised journalLinker wrappers.

Emits one-line JSON events on stderr with prefix JOURNAL_LINKER_EVENT=
for monitoring ingestion. Wrappers finalize job.completed after Python exits;
long-running daemons emit health.probe periodically.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

EVENT_PREFIX = "JOURNAL_LINKER_EVENT="


def _emit(event: dict[str, Any]) -> None:
    line = EVENT_PREFIX + json.dumps(event, separators=(",", ":"), sort_keys=True)
    print(line, file=sys.stderr, flush=True)


def payload_path_from_env() -> Path | None:
    raw = os.environ.get("JOURNAL_LINKER_JOB_PAYLOAD_FILE", "").strip()
    if not raw:
        return None
    return Path(raw)


def maybe_write_job_payload(**fields: Any) -> None:
    """Write payload when wrapper set JOURNAL_LINKER_JOB_PAYLOAD_FILE."""
    path = payload_path_from_env()
    if path is not None:
        write_job_payload(path, **fields)


def write_job_payload(path: str | Path, **fields: Any) -> None:
    """Atomically write service-specific fields for wrapper finalize."""
    payload_path = Path(path)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = payload_path.with_suffix(payload_path.suffix + ".tmp")
    tmp.write_text(json.dumps(fields, separators=(",", ":")), encoding="utf-8")
    tmp.replace(payload_path)


def _read_payload(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload_path = Path(path)
    if not payload_path.is_file():
        return {}
    try:
        data = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def finalize_job_event(
    *,
    service: str,
    run_id: str,
    exit_code: int,
    duration_sec: float | int,
    payload_file: str | Path | None = None,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> None:
    """Merge wrapper fields with Python payload and emit job.completed."""
    extra = _read_payload(payload_file)
    event: dict[str, Any] = {
        "event": "job.completed",
        "service": service,
        "run_id": run_id,
        "exit_code": int(exit_code),
        "duration_sec": round(float(duration_sec), 3),
        "skipped": bool(skipped),
        "skip_reason": skip_reason,
    }
    event.update(extra)
    _emit(event)
    if payload_file:
        with contextlib.suppress(Exception):
            Path(payload_file).unlink(missing_ok=True)


def emit_health_probe(service: str, **fields: Any) -> None:
    """Emit a periodic health line for long-running services."""
    event: dict[str, Any] = {
        "event": "health.probe",
        "service": service,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    event.update(fields)
    _emit(event)


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Parse a log line containing JOURNAL_LINKER_EVENT=…; for tests."""
    stripped = line.strip()
    if not stripped.startswith(EVENT_PREFIX):
        return None
    try:
        data = json.loads(stripped[len(EVENT_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def _cli_finalize(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize a journalLinker job event line.")
    parser.add_argument("--service", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--payload-file", default="")
    parser.add_argument("--skipped", action="store_true")
    parser.add_argument("--skip-reason", default="")
    args = parser.parse_args(argv)
    finalize_job_event(
        service=args.service,
        run_id=args.run_id,
        exit_code=args.exit_code,
        duration_sec=args.duration_sec,
        payload_file=args.payload_file or None,
        skipped=args.skipped,
        skip_reason=args.skip_reason or None,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: journal_linker_telemetry finalize …", file=sys.stderr)
        return 2
    if argv[0] == "finalize":
        return _cli_finalize(argv[1:])
    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
