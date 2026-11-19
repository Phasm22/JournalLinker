#!/usr/bin/env python3
"""Collapse duplicate wellness notes that share a date, type, and items.

Dry-run by default. Prints each group, the file it keeps, and the files it
would move. The keeper is the earliest-created note whose spoken time is the
earliest in the group (the clock before a +1h filename shift).

--apply moves the other files to <vault>/.trash-journal-linker/<YYYY-MM-DD>/
and writes a manifest there. It never deletes a note.

Paths are resolved and must stay inside the vault. `..` and a symlink that
escapes the vault are rejected.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_ledger import (  # noqa: E402
    _spoken_sort_value,
    _unquote_yaml,
    normalize_label,
    parse_frontmatter_items,
    read_frontmatter,
)


class PathViolation(Exception):
    """A path left the vault or used a rejected symlink."""


def _has_dotdot(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def confine(root: Path, candidate: Path) -> Path:
    """Resolve candidate and require it to stay inside root.

    Symlinks are rejected so a link cannot point the move at another tree.
    """
    if _has_dotdot(root) or _has_dotdot(candidate):
        raise PathViolation("path contains ..")
    if root.is_symlink():
        raise PathViolation("symlink rejected")
    if not root.is_dir():
        raise PathViolation("vault is not a directory")
    root_resolved = root.resolve(strict=True)
    absolute = candidate if candidate.is_absolute() else (root_resolved / candidate)
    if _has_dotdot(absolute):
        raise PathViolation("path contains ..")
    if absolute.is_symlink():
        raise PathViolation("symlink rejected")
    existing = absolute
    missing: list[str] = []
    while not existing.exists():
        if existing.name in {"", ".", ".."}:
            raise PathViolation("path contains ..")
        missing.append(existing.name)
        if existing.parent == existing:
            raise PathViolation("path escapes vault")
        existing = existing.parent
    if existing.is_symlink():
        raise PathViolation("symlink rejected")
    resolved_existing = existing.resolve(strict=True)
    if not resolved_existing.is_relative_to(root_resolved):
        raise PathViolation("path escapes vault")
    cursor = root_resolved
    for part in resolved_existing.relative_to(root_resolved).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PathViolation("symlink rejected")
    resolved = resolved_existing
    for part in reversed(missing):
        resolved = resolved / part
    if not resolved.is_relative_to(root_resolved):
        raise PathViolation("path escapes vault")
    return resolved


def _read_note(path: Path) -> dict:
    fields = read_frontmatter(path.read_text(encoding="utf-8"))
    source_date = _unquote_yaml(fields.get("source_date", ""))
    if not source_date and len(path.name) >= 10:
        source_date = path.name[:10]
    items = parse_frontmatter_items(fields.get("items", ""))
    return {
        "path": path,
        "name": path.name,
        "source_date": source_date,
        "type": _unquote_yaml(fields.get("type", "")),
        "items": items,
        "item_key": tuple(sorted(normalize_label(item) for item in items)),
        "time": _unquote_yaml(fields.get("time", "")),
        "status": _unquote_yaml(fields.get("status", "")) or "active",
        "mtime": path.stat().st_mtime,
    }


def _group_key(record: dict) -> tuple:
    return (record["source_date"], record["type"], record["item_key"])


def choose_keeper(records: list[dict]) -> tuple[dict, list[dict]]:
    """Keep the earliest file among notes that carry the earliest spoken time."""
    active = [record for record in records if record["status"] != "superseded"]
    pool = active or list(records)
    ranked = sorted(pool, key=lambda record: (_spoken_sort_value(str(record["time"])),))
    earliest = _spoken_sort_value(str(ranked[0]["time"]))
    correct = [
        record for record in pool
        if _spoken_sort_value(str(record["time"])) == earliest
    ]
    correct.sort(key=lambda record: (record["mtime"], record["name"]))
    keep = correct[0]
    moves = [record for record in records if record["path"] != keep["path"]]
    return keep, moves


def plan_dedupe(wellness_dir: Path, vault: Path) -> tuple[int, list[dict]]:
    """Return (note count, groups). A group has keep and moves."""
    note_count = 0
    grouped: dict[tuple, list[dict]] = {}
    for path in sorted(wellness_dir.glob("*.md")):
        confined = confine(vault, path)
        if not confined.is_file():
            continue
        note_count += 1
        record = _read_note(confined)
        grouped.setdefault(_group_key(record), []).append(record)
    groups: list[dict] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2])):
        records = grouped[key]
        if len(records) < 2:
            continue
        keep, moves = choose_keeper(records)
        if not moves:
            continue
        groups.append({
            "source_date": key[0],
            "type": key[1],
            "item_count": len(key[2]),
            "keep": keep,
            "moves": moves,
        })
    return note_count, groups


def format_report(note_count: int, groups: list[dict], *, apply: bool) -> str:
    keep_count = len(groups)
    move_count = sum(len(group["moves"]) for group in groups)
    lines = [
        "apply" if apply else "dry-run",
        f"wellness_notes: {note_count}",
        f"groups: {len(groups)}",
        f"keep: {keep_count}",
        f"move: {move_count}",
    ]
    for group in groups:
        lines.append(
            f"GROUP source_date={group['source_date']} type={group['type']} "
            f"items={group['item_count']} files={1 + len(group['moves'])}"
        )
        kept = group["keep"]
        lines.append(f"  KEEP {kept['name']} time={kept['time']}")
        for record in sorted(group["moves"], key=lambda item: item["name"]):
            lines.append(f"  MOVE {record['name']} time={record['time']}")
    return "\n".join(lines)


def _unique_dest(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem = Path(name).stem
    suffix = Path(name).suffix
    number = 2
    while True:
        candidate = dest_dir / f"{stem}-{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def apply_moves(vault: Path, groups: list[dict], run_date: str | None = None) -> Path:
    """Move duplicates into the vault trash. Return the manifest path."""
    day = run_date or date.today().isoformat()
    dest_dir = confine(vault, vault / ".trash-journal-linker" / day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[dict] = []
    for group in groups:
        for record in group["moves"]:
            src = confine(vault, record["path"])
            dest = confine(vault, _unique_dest(dest_dir, record["name"]))
            shutil.move(str(src), str(dest))
            moved.append({
                "file": record["name"],
                "to": dest.relative_to(vault).as_posix(),
            })
    manifest = {
        "moved": moved,
        "kept": [group["keep"]["name"] for group in groups],
    }
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def resolve_wellness_dir(vault: Path, journal_dir: str, wellness_dir: str) -> Path:
    if wellness_dir:
        return confine(vault, Path(wellness_dir).expanduser())
    if journal_dir:
        journal = confine(vault, Path(journal_dir).expanduser())
        return confine(vault, journal / "wellness")
    return confine(vault, vault / "wellness")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dedupe wellness notes. Dry-run unless --apply.")
    parser.add_argument("--vault", required=True, help="Vault root. Every path must stay inside it.")
    parser.add_argument("--journal-dir", default="", help="Daily-notes directory inside the vault.")
    parser.add_argument("--wellness-dir", default="", help="Wellness directory inside the vault.")
    parser.add_argument("--apply", action="store_true", help="Move duplicates into .trash-journal-linker/")
    args = parser.parse_args(argv)
    try:
        raw_vault = Path(args.vault).expanduser()
        if _has_dotdot(raw_vault) or raw_vault.is_symlink():
            raise PathViolation("symlink rejected" if raw_vault.is_symlink() else "path contains ..")
        if not raw_vault.is_dir():
            raise PathViolation("vault is not a directory")
        vault = raw_vault.resolve(strict=True)
        wellness = resolve_wellness_dir(vault, args.journal_dir, args.wellness_dir)
        if not wellness.is_dir():
            print("dry-run" if not args.apply else "apply")
            print("wellness_notes: 0")
            print("groups: 0")
            print("keep: 0")
            print("move: 0")
            return 0
        note_count, groups = plan_dedupe(wellness, vault)
        print(format_report(note_count, groups, apply=args.apply))
        if args.apply:
            manifest = apply_moves(vault, groups)
            print(f"manifest: {manifest.relative_to(vault).as_posix()}")
    except PathViolation as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
