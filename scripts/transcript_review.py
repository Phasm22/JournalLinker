"""Checks that run on every voice transcript after Scribe.

Whisper's word probabilities and segment scores decide where to look.
The phrase table, a near-miss against the learning store, and the model
already loaded in Ollama decide the replacement. A repeated loop is only
quoted. The note is not rewritten here.

One finding is one quiet Pushover. A Fix link is attached only when the
replacement is already known and the span appears once in the saved note.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fix_link

WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
VOICE_BLOCK_RE = re.compile(
    r"^> \[!voice\][ \t]+(\d{2}:\d{2})[ \t]*\n((?:>(?: .*)?\n?)*)",
    re.M,
)
MAX_PINGS = 3
WORD_PROB_THRESHOLD = 0.45
LOGPROB_THRESHOLD = -1.0
COMPRESSION_THRESHOLD = 2.4
REVIEW_LOG = "transcript_reviews.jsonl"
_CORRECTIONS = Path(__file__).resolve().parent / "transcript_corrections.json"

_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "it", "that", "this", "i", "you", "we",
    "they", "my", "me", "so", "if", "not", "was", "were", "be", "been", "am",
    "are", "do", "did", "just", "like", "yeah", "um", "uh",
})
_COMMON_WORDS = _FUNCTION_WORDS | frozenset({
    "about", "after", "again", "because", "before", "being", "could", "other",
    "really", "should", "there", "their", "these", "those", "through", "would",
    "which", "where", "while", "little", "going", "gonna",
})

_CHECK_TITLES = {
    "known_mishear": "Saved phrase",
    "vocab_near_miss": "Vocabulary near-miss",
    "uncertain_words": "Uncertain words",
    "weak_segment": "Weak segment",
    "repeated_loop": "Repeated phrase",
}
_WITH_REPLACEMENT = ("known_mishear", "vocab_near_miss", "uncertain_words", "weak_segment")


def review_enabled() -> bool:
    raw = os.getenv("TRANSCRIPT_REVIEW", "on").strip().lower()
    return raw not in {"0", "off", "false", "no", "none", "disable", "disabled"}


def load_corrections(path: Path | None = None) -> list[tuple[str, str]]:
    source = path or _CORRECTIONS
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    rows = []
    for heard, suggestion in data.items():
        heard_s = str(heard).strip()
        suggestion_s = str(suggestion).strip()
        if heard_s and suggestion_s and heard_s.lower() != suggestion_s.lower():
            rows.append((heard_s, suggestion_s))
    return rows


def load_vocab_terms(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    memory = data.get("term_memory") if isinstance(data, dict) else None
    if not isinstance(memory, dict):
        return []
    terms = []
    for record in memory.values():
        if not isinstance(record, dict):
            continue
        if not (record.get("success_count") or 0):
            continue
        term = str(record.get("term") or "").strip()
        if len(term) >= 6 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", term):
            terms.append(term)
    return terms


def word_spans(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]


def find_phrase(text: str, phrase: str) -> list[list[tuple[int, int]]]:
    wanted = [w.lower() for w in WORD_RE.findall(phrase)]
    if not wanted:
        return []
    spans = word_spans(text)
    hits = []
    width = len(wanted)
    for index in range(len(spans) - width + 1):
        window = [spans[index + offset][0].lower() for offset in range(width)]
        if window == wanted:
            hits.append([(spans[index + offset][1], spans[index + offset][2]) for offset in range(width)])
    return hits


def splice_span(text: str, offsets: list[tuple[int, int]], suggestion: str) -> tuple[str, str]:
    """Exact file slice covering the words, and that slice with the suggestion in it."""
    start = offsets[0][0]
    end = offsets[-1][1]
    old = text[start:end]
    relative = [(begin - start, finish - start) for begin, finish in offsets]
    updated = old
    for begin, finish in reversed(relative[1:]):
        updated = updated[:begin] + updated[finish:]
    first_begin, first_finish = relative[0]
    updated = updated[:first_begin] + suggestion + updated[first_finish:]
    lines = []
    for line in updated.split("\n"):
        prefix = ""
        rest = line
        if rest.startswith("> "):
            prefix, rest = "> ", rest[2:]
        elif rest.startswith(">"):
            prefix, rest = ">", rest[1:]
        rest = re.sub(r" {2,}", " ", rest).strip()
        if prefix == "> " and not rest:
            # The space before the next word sits outside this slice.
            lines.append(">")
        elif prefix == ">":
            lines.append("> " + rest if rest else ">")
        else:
            lines.append(prefix + rest)
    return old, "\n".join(lines)


def edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 1:
        return 2
    previous = list(range(len(right) + 1))
    for i, ch_left in enumerate(left, start=1):
        current = [i]
        for j, ch_right in enumerate(right, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace = previous[j - 1] + (ch_left != ch_right)
            current.append(min(insert, delete, replace))
        previous = current
    return previous[-1]


def _plural_pair(left: str, right: str) -> bool:
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return long == short + "s" or long == short + "es"


def check_known_mishear(text: str, corrections: list[tuple[str, str]]) -> list[dict]:
    findings = []
    for heard, suggestion in corrections:
        if find_phrase(text, heard):
            findings.append({
                "check": "known_mishear",
                "heard": heard,
                "suggestion": suggestion,
                "why": "phrase table",
            })
    return findings


def check_vocab_near_miss(text: str, terms: list[str]) -> list[dict]:
    spans = word_spans(text)
    words = [word.lower() for word, _, _ in spans]
    present = set(words)
    findings = []
    seen: set[str] = set()
    for term in terms:
        pieces = [part.lower() for part in WORD_RE.findall(term)]
        if not pieces or any(part in _COMMON_WORDS for part in pieces):
            continue
        if all(part in present for part in pieces):
            continue
        if not any(ch.isupper() for ch in term):
            continue
        if len(pieces) == 1:
            canonical = pieces[0]
            if len(canonical) < 7:
                continue
            for word, _, _ in spans:
                heard_word = word.lower()
                if heard_word in _COMMON_WORDS or len(heard_word) < 7 or _plural_pair(heard_word, canonical):
                    continue
                if edit_distance(heard_word, canonical) == 1:
                    key = heard_word
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({
                        "check": "vocab_near_miss",
                        "heard": word,
                        "suggestion": term,
                        "why": "one letter from a learned name",
                    })
                    break
        elif len(pieces) == 2:
            for index in range(len(words) - 1):
                pair = words[index:index + 2]
                if pair[0] in _COMMON_WORDS or pair[1] in _COMMON_WORDS:
                    continue
                first_ok = pair[0] == pieces[0] or edit_distance(pair[0], pieces[0]) == 1
                second_ok = pair[1] == pieces[1] or edit_distance(pair[1], pieces[1]) == 1
                if not (first_ok and second_ok):
                    continue
                if pair == pieces:
                    continue
                heard = f"{spans[index][0]} {spans[index + 1][0]}"
                key = heard.lower()
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "check": "vocab_near_miss",
                    "heard": heard,
                    "suggestion": term,
                    "why": "close to a learned term",
                })
                break
        if len(findings) >= 3:
            break
    return findings


def check_repeated_loop(text: str) -> list[dict]:
    """Flag a 4-gram said three times back to back. Scattered repeats are speech."""
    words = [word for word, _, _ in word_spans(text)]
    if len(words) < 12:
        return []
    for index in range(len(words) - 11):
        gram = tuple(word.lower() for word in words[index:index + 4])
        if gram != tuple(word.lower() for word in words[index + 4:index + 8]):
            continue
        if gram != tuple(word.lower() for word in words[index + 8:index + 12]):
            continue
        return [{
            "check": "repeated_loop",
            "heard": " ".join(gram),
            "suggestion": "",
            "why": "repeated three times in a row",
        }]
    return []


def check_uncertain_words(segments: list[dict]) -> list[dict]:
    words: list[tuple[str, float]] = []
    for segment in segments:
        for word, probability in segment.get("words") or []:
            cleaned = str(word).strip()
            if cleaned:
                words.append((cleaned, float(probability)))
    findings = []
    group: list[str] = []

    def flush() -> None:
        if not group:
            return
        while group and group[0].lower() in _FUNCTION_WORDS:
            group.pop(0)
        while group and group[-1].lower() in _FUNCTION_WORDS:
            group.pop()
        if group:
            findings.append({
                "check": "uncertain_words",
                "heard": " ".join(group),
                "suggestion": "",
                "why": "whisper was unsure of these words",
            })
        group.clear()

    for word, probability in words:
        if probability < WORD_PROB_THRESHOLD:
            group.append(word)
        else:
            flush()
    flush()
    return findings


def check_weak_segment(segments: list[dict]) -> list[dict]:
    findings = []
    for segment in segments:
        text = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
        if not text:
            continue
        logprob = float(segment.get("avg_logprob") or 0)
        compression = float(segment.get("compression_ratio") or 0)
        if logprob < LOGPROB_THRESHOLD or compression > COMPRESSION_THRESHOLD:
            findings.append({
                "check": "weak_segment",
                "heard": text,
                "suggestion": "",
                "why": "whisper was unsure of this stretch",
            })
    return findings


def _covered(finding: dict, explained: list[dict]) -> bool:
    heard = {word.lower() for word in WORD_RE.findall(finding.get("heard") or "")}
    if not heard:
        return False
    for other in explained:
        other_words = {word.lower() for word in WORD_RE.findall(other.get("heard") or "")}
        if heard & other_words:
            return True
    return False


def _dedupe(findings: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for finding in findings:
        if _covered(finding, kept):
            continue
        kept.append(finding)
    return kept


def resident_model_name() -> str | None:
    try:
        import ollama  # type: ignore
        listed = ollama.ps()
    except Exception:
        return None
    models = listed.get("models") if isinstance(listed, dict) else getattr(listed, "models", None)
    if not models:
        return None
    first = models[0]
    name = first.get("name") if isinstance(first, dict) else getattr(first, "name", "") or getattr(first, "model", "")
    return str(name).strip() or None


def suggest_for_span(sentence: str, span: str, model: str) -> str:
    """Ask the already-loaded model for replacement words, or empty."""
    import ollama  # type: ignore

    prompt = (
        "A speech transcript has one unsure span. "
        "Reply with only the replacement words, or NONE.\n"
        f"Sentence: {sentence}\n"
        f"Unsure span: {span}"
    )
    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0, "num_ctx": 2048},
        keep_alive="5m",
    )
    content = response["message"]["content"] if isinstance(response, dict) else response.message.content
    text = re.sub(r"\s+", " ", str(content)).strip().strip('"')
    if not text or text.lower() == "none":
        return ""
    if "\n" in str(content):
        return ""
    span_words = WORD_RE.findall(span)
    suggestion_words = WORD_RE.findall(text)
    if not suggestion_words or len(suggestion_words) > max(8, len(span_words) + 3):
        return ""
    if text.lower() == re.sub(r"\s+", " ", span).strip().lower():
        return ""
    return text


def fill_uncertain_suggestions(text: str, findings: list[dict], model: str | None) -> None:
    if not model:
        return
    for finding in findings:
        if finding["check"] not in {"uncertain_words", "weak_segment"} or finding.get("suggestion"):
            continue
        heard = finding["heard"]
        window = _sentence_around(text, heard)
        try:
            suggestion = suggest_for_span(window, heard, model)
        except Exception as exc:  # noqa: BLE001 — a cold model must not fail the check
            print(f"[voice] span suggestion skipped: {exc}", file=sys.stderr)
            return
        if suggestion:
            finding["suggestion"] = suggestion


def _sentence_around(text: str, heard: str) -> str:
    flat = re.sub(r"\s+", " ", text)
    lowered = flat.lower()
    needle = re.sub(r"\s+", " ", heard).strip().lower()
    index = lowered.find(needle)
    if index < 0:
        return flat[:240]
    start = max(0, index - 80)
    end = min(len(flat), index + len(needle) + 80)
    return flat[start:end].strip()


def run_checks(
    text: str,
    *,
    segments: list[dict] | None = None,
    corrections: list[tuple[str, str]] | None = None,
    terms: list[str] | None = None,
    model: str | None = None,
    ask_model: bool = True,
) -> list[dict]:
    """Run every check. Model is used only to name a replacement for an unsure span."""
    segments = segments or []
    table = check_known_mishear(text, corrections if corrections is not None else load_corrections())
    vocab = check_vocab_near_miss(text, terms or [])
    loop = check_repeated_loop(text)
    unsure = check_uncertain_words(segments) + check_weak_segment(segments)
    explained = [item for item in table + vocab if item.get("suggestion")]
    unsure = [item for item in unsure if not _covered(item, explained)]
    if ask_model:
        fill_uncertain_suggestions(text, unsure, model if model is not None else resident_model_name())
    ordered = table + vocab + [item for item in unsure if item.get("suggestion")]
    ordered += [item for item in unsure if not item.get("suggestion")]
    ordered += loop
    return _dedupe(ordered)


def locate_replacement(note: str, finding: dict) -> tuple[str, str] | None:
    """File slice and its replacement, only when the span appears once."""
    suggestion = str(finding.get("suggestion") or "").strip()
    if not suggestion:
        return None
    hits = find_phrase(note, finding["heard"])
    if len(hits) != 1:
        return None
    old, new = splice_span(note, hits[0], suggestion)
    if not old or old == new:
        return None
    if note.count(old) != 1:
        return None
    return old, new


def excerpt_around(text: str, needle: str, *, wing: int = 36) -> str:
    """A short slice of the line around needle, with ellipses when trimmed."""
    flat = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    flat = re.sub(r"(?m)^>\s?", "", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    needle_norm = re.sub(r"\s+", " ", needle).strip()
    if not flat or not needle_norm:
        return ""
    index = flat.lower().find(needle_norm.lower())
    if index < 0:
        return ""
    start = max(0, index - wing)
    end = min(len(flat), index + len(needle_norm) + wing)
    if start > 0:
        space = flat.rfind(" ", 0, start)
        start = space + 1 if space != -1 else start
    if end < len(flat):
        space = flat.find(" ", end)
        end = space if space != -1 else end
    chunk = flat[start:end].strip()
    if start > 0:
        chunk = "…" + chunk
    if end < len(flat):
        chunk = chunk + "…"
    return chunk


def format_notice(when: str, finding: dict) -> tuple[str, str]:
    title = _CHECK_TITLES.get(finding["check"], "Transcript to check")
    suggestion = str(finding.get("suggestion") or "").strip()
    heard = str(finding.get("heard") or "").strip()
    line = f"{heard} → {suggestion}" if suggestion else f"\"{heard}\""
    why = str(finding.get("why") or "").strip()
    excerpt = str(finding.get("excerpt") or "").strip()
    parts = [when]
    if excerpt:
        parts.append(excerpt)
    parts.append(line)
    body = "\n\n".join(parts)
    if why and not suggestion:
        body = f"{body}\n{why}"
    return title, body


def _log_path(state_dir: Path) -> Path:
    return state_dir / REVIEW_LOG


def _fingerprint(path: Path, finding: dict) -> str:
    raw = f"{path}|{finding.get('check')}|{finding.get('heard')}|{finding.get('suggestion')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def already_logged(state_dir: Path, fingerprint: str) -> bool:
    path = _log_path(state_dir)
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("fingerprint") or "") == fingerprint:
            return True
    return False


def _append_log(state_dir: Path, record: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with _log_path(state_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def default_state_dir() -> Path:
    raw = os.getenv("INTENT_STATE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state" / "journal-linker" / "intents"


def fix_base_url() -> str:
    explicit = os.getenv("FIX_LINK_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.getenv("FIX_LINK_PORT", str(fix_link.DEFAULT_PORT))
    try:
        out = subprocess.check_output(["tailscale", "status", "--json"], text=True, timeout=3)
        dns = str(json.loads(out).get("Self", {}).get("DNSName") or "").rstrip(".")
        if dns:
            return f"https://{dns}:{port}"
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def _send(title: str, body: str, link: str) -> None:
    from process_intents import send_pushover

    send_pushover(
        title,
        body,
        urgency="low",
        priority="-1",
        link=link,
        link_title="Fix?" if link else "",
    )


def publish_findings(
    note_path: Path,
    findings: list[dict],
    *,
    when: str,
    state_dir: Path | None = None,
    base_url: str | None = None,
    send=None,
    force: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Notify up to three findings. Mint a Fix link only for a unique replacement."""
    state = state_dir or default_state_dir()
    note = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
    sender = send or _send
    root = base_url if base_url is not None else fix_base_url()
    sent = []
    for finding in findings:
        if len(sent) >= MAX_PINGS:
            break
        fingerprint = _fingerprint(note_path, finding)
        if not force and already_logged(state, fingerprint):
            continue
        link = ""
        located = locate_replacement(note, finding) if note else None
        finding["excerpt"] = excerpt_around(note, str(finding.get("heard") or ""))
        if dry_run:
            title, body = format_notice(when, finding)
            print(f"\n{title}\n{body}")
            if located:
                print("(fix link would be minted)")
            sent.append(finding)
            continue
        if located and root:
            old, new = located
            link = fix_link.mint_fix_link(
                path=note_path,
                old=old,
                new=new,
                heard=str(finding["heard"]),
                suggestion=str(finding["suggestion"]),
                excerpt=str(finding.get("excerpt") or ""),
                base_url=root,
            )
        title, body = format_notice(when, finding)
        try:
            sender(title, body, link)
        except Exception as exc:  # noqa: BLE001 — a missed ping must not fail the voice job
            print(f"[voice] transcript notify skipped: {exc}", file=sys.stderr)
            continue
        _append_log(state, {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fingerprint": fingerprint,
            "path": str(note_path),
            "when": when,
            "check": finding["check"],
            "heard": finding["heard"],
            "suggestion": finding.get("suggestion") or "",
            "linked": bool(link),
        })
        sent.append({**finding, "link": link})
        print(f"[voice] transcript check: {finding['check']}: {finding['heard']}", file=sys.stderr)
    return sent


