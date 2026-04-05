#!/usr/bin/env python3
"""process_voice.py — Voice-to-Journal bridge for journalLinker.

Picks up .m4a recordings from an iCloud Drive drop folder (VoiceDrop/),
transcribes them with faster-whisper using the Scribe learning store as
vocabulary context, appends a voice callout block to the target daily note,
then hands off to Scribe.py --write-back so the existing feedback loop runs.

Usage:
    python3 scripts/process_voice.py                       # scan drop dir
    python3 scripts/process_voice.py path/to/note.m4a     # process one file
    python3 scripts/process_voice.py --dry-run             # no writes, print only

Env vars (from .env or environment):
    SCRIBE_JOURNAL_DIR    — daily notes folder (required)
    SCRIBE_VOICEDROP_DIR  — watch folder (default: ~/Library/Mobile Documents/
                             com~apple~CloudDocs/VoiceDrop)
    SCRIBE_WHISPER_MODEL  — faster-whisper model name (default: base.en)
    SCRIBE_NIGHT_CUTOFF   — hour 0-23; recordings before this hour are
                             attributed to the previous calendar day (default: 4)
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

RECENCY_LAMBDA = 0.08  # same as Scribe.py
WHISPER_PROMPT_MAX_CHARS = 800  # ~200 tokens; Whisper decoder prefix limit is 223
DEFAULT_WHISPER_MODEL = "base.en"
DEFAULT_NIGHT_CUTOFF = 4
PROCESSED_SUFFIX = ".processed"
VOICEDROP_DEFAULT = (
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "VoiceDrop"
)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ[key] = value


# ---------------------------------------------------------------------------
# Vocab injection: learning store → Whisper initial_prompt
# ---------------------------------------------------------------------------

def _parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def extract_whisper_prompt(learning_file: Path, reference_date: str | None = None) -> str:
    """Read scribe_learning.json, rank terms by success*recency, return prompt str.

    Uses the same RECENCY_LAMBDA and scoring approach as Scribe.py's
    rank_link_candidates so the Whisper vocabulary bias reflects the same
    weights the feedback loop has already learned.
    """
    if not learning_file.exists():
        return ""
    try:
        data = json.loads(learning_file.read_text(encoding="utf-8"))
    except Exception:
        return ""

    term_memory = data.get("term_memory")
    if not isinstance(term_memory, dict):
        return ""

    ref_dt = _parse_iso_date(reference_date) or datetime.now()
    ref_str = ref_dt.strftime("%Y-%m-%d")

    DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    scored: list[tuple[float, str]] = []
    for _key, record in term_memory.items():
        if not isinstance(record, dict):
            continue
        canonical = record.get("term", "").strip()
        if not canonical or DATE_RE.fullmatch(canonical):
            continue
        successes = record.get("success_count", 0) or 0
        if successes <= 0:
            continue
        last_date = record.get("last_success_date") or record.get("last_seen_date")
        last_dt = _parse_iso_date(last_date)
        days = max(0, (ref_dt - last_dt).days) if last_dt else 9999
        recency = math.exp(-RECENCY_LAMBDA * days)
        score = successes * recency
        if score > 0:
            scored.append((score, canonical))

    scored.sort(reverse=True)

    parts: list[str] = []
    total_chars = len("Topics: ") + 1  # "Topics: " prefix + trailing "."
    for _, term in scored:
        cost = len(term) + 2  # ", " separator
        if total_chars + cost > WHISPER_PROMPT_MAX_CHARS:
            break
        parts.append(term)
        total_chars += cost

    if not parts:
        return ""
    return "Topics: " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Date resolution from filename
# ---------------------------------------------------------------------------

FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})$")


def resolve_target_date(audio_path: Path, night_cutoff_hour: int = DEFAULT_NIGHT_CUTOFF) -> tuple[str, str]:
    """Return (date_str YYYY-MM-DD, time_str HH:MM) for the recording.

    Filename convention: YYYY-MM-DD-HHmm.m4a  (produced by the iOS Shortcut).
    If the recording hour is before night_cutoff_hour, it is attributed to the
    previous calendar day (e.g. a 01:30 AM recording belongs to "yesterday").
    Falls back to mtime if the filename doesn't match the convention.
    """
    m = FILENAME_RE.fullmatch(audio_path.stem)
    if m:
        date_str = m.group(1)
        hour = int(m.group(2))
        minute = int(m.group(3))
        time_str = f"{hour:02d}:{minute:02d}"
        if hour < night_cutoff_hour:
            dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
            date_str = dt.strftime("%Y-%m-%d")
        return date_str, time_str

    # Fallback: mtime
    mtime = datetime.fromtimestamp(audio_path.stat().st_mtime)
    time_str = mtime.strftime("%H:%M")
    if mtime.hour < night_cutoff_hour:
        mtime -= timedelta(days=1)
    return mtime.strftime("%Y-%m-%d"), time_str


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def load_whisper_model(model_name: str):
    """Load a faster-whisper WhisperModel. Exits with a helpful message if not installed."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print(
            "[voice] faster-whisper is not installed.\n"
            "  Install it with:  just voice-install\n"
            "  or manually:      pip install faster-whisper",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[voice] loading Whisper model '{model_name}' (downloads on first use) …", file=sys.stderr)
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def transcribe_audio(audio_path: Path, model, initial_prompt: str) -> tuple[str, float, str]:
    """Return (transcript_text, duration_sec, detected_language).

    Passes initial_prompt to Whisper so the decoder prefers vocabulary from
    the learning store. Segments are joined into a single continuous transcript.
    """
    segments, info = model.transcribe(
        str(audio_path),
        initial_prompt=initial_prompt or None,
        language="en",
        beam_size=5,
    )
    parts = [seg.text.strip() for seg in segments]
    transcript = " ".join(p for p in parts if p)
    duration = getattr(info, "duration", 0.0)
    language = getattr(info, "language", "en")
    return transcript, duration, language


