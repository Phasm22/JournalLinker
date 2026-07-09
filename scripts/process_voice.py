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

Env vars (from environment, optional XDG env file, or legacy repo `.env`):
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

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from journal_linker_env import bootstrap_journal_linker_env
from journal_linker_telemetry import maybe_write_job_payload
import journal_commands as jc

RECENCY_LAMBDA = 0.08  # same as Scribe.py
WHISPER_PROMPT_MAX_CHARS = 800  # ~200 tokens; Whisper decoder prefix limit is 223
DEFAULT_WHISPER_MODEL = "base.en"
DEFAULT_NIGHT_CUTOFF = 4

# Mirrors process_intents.py's VOICE_ANOMALY_LOG_FILENAME — kept as a literal
# here rather than importing process_intents.py (heavier module) just for
# one constant.
VOICE_ANOMALY_LOG_FILENAME = "voice_anomalies.jsonl"
PROCESSED_SUFFIX = ".processed"
FAILED_SUFFIX = ".failed"

# Failure kinds written into the .failed marker file.
# Transient = worth retrying automatically (Ollama/network down, Mac was asleep).
# Permanent = do not auto-retry (empty transcript, corrupt audio, bad file).
FAIL_TRANSIENT = "transient"
FAIL_PERMANENT = "permanent"

# Structural-completeness probe (see probe_decodable). We decode with the same
# backend transcription uses. A decoded stream materially shorter than the
# container's declared duration means a truncated tail (the faststart case: the
# front-loaded moov advertises the full length but the mdat is short and decode
# stops early without erroring). Allow a small slack for codec priming / rounding.
PROBE_SAMPLE_RATE = 16000
DECODE_DURATION_TOLERANCE = 0.97       # decoded must reach >=97% of declared …
DECODE_DURATION_ABS_SLACK = 0.5        # … or be within 0.5s, whichever is looser
PROBE_TIMEOUT_SEC = 120                # a decode that hangs longer is treated as transient

# The decode probe runs in a child process (`--_probe-decode`): a malformed
# container can make libav *segfault*, and an uncatchable SIGSEGV in-process
# would take down the whole batch (and, with no marker written, crash it again
# every run). Isolating the decode lets the parent survive and disposition the
# child by its exit code — a signal death (negative returncode) is caught, not
# fatal. Worker exit codes:
PROBE_EXIT_OK = 0            # decoded; stdout carries [decoded_sec, declared_sec]
PROBE_EXIT_CONTENT = 3      # caught content defect (corrupt/EOF/no audio stream)
PROBE_EXIT_ENV = 4         # caught environmental error (I/O, missing codec, …)
PROBE_WORKER_FLAG = "--_probe-decode"

# A transient failure retries on the voice-retry timer. Genuinely time-based
# reasons (Ollama down, machine asleep) resolve within a few cycles; a reason
# that never resolves would otherwise retry forever. Cap the attempts, then
# escalate to permanent and notify once, so a stuck file leaves the queue
# instead of silently looping. Override via SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS.
DEFAULT_MAX_TRANSIENT_ATTEMPTS = 10
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


