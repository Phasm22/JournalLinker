#!/usr/bin/env python3
"""journal_commands.py — Wake-word command recognition for journal notes.

Detects directives *addressed to the assistant* inside a daily note, e.g.:

    "Palindrome, can you pull the latest 10-K for Ford?"

This is distinct from the intent gate (which extracts personal to-dos). A
recognized command maps to an executable route. Two routes exist:
`hot_seat_fetch` (pull a company's latest 10-K into hot_seat) and
`watchlist_add` ("Palindrome, watch/track/follow <company>" — record the
ticker in the Obsidian watchlist, independent of whether a 10-K exists or
was ever fetched). The two routes use disjoint verb vocabularies so they
never compete for the same utterance.

Recognition is deterministic (no LLM): a wake word anchors the command, and a
few normalizers turn spoken forms ("ten K", "annual report") into a route +
company reference. Company -> ticker resolution happens later in
hot_seat_fetch (OpenAI + EDGAR), not here.

Design notes:
- High precision by construction: nothing fires without the wake word, so
  normal journaling never triggers a route.
- Only fetch-verb or watch-verb phrasing is claimed; any other wake-word
  sentence returns None and falls through to normal intent handling.
"""

import os
import re
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from text_normalize import has_spelled_letters, normalize_command_text

DEFAULT_WAKE_WORDS = ["palindrome"]

# Verbs that indicate "acquire this document", grouped by lemma. "canonical"
# forms are phrasings someone would actually choose; "artifact" forms are
# tense mismatches or homophones a user would never intentionally say as the
# activation verb but that speech-to-text produces (confirmed real cases:
# "pulled"/"pulling" — tense drift, and "pole"/"poll" — a Whisper mishearing
# of "pull"). Both recognize the verb so a real command isn't silently
# dropped; downstream code can still tell the two apart for anomaly tagging.
_FETCH_VERB_FORMS = {
    "pull": {
        "canonical": ["pull up", "pull down", "pull"],
        "artifact": [
            "pulled up", "pulled down", "pulling up", "pulling down",
            "pulled", "pulling", "pole", "poll",
        ],
    },
    "fetch": {
        "canonical": ["fetch"],
        "artifact": ["fetched", "fetching"],
    },
    "grab": {
        "canonical": ["grab"],
        "artifact": ["grabbed", "grabbing"],
    },
    "download": {
        "canonical": ["download"],
        "artifact": ["downloaded", "downloading"],
    },
    "load": {
        "canonical": ["load"],
        "artifact": ["loaded", "loading"],
    },
    "get": {
        "canonical": ["get me", "get"],
        "artifact": ["got me", "got", "getting"],
    },
    "bring": {
        "canonical": ["bring up", "bring"],
        "artifact": ["brought up", "brought", "bringing up", "bringing"],
    },
    "add": {
        "canonical": ["add"],
        "artifact": ["added", "adding"],
    },
    "look up": {
        "canonical": ["look up"],
        "artifact": ["looked up", "looking up"],
    },
}


# Verbs that indicate "start tracking this ticker, no filing needed" —
# distinct vocabulary from _FETCH_VERB_FORMS by design, so the two routes
# never collide on the same word. Tense variants are "artifact" for the same
# reason as the fetch verbs: an activation command phrased in past tense
# ("Palindrome, watched Ford") isn't something you'd intentionally say.
_WATCH_VERB_FORMS = {
    "watch": {
        "canonical": ["watch"],
        "artifact": ["watched", "watching"],
    },
    "track": {
        "canonical": ["track"],
        "artifact": ["tracked", "tracking"],
    },
    "follow": {
        "canonical": ["follow"],
        "artifact": ["followed", "following"],
    },
}


def _flatten_verb_forms(
    forms_map: dict[str, dict[str, list[str]]],
) -> tuple[tuple[str, ...], dict[str, tuple[str, bool]]]:
    """Flatten a lemma dict into (surface forms longest-first, form -> (lemma, is_artifact))."""
    info: dict[str, tuple[str, bool]] = {}
    for lemma, buckets in forms_map.items():
        for form in buckets.get("canonical", []):
            info[form] = (lemma, False)
        for form in buckets.get("artifact", []):
            info[form] = (lemma, True)
    forms = tuple(sorted(info, key=len, reverse=True))
    return forms, info


# All recognized surface forms, longest-first (so "pull up" is preferred over
# a bare "pull" substring match), and a lookup from surface form -> (lemma,
# is_artifact).
_FETCH_VERBS, _VERB_FORM_INFO = _flatten_verb_forms(_FETCH_VERB_FORMS)
_WATCH_VERBS, _WATCH_VERB_FORM_INFO = _flatten_verb_forms(_WATCH_VERB_FORMS)

