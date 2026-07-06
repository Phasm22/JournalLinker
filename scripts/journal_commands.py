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

import json
import os
import re
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from text_normalize import has_spelled_letters, normalize_command_text

DEFAULT_WAKE_WORDS = ["palindrome"]

# Route names for the two built-in routes. `journal_commands` exposes a couple
# of route-specific helper wrappers (parse_command / parse_watchlist_command)
# bound to these names for callers, but recognition itself is fully
# config-driven and generic (see _ROUTES) — a brand-new route can be added by
# editing voice_command_routes.json alone, with no code change here.
_HOT_SEAT_ROUTE = "hot_seat_fetch"
_WATCHLIST_ROUTE = "watchlist_add"

# Declarative route definitions. Each route's verb forms are grouped by lemma:
# "canonical" forms are phrasings someone would actually choose; "artifact"
# forms are tense mismatches or homophones a user would never intentionally say
# as the activation verb but that speech-to-text produces (confirmed real
# cases: "pulled"/"pulling" — tense drift, and "pole"/"poll" — a Whisper
# mishearing of "pull"). Both recognize the verb so a real command isn't
# silently dropped; downstream code can still tell the two apart for anomaly
# tagging. Routes use disjoint verb vocabularies by design, so they never
# compete for the same utterance.
_ROUTES_CONFIG_FILENAME = "voice_command_routes.json"


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


