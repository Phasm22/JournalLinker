#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_LOG_DAYS = 30
DEFAULT_KEEP_NEWEST = 20
GENERATED_LOG_PREFIXES = (
    "scribe-",
    "daily-reflection-",
    "voice-",
    "voice-retry-",
    "intent-",
    "intent-retry-",
    "feedback-sender-",
)


@dataclass
class Candidate:
    path: Path
    age_days: int
    size: int


def default_state_dir() -> Path:
    return Path(os.environ.get("SCRIBE_JOB_LOG_DIR") or "~/.local/state/journal-linker").expanduser()


def _latest_targets(root: Path) -> set[Path]:
    targets: set[Path] = set()
    if not root.exists():
        return targets
    for path in root.glob("*-latest.log"):
        targets.add(path.resolve())
        try:
            targets.add(path.resolve(strict=True))
        except FileNotFoundError:
            continue
    return targets


def _generated_log(path: Path) -> bool:
    return path.name.endswith(".log") and path.name.startswith(GENERATED_LOG_PREFIXES) and "-latest" not in path.name


def plan_log_deletes(
    root: Path,
    *,
    log_days: int = DEFAULT_LOG_DAYS,
    keep_newest: int = DEFAULT_KEEP_NEWEST,
    now: datetime | None = None,
) -> list[Candidate]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=log_days)
    preserved = _latest_targets(root)
    logs = []
    if root.exists():
        logs = sorted(
            (p for p in root.glob("*.log") if p.is_file() and _generated_log(p)),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
    newest = {p.resolve() for p in logs[:keep_newest]}
    planned: list[Candidate] = []
    for path in logs:
        resolved = path.resolve()
        if resolved in newest or resolved in preserved:
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if mtime <= cutoff:
            planned.append(Candidate(path=path, age_days=(now - mtime).days, size=stat.st_size))
    return planned


def run_cleanup(
    *,
    state_dir: Path,
    apply: bool,
    log_days: int,
    keep_newest: int,
    prune_intents: bool,
) -> dict:
    planned = plan_log_deletes(state_dir, log_days=log_days, keep_newest=keep_newest)
    deleted: list[str] = []
    errors: list[str] = []
    if apply:
        for item in planned:
            try:
                item.path.unlink()
                deleted.append(str(item.path))
            except OSError as exc:
                errors.append(f"{item.path}: {exc}")

    intent_result = None
    if prune_intents and apply:
        script = Path(__file__).with_name("process_intents.py")
        proc = subprocess.run(
            [sys.executable, str(script), "--prune-ledger", "--older-than", f"{log_days}d"],
            cwd=str(script.parents[1]),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        intent_result = {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:],
        }
        if proc.returncode != 0:
            errors.append(f"process_intents prune failed: exit {proc.returncode}")

    return {
        "mode": "apply" if apply else "dry-run",
        "state_dir": str(state_dir),
        "policy": {
            "log_days": log_days,
            "keep_newest": keep_newest,
            "generated_only": True,
            "preserve_latest_symlinks": True,
            "prune_intents_on_apply": prune_intents,
        },
        "planned": [item.__dict__ | {"path": str(item.path)} for item in planned],
        "deleted": deleted,
        "errors": errors,
        "intent_prune": intent_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune generated journalLinker logs/state.")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--log-days", type=int, default=DEFAULT_LOG_DAYS)
    parser.add_argument("--keep-newest", type=int, default=DEFAULT_KEEP_NEWEST)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-prune-intents", action="store_true")
    args = parser.parse_args()
    if args.log_days < 1 or args.keep_newest < 0:
        parser.error("--log-days must be >=1 and --keep-newest must be >=0")
    result = run_cleanup(
        state_dir=args.state_dir.expanduser(),
        apply=args.apply,
        log_days=args.log_days,
        keep_newest=args.keep_newest,
        prune_intents=not args.no_prune_intents,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        action = "deleted" if args.apply else "would delete"
        print(f"journalLinker retention {result['mode']}: {action} {len(result['planned'])} generated logs")
        for item in result["planned"]:
            print(f"{action}: {item['path']}")
        if result["errors"]:
            print(f"errors: {len(result['errors'])}", file=sys.stderr)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