# Every individual token that appears across all verb surface forms (e.g.
# "pull", "up", "pulled", "pole", "watch", "tracking") is also a filler word
# when isolating the company reference. Deriving this from the same dicts the
# verb matchers use keeps them permanently in sync — a verb tense/synonym
# added to either forms dict is automatically stripped as filler too.
_FETCH_VERB_TOKENS = {tok for form in _VERB_FORM_INFO for tok in form.split()}
_WATCH_VERB_TOKENS = {tok for form in _WATCH_VERB_FORM_INFO for tok in form.split()}

# Words dropped when isolating the company reference.
_FILLER = {
    "can", "you", "could", "would", "will", "please", "the", "a", "an",
    "latest", "most", "recent", "newest", "current", "me", "my", "for", "of",
    "on", "hey", "ok", "okay", "and", "thanks", "thank", "now", "to", "up",
    "us", "find", "look", "report", "annual", "filing", "filings", "yearly",
    "document", "documents", "into", "hot", "seat", "s", "that", "this",
    "their", "its", "it", "from", "down", "some", "go", "ahead",
    "ticker", "symbol",
} | _FETCH_VERB_TOKENS | _WATCH_VERB_TOKENS

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


def _find_fetch_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """Return (matched_form, lemma, is_artifact, start, end) for the first
    matching fetch verb (by lemma-list priority, longest phrase first), or None.
    """
    for form in _FETCH_VERBS:
        idx = low.find(form)
        if idx >= 0:
            lemma, is_artifact = _VERB_FORM_INFO[form]
            return form, lemma, is_artifact, idx, idx + len(form)
    return None


def _find_watch_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """Same as _find_fetch_verb(), over the watch/track/follow vocabulary."""
    for form in _WATCH_VERBS:
        idx = low.find(form)
        if idx >= 0:
            lemma, is_artifact = _WATCH_VERB_FORM_INFO[form]
            return form, lemma, is_artifact, idx, idx + len(form)
    return None


def _find_any_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """Fetch or watch verb match, whichever is found — used only by
    _extract_company()'s before/after split heuristic, which doesn't care
    which route the verb belongs to.
    """
    return _find_fetch_verb(low) or _find_watch_verb(low)


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

    # No prepositional object: the company can also precede the verb
    # entirely ("Ford's 10-K, can you pull it") rather than follow it. Locate
    # the verb fresh in the (already form-stripped) text — re-running the
    # cheap substring search here avoids fragile offset math from splicing
    # the form span out of the original string — and try both sides.
    verb_match = _find_any_verb(low)
    if verb_match:
        _, _, _, v_start, v_end = verb_match
        after = _clean_company(text[v_end:])
        if after:
            return after
        before = _clean_company(text[:v_start])
        if before:
            return before

    return _clean_company(text)


def parse_command_diagnostic(command_text: str) -> tuple[dict | None, dict]:
    """Parse a single command sentence, returning (result, diagnostics).

    `result` is the same dict `parse_command()` returns, or None. `diagnostics`
    explains *why* a None result happened (or flags a match worth a caveat),
    for the anomaly-reporting layer — {reason: "no_form"|"no_verb"|
    "no_company"|"ok", verb_form, verb_lemma, is_artifact_verb,
    had_spelled_letters}. A wake-word span with reason != "ok" produced no
    command at all (nothing downstream ever sees it); is_artifact_verb /
    had_spelled_letters can be true even when reason == "ok".
    """
    diag = {
        "reason": "no_form",
        "verb_form": None,
        "verb_lemma": None,
        "is_artifact_verb": False,
        "had_spelled_letters": False,
    }
    if not command_text:
        return None, diag

    diag["had_spelled_letters"] = has_spelled_letters(command_text)
    normalized = normalize_command_text(command_text)
    low = normalized.lower()

    # Compute form/verb detection independent of each other (not early-return
    # per check) so diagnostics stay informative even when the command is
    # rejected for an earlier reason — e.g. "pole ticker APH" (no form
    # mentioned) still records that "pole" (an artifact of "pull") was said,
    # which the anomaly-reporting layer wants regardless of why parsing
    # ultimately failed.
    form, form_span = detect_form(normalized)
    verb_match = _find_fetch_verb(low)
    if verb_match:
        verb_form, verb_lemma, is_artifact, _v_start, _v_end = verb_match
        diag.update({
            "verb_form": verb_form,
            "verb_lemma": verb_lemma,
            "is_artifact_verb": is_artifact,
        })

    if not form:
        diag["reason"] = "no_form"
        return None, diag
    if not verb_match:
        diag["reason"] = "no_verb"
        return None, diag

    company = _extract_company(normalized, form_span)
    if not company:
        diag["reason"] = "no_company"
        return None, diag

    diag["reason"] = "ok"
    result = {
        "route": "hot_seat_fetch",
        "form": form,
        "company": company,
        "command_text": normalized.strip(),
    }
    return result, diag


def parse_command(command_text: str) -> dict | None:
    """Parse a single command sentence into a route dict, or None.

    Returns {route, form, company, command_text} for a recognized 10-K fetch.
    See parse_command_diagnostic() for the reason behind a None result.
    """
    result, _diag = parse_command_diagnostic(command_text)
    return result


