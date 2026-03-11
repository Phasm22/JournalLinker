import argparse
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import ollama
except Exception:  # pragma: no cover - import availability depends on local runtime
    ollama = None


DEFAULT_MEMORY_STORE_FILE = Path(__file__).with_name("scribe_learning.json")
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
DATE_TERM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXCLUDED_TOPIC_TERMS = {"yesterday", "tomorrow", "today"}
MIN_SUBSTANTIVE_ENTRY_WORDS = 35
MIN_WEEKLY_WORDS = 80
MIN_SUBSTANTIVE_ENTRIES = 2
MIN_CONFIDENCE_TO_WRITE = 0.45
MAX_ENTRY_EXCERPT_CHARS = 700
KEEP_ALIVE = "5m"
STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "am",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "did",
    "do",
    "for",
    "from",
    "feel",
    "felt",
    "good",
    "had",
    "has",
    "have",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "lately",
    "like",
    "lot",
    "maybe",
    "me",
    "my",
    "not",
    "of",
    "on",
    "or",
    "out",
    "putting",
    "really",
    "so",
    "something",
    "that",
    "the",
    "their",
    "them",
    "there",
    "they",
    "thing",
    "things",
    "this",
    "thought",
    "time",
    "to",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "with",
    "work",
    "would",
    "why",
    "you",
}


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
        if len(value) >= 2 and ((value[0] == value[-1] == "\"") or (value[0] == value[-1] == "'")):
            value = value[1:-1]
        os.environ[key] = value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    load_local_env(Path(__file__).with_name(".env"))

    parser = argparse.ArgumentParser(description="Generate a weekly Obsidian reflection note.")
    parser.add_argument("--journal-dir", default=os.getenv("SCRIBE_JOURNAL_DIR"))
    parser.add_argument("--learning-file", default=str(DEFAULT_MEMORY_STORE_FILE))
    parser.add_argument("--week", help="ISO week label in the form YYYY-Www (example: 2026-W10)")
    return parser.parse_args(argv)


def load_memory_store(path: Path) -> dict:
    if not path.exists():
        return {"term_weights": {}, "runs": {}, "term_memory": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"term_weights": {}, "runs": {}, "term_memory": {}}
    if not isinstance(data, dict):
        return {"term_weights": {}, "runs": {}, "term_memory": {}}
    data.setdefault("term_weights", {})
    data.setdefault("runs", {})
    data.setdefault("term_memory", {})
    if not isinstance(data["term_weights"], dict):
        data["term_weights"] = {}
    if not isinstance(data["runs"], dict):
        data["runs"] = {}
    if not isinstance(data["term_memory"], dict):
        data["term_memory"] = {}
    return data


def parse_iso_date(date_str: str | None) -> date | None:
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None


def parse_week_label(week_label: str | None) -> tuple[str, date, date]:
    if not week_label:
        today = datetime.now().date()
        iso_year, iso_week, _ = today.isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"

    match = WEEK_RE.match(week_label)
    if not match:
        raise ValueError("Week must match YYYY-Www, e.g. 2026-W10")
    iso_year = int(match.group(1))
    iso_week = int(match.group(2))
    week_start = date.fromisocalendar(iso_year, iso_week, 1)
    week_end = week_start + timedelta(days=6)
    return week_label, week_start, week_end


def strip_yaml_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return text
    return parts[1].lstrip("\n")