def review_saved_note(
    note_path: Path,
    *,
    segments: list[dict] | None = None,
    when: str,
    focus_text: str | None = None,
    terms: list[str] | None = None,
    state_dir: Path | None = None,
    base_url: str | None = None,
    send=None,
    force: bool = False,
    ask_model: bool = True,
) -> list[dict]:
    """Check one saved note. focus_text limits the search to the new transcript."""
    if not review_enabled() or not note_path.is_file():
        return []
    note = note_path.read_text(encoding="utf-8")
    source = focus_text if focus_text is not None else note
    vocab_path = Path(__file__).resolve().parents[1] / "scribe_learning.json"
    findings = run_checks(
        source,
        segments=segments or [],
        terms=terms if terms is not None else load_vocab_terms(vocab_path),
        ask_model=ask_model,
    )
    return publish_findings(
        note_path,
        findings,
        when=when,
        state_dir=state_dir,
        base_url=base_url,
        send=send,
        force=force,
    )


def voice_blocks(note: str) -> list[tuple[str, str]]:
    """Return (HH:MM, plain text) for each voice callout."""
    blocks = []
    for match in VOICE_BLOCK_RE.finditer(note):
        lines = []
        for line in match.group(2).splitlines():
            if line.startswith("> "):
                lines.append(line[2:])
            elif line.strip() == ">":
                lines.append("")
        blocks.append((match.group(1), "\n".join(lines).strip()))
    return blocks