# ---------------------------------------------------------------------------
# Journal append
# ---------------------------------------------------------------------------

def _wrap_callout(time_str: str, transcript: str) -> str:
    """Format transcript as an Obsidian voice callout block."""
    header = f"> [!voice] {time_str}"
    lines = textwrap.wrap(transcript.strip(), width=76) or ["(empty transcript)"]
    body = "\n".join(f"> {line}" for line in lines)
    return f"\n\n{header}\n{body}\n"


def append_voice_block(
    journal_dir: Path,
    date_str: str,
    time_str: str,
    transcript: str,
    dry_run: bool = False,
) -> Path:
    """Append the voice callout block to YYYY-MM-DD.md, creating it if absent."""
    note_path = journal_dir / f"{date_str}.md"
    callout = _wrap_callout(time_str, transcript)

    if dry_run:
        print(f"[voice] dry-run: would append to {note_path}")
        print(callout)
        return note_path

    if note_path.exists():
        existing = note_path.read_text(encoding="utf-8")
        note_path.write_text(existing.rstrip("\n") + callout, encoding="utf-8")
    else:
        note_path.write_text(f"# {date_str}\n{callout}", encoding="utf-8")

    print(f"[voice] appended to {note_path}", file=sys.stderr)
    return note_path


# ---------------------------------------------------------------------------
# Scribe handoff
# ---------------------------------------------------------------------------

def run_scribe(python_bin: Path, scribe_py: Path, date_str: str, dry_run: bool = False) -> int:
    """Call Scribe.py --write-back --active-date=DATE.

    stdin is redirected from /dev/null so Scribe sees 'stdin_pipe_empty' and
    reads the on-disk note body — triggering the normal feedback loop.
    """
    if dry_run:
        print(f"[voice] dry-run: would run Scribe --write-back --active-date={date_str}", file=sys.stderr)
        return 0

    cmd = [str(python_bin), str(scribe_py), "--write-back", f"--active-date={date_str}"]
    print(f"[voice] running Scribe: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# Processed marker
# ---------------------------------------------------------------------------

def is_processed(audio_path: Path) -> bool:
    return audio_path.with_suffix(audio_path.suffix + PROCESSED_SUFFIX).exists()


def mark_processed(audio_path: Path) -> None:
    audio_path.with_suffix(audio_path.suffix + PROCESSED_SUFFIX).touch()


# ---------------------------------------------------------------------------
# Single-file pipeline
# ---------------------------------------------------------------------------

def process_file(
    audio_path: Path,
    *,
    journal_dir: Path,
    model,
    whisper_prompt: str,
    python_bin: Path,
    scribe_py: Path,
    night_cutoff: int,
    dry_run: bool,
    verbose: bool,
) -> bool:
    """Process one .m4a file. Returns True on success."""
    print(f"[voice] processing {audio_path.name}", file=sys.stderr)

    date_str, time_str = resolve_target_date(audio_path, night_cutoff)
    print(f"[voice] target date={date_str} time={time_str}", file=sys.stderr)

    if verbose and whisper_prompt:
        print(f"[voice] whisper prompt ({len(whisper_prompt)} chars): {whisper_prompt[:120]}…", file=sys.stderr)

    transcript, duration, language = transcribe_audio(audio_path, model, whisper_prompt)
    print(
        f"[voice] transcribed {duration:.1f}s audio [{language}]: {len(transcript.split())} words",
        file=sys.stderr,
    )
    if verbose:
        print(f"[voice] transcript: {transcript[:300]}", file=sys.stderr)

    if not transcript.strip():
        print(f"[voice] empty transcript, skipping {audio_path.name}", file=sys.stderr)
        if not dry_run:
            mark_processed(audio_path)
        return False

    append_voice_block(journal_dir, date_str, time_str, transcript, dry_run=dry_run)

    scribe_exit = run_scribe(python_bin, scribe_py, date_str, dry_run=dry_run)
    if scribe_exit != 0:
        print(f"[voice] Scribe exited {scribe_exit} for {audio_path.name}", file=sys.stderr)

    if not dry_run:
        mark_processed(audio_path)
        print(f"[voice] marked processed: {audio_path.name}", file=sys.stderr)

    return scribe_exit == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_cli() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    load_local_env(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description="Transcribe voice recordings and append to the Obsidian journal."
    )
    parser.add_argument(
        "audio_file",
        nargs="?",
        help="Path to a single .m4a file. Omit to scan SCRIBE_VOICEDROP_DIR.",
    )
    parser.add_argument(
        "--journal-dir",
        default=os.getenv("SCRIBE_JOURNAL_DIR"),
        help="Journal directory (default: $SCRIBE_JOURNAL_DIR)",
    )
    parser.add_argument(
        "--drop-dir",
        default=os.getenv("SCRIBE_VOICEDROP_DIR", str(VOICEDROP_DEFAULT)),
        help="iCloud VoiceDrop folder to scan (default: $SCRIBE_VOICEDROP_DIR or ~/…/VoiceDrop)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SCRIBE_WHISPER_MODEL", DEFAULT_WHISPER_MODEL),
        help="faster-whisper model name (default: $SCRIBE_WHISPER_MODEL or base.en)",
    )
    parser.add_argument(
        "--night-cutoff",
        type=int,
        default=int(os.getenv("SCRIBE_NIGHT_CUTOFF", str(DEFAULT_NIGHT_CUTOFF))),
        help="Hour (0-23) before which recordings are attributed to the previous day (default: 4)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Transcribe and print; do not write or mark processed")
    parser.add_argument("--force", "-f", action="store_true", help="Re-process files even if already marked .processed")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print transcript preview and prompt details")
    return parser.parse_args()