def _routes_config_path() -> Path:
    """Resolve the route-config JSON path (env override, else next to module)."""
    raw = os.getenv("INTENT_VOICE_ROUTES_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / _ROUTES_CONFIG_FILENAME


def _build_route(entry: dict) -> dict:
    """Compile one config route entry into its runtime recognition structure."""
    verb_forms = entry.get("verb_forms", {})
    # Longest-first surface forms (so "pull up" beats a bare "pull") + a lookup
    # from surface form -> (lemma, is_artifact).
    verbs, info = _flatten_verb_forms(verb_forms)
    # Every individual token across all verb surface forms (e.g. "pull", "up",
    # "pulled", "pole", "watch", "tracking") becomes a filler word when
    # isolating the company reference. Deriving this from the same forms the
    # verb matchers use keeps them permanently in sync — a verb tense/synonym
    # added to the config is automatically stripped as filler too.
    tokens = {tok for form in info for tok in form.split()}
    canonical = entry.get("form_canonical")
    form_patterns = [
        (re.compile(p, re.IGNORECASE), canonical)
        for p in entry.get("form_patterns", [])
    ]
    return {
        "name": entry["name"],
        "requires_form": bool(entry.get("requires_form", False)),
        "form_canonical": canonical,
        "verb_forms": verb_forms,
        "verbs": verbs,
        "verb_form_info": info,
        "verb_tokens": tokens,
        "form_patterns": form_patterns,
    }


def _load_route_config(path: Path | None = None) -> dict[str, dict]:
    """Load route definitions from the JSON config into a name -> route dict,
    preserving config order (form-requiring routes are listed first, which
    fixes their recognition precedence)."""
    cfg_path = path or _routes_config_path()
    with open(cfg_path, encoding="utf-8") as fh:
        data = json.load(fh)
    routes: dict[str, dict] = {}
    for entry in data.get("routes", []):
        routes[entry["name"]] = _build_route(entry)
    return routes


_ROUTES = _load_route_config()

# Public bindings over the config-driven structures, kept so existing callers
# and tests import the same names. These derive from _ROUTES rather than being
# hand-maintained — the config is the single source of truth.
_FETCH_VERB_FORMS = _ROUTES[_HOT_SEAT_ROUTE]["verb_forms"]
_WATCH_VERB_FORMS = _ROUTES[_WATCHLIST_ROUTE]["verb_forms"]
_FETCH_VERBS = _ROUTES[_HOT_SEAT_ROUTE]["verbs"]
_VERB_FORM_INFO = _ROUTES[_HOT_SEAT_ROUTE]["verb_form_info"]
_WATCH_VERBS = _ROUTES[_WATCHLIST_ROUTE]["verbs"]
_WATCH_VERB_FORM_INFO = _ROUTES[_WATCHLIST_ROUTE]["verb_form_info"]
_FETCH_VERB_TOKENS = _ROUTES[_HOT_SEAT_ROUTE]["verb_tokens"]
_WATCH_VERB_TOKENS = _ROUTES[_WATCHLIST_ROUTE]["verb_tokens"]

# Form detection -> canonical form for the hot_seat route (kept as a public
# binding; per-route detection uses each route's own compiled patterns).
_FORM_PATTERNS = _ROUTES[_HOT_SEAT_ROUTE]["form_patterns"]

# Words dropped when isolating the company reference. The verb tokens come from
# *every* configured route's forms (union), so adding a route via config alone
# keeps the verb/filler invariant that this whole feature exists to protect.
_ALL_VERB_TOKENS: set[str] = set().union(
    *(route["verb_tokens"] for route in _ROUTES.values())
) if _ROUTES else set()
_FILLER = {
    "can", "you", "could", "would", "will", "please", "the", "a", "an",
    "latest", "most", "recent", "newest", "current", "me", "my", "for", "of",
    "on", "hey", "ok", "okay", "and", "thanks", "thank", "now", "to", "up",
    "us", "find", "look", "report", "annual", "filing", "filings", "yearly",
    "document", "documents", "into", "hot", "seat", "s", "that", "this",
    "their", "its", "it", "from", "down", "some", "go", "ahead",
    "ticker", "symbol",
} | _ALL_VERB_TOKENS

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


def _detect_form_for_route(
    text: str, route: dict
) -> tuple[str | None, tuple[int, int] | None]:
    """Return (canonical_form, (span_start, span_end)) for a route's own form
    patterns, or (None, None)."""
    for rx, form in route["form_patterns"]:
        m = rx.search(text)
        if m:
            return form, (m.start(), m.end())
    return None, None


def detect_form(text: str) -> tuple[str | None, tuple[int, int] | None]:
    """Return (canonical_form, (span_start, span_end)) or (None, None).

    Uses the hot_seat route's filing-type patterns (the only form-requiring
    route today); route-generic code calls _detect_form_for_route().
    """
    return _detect_form_for_route(text, _ROUTES[_HOT_SEAT_ROUTE])


def _find_verb_for_route(
    low: str, route_name: str
) -> tuple[str, str, bool, int, int] | None:
    """Return (matched_form, lemma, is_artifact, start, end) for the first
    matching verb of `route_name` (by lemma-list priority, longest phrase
    first), or None.
    """
    route = _ROUTES[route_name]
    info = route["verb_form_info"]
    for form in route["verbs"]:
        idx = low.find(form)
        if idx >= 0:
            lemma, is_artifact = info[form]
            return form, lemma, is_artifact, idx, idx + len(form)
    return None


def _find_fetch_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """First matching hot_seat fetch verb, or None."""
    return _find_verb_for_route(low, _HOT_SEAT_ROUTE)


def _find_watch_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """Same as _find_fetch_verb(), over the watch/track/follow vocabulary."""
    return _find_verb_for_route(low, _WATCHLIST_ROUTE)


def _find_any_verb(low: str) -> tuple[str, str, bool, int, int] | None:
    """First verb match across all configured routes, whichever is found —
    used only by _extract_company()'s before/after split heuristic, which
    doesn't care which route the verb belongs to.
    """
    for route_name in _ROUTES:
        match = _find_verb_for_route(low, route_name)
        if match:
            return match
    return None


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


# Parse-level confidence tiers. A clean canonical verb with no spelled-out
# letters is a full-confidence match; an artifact verb (tense drift/homophone
# Whisper produced) or spelled-out letters ("O-C-M") is a recognized-but-shaky
# signal worth a lower score. Ticker-resolution confidence (e.g. a fuzzy
# difflib match) is a separate tier composed in later (see hot_seat_fetch
# .resolver_confidence and run_command_stage).
_CONFIDENCE_CANONICAL = 1.0
_CONFIDENCE_ARTIFACT = 0.7


def parse_confidence(is_artifact_verb: bool, had_spelled_letters: bool) -> float:
    """Parse-level confidence for a recognized command (0.0-1.0).

    Canonical verb + no spelled-out letters (+ an exact form match, which a
    successful form-requiring parse already implies) -> 1.0; an artifact verb
    or spelled-out letters -> 0.7. Kept in one place so the tiers can't drift.
    """
    if is_artifact_verb or had_spelled_letters:
        return _CONFIDENCE_ARTIFACT
    return _CONFIDENCE_CANONICAL


def parse_route_diagnostic(command_text: str, route_name: str) -> tuple[dict | None, dict]:
    """Parse a single command sentence against one configured route, returning
    (result, diagnostics).

    `result` is a route dict ({route, [form,] company, command_text}) or None.
    `diagnostics` explains *why* a None result happened (or flags a match worth
    a caveat), for the anomaly-reporting layer — {reason: "no_form"|"no_verb"|
    "no_company"|"ok", verb_form, verb_lemma, is_artifact_verb,
    had_spelled_letters}. A route that doesn't require a filing type ("form")
    never yields "no_form". A wake-word span with reason != "ok" produced no
    command at all (nothing downstream ever sees it); is_artifact_verb /
    had_spelled_letters can be true even when reason == "ok".
    """
    route = _ROUTES[route_name]
    requires_form = route["requires_form"]
    diag = {
        "reason": "no_form" if requires_form else "no_verb",
        "route": route_name,
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
    form, form_span = (None, None)
    if requires_form:
        form, form_span = _detect_form_for_route(normalized, route)
    verb_match = _find_verb_for_route(low, route_name)
    if verb_match:
        verb_form, verb_lemma, is_artifact, _v_start, _v_end = verb_match
        diag.update({
            "verb_form": verb_form,
            "verb_lemma": verb_lemma,
            "is_artifact_verb": is_artifact,
        })

    if requires_form and not form:
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
    confidence = parse_confidence(diag["is_artifact_verb"], diag["had_spelled_letters"])
    diag["confidence"] = confidence
    result: dict = {"route": route_name}
    if requires_form:
        result["form"] = form
    result["company"] = company
    result["command_text"] = normalized.strip()
    result["confidence"] = confidence
    return result, diag


def parse_command_diagnostic(command_text: str) -> tuple[dict | None, dict]:
    """Parse a single command sentence for the hot_seat 10-K fetch route.

    Thin wrapper over parse_route_diagnostic() bound to hot_seat_fetch; see
    there for the (result, diagnostics) shape.
    """
    return parse_route_diagnostic(command_text, _HOT_SEAT_ROUTE)


def parse_command(command_text: str) -> dict | None:
    """Parse a single command sentence into a route dict, or None.

    Returns {route, form, company, command_text} for a recognized 10-K fetch.
    See parse_command_diagnostic() for the reason behind a None result.
    """
    result, _diag = parse_command_diagnostic(command_text)
    return result


def parse_watchlist_command_diagnostic(command_text: str) -> tuple[dict | None, dict]:
    """Parse "Palindrome, watch/track/follow <company>" into a watchlist_add
    route, or None. Thin wrapper over parse_route_diagnostic() — this route
    doesn't consult filing type at all, by design: it's for tracking a ticker
    independent of whether a 10-K exists or was ever fetched.
    """
    return parse_route_diagnostic(command_text, _WATCHLIST_ROUTE)


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
        # Try each configured route in config order (form-requiring routes
        # first). The first route that produces a command wins. If none does,
        # keep the most informative diagnostic — one that at least recognized a
        # verb (e.g. "watch" with no company) — so the anomaly reflects what
        # was actually said rather than an earlier route's empty miss.
        parsed: dict | None = None
        diag: dict | None = None
        for route_name in _ROUTES:
            p, d = parse_route_diagnostic(span["command_text"], route_name)
            if p:
                parsed, diag = p, d
                break
            if diag is None or (diag["verb_form"] is None and d["verb_form"] is not None):
                diag = d

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