def clean_daily_journal_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = strip_yaml_frontmatter(normalized)
    normalized = re.split(r"\n---\s*\n##\s+Portability Export\b", normalized, maxsplit=1)[0]
    normalized = re.split(r"\n##\s+Daily Questions\b", normalized, maxsplit=1)[0]
    normalized = re.sub(r"^#\s+Daily Log\s+-\s+\d{4}-\d{2}-\d{2}\s*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(
        r"^\[\[[^\]]+\|Yesterday\]\]\s*\|\s*\[\[[^\]]+\|Tomorrow\]\]\s*$",
        "",
        normalized,
        flags=re.MULTILINE,
    )
    normalized = re.sub(r"^>\s*\[!TIP\].*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"^>\s*What is one interaction from today that felt significant\?.*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"^---\s*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    lines = [line.strip() for line in normalized.splitlines()]
    body_lines = [line for line in lines if line]
    return "\n\n".join(body_lines).strip()


def extract_wikilink_terms(text: str) -> set[str]:
    out: set[str] = set()
    for raw in re.findall(r"\[\[(.*?)\]\]", text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target:
            continue
        term = target.lower()
        if DATE_TERM_RE.fullmatch(term):
            continue
        if term in EXCLUDED_TOPIC_TERMS:
            continue
        out.add(term)
    return out


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text.lower())


def extract_keyword_tokens(text: str) -> list[str]:
    return [token for token in tokenize_words(text) if len(token) >= 4 and token not in STOPWORDS]


def extract_repeated_phrases(entry_texts: list[str], max_phrases: int = 5) -> list[str]:
    phrase_presence: Counter[str] = Counter()
    for text in entry_texts:
        tokens = extract_keyword_tokens(text)
        seen: set[str] = set()
        for i in range(len(tokens) - 1):
            phrase = f"{tokens[i]} {tokens[i + 1]}"
            seen.add(phrase)
        for phrase in seen:
            phrase_presence[phrase] += 1
    repeated = [phrase for phrase, count in phrase_presence.items() if count >= 2]
    repeated.sort(key=lambda phrase: (-phrase_presence[phrase], phrase))
    return repeated[:max_phrases]


def build_entry_excerpt(text: str, max_chars: int = MAX_ENTRY_EXCERPT_CHARS) -> str:
    clipped = text.strip()
    if len(clipped) <= max_chars:
        return clipped
    shortened = clipped[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{shortened}..."


def collect_weekly_entries(journal_dir: Path, week_start: date, week_end: date) -> list[dict]:
    entries: list[dict] = []
    cursor = week_start
    while cursor <= week_end:
        date_key = cursor.isoformat()
        note_path = journal_dir / f"{date_key}.md"
        if note_path.exists():
            try:
                raw_text = note_path.read_text(encoding="utf-8")
            except Exception:
                raw_text = ""
            cleaned_text = clean_daily_journal_text(raw_text)
            word_count = len(tokenize_words(cleaned_text))
            entries.append(
                {
                    "date": date_key,
                    "path": note_path,
                    "raw_text": raw_text,
                    "cleaned_text": cleaned_text,
                    "word_count": word_count,
                    "is_substantive": word_count >= MIN_SUBSTANTIVE_ENTRY_WORDS,
                }
            )
        cursor += timedelta(days=1)
    return entries


def collect_memory_term_hits(entries: list[dict], memory_store: dict) -> list[str]:
    term_memory = memory_store.get("term_memory", {})
    if not isinstance(term_memory, dict):
        return []

    presence: Counter[str] = Counter()
    for term in term_memory:
        if not isinstance(term, str) or not term.strip():
            continue
        lowered_term = term.lower()
        for entry in entries:
            text = entry["cleaned_text"].lower()
            if not text:
                continue
            if " " in lowered_term:
                matched = lowered_term in text
            else:
                matched = re.search(rf"\b{re.escape(lowered_term)}\b", text) is not None
            if matched:
                presence[lowered_term] += 1
    repeated = [term for term, count in presence.items() if count >= 2]
    repeated.sort(key=lambda term: (-presence[term], term))
    return repeated[:6]


def build_weekly_reflection_signals(
    entries: list[dict],
    memory_store: dict,
    week_label: str,
    week_start: date,
    week_end: date,
) -> dict:
    substantive_entries = [entry for entry in entries if entry["is_substantive"]]
    substantive_texts = [entry["cleaned_text"] for entry in substantive_entries if entry["cleaned_text"]]
    total_words = sum(entry["word_count"] for entry in substantive_entries)

    keyword_presence: Counter[str] = Counter()
    for entry in substantive_entries:
        seen = set(extract_keyword_tokens(entry["cleaned_text"]))
        for keyword in seen:
            keyword_presence[keyword] += 1

    repeated_keywords = [term for term, count in keyword_presence.items() if count >= 2]
    repeated_keywords.sort(key=lambda term: (-keyword_presence[term], term))
    repeated_keywords = repeated_keywords[:8]

    repeated_phrases = extract_repeated_phrases(substantive_texts)
    repeated_memory_terms = collect_memory_term_hits(substantive_entries, memory_store)

    confidence = (
        min(1.0, len(substantive_entries) / 3.0) * 0.40
        + min(1.0, total_words / 220.0) * 0.25
        + min(1.0, len(repeated_keywords) / 3.0) * 0.20
        + min(1.0, len(repeated_memory_terms) / 2.0) * 0.15
    )
    confidence = round(confidence, 2)

    return {
        "week_label": week_label,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "entries_found": len(entries),
        "substantive_entries": len(substantive_entries),
        "total_words": total_words,
        "confidence": confidence,
        "repeated_keywords": repeated_keywords,
        "repeated_phrases": repeated_phrases,
        "repeated_memory_terms": repeated_memory_terms,
        "entries": [
            {
                "date": entry["date"],
                "word_count": entry["word_count"],
                "is_substantive": entry["is_substantive"],
                "excerpt": build_entry_excerpt(entry["cleaned_text"]),
            }
            for entry in substantive_entries
        ],
    }


def evaluate_skip_reason(signals: dict) -> str | None:
    if signals["substantive_entries"] < MIN_SUBSTANTIVE_ENTRIES:
        return "insufficient weekly signal"
    if signals["total_words"] < MIN_WEEKLY_WORDS:
        return "insufficient weekly signal"
    if signals["confidence"] < MIN_CONFIDENCE_TO_WRITE:
        return "insufficient weekly signal"
    return None


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def extract_json_obj(text: str) -> dict:
    cleaned = strip_think(text)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("No valid JSON object found in model output.")


def build_weekly_arc_prompt(signals: dict) -> str:
    return f"""
Return JSON only.

Write a calm, grounded weekly reflection from journal evidence.

Rules:
- Output exactly this shape: {{"weekly_arc":"...","confidence":0.0,"should_write":true,"reason":"..."}}
- "weekly_arc" must be one short paragraph, maximum 3 sentences.
- Keep the tone calm, concrete, and restrained.
- Focus on: what seemed to matter, what felt heavy or draining, and what may be needed next.
- Do not overclaim. Do not psychoanalyze. Do not use therapy-speak, motivational filler, or generic productivity language.
- Base everything on the provided evidence only.
- If the evidence is too thin for a real reflection, set "should_write" to false and "reason" to "insufficient weekly signal".
- Confidence should be a float from 0.0 to 1.0.

WEEKLY SIGNALS:
{json.dumps(signals, ensure_ascii=True, indent=2)}
""".strip()


def request_weekly_arc(signals: dict) -> dict:
    if ollama is None:
        raise RuntimeError("The 'ollama' Python package is required for weekly insight generation.")

    model = os.getenv("SCRIBE_MODEL", "llama3.1:8b").strip() or "llama3.1:8b"
    num_ctx_raw = os.getenv("SCRIBE_CTX", "8192")
    try:
        num_ctx = int(num_ctx_raw)
    except Exception:
        num_ctx = 8192

    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": build_weekly_arc_prompt(signals)}],
        options={"temperature": 0.2, "num_ctx": num_ctx},
        keep_alive=KEEP_ALIVE,
    )
    data = extract_json_obj(response["message"]["content"])
    weekly_arc = str(data.get("weekly_arc", "")).strip()
    reason = str(data.get("reason", "")).strip() or "insufficient weekly signal"
    try:
        model_confidence = float(data.get("confidence", 0.0))
    except Exception:
        model_confidence = 0.0
    should_write = bool(data.get("should_write", False))
    return {
        "weekly_arc": weekly_arc,
        "reason": reason,
        "confidence": max(0.0, min(1.0, model_confidence)),
        "should_write": should_write and bool(weekly_arc),
    }


def build_weekly_note(week_label: str, week_start: date, week_end: date, weekly_arc: str, confidence: float) -> str:
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        "---",
        "tags: [insight, weekly]",
        f"week: {week_label}",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# Weekly Insight - {week_label}",
        "",
        f"Week Window: {week_start.isoformat()} to {week_end.isoformat()}",
        f"Confidence: {confidence:.2f}",
        "",
        "## Weekly Arc",
        weekly_arc.strip(),
        "",
    ]
    return "\n".join(lines)