def _default_state_dir() -> Path:
    """Mirrors process_intents.py's get_state_dir() default."""
    raw = os.getenv("INTENT_STATE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state" / "journal-linker" / "intents"


def _load_anomaly_corrected_terms(state_dir: Path) -> list[str]:
    """Distinct `corrected_term` values from voice_anomalies.jsonl, in the
    order first seen. These are terms Whisper has already gotten wrong once
    (e.g. "OCM" corrected to "OMC") — feeding them back as vocabulary bias is
    the point of logging them in the first place.
    """
    path = state_dir / VOICE_ANOMALY_LOG_FILENAME
    if not path.exists():
        return []
    terms: list[str] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        term = str(record.get("corrected_term") or "").strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def _guaranteed_vocab_terms(state_dir: Path | None = None) -> list[str]:
    """Terms always included in the Whisper prompt, regardless of
    scribe_learning.json success counts: the configured wake word(s), and
    corrected terms from previously-logged voice-command anomalies.
    """
    terms: list[str] = []
    try:
        terms.extend(w.capitalize() for w in jc.get_wake_words())
    except Exception:
        pass
    try:
        terms.extend(_load_anomaly_corrected_terms(state_dir or _default_state_dir()))
    except Exception:
        pass
    return terms


def extract_whisper_prompt(
    learning_file: Path,
    reference_date: str | None = None,
    guaranteed_terms: list[str] | None = None,
) -> str:
    """Read scribe_learning.json, rank terms by success*recency, return prompt str.

    Uses the same RECENCY_LAMBDA and scoring approach as Scribe.py's
    rank_link_candidates so the Whisper vocabulary bias reflects the same
    weights the feedback loop has already learned.

    `guaranteed_terms` (default: wake word(s) + logged anomaly corrections,
    via _guaranteed_vocab_terms()) are included first, ahead of the ranked
    terms, since they shouldn't depend on scribe_learning.json's success
    counts — a term that's never been successfully linked yet (like a
    ticker mentioned for the first time) still needs Whisper's bias.
    """
    if guaranteed_terms is None:
        guaranteed_terms = _guaranteed_vocab_terms()

    scored: list[tuple[float, str]] = []
    try:
        data = json.loads(learning_file.read_text(encoding="utf-8")) if learning_file.exists() else {}
        term_memory = data.get("term_memory")
        if isinstance(term_memory, dict):
            ref_dt = _parse_iso_date(reference_date) or datetime.now()
            DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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
    except Exception:
        scored = []

    scored.sort(reverse=True)

    parts: list[str] = []
    seen_lower: set[str] = set()
    total_chars = len("Topics: ") + 1  # "Topics: " prefix + trailing "."

    def _try_add(term: str) -> None:
        nonlocal total_chars
        term = term.strip()
        if not term:
            return
        key = term.lower()
        if key in seen_lower:
            return
        cost = len(term) + 2  # ", " separator
        if total_chars + cost > WHISPER_PROMPT_MAX_CHARS:
            return
        parts.append(term)
        seen_lower.add(key)
        total_chars += cost

    for term in guaranteed_terms:
        _try_add(term)
    for _, term in scored:
        _try_add(term)

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
        # Guard against duplicate: skip if this exact timestamp callout already exists.
        # Handles the case where a prior run appended the block but Scribe then failed.
        marker = f"> [!voice] {time_str}"
        if marker in existing:
            print(f"[voice] voice block for {time_str} already in {note_path.name}, skipping append", file=sys.stderr)
            return note_path
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


def is_failed(audio_path: Path) -> bool:
    return audio_path.with_suffix(audio_path.suffix + FAILED_SUFFIX).exists()


def mark_processed(audio_path: Path) -> None:
    failed = audio_path.with_suffix(audio_path.suffix + FAILED_SUFFIX)
    if failed.exists():
        failed.unlink()
    audio_path.with_suffix(audio_path.suffix + PROCESSED_SUFFIX).touch()


def _max_transient_attempts() -> int:
    """Attempt cap before a transient failure escalates to permanent."""
    raw = os.getenv("SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS", "").strip()
    if not raw:
        return DEFAULT_MAX_TRANSIENT_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_TRANSIENT_ATTEMPTS


def _read_marker_attempts(audio_path: Path) -> int:
    """Prior transient-attempt count from an existing .failed marker (0 if none
    or legacy/unparsable)."""
    marker = audio_path.with_suffix(audio_path.suffix + FAILED_SUFFIX)
    if not marker.exists():
        return 0
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("attempts:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        return 0
    return 0


def _notify(title: str, body: str) -> None:
    """Best-effort desktop/push notification via the pnotify CLI (TJ's Pushover
    wrapper). Mirrors process_intents' pnotify tier without importing that
    heavy module. Never raises into the pipeline; logs and moves on."""
    import shutil

    pnotify = shutil.which("pnotify")
    if not pnotify:
        candidate = Path.home() / "bin" / "pnotify"
        pnotify = str(candidate) if os.access(candidate, os.X_OK) else ""
    if not pnotify:
        print(f"[voice] (no pnotify) {title}: {body}", file=sys.stderr)
        return
    try:
        subprocess.run([pnotify, title, body], capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        print(f"[voice] pnotify error (ignored): {exc}", file=sys.stderr)


def _write_marker(audio_path: Path, kind: str, reason: str, attempts: int) -> None:
    marker = audio_path.with_suffix(audio_path.suffix + FAILED_SUFFIX)
    marker.write_text(
        f"kind: {kind}\nreason: {reason or 'unknown error'}\nattempts: {attempts}\n",
        encoding="utf-8",
    )


def mark_failed(audio_path: Path, reason: str = "", kind: str = FAIL_PERMANENT) -> None:
    """Write a structured .failed marker, with bounded transient retries.

    kind is FAIL_TRANSIENT or FAIL_PERMANENT. Permanent failures (a content
    verdict — corrupt/truncated audio, empty transcript) are written straight
    through: not retried, not counted. Transient failures are eligible for the
    timed retry loop, but the attempt count is tracked in the marker; once it
    reaches the cap the failure escalates to permanent and fires a one-time
    notification, so a reason that never resolves leaves the queue instead of
    looping forever (and silently).
    """
    if kind != FAIL_TRANSIENT:
        _write_marker(audio_path, FAIL_PERMANENT, reason, _read_marker_attempts(audio_path))
        return

    attempts = _read_marker_attempts(audio_path) + 1
    cap = _max_transient_attempts()
    if attempts >= cap:
        escalated = f"escalated to permanent after {attempts} transient attempts: {reason}"
        _write_marker(audio_path, FAIL_PERMANENT, escalated, attempts)
        print(f"[voice] {audio_path.name}: {escalated}", file=sys.stderr)
        _notify(
            "Voice recording gave up",
            f"{audio_path.name} failed {attempts} transient retries and was "
            f"marked permanent. Last reason: {reason or 'unknown error'}",
        )
        return
    _write_marker(audio_path, FAIL_TRANSIENT, reason, attempts)


def is_transient_failed(audio_path: Path) -> bool:
    """Return True if this file has a .failed marker with kind: transient.

    Legacy markers (plain-text, no kind: prefix) are treated as transient
    because all pre-classification failures came from the processing pipeline
    (Ollama unavailable, Mac asleep), not from permanently bad audio.
    """
    marker = audio_path.with_suffix(audio_path.suffix + FAILED_SUFFIX)
    if not marker.exists():
        return False
    try:
        content = marker.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.strip().startswith("kind:"):
                return line.split(":", 1)[1].strip() == FAIL_TRANSIENT
        # Legacy format — no kind: line; assume transient.
        return True
    except Exception:
        return True


def is_fully_synced(audio_path: Path) -> bool:
    """Return False if the file is an iCloud placeholder not yet downloaded.

    iCloud marks undownloaded files with the com.apple.icloud.itemName xattr
    and/or a zero-byte size. We also trigger a download via brctl so that by
    the time this returns True, the file is locally available and not locked.
    """
    try:
        if audio_path.stat().st_size == 0:
            return False
        # Check for iCloud eviction marker (placeholder not yet downloaded)
        result = subprocess.run(
            ["xattr", "-p", "com.apple.icloud.itemName", str(audio_path)],
            capture_output=True,
        )
        if result.returncode == 0:
            # Trigger download and return False — caller will retry later
            subprocess.run(["brctl", "download", str(audio_path)], capture_output=True)
            return False
        # File is local: trigger brctl download to ensure no partial-write lock,
        # then give iCloud a moment to release any write lock before we open it.
        subprocess.run(["brctl", "download", str(audio_path)], capture_output=True)
        import time as _time
        _time.sleep(0.5)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Structural completeness probe
# ---------------------------------------------------------------------------

def _probe_decode(audio_path: Path) -> tuple[float | None, float]:
    """Decode `audio_path`'s audio stream to EOF via PyAV.

    Returns (decoded_seconds, declared_seconds), or (None, declared) when the
    container opens but carries no audio stream (a definitive content defect,
    not an exception). Decodes with libav directly rather than through
    faster_whisper.decode_audio: it's the same underlying decoder transcription
    uses, but surfaces libav's *typed* errors (InvalidDataError / EOFError) on a
    bad container instead of the bare IndexError decode_audio raises on short
    input — which we need to disposition correctly. Isolated from
    probe_decodable() so tests can stub it without real audio fixtures.
    """
    import av

    with av.open(str(audio_path)) as container:
        declared = (container.duration / av.time_base) if container.duration else 0.0
        if not container.streams.audio:
            return None, declared
        # Sum seconds from each decoded frame's own sample_rate rather than the
        # stream's — a truncated stream can be missing codec params (stream.rate
        # raises), but every decoded frame reliably carries samples + sample_rate.
        decoded_seconds = 0.0
        for frame in container.decode(audio=0):
            rate = frame.sample_rate or PROBE_SAMPLE_RATE
            decoded_seconds += frame.samples / rate
    return decoded_seconds, declared


def _probe_worker(audio_path: str) -> int:
    """Child-process entry: decode the file and report via exit code + stdout.

    Only the two unambiguous content errors (invalid container, premature EOF)
    and a missing audio stream are content defects; everything else is
    environmental. A libav *segfault* here kills this child with a signal — the
    parent sees the negative returncode and survives.
    """
    try:
        import av
        try:
            decoded, declared = _probe_decode(Path(audio_path))
        except (av.error.InvalidDataError, av.error.EOFError):
            return PROBE_EXIT_CONTENT
    except Exception:  # noqa: BLE001 — import/decode env error, default-deny
        return PROBE_EXIT_ENV
    if decoded is None:  # container opened, no audio stream — content defect
        return PROBE_EXIT_CONTENT
    sys.stdout.write(json.dumps([decoded, declared]))
    return PROBE_EXIT_OK


def _reconcile_duration(decoded: float, declared: float) -> tuple[bool, str, str]:
    """Flag a decoded stream that falls materially short of the declared
    duration (the faststart short-decode that doesn't raise)."""
    if declared > 0:
        allowed_shortfall = max(DECODE_DURATION_ABS_SLACK,
                                declared * (1.0 - DECODE_DURATION_TOLERANCE))
        if (declared - decoded) > allowed_shortfall:
            return (False, FAIL_PERMANENT,
                    f"truncated: decoded {decoded:.1f}s < declared {declared:.1f}s")
    return True, "", ""


def probe_decodable(audio_path: Path) -> tuple[bool, str, str]:
    """Verify a recording is a structurally complete, decodable container.

    Returns (ok, kind, reason). The decode runs in an isolated child process so a
    libav crash on a malformed file can't take down the batch. Disposition:
    FAIL_PERMANENT for a deterministic content defect (corrupt, no audio stream,
    or decoded materially shorter than declared); FAIL_TRANSIENT for an
    environmental error, a timeout, or a signal death (crash/OOM) — default-deny
    toward a bounded retry, never a silent pass. Runs before transcription so a
    partial/corrupt file is caught instead of yielding a plausible-but-truncated
    transcript that gets marked processed.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), PROBE_WORKER_FLAG, str(audio_path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, FAIL_TRANSIENT, f"decode timed out after {PROBE_TIMEOUT_SEC}s"

    rc = proc.returncode
    if rc == PROBE_EXIT_CONTENT:
        return False, FAIL_PERMANENT, "corrupt / truncated / no audio stream"
    if rc == PROBE_EXIT_ENV:
        return False, FAIL_TRANSIENT, "environmental decode error"
    if rc < 0:
        # Killed by a signal (e.g. SIGSEGV from libav on hostile bytes, or OOM):
        # cause is ambiguous, so default-deny to a bounded transient retry. The
        # win is that the parent survived and the file got dispositioned.
        return False, FAIL_TRANSIENT, f"decode crashed (signal {-rc})"
    if rc != PROBE_EXIT_OK:
        return False, FAIL_TRANSIENT, f"probe exited {rc}"
    try:
        decoded, declared = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001 — malformed worker output, default-deny
        return False, FAIL_TRANSIENT, "probe produced no result"
    return _reconcile_duration(decoded, declared)


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

    # Structural completeness gate: reject a corrupt or truncated file up front
    # rather than letting it become a plausible-but-short transcript that gets
    # marked processed. Content-deterministic failures go straight to permanent;
    # environmental ones stay transient (see probe_decodable / mark_failed).
    ok, fail_kind, fail_reason = probe_decodable(audio_path)
    if not ok:
        print(f"[voice] incomplete/undecodable {audio_path.name}: {fail_reason} "
              f"({fail_kind})", file=sys.stderr)
        if not dry_run:
            mark_failed(audio_path, fail_reason, kind=fail_kind)
        return False

    if verbose and whisper_prompt:
        print(f"[voice] whisper prompt ({len(whisper_prompt)} chars): {whisper_prompt[:120]}…", file=sys.stderr)

    try:
        transcript, duration, language = transcribe_audio(audio_path, model, whisper_prompt)
    except Exception as exc:
        # Reached only after probe_decodable() vetted the container, so a failure
        # here is genuinely model/infra (model load, resampler I/O) — not the
        # file — and is correctly transient.
        print(f"[voice] transcription failed for {audio_path.name}: {exc}", file=sys.stderr)
        if not dry_run:
            # Treat as transient — model load or I/O error may resolve on retry.
            mark_failed(audio_path, f"transcription error: {exc}", kind=FAIL_TRANSIENT)
        return False

    print(
        f"[voice] transcribed {duration:.1f}s audio [{language}]: {len(transcript.split())} words",
        file=sys.stderr,
    )
    if verbose:
        print(f"[voice] transcript: {transcript[:300]}", file=sys.stderr)

    if not transcript.strip():
        print(f"[voice] empty transcript, skipping {audio_path.name}", file=sys.stderr)
        if not dry_run:
            # Permanent — re-transcribing the same audio won't produce words.
            mark_failed(audio_path, "empty transcript", kind=FAIL_PERMANENT)
        return False

    append_voice_block(journal_dir, date_str, time_str, transcript, dry_run=dry_run)

    scribe_exit = run_scribe(python_bin, scribe_py, date_str, dry_run=dry_run)
    if scribe_exit != 0:
        print(f"[voice] Scribe exited {scribe_exit} for {audio_path.name}", file=sys.stderr)
        if not dry_run:
            # Transient — Ollama may have been unavailable (Mac asleep, model not loaded).
            mark_failed(audio_path, f"Scribe exit code {scribe_exit}", kind=FAIL_TRANSIENT)
        return False

    if not dry_run:
        mark_processed(audio_path)
        print(f"[voice] marked processed: {audio_path.name}", file=sys.stderr)

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_cli() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    bootstrap_journal_linker_env(repo_root=repo_root)

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
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Re-process files marked .failed with kind: transient (e.g. Ollama was down, Mac was asleep)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print transcript preview and prompt details")
    return parser.parse_args()


def _write_voice_payload(**fields) -> None:
    maybe_write_job_payload(**fields)


def main() -> int:
    # Isolated decode-probe child (see probe_decodable). Handled before the
    # heavy env bootstrap / arg parsing so it stays a cheap, self-contained run.
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == PROBE_WORKER_FLAG:
        return _probe_worker(argv[1])

    args = parse_cli()

    if not args.journal_dir:
        print(
            "[voice] SCRIBE_JOURNAL_DIR is not set. Export it, add it to ~/.config/journal-linker/journal-linker.env, "
            "or pass --journal-dir.",
            file=sys.stderr,
        )
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
        _write_voice_payload(
            items_processed=1 if success else 0,
            items_failed=0 if success else 1,
            items_skipped_placeholder=0,
            items_skipped_already=0,
            items_pending_at_start=1,
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

    if args.force:
        pending = list(candidates)
    elif args.retry:
        # Only pick up transient failures; leave permanent ones alone.
        pending = [f for f in candidates if not is_processed(f) and (
            not is_failed(f) or is_transient_failed(f)
        )]
        transient_retry = [f for f in candidates if is_transient_failed(f)]
        permanent = [f for f in candidates if is_failed(f) and not is_transient_failed(f)]
        if transient_retry:
            print(f"[voice] retrying {len(transient_retry)} transient-failed file(s): "
                  + ", ".join(f.name for f in transient_retry), file=sys.stderr)
        if permanent:
            print(f"[voice] skipping {len(permanent)} permanently-failed file(s) (use --force to override): "
                  + ", ".join(f.name for f in permanent), file=sys.stderr)
    else:
        pending = [f for f in candidates if not is_processed(f) and not is_failed(f)]
        failed_files = [f for f in candidates if is_failed(f)]
        if failed_files:
            transient = [f for f in failed_files if is_transient_failed(f)]
            permanent = [f for f in failed_files if not is_transient_failed(f)]
            if transient:
                print(f"[voice] {len(transient)} transient-failed file(s) pending retry (use --retry): "
                      + ", ".join(f.name for f in transient), file=sys.stderr)
            if permanent:
                print(f"[voice] {len(permanent)} permanently-failed file(s) (use --force to override): "
                      + ", ".join(f.name for f in permanent), file=sys.stderr)
    # Drop iCloud placeholders — WatchPaths fires before the file downloads
    synced = [f for f in pending if is_fully_synced(f)]
    waiting = [f for f in pending if not is_fully_synced(f)]
    if waiting:
        print(f"[voice] skipping {len(waiting)} file(s) still downloading from iCloud: "
              + ", ".join(f.name for f in waiting), file=sys.stderr)
    pending = synced
    items_skipped_already = max(0, len(candidates) - len(pending) - len(waiting))
    items_skipped_placeholder = len(waiting)
    items_pending_at_start = len(pending)
    skipped = items_skipped_already
    print(f"[voice] found {len(pending)} to process of {len(candidates)} recordings in {drop_dir}"
          + (f" ({skipped} already processed — use --force to re-run)" if skipped else ""), file=sys.stderr)

    if not pending:
        _write_voice_payload(
            items_processed=0,
            items_failed=0,
            items_skipped_placeholder=items_skipped_placeholder,
            items_skipped_already=items_skipped_already,
            items_pending_at_start=0,
        )
        return 0

    errors = 0
    processed = 0
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
        if ok:
            processed += 1
        else:
            errors += 1

    _write_voice_payload(
        items_processed=processed,
        items_failed=errors,
        items_skipped_placeholder=items_skipped_placeholder,
        items_skipped_already=items_skipped_already,
        items_pending_at_start=items_pending_at_start,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
