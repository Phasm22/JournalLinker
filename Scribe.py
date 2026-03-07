import sys
import re
import json
import time
import subprocess
import ollama
import os
import html
from datetime import datetime, timedelta
from pathlib import Path


def get_input_text(remaining_args: list[str]) -> str:
    """Get journal entry text from argv, stdin, or clipboard.

    Priority:
    1) argv: `python Scribe.py "..."`
    2) piped stdin: `pbpaste | python Scribe.py`
    3) interactive run: fallback to clipboard (macOS)

    Important: never block waiting on stdin when run interactively.
    """
    if remaining_args:
        return " ".join(remaining_args).strip("\n")

    # If stdin is being piped, read it. If we're on a TTY, do NOT block.
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        return data.strip("\n")

    # Interactive invocation: pull from clipboard so `python Scribe.py` just works.
    return get_clipboard_text()


def parse_cli() -> tuple[str, int, str | None, bool, list[str]]:
    model = "llama3.1:8b"
    num_ctx = 8192
    journal_dir: str | None = os.getenv("SCRIBE_JOURNAL_DIR")
    reset_learning = False
    env_model = os.getenv("SCRIBE_MODEL")
    env_ctx = os.getenv("SCRIBE_CTX")

    if env_model and env_model.strip():
        model = env_model.strip()
    if env_ctx:
        try:
            num_ctx = int(env_ctx)
        except Exception:
            pass

    args = sys.argv[1:]
    remaining_args = []
    skip_next = False
    i = 0
    while i < len(args):
        arg = args[i]
        if skip_next:
            skip_next = False
            i += 1
            continue
        if arg == "--model":
            if i + 1 < len(args):
                model = args[i + 1]
                skip_next = True
            i += 1
        elif arg.startswith("--model="):
            model = arg.split("=", 1)[1]
            i += 1
        elif arg == "--ctx":
            if i + 1 < len(args):
                try:
                    num_ctx = int(args[i + 1])
                except Exception:
                    pass
                skip_next = True
            i += 1
        elif arg.startswith("--ctx="):
            try:
                num_ctx = int(arg.split("=", 1)[1])
            except Exception:
                pass
            i += 1
        elif arg == "--journal-dir":
            if i + 1 < len(args):
                journal_dir = args[i + 1]
                skip_next = True
            i += 1
        elif arg.startswith("--journal-dir="):
            journal_dir = arg.split("=", 1)[1]
            i += 1
        elif arg == "--reset-learning":
            reset_learning = True
            i += 1
        else:
            remaining_args.append(arg)
            i += 1

    return model, num_ctx, journal_dir, reset_learning, remaining_args


def get_clipboard_text() -> str:
    """Best-effort clipboard read on macOS via pbpaste."""
    try:
        p = subprocess.run(["pbpaste"], check=False, capture_output=True, text=True)
        return (p.stdout or "").strip("\n")
    except Exception:
        return ""


def strip_html_if_needed(text: str) -> str:
    t = text.lstrip()
    looks_like_html = (
        t.startswith("<!DOCTYPE")
        or t.startswith("<html")
        or "<meta charset" in t.lower()
        or "</p>" in t.lower()
        or "<br" in t.lower()
    )
    if not looks_like_html:
        return text

    # Preserve paragraph/line breaks first, then remove the remaining tags.
    out = re.sub(r"(?i)<br\s*/?>", "\n", text)
    out = re.sub(r"(?i)</p\s*>", "\n", out)
    out = re.sub(r"(?i)<p[^>]*>", "", out)
    out = re.sub(r"(?is)<style.*?>.*?</style>", "", out)
    out = re.sub(r"(?is)<script.*?>.*?</script>", "", out)
    out = re.sub(r"(?is)<[^>]+>", "", out)
    out = html.unescape(out)
    out = out.replace("\u00a0", " ")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def build_prompt(input_text: str) -> str:
    return f"""
Return JSON only.

Goal: pick high-value Obsidian backlinks for the JOURNAL ENTRY.

Rules:
- Output: {{\"links\":[...]}} where links is a list of strings.
- Return 25 to 45 candidates when possible.
- Each string MUST be an exact substring that appears verbatim in the entry.
- Do NOT include anything already inside [[double brackets]].
- Avoid generic words unless clearly recurring themes.
- Prefer people, relationships, organizations, places, events, media, routines, goals, blockers, and health signals.
- Prefer terms likely to recur across future entries over one-off details.
- Use single words for names, places, concrete nouns (e.g. Jill, movie, dinner). Use short phrases (2-5 words) when they capture a goal, struggle, or recurring intention that appears verbatim (e.g. spend more time outside, eating better food). Do not force phrases where a single word is clearer.
- Focus on the narrative journal body; ignore YAML frontmatter tags and date-navigation links.
- Order links from highest value to lowest value.
- No markdown, no commentary, no extra keys.

JOURNAL ENTRY:
{input_text}
""".strip()