def parse_watchlist_command_diagnostic(command_text: str) -> tuple[dict | None, dict]:
    """Parse "Palindrome, watch/track/follow <company>" into a watchlist_add
    route, or None. Same (result, diagnostics) shape as
    parse_command_diagnostic(), minus the "no_form" reason — this route
    doesn't consult filing type at all, by design: it's for tracking a
    ticker independent of whether a 10-K exists or was ever fetched.
    """
    diag = {
        "reason": "no_verb",
        "verb_form": None,
        "verb_lemma": None,
        "is_artifact_verb": False,
        "had_spelled_letters": False,
    }
    if not command_text:
        return None, diag

    diag["had_spelled_letters"] = has_spelled_letters(command_text)
    normalized = normalize_command_text(command_text)
    low = normalized.lower()

    verb_match = _find_watch_verb(low)
    if not verb_match:
        diag["reason"] = "no_verb"
        return None, diag
    verb_form, verb_lemma, is_artifact, _v_start, _v_end = verb_match
    diag.update({
        "verb_form": verb_form,
        "verb_lemma": verb_lemma,
        "is_artifact_verb": is_artifact,
    })

    company = _extract_company(normalized, None)
    if not company:
        diag["reason"] = "no_company"
        return None, diag

    diag["reason"] = "ok"
    result = {
        "route": "watchlist_add",
        "company": company,
        "command_text": normalized.strip(),
    }
    return result, diag


def parse_watchlist_command(command_text: str) -> dict | None:
    """Parse a single command sentence into a watchlist_add route dict, or None.

    Returns {route, company, command_text}. See
    parse_watchlist_command_diagnostic() for the reason behind a None result.
    """
    result, _diag = parse_watchlist_command_diagnostic(command_text)
    return result


def command_key(cmd: dict) -> str:
    """Stable per-command key for idempotency (route + form + company)."""
    return "|".join([
        str(cmd.get("route", "")),
        str(cmd.get("form", "")).upper(),
        _norm(cmd.get("company", "")),
    ])


def find_commands_with_diagnostics(
    note_text: str, wake_words: list[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Like find_commands(), but also surfaces anomaly records.

    Each wake-word span is tried against both recognized routes: the
    hot_seat_fetch (10-K) parser first, then the watchlist_add parser if that
    doesn't produce a command — the two use disjoint verb vocabularies by
    design, so they never compete for the same utterance.

    An anomaly is a wake-word span that shows real evidence of an attempted
    command — a fetch or watch verb (canonical or artifact) was recognized —
    but either didn't complete (no form/company, e.g. "pole ticker APH" with
    no form mentioned) or completed while matching an artifact verb form or
    spelled-out letters (still executes; just worth flagging as a likely
    transcription artifact). A wake-word span with *no* recognized verb at
    all (ordinary journaling like "Palindrome, remind me to call the
    dentist") is never an anomaly — that's normal, expected fallthrough.
    Returns (commands, anomalies).
    """
    commands: list[dict] = []
    anomalies: list[dict] = []
    seen: set[str] = set()
    for span in extract_command_spans(note_text, wake_words):
        parsed, diag = parse_command_diagnostic(span["command_text"])
        if not parsed:
            watch_parsed, watch_diag = parse_watchlist_command_diagnostic(span["command_text"])
            if watch_parsed:
                parsed, diag = watch_parsed, watch_diag
            elif watch_diag["verb_form"] is not None:
                # Neither route completed, but the watch parser at least
                # recognized a verb (e.g. "watch" with no company at all) —
                # prefer that diagnostic so the anomaly reflects what was
                # actually said.
                diag = watch_diag

        raw = note_text[span["start"]:span["end"]].strip()
        if diag["reason"] == "ok":
            is_anomaly = diag["is_artifact_verb"] or diag["had_spelled_letters"]
        else:
            is_anomaly = diag["verb_form"] is not None
        if not parsed:
            if is_anomaly:
                anomalies.append({**diag, "wake": span["wake"], "start": span["start"],
                                   "end": span["end"], "raw": raw})
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
            "raw": raw,
        })
        commands.append(merged)
        if is_anomaly:
            anomalies.append({**diag, "wake": span["wake"], "start": span["start"],
                               "end": span["end"], "raw": raw,
                               "company": parsed["company"], "form": parsed.get("form", "")})
    return commands, anomalies


def find_commands(note_text: str, wake_words: list[str] | None = None) -> list[dict]:
    """Find + parse all recognized commands in a note.

    Each result merges the parsed route with span metadata and a dedup key.
    Duplicate keys within one note are collapsed (first occurrence wins).
    See find_commands_with_diagnostics() for anomaly visibility.
    """
    commands, _anomalies = find_commands_with_diagnostics(note_text, wake_words)
    return commands


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
