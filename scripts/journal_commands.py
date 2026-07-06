#!/usr/bin/env python3
"""journal_commands.py — Wake-word command recognition for journal notes.

Detects directives *addressed to the assistant* inside a daily note, e.g.:

    "Palindrome, can you pull the latest 10-K for Ford?"

This is distinct from the intent gate (which extracts personal to-dos). A
recognized command maps to an executable route. Today one route exists:
`hot_seat_fetch` (pull a company's latest 10-K into hot_seat).

Recognition is deterministic (no LLM): a wake word anchors the command, and a
few normalizers turn spoken forms ("ten K", "annual report") into a route +
company reference. Company -> ticker resolution happens later in
hot_seat_fetch (OpenAI + EDGAR), not here.

Design notes:
- High precision by construction: nothing fires without the wake word, so
  normal journaling never triggers a route.
- Only filing-fetch phrasing is claimed; any other wake-word sentence returns
  None and falls through to normal intent handling.
"""

import os
import re

DEFAULT_WAKE_WORDS = ["palindrome"]

# Verbs that indicate "acquire this document".
_FETCH_VERBS = (
    "pull up", "pull", "fetch", "grab", "download", "load", "get me",
    "get", "bring up", "bring", "add", "look up", "pull down",
)

# Words dropped when isolating the company reference.
_FILLER = {
    "can", "you", "could", "would", "will", "please", "the", "a", "an",
    "latest", "most", "recent", "newest", "current", "me", "my", "for", "of",
    "on", "hey", "ok", "okay", "and", "thanks", "thank", "now", "to", "up",
    "us", "pull", "fetch", "get", "grab", "download", "load", "add", "bring",
    "find", "look", "report", "annual", "filing", "filings", "yearly",
    "document", "documents", "into", "hot", "seat", "s", "that", "this",
    "their", "its", "from", "down", "some", "go", "ahead",
}

# Form detection -> canonical form. Only 10-K is acted on right now.
_FORM_PATTERNS = [
    (re.compile(r"\b10[\s\-]?k\b", re.IGNORECASE), "10-K"),
    (re.compile(r"\bten[\s\-]?k\b", re.IGNORECASE), "10-K"),
    (re.compile(r"\bannual\s+report\b", re.IGNORECASE), "10-K"),
    (re.compile(r"\bannual\s+filing\b", re.IGNORECASE), "10-K"),
]

_SENTENCE_TERMINATORS = ".?!\n"


def get_wake_words() -> list[str]:
    raw = os.getenv("INTENT_COMMAND_WAKE_WORDS", "").strip()
    if not raw:
        return list(DEFAULT_WAKE_WORDS)
    words = [w.strip().lower() for w in raw.split(",") if w.strip()]
    return words or list(DEFAULT_WAKE_WORDS)


def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def extract_command_spans(note_text: str, wake_words: list[str] | None = None) -> list[dict]:
    """Return wake-word-anchored command spans from note text.

    Each result: {wake, command_text, start, end} where start/end bound the
    full matched span (wake word + command sentence) in the original text.
    """
    if not note_text:
        return []
    words = wake_words if wake_words is not None else get_wake_words()
    if not words:
        return []
    alt = "|".join(re.escape(w) for w in words)
    # Optional "hey/ok/okay" prefix, wake word, optional punctuation, then the
    # command text up to the next sentence terminator.
    pattern = re.compile(
        rf"(?i)(?:\b(?:hey|ok|okay)\s+)?\b(?P<wake>{alt})\b[\s,:;\-]*"
        rf"(?P<cmd>[^{re.escape(_SENTENCE_TERMINATORS)}]+)"
    )
    spans: list[dict] = []
    for m in pattern.finditer(note_text):
        cmd = m.group("cmd").strip()
        if not cmd:
            continue
        spans.append({
            "wake": m.group("wake").lower(),
            "command_text": cmd,
            "start": m.start(),
            "end": m.end(),
        })
    return spans


def detect_form(text: str) -> tuple[str | None, tuple[int, int] | None]:
    """Return (canonical_form, (span_start, span_end)) or (None, None)."""
    for rx, form in _FORM_PATTERNS:
        m = rx.search(text)
        if m:
            return form, (m.start(), m.end())
    return None, None


def _has_fetch_verb(low: str) -> bool:
    return any(v in low for v in _FETCH_VERBS)


def _clean_company(text: str) -> str:
    toks = [w for w in _norm(text).split() if w and w not in _FILLER]
    return " ".join(toks).strip()


def _extract_company(command_text: str, form_span: tuple[int, int] | None) -> str:
    # Remove the form phrase so "for the 10-K" doesn't leak into the company.
    if form_span:
        s, e = form_span
        text = command_text[:s] + " " + command_text[e:]
    else:
        text = command_text
    low = text.lower()
    # Prefer the object of a preposition ("... for Ford", "... of Apple").
    for prep in (" for ", " of ", " on "):
        idx = low.rfind(prep)
        if idx >= 0:
            candidate = _clean_company(text[idx + len(prep):])
            if candidate:
                return candidate
    return _clean_company(text)


def parse_command(command_text: str) -> dict | None:
    """Parse a single command sentence into a route dict, or None.

    Returns {route, form, company, command_text} for a recognized 10-K fetch.
    """
    if not command_text:
        return None
    low = command_text.lower()
    form, form_span = detect_form(command_text)
    if not form:
        return None
    if not _has_fetch_verb(low):
        return None
    company = _extract_company(command_text, form_span)
    if not company:
        return None
    return {
        "route": "hot_seat_fetch",
        "form": form,
        "company": company,
        "command_text": command_text.strip(),
    }


def command_key(cmd: dict) -> str:
    """Stable per-command key for idempotency (route + form + company)."""
    return "|".join([
        str(cmd.get("route", "")),
        str(cmd.get("form", "")).upper(),
        _norm(cmd.get("company", "")),
    ])


def find_commands(note_text: str, wake_words: list[str] | None = None) -> list[dict]:
    """Find + parse all recognized commands in a note.

    Each result merges the parsed route with span metadata and a dedup key.
    Duplicate keys within one note are collapsed (first occurrence wins).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for span in extract_command_spans(note_text, wake_words):
        parsed = parse_command(span["command_text"])
        if not parsed:
            continue
        key = command_key(parsed)
        if key in seen:
            continue
        seen.add(key)
        merged = dict(parsed)
        merged.update({
            "wake": span["wake"],
            "start": span["start"],
            "end": span["end"],
            "key": key,
            "raw": note_text[span["start"]:span["end"]].strip(),
        })
        out.append(merged)
    return out


def strip_command_spans(note_text: str, commands: list[dict]) -> str:
    """Remove recognized command spans so the intent gate doesn't re-handle them."""
    if not commands:
        return note_text
    spans = sorted(
        [(c["start"], c["end"]) for c in commands if "start" in c and "end" in c],
        reverse=True,
    )
    text = note_text
    for start, end in spans:
        text = text[:start] + text[end:]
    return text
