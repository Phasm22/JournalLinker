#!/usr/bin/env python3
"""corpus_sample.py — Offline list / filter / sample dated journal notes (no intent pipeline, no time delays).

Use this to iterate on historical vault text, export slices for stats (Python/R), or spot-check
without process_intents, watchers, or Telegram.

Expects daily notes named YYYY-MM-DD.md (same convention as Scribe / vault_mapper).

Examples:
  python3 scripts/corpus_sample.py --journal-dir "$SCRIBE_JOURNAL_DIR" --list
  python3 scripts/corpus_sample.py --from-date 2025-01-01 --to-date 2025-12-31 --sample 12 --seed 42 --format jsonl --preview-chars 400
  python3 scripts/corpus_sample.py --roots ~/vault/daily ~/vault/cortex --recursive --format paths

Env:
  SCRIBE_JOURNAL_DIR  default for --journal-dir when no --roots given
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from journal_linker_env import bootstrap_journal_linker_env

DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


class NoteRef(NamedTuple):
    day: date
    path: Path


def _parse_iso_day(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def discover_daily_notes(roots: list[Path], *, recursive: bool) -> list[NoteRef]:
    """Collect YYYY-MM-DD.md files under each root."""
    out: list[NoteRef] = []
    for root in roots:
        if not root.is_dir():
            continue
        if recursive:
            candidates = root.rglob("*.md")
        else:
            candidates = root.glob("*.md")
        for path in candidates:
            if not path.is_file():
                continue
            if not DATE_FILE_RE.fullmatch(path.name):
                continue
            try:
                d = _parse_iso_day(path.stem)
            except ValueError:
                continue
            out.append(NoteRef(day=d, path=path.resolve()))
    # Stable order for reproducibility before shuffle/sample
    out.sort(key=lambda r: (r.day, str(r.path)))
    return out


def filter_date_range(
    notes: list[NoteRef],
    *,
    from_date: date | None,
    to_date: date | None,
) -> list[NoteRef]:
    lo = from_date or date.min
    hi = to_date or date.max
    return [n for n in notes if lo <= n.day <= hi]


def shuffle_and_sample(
    notes: list[NoteRef],
    *,
    shuffle: bool,
    sample_k: int | None,
    seed: int | None,
) -> list[NoteRef]:
    seq = list(notes)
    rng = random.Random(seed) if seed is not None else None
    if shuffle:
        if rng is not None:
            rng.shuffle(seq)
        else:
            random.shuffle(seq)
    if sample_k is None:
        return seq
    k = min(sample_k, len(seq))
    if k <= 0:
        return []
    if rng is not None:
        return rng.sample(seq, k)
    return random.sample(seq, k)


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) >= 3 and parts[0] == "":
        return parts[2].lstrip("\n")
    return text


def preview_text(path: Path, max_chars: int, *, strip_fm: bool) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if strip_fm:
        raw = strip_frontmatter(raw)
    raw = raw.strip()
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "…"


def parse_cli(argv: list[str] | None) -> argparse.Namespace:
    bootstrap_journal_linker_env(repo_root=Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description="Sample dated journal markdown offline.")
    parser.add_argument(
        "--journal-dir",
        default=os.getenv("SCRIBE_JOURNAL_DIR"),
        help="Single journal root (default: $SCRIBE_JOURNAL_DIR)",
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        metavar="DIR",
        default=None,
        help="One or more roots to scan (overrides --journal-dir when set)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan recursively for YYYY-MM-DD.md (default: only top-level *.md per root)",
    )
    parser.add_argument("--from-date", metavar="YYYY-MM-DD", help="Inclusive lower bound")
    parser.add_argument("--to-date", metavar="YYYY-MM-DD", help="Inclusive upper bound")
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        default=None,
        help="Randomly select N notes after filtering (without replacement)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for --sample",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle order after filtering (before --sample if both set)",
    )
    parser.add_argument(
        "--format",
        choices=("paths", "jsonl", "text"),
        default="paths",
        help="paths: one path per line; jsonl: records; text: date + preview",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=0,
        help="Include this many body chars in jsonl/text (0 = paths only for jsonl metadata)",
    )
    parser.add_argument(
        "--strip-frontmatter",
        action="store_true",
        help="Strip leading YAML frontmatter for previews only",
    )
    parser.add_argument("--list", action="store_true", help="Same as --format paths (counts to stderr)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_cli(argv)
    if args.roots:
        roots = [Path(p).expanduser().resolve() for p in args.roots]
    elif args.journal_dir:
        roots = [Path(args.journal_dir).expanduser().resolve()]
    else:
        print("Error: set --journal-dir, $SCRIBE_JOURNAL_DIR, or --roots", file=sys.stderr)
        return 2

    notes = discover_daily_notes(roots, recursive=args.recursive)
    fd = _parse_iso_day(args.from_date) if args.from_date else None
    td = _parse_iso_day(args.to_date) if args.to_date else None
    notes = filter_date_range(notes, from_date=fd, to_date=td)

    notes = shuffle_and_sample(
        notes,
        shuffle=args.shuffle,
        sample_k=args.sample,
        seed=args.seed,
    )

    out_fmt = "paths" if args.list else args.format

    if out_fmt == "paths":
        for n in notes:
            print(n.path)
        print(f"# count={len(notes)}", file=sys.stderr)
        return 0

    if out_fmt == "jsonl":
        for n in notes:
            rec: dict = {"date": n.day.isoformat(), "path": str(n.path)}
            if args.preview_chars > 0:
                body = preview_text(n.path, args.preview_chars, strip_fm=args.strip_frontmatter)
                rec["preview"] = body
                rec["preview_chars"] = len(body)
            print(json.dumps(rec, ensure_ascii=False))
        print(f"# count={len(notes)}", file=sys.stderr)
        return 0

    # text
    for n in notes:
        print(f"=== {n.day.isoformat()}  {n.path} ===")
        if args.preview_chars > 0:
            print(preview_text(n.path, args.preview_chars, strip_fm=args.strip_frontmatter))
        else:
            print(n.path.read_text(encoding="utf-8", errors="replace"))
        print()
    print(f"# count={len(notes)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