def strip_think(s: str) -> str:
    # Some models output <think>...</think>
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()


def extract_json_obj(s: str) -> dict:
    s = strip_think(s)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("No valid JSON object found in model output.")


GENERIC_TERMS = {
    "day",
    "thing",
    "things",
    "time",
    "week",
    "today",
    "yesterday",
    "tomorrow",
    "work",
    "project",
    "decision",
    "details",
}

LEARNING_FILE = Path(__file__).with_name("scribe_learning.json")
MAX_TERM_WEIGHT = 30.0
POSITIVE_DELTA = 2.0
NEGATIVE_DELTA = -1.5


def load_learning(path: Path) -> dict:
    if not path.exists():
        return {"term_weights": {}, "runs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"term_weights": {}, "runs": {}}
    if not isinstance(data, dict):
        return {"term_weights": {}, "runs": {}}
    data.setdefault("term_weights", {})
    data.setdefault("runs", {})
    if not isinstance(data["term_weights"], dict):
        data["term_weights"] = {}
    if not isinstance(data["runs"], dict):
        data["runs"] = {}
    return data


def save_learning(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def parse_journal_date(text: str) -> str | None:
    title_match = re.search(r"(?m)^#\s*Daily Log\s*-\s*(\d{4}-\d{2}-\d{2})\s*$", text)
    if title_match:
        return title_match.group(1)
    nav_match = re.search(r"\[\[(\d{4}-\d{2}-\d{2})\|Yesterday\]\]", text)
    if nav_match:
        try:
            d = datetime.strptime(nav_match.group(1), "%Y-%m-%d")
            return (d + timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            return None
    return None


def extract_wikilink_terms(text: str) -> set[str]:
    out: set[str] = set()
    for raw in re.findall(r"\[\[(.*?)\]\]", text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            out.add(target.lower())
    return out


def apply_yesterday_learning(
    learning: dict,
    current_entry_text: str,
    journal_dir: str | None,
) -> str | None:
    current_date = parse_journal_date(current_entry_text)
    if not current_date:
        return None

    try:
        current_dt = datetime.strptime(current_date, "%Y-%m-%d")
    except Exception:
        return None
    yesterday = (current_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    run = learning.get("runs", {}).get(yesterday)
    if not isinstance(run, dict):
        return current_date

    if not journal_dir:
        return current_date
    yesterday_path = Path(journal_dir) / f"{yesterday}.md"
    if not yesterday_path.exists():
        return current_date

    try:
        yesterday_text = yesterday_path.read_text(encoding="utf-8")
    except Exception:
        return current_date

    actual_links = extract_wikilink_terms(yesterday_text)
    suggested = run.get("suggested_terms", [])
    if not isinstance(suggested, list):
        return current_date

    weights = learning.setdefault("term_weights", {})
    for term in suggested:
        if not isinstance(term, str):
            continue
        key = normalize_term(term).lower()
        if not key:
            continue
        current = float(weights.get(key, 0.0))
        if key in actual_links:
            current += POSITIVE_DELTA
        else:
            current += NEGATIVE_DELTA
        current = max(-MAX_TERM_WEIGHT, min(MAX_TERM_WEIGHT, current))
        if abs(current) < 0.001:
            weights.pop(key, None)
        else:
            weights[key] = round(current, 3)

    return current_date


def persist_current_run(learning: dict, current_date: str | None, ranked_terms: list[str]) -> None:
    if not current_date:
        return
    learning.setdefault("runs", {})
    learning["runs"][current_date] = {
        "suggested_terms": ranked_terms,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_term(term: str) -> str:
    return re.sub(r"\s+", " ", term).strip()


def is_name_like(term: str) -> bool:
    words = term.split()
    if not words:
        return False
    if words[0].lower() in {"the", "a", "an", "i"}:
        return False
    return all(w[:1].isupper() for w in words)


def term_frequency(text: str, term: str) -> int:
    return len(re.findall(re.escape(term), text, flags=re.IGNORECASE))


def rank_terms(original: str, terms: list[str], learning: dict, max_links: int = 45) -> list[str]:
    scored: list[tuple[float, int, int, int, str]] = []
    seen: set[str] = set()
    weights = learning.get("term_weights", {})

    for idx, raw in enumerate(terms):
        if not isinstance(raw, str):
            continue
        term = normalize_term(raw)
        if len(term) < 3:
            continue

        key = term.lower()
        if key in seen:
            continue
        seen.add(key)

        freq = term_frequency(original, term)
        score = 0.0
        if is_name_like(term):
            score += 20.0
        score += min(freq, 8) * 6.0
        score += min(len(term), 48) * 0.12
        if key in GENERIC_TERMS:
            score -= 20.0
        score += float(weights.get(key, 0.0))
        # Keep some influence from the model's original ordering.
        score += max(0.0, 8.0 - (idx * 0.15))

        scored.append((score, freq, len(term), -idx, term))

    scored.sort(reverse=True)
    return [term for _, _, _, _, term in scored[:max_links]]


def is_boundary_ok(text: str, s: int, e: int) -> bool:
    left_ok = s == 0 or not text[s - 1].isalnum()
    right_ok = e == len(text) or not text[e].isalnum()
    return left_ok and right_ok


def iter_unlinked_ranges(text: str):
    last = 0
    for m in re.finditer(r"\[\[.*?\]\]", text, flags=re.DOTALL):
        if m.start() > last:
            yield last, m.start()
        last = m.end()
    if last < len(text):
        yield last, len(text)


def find_unlinked_span(text: str, term: str) -> tuple[int, int] | None:
    patterns = [
        re.compile(re.escape(term)),
        re.compile(re.escape(term), flags=re.IGNORECASE),
    ]
    for pattern in patterns:
        for start, end in iter_unlinked_ranges(text):
            segment = text[start:end]
            for m in pattern.finditer(segment):
                s, e = start + m.start(), start + m.end()
                if not is_boundary_ok(text, s, e):
                    continue
                if text[max(0, s - 2):s] == "[[" or text[e:e + 2] == "]]":
                    continue
                return s, e
    return None


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    end_line = text.find("\n", end + 4)
    if end_line == -1:
        end_line = len(text)
    return text[:end_line], text[end_line:]


def apply_links_in_chunks(text: str, ranked_terms: list[str]) -> str:
    chunks = re.split(r"(\n\s*\n)", text)
    for term in ranked_terms:
        for i, chunk in enumerate(chunks):
            if re.fullmatch(r"\n\s*\n", chunk):
                continue
            span = find_unlinked_span(chunk, term)
            if not span:
                continue
            s, e = span
            chunks[i] = chunk[:s] + "[[" + chunk[s:e] + "]]" + chunk[e:]
            break
    return "".join(chunks)


def apply_links(original: str, terms: list[str], learning: dict, max_links: int = 45) -> tuple[str, list[str]]:
    ranked_terms = rank_terms(original, terms, learning, max_links=max_links)
    frontmatter, body = split_frontmatter(original)

    marker = "\n## Portability Export"
    portability = ""
    if marker in body:
        main_body, portability_tail = body.split(marker, 1)
        portability = marker + portability_tail
    else:
        main_body = body

    linked_main = apply_links_in_chunks(main_body, ranked_terms)
    return frontmatter + linked_main + portability, ranked_terms


def main() -> int:
    input_text = None
    MODEL, NUM_CTX, JOURNAL_DIR, RESET_LEARNING, remaining_args = parse_cli()
    input_text = get_input_text(remaining_args)
    input_text = strip_html_if_needed(input_text)

    if not input_text.strip():
        print(
            "Error: No input provided. Pipe text in (pbpaste | python Scribe.py) or pass as an argument.",
            file=sys.stderr,
        )
        return 2

    prompt = build_prompt(input_text)

    # MODEL = "deepseek-r1:32b"  # removed as per instructions
    # Use string "5m" per API docs so model stays loaded; run "ollama ps" while Scribe runs or right after
    KEEP_ALIVE = "5m"

    try:
        if RESET_LEARNING and LEARNING_FILE.exists():
            LEARNING_FILE.unlink(missing_ok=True)

        learning_data = load_learning(LEARNING_FILE)
        current_date = apply_yesterday_learning(learning_data, input_text, JOURNAL_DIR)

        t0 = time.perf_counter()
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0, "num_ctx": NUM_CTX},
            keep_alive=KEEP_ALIVE,
        )
        t1 = time.perf_counter()
        data = extract_json_obj(response["message"]["content"])
        terms = data.get("links", [])
        if not isinstance(terms, list):
            raise ValueError("Model JSON must include a list at key 'links'.")
        out, ranked_terms = apply_links(input_text, terms, learning_data)
        persist_current_run(learning_data, current_date, ranked_terms)
        save_learning(LEARNING_FILE, learning_data)
        t2 = time.perf_counter()
        # Diagnostics to stderr so stdout stays clean for piping
        print(
            f"[Scribe] input_chars={len(input_text)} prompt_chars={len(prompt)} "
            f"ollama_sec={t1 - t0:.1f} postprocess_sec={t2 - t1:.3f}",
            file=sys.stderr,
        )
        print(
            f"[Scribe] model={MODEL} num_ctx={NUM_CTX} journal_dir={JOURNAL_DIR or 'unset'}",
            file=sys.stderr,
        )
        if response.get("eval_duration"):
            print(f"[Scribe] eval_duration_ns={response['eval_duration']}", file=sys.stderr)
        print(out)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