def generate_weekly_insight(
    journal_dir: str,
    learning_file: str,
    week_label: str | None = None,
) -> tuple[Path | None, dict[str, int | float | str | bool]]:
    week_label, week_start, week_end = parse_week_label(week_label)
    journal_path = Path(journal_dir)
    memory_store_path = Path(learning_file)
    memory_store = load_memory_store(memory_store_path)

    entries = collect_weekly_entries(journal_path, week_start, week_end)
    signals = build_weekly_reflection_signals(entries, memory_store, week_label, week_start, week_end)

    stats: dict[str, int | float | str | bool] = {
        "entries_found": signals["entries_found"],
        "substantive_entries": signals["substantive_entries"],
        "total_words": signals["total_words"],
        "confidence": signals["confidence"],
        "week_label": week_label,
        "skipped": False,
        "reason": "",
        "output_path": "",
    }

    skip_reason = evaluate_skip_reason(signals)
    if skip_reason:
        stats["skipped"] = True
        stats["reason"] = skip_reason
        return None, stats

    model_result = request_weekly_arc(signals)
    if (not model_result["should_write"]) or model_result["confidence"] < MIN_CONFIDENCE_TO_WRITE:
        stats["skipped"] = True
        stats["reason"] = model_result["reason"] or "insufficient weekly signal"
        stats["confidence"] = round(min(float(stats["confidence"]), model_result["confidence"]), 2)
        return None, stats

    final_confidence = round(min(float(stats["confidence"]), model_result["confidence"]), 2)
    note_text = build_weekly_note(
        week_label=week_label,
        week_start=week_start,
        week_end=week_end,
        weekly_arc=model_result["weekly_arc"],
        confidence=final_confidence,
    )

    insights_dir = journal_path / "Insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    output_path = insights_dir / f"Weekly Insight - {week_label}.md"
    output_path.write_text(note_text, encoding="utf-8")

    stats["confidence"] = final_confidence
    stats["output_path"] = str(output_path)
    return output_path, stats


def main() -> int:
    args = parse_args()

    if not args.journal_dir:
        print("Error: journal directory is required. Use --journal-dir or SCRIBE_JOURNAL_DIR.")
        return 2

    try:
        output_path, stats = generate_weekly_insight(
            journal_dir=args.journal_dir,
            learning_file=args.learning_file,
            week_label=args.week,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    if output_path is None:
        print(
            "[weekly_insights] "
            f"skipped week={stats['week_label']} "
            f"reason={stats['reason']} "
            f"entries={stats['entries_found']} "
            f"substantive_entries={stats['substantive_entries']} "
            f"confidence={float(stats['confidence']):.2f}"
        )
        return 0

    print(
        "[weekly_insights] "
        f"wrote={output_path} "
        f"week={stats['week_label']} "
        f"entries={stats['entries_found']} "
        f"substantive_entries={stats['substantive_entries']} "
        f"confidence={float(stats['confidence']):.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