def main() -> int:
    args = parse_cli()

    if not args.journal_dir:
        print("[voice] SCRIBE_JOURNAL_DIR is not set. Add it to .env or pass --journal-dir.", file=sys.stderr)
        return 1

    journal_dir = Path(args.journal_dir).expanduser()
    if not journal_dir.is_dir():
        print(f"[voice] journal dir not found: {journal_dir}", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    python_bin = repo_root / "ScribeVenv" / "bin" / "python3"
    scribe_py = repo_root / "Scribe.py"
    learning_file = repo_root / "scribe_learning.json"

    if not python_bin.exists():
        python_bin = Path(sys.executable)
    if not scribe_py.exists():
        print(f"[voice] Scribe.py not found at {scribe_py}", file=sys.stderr)
        return 1

    today_str = datetime.now().strftime("%Y-%m-%d")
    whisper_prompt = extract_whisper_prompt(learning_file, reference_date=today_str)
    term_count = whisper_prompt.count(",") + 1 if whisper_prompt else 0
    print(f"[voice] vocab prompt: {term_count} terms ({len(whisper_prompt)} chars)", file=sys.stderr)

    model = load_whisper_model(args.model)

    # Single-file mode
    if args.audio_file:
        audio_path = Path(args.audio_file).expanduser()
        if not audio_path.exists():
            print(f"[voice] file not found: {audio_path}", file=sys.stderr)
            return 1
        if is_processed(audio_path) and not args.force:
            print(f"[voice] already processed: {audio_path.name} (use --force to re-run)", file=sys.stderr)
            return 0
        if args.force and is_processed(audio_path):
            print(f"[voice] --force: re-processing {audio_path.name}", file=sys.stderr)
        success = process_file(
            audio_path,
            journal_dir=journal_dir,
            model=model,
            whisper_prompt=whisper_prompt,
            python_bin=python_bin,
            scribe_py=scribe_py,
            night_cutoff=args.night_cutoff,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        return 0 if success else 1

    # Scan-dir mode
    drop_dir = Path(args.drop_dir).expanduser()
    if not drop_dir.is_dir():
        print(f"[voice] VoiceDrop dir not found: {drop_dir}", file=sys.stderr)
        print("  Create it in the Files app on your iPhone, or run:", file=sys.stderr)
        print(f"  mkdir -p '{drop_dir}'", file=sys.stderr)
        return 1

    candidates = sorted(
        f for ext in ("*.m4a", "*.wav", "*.mp4", "*.aac")
        for f in drop_dir.glob(ext)
    )
    pending = [f for f in candidates if args.force or not is_processed(f)]
    skipped = len(candidates) - len(pending)
    print(f"[voice] found {len(pending)} to process of {len(candidates)} recordings in {drop_dir}"
          + (f" ({skipped} skipped — use --force to re-run)" if skipped else ""), file=sys.stderr)

    if not pending:
        return 0

    errors = 0
    for audio_path in pending:
        ok = process_file(
            audio_path,
            journal_dir=journal_dir,
            model=model,
            whisper_prompt=whisper_prompt,
            python_bin=python_bin,
            scribe_py=scribe_py,
            night_cutoff=args.night_cutoff,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        if not ok:
            errors += 1

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