def _when_from_note(note_path: Path, clock: str) -> str:
    try:
        day = datetime.strptime(note_path.stem, "%Y-%m-%d")
    except ValueError:
        return note_path.name
    hour, minute = (int(part) for part in clock.split(":"))
    hour12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    return f"{day.strftime('%b')} {day.day}, {hour12}:{minute:02d} {ampm}"


def review_daily_note(
    note_path: Path,
    *,
    state_dir: Path | None = None,
    base_url: str | None = None,
    send=None,
    force: bool = False,
    ask_model: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Text checks over each voice callout already in a daily note. No re-transcription."""
    if not note_path.is_file():
        return []
    note = note_path.read_text(encoding="utf-8")
    sent: list[dict] = []
    vocab_path = Path(__file__).resolve().parents[1] / "scribe_learning.json"
    terms = load_vocab_terms(vocab_path)
    for clock, text in voice_blocks(note):
        if len(sent) >= MAX_PINGS:
            break
        findings = run_checks(text, terms=terms, ask_model=ask_model, model=None)
        room = MAX_PINGS - len(sent)
        sent.extend(publish_findings(
            note_path,
            findings[:room],
            when=_when_from_note(note_path, clock),
            state_dir=state_dir,
            base_url=base_url,
            send=send,
            force=force,
            dry_run=dry_run,
        ))
    if not sent:
        print(f"[voice] transcript check: nothing new in {note_path.name}", file=sys.stderr)
    return sent


def main() -> int:
    import argparse

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    from journal_linker_env import bootstrap_journal_linker_env

    bootstrap_journal_linker_env(repo_root=repo)
    parser = argparse.ArgumentParser(description="Run transcript text checks on a few daily notes.")
    parser.add_argument("--journal-dir", default=os.getenv("SCRIBE_JOURNAL_DIR"))
    parser.add_argument("--file", action="append", default=[], help="Daily note to check. Repeatable.")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="Notify again even if this finding was already sent.")
    parser.add_argument("--dry-run", action="store_true", help="Print findings. Do not send or mint links.")
    args = parser.parse_args()
    if not args.journal_dir and not args.file:
        print("SCRIBE_JOURNAL_DIR is not set and no --file was given.", file=sys.stderr)
        return 1
    paths = [Path(item).expanduser() for item in args.file]
    if not paths:
        journal = Path(args.journal_dir).expanduser()
        paths = sorted(journal.glob("????-??-??.md"), reverse=True)
        paths = [path for path in paths if "> [!voice]" in path.read_text(encoding="utf-8")][: args.limit]

    for path in paths:
        print(f"[voice] checking {path.name}", file=sys.stderr)
        review_daily_note(
            path,
            force=args.force,
            ask_model=False,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
