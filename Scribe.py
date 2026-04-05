import sys
import re
import json
import time
import subprocess
import ollama
import os
import html
import math
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from local_embeddings import LocalEmbeddingCache, cosine_similarity as embedding_cosine_similarity, normalize_embedding_text


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


def get_input_text(remaining_args: list[str]) -> tuple[str, str]:
    """Return (text, origin) for how input was obtained.

    origin is one of: argv, stdin, stdin_pipe_empty, clipboard.

    Empty stdin pipe does not read clipboard here — main() may use the journal file
    or clipboard after resolve_current_journal_context.
    """
    if remaining_args:
        return " ".join(remaining_args).strip("\n"), "argv"

    if not sys.stdin.isatty():
        data = sys.stdin.read().strip("\n")
        if data.strip():
            return data, "stdin"
        return "", "stdin_pipe_empty"

    return get_clipboard_text(), "clipboard"


def parse_cli() -> tuple[str, int, str | None, bool, str | None, str | None, bool, list[str]]:
    load_local_env(Path(__file__).with_name(".env"))

    model = "llama3.1:8b"
    num_ctx = 8192
    journal_dir: str | None = os.getenv("SCRIBE_JOURNAL_DIR")
    reset_learning = False
    active_date: str | None = None
    active_file: str | None = None
    write_back = False
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
        elif arg == "--write-back":
            write_back = True
            i += 1
        elif arg == "--active-date":
            if i + 1 < len(args):
                active_date = args[i + 1]
                skip_next = True
            i += 1
        elif arg.startswith("--active-date="):
            active_date = arg.split("=", 1)[1]
            i += 1
        elif arg == "--active-file":
            if i + 1 < len(args):
                active_file = args[i + 1]
                skip_next = True
            i += 1
        elif arg.startswith("--active-file="):
            active_file = arg.split("=", 1)[1]
            i += 1
        else:
            remaining_args.append(arg)
            i += 1

    return model, num_ctx, journal_dir, reset_learning, active_date, active_file, write_back, remaining_args


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

MEMORY_STORE_FILE = Path(__file__).with_name("scribe_learning.json")
RUN_REPORTS_DIRNAME = "journal-linker"
RUN_HISTORY_FILENAME = "Run History.md"
MAX_TERM_WEIGHT = 30.0
POSITIVE_DELTA = 2.0
NEGATIVE_DELTA = -1.5
SEMANTIC_CONTEXT_LIMIT = 8
RECENCY_LAMBDA = 0.08
BURST_LOOKBACK_DAYS = 3
BURST_WEIGHT = 4.0

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "we",
    "with",
    "you",
}
NAV_LINKS_PATTERN = re.compile(r"\[\[[^\]]+\|Yesterday\]\]\s*\|\s*\[\[[^\]]+\|Tomorrow\]\]")


def load_memory_store(path: Path) -> dict:
    if not path.exists():
        return {"term_weights": {}, "runs": {}, "term_memory": {}, "embedding_cache": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"term_weights": {}, "runs": {}, "term_memory": {}, "embedding_cache": {}}
    if not isinstance(data, dict):
        return {"term_weights": {}, "runs": {}, "term_memory": {}, "embedding_cache": {}}
    data.setdefault("term_weights", {})
    data.setdefault("runs", {})
    data.setdefault("term_memory", {})
    data.setdefault("embedding_cache", {})
    if not isinstance(data["term_weights"], dict):
        data["term_weights"] = {}
    if not isinstance(data["runs"], dict):
        data["runs"] = {}
    if not isinstance(data["term_memory"], dict):
        data["term_memory"] = {}
    if not isinstance(data["embedding_cache"], dict):
        data["embedding_cache"] = {}
    return data


def save_memory_store(path: Path, data: dict) -> None:
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


def parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None


def today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def extract_date_from_journal_filename(path: Path) -> str | None:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", path.name)
    if not match:
        return None
    date_str = match.group(1)
    if not parse_iso_date(date_str):
        return None
    return date_str


def read_file_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def journal_note_has_substantive_body(raw_text: str | None, iso_date: str) -> bool:
    """True if the daily note has real body text (skip empty / date-only stubs for nav)."""
    if raw_text is None:
        return False
    text = raw_text.strip()
    if not text:
        return False
    collapsed = " ".join(text.split())
    if collapsed == iso_date:
        return False
    if re.fullmatch(rf"#\s*Daily Log\s*-\s*{re.escape(iso_date)}", collapsed):
        return False

    _, body = split_frontmatter(text)
    work = body.strip()
    work = re.sub(rf"(?m)^#\s*Daily Log\s*-\s*{re.escape(iso_date)}\s*$", "", work)
    work = NAV_LINKS_PATTERN.sub("", work)
    work = work.strip()
    kept: list[str] = []
    for line in work.splitlines():
        s = line.strip()
        if not s:
            continue
        if s == iso_date:
            continue
        if re.fullmatch(rf"#\s*Daily Log\s*-\s*{re.escape(iso_date)}", s):
            continue
        kept.append(s)
    work = "\n".join(kept).strip()
    return bool(work)


def count_wikilink_markers(text: str) -> int:
    return text.count("[[")


def derive_report_base_dir(journal_dir: str | None, note_path: Path | None) -> Path | None:
    if journal_dir:
        return Path(journal_dir)
    if note_path:
        return note_path.parent
    return None


def markdown_cell(value: object) -> str:
    text = str(value)
    text = text.replace("|", "\\|")
    return text.replace("\n", "<br>")


def build_run_report_markdown(
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    model: str,
    num_ctx: int,
    journal_dir: str | None,
    active_context_source: str,
    active_date: str | None,
    active_file: Path | None,
    input_chars: int,
    prompt_chars: int,
    input_wikilinks: int,
    suggested_terms: list[str],
    ranked_terms: list[str],
    output_text: str | None,
    file_nav_sync_applied: bool,
    previous_file_nav_sync_applied: bool,
    actions: list[dict[str, str]],
    touched_files: list[Path],
    error_message: str | None,
    traceback_text: str | None,
    ollama_sec: float | None = None,
    postprocess_sec: float | None = None,
    eval_duration_ns: int | None = None,
) -> str:
    status_label = "Success" if status == "success" else "Error"
    suggested_preview = ", ".join(ranked_terms[:12]) if ranked_terms else ", ".join(suggested_terms[:12])

    lines = [
        f"# Journal Linker Run - {started_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**Status:** {status_label}",
        "",
        "## Summary",
        "",
        f"- Started: {started_at.isoformat(timespec='seconds')}",
        f"- Finished: {finished_at.isoformat(timespec='seconds')}",
        f"- Duration seconds: {(finished_at - started_at).total_seconds():.3f}",
        f"- Model: `{model}`",
        f"- Context window: `{num_ctx}`",
        f"- Journal directory: `{journal_dir or 'unset'}`",
        f"- Active context source: `{active_context_source}`",
        f"- Active date: `{active_date or 'unset'}`",
        f"- Active file: `{str(active_file) if active_file else 'unset'}`",
        f"- Input characters: `{input_chars}`",
        f"- Prompt characters: `{prompt_chars}`",
        f"- Input wikilink markers: `{input_wikilinks}`",
        f"- Suggested terms returned: `{len(suggested_terms)}`",
        f"- Ranked terms considered: `{len(ranked_terms)}`",
        f"- Current note nav sync applied: `{file_nav_sync_applied}`",
        f"- Previous note nav sync applied: `{previous_file_nav_sync_applied}`",
    ]

    if output_text is not None:
        output_wikilinks = count_wikilink_markers(output_text)
        lines.append(f"- Output wikilink markers: `{output_wikilinks}`")
        lines.append(f"- Approximate new wikilinks added: `{max(0, output_wikilinks - input_wikilinks)}`")
    if ollama_sec is not None:
        lines.append(f"- Ollama seconds: `{ollama_sec:.3f}`")
    if postprocess_sec is not None:
        lines.append(f"- Post-process seconds: `{postprocess_sec:.3f}`")
    if eval_duration_ns is not None:
        lines.append(f"- Model eval duration ns: `{eval_duration_ns}`")

    lines.extend(
        [
            "",
            "## Actions",
            "",
            "| Action | Result | Target |",
            "| --- | --- | --- |",
        ]
    )
    for action in actions:
        lines.append(
            f"| {markdown_cell(action.get('action', ''))} | "
            f"{markdown_cell(action.get('result', ''))} | "
            f"{markdown_cell(action.get('target', ''))} |"
        )

    lines.extend(["", "## Files Touched", ""])
    if touched_files:
        seen_files: set[str] = set()
        for path in touched_files:
            path_str = str(path)
            if path_str in seen_files:
                continue
            seen_files.add(path_str)
            lines.append(f"- `{path_str}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Suggested Links", ""])
    if suggested_preview:
        lines.append(suggested_preview)
    else:
        lines.append("_No suggestions were recorded._")

    if error_message:
        lines.extend(["", "## Error", "", error_message])
    if traceback_text:
        lines.extend(["", "## Traceback", "", "```text", traceback_text.strip(), "```"])

    lines.append("")
    return "\n".join(lines)


def write_run_report(
    *,
    base_dir: Path | None,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    model: str,
    num_ctx: int,
    journal_dir: str | None,
    active_context_source: str,
    active_date: str | None,
    active_file: Path | None,
    input_text: str,
    prompt: str | None,
    suggested_terms: list[str],
    ranked_terms: list[str],
    output_text: str | None,
    file_nav_sync_applied: bool,
    previous_file_nav_sync_applied: bool,
    actions: list[dict[str, str]],
    touched_files: list[Path],
    error_message: str | None,
    traceback_text: str | None,
    ollama_sec: float | None = None,
    postprocess_sec: float | None = None,
    eval_duration_ns: int | None = None,
) -> Path | None:
    if base_dir is None:
        return None

    report_dir = base_dir / RUN_REPORTS_DIRNAME
    report_dir.mkdir(parents=True, exist_ok=True)

    # Keep only one "last run" file; purge old timestamped reports.
    report_name = "Journal Linker Run - Latest.md"
    report_path = report_dir / report_name
    for old in report_dir.glob("Journal Linker Run - 20??-??-?? ??-??-??.md"):
        try:
            old.unlink()
        except Exception:
            pass
    history_path = report_dir / RUN_HISTORY_FILENAME

    report_body = build_run_report_markdown(
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        model=model,
        num_ctx=num_ctx,
        journal_dir=journal_dir,
        active_context_source=active_context_source,
        active_date=active_date,
        active_file=active_file,
        input_chars=len(input_text),
        prompt_chars=len(prompt or ""),
        input_wikilinks=count_wikilink_markers(input_text),
        suggested_terms=suggested_terms,
        ranked_terms=ranked_terms,
        output_text=output_text,
        file_nav_sync_applied=file_nav_sync_applied,
        previous_file_nav_sync_applied=previous_file_nav_sync_applied,
        actions=actions,
        touched_files=touched_files,
        error_message=error_message,
        traceback_text=traceback_text,
        ollama_sec=ollama_sec,
        postprocess_sec=postprocess_sec,
        eval_duration_ns=eval_duration_ns,
    )
    report_path.write_text(report_body, encoding="utf-8")

    status_label = "Success" if status == "success" else "Error"
    history_header = "# Journal Linker Run History\n\n"
    history_entry_lines = [
        f"## {started_at.strftime('%Y-%m-%d %H:%M:%S')} - {status_label}",
        "",
        f"- Report: [{report_name}]({report_name})",
        f"- Active date: `{active_date or 'unset'}`",
        f"- Active file: `{str(active_file) if active_file else 'unset'}`",
        f"- Result: `{status_label}`",
    ]
    if error_message:
        history_entry_lines.append(f"- Error: `{error_message}`")
    history_entry = "\n".join(history_entry_lines) + "\n\n"

    existing_history = read_file_text(history_path) or ""
    if existing_history.startswith(history_header):
        existing_body = existing_history[len(history_header):].lstrip()
    else:
        existing_body = existing_history.strip()

    new_history = history_header + history_entry
    if existing_body:
        new_history += existing_body
        if not new_history.endswith("\n"):
            new_history += "\n"
    history_path.write_text(new_history, encoding="utf-8")
    return report_path


def find_latest_modified_journal_note(journal_dir: str | None) -> Path | None:
    if not journal_dir:
        return None
    journal_path = Path(journal_dir)
    if not journal_path.exists():
        return None

    latest_path: Path | None = None
    latest_mtime: float | None = None
    for note_path in journal_path.glob("*.md"):
        if not extract_date_from_journal_filename(note_path):
            continue
        try:
            mtime = note_path.stat().st_mtime
        except Exception:
            continue
        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
            latest_path = note_path
    return latest_path


def resolve_current_journal_context(
    input_text: str,
    journal_dir: str | None,
    active_date_override: str | None = None,
    active_file_override: str | None = None,
) -> tuple[str | None, Path | None, str | None, str]:
    if active_file_override:
        candidate = Path(active_file_override).expanduser()
        if candidate.exists() and candidate.is_file():
            note_text = read_file_text(candidate)
            date_from_name = extract_date_from_journal_filename(candidate)
            current_date = date_from_name or parse_journal_date(note_text or "")
            if current_date:
                return current_date, candidate, note_text, "active_file"

    if active_date_override and parse_iso_date(active_date_override):
        note_path: Path | None = None
        note_text: str | None = None
        if journal_dir:
            candidate = Path(journal_dir) / f"{active_date_override}.md"
            if candidate.exists():
                note_path = candidate
                note_text = read_file_text(candidate)
        return active_date_override, note_path, note_text, "active_date"

    input_date = parse_journal_date(input_text)
    if input_date:
        note_path = None
        note_text = None
        if journal_dir:
            candidate = Path(journal_dir) / f"{input_date}.md"
            if candidate.exists():
                note_path = candidate
                note_text = read_file_text(candidate)
        return input_date, note_path, note_text, "input_date"

    # Prefer today's calendar note if the file exists (avoids "wrong day" when an older note was edited last).
    today = today_date_str()
    if journal_dir and parse_iso_date(today):
        today_path = Path(journal_dir) / f"{today}.md"
        if today_path.is_file():
            return today, today_path, read_file_text(today_path), "calendar_today_file"

    latest_note = find_latest_modified_journal_note(journal_dir)
    if latest_note:
        current_date = extract_date_from_journal_filename(latest_note)
        if current_date:
            return current_date, latest_note, read_file_text(latest_note), "latest_modified_file"

    return None, None, None, "none"


def find_adjacent_journal_dates(
    journal_dir: str | None,
    current_date: str,
) -> tuple[str, str] | None:
    """Yesterday / Tomorrow target the nearest substantive daily notes, not empty stubs.

    If there is no substantive neighbor on a side, falls back to calendar ± 1 day.
    """
    current_dt = parse_iso_date(current_date)
    if not journal_dir or not current_dt:
        return None
    journal_path = Path(journal_dir)
    if not journal_path.exists():
        return None

    previous_dt: datetime | None = None
    next_dt: datetime | None = None
    for note_path in journal_path.glob("*.md"):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", note_path.name)
        if not match:
            continue
        note_date_str = match.group(1)
        note_dt = parse_iso_date(note_date_str)
        if not note_dt:
            continue
        note_text = read_file_text(note_path)
        if not journal_note_has_substantive_body(note_text, note_date_str):
            continue
        if note_dt < current_dt and (previous_dt is None or note_dt > previous_dt):
            previous_dt = note_dt
        if note_dt > current_dt and (next_dt is None or note_dt < next_dt):
            next_dt = note_dt

    prev = (previous_dt or (current_dt - timedelta(days=1))).strftime("%Y-%m-%d")
    nxt = (next_dt or (current_dt + timedelta(days=1))).strftime("%Y-%m-%d")
    return prev, nxt


def find_previous_existing_journal_date(
    journal_dir: str | None,
    current_date: str,
) -> str | None:
    """Latest substantive daily note strictly before current_date (for nav sync on gap days)."""
    current_dt = parse_iso_date(current_date)
    if not journal_dir or not current_dt:
        return None
    journal_path = Path(journal_dir)
    if not journal_path.exists():
        return None

    previous_dt: datetime | None = None
    for note_path in journal_path.glob("*.md"):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", note_path.name)
        if not match:
            continue
        note_date_str = match.group(1)
        note_dt = parse_iso_date(note_date_str)
        if not note_dt or note_dt >= current_dt:
            continue
        note_text = read_file_text(note_path)
        if not journal_note_has_substantive_body(note_text, note_date_str):
            continue
        if previous_dt is None or note_dt > previous_dt:
            previous_dt = note_dt
    if not previous_dt:
        return None
    return previous_dt.strftime("%Y-%m-%d")


def apply_navigation_links_to_text(
    text: str,
    journal_dir: str | None,
    current_date: str,
) -> tuple[str, bool]:
    if not parse_iso_date(current_date):
        return text, False
    adjacent = find_adjacent_journal_dates(journal_dir, current_date)
    if not adjacent:
        return text, False
    previous_date, next_date = adjacent
    nav_line = f"[[{previous_date}|Yesterday]] | [[{next_date}|Tomorrow]]"

    if NAV_LINKS_PATTERN.search(text):
        updated = NAV_LINKS_PATTERN.sub(nav_line, text, count=1)
        return updated, updated != text

    heading_match = re.search(
        rf"(?m)^#\s*Daily Log\s*-\s*{re.escape(current_date)}\s*$",
        text,
    )
    if not heading_match:
        return text, False
    line_end = text.find("\n", heading_match.end())
    if line_end == -1:
        updated = text + f"\n\n{nav_line}\n"
        return updated, updated != text
    insert_pos = line_end + 1
    tail = text[insert_pos:].lstrip("\n")
    updated = text[:insert_pos] + "\n" + nav_line + "\n\n" + tail
    return updated, updated != text


def sync_navigation_links_in_file(
    note_path: Path,
    journal_dir: str | None,
    current_date: str,
) -> bool:
    existing = read_file_text(note_path)
    if existing is None:
        return False
    updated, changed = apply_navigation_links_to_text(existing, journal_dir, current_date)
    if not changed:
        return False
    try:
        note_path.write_text(updated, encoding="utf-8")
    except Exception:
        return False
    return True


def sync_daily_navigation_links(
    text: str,
    journal_dir: str | None,
    current_date_override: str | None = None,
) -> str:
    current_date = current_date_override or parse_journal_date(text)
    if not current_date:
        return text
    updated, _ = apply_navigation_links_to_text(text, journal_dir, current_date)
    return updated


def build_term_record(memory: dict, key: str, term_label: str) -> dict:
    record = memory.setdefault(key, {})
    if not isinstance(record, dict):
        record = {}
        memory[key] = record
    record.setdefault("term", term_label)
    record.setdefault("reinforcement", 0.0)
    record.setdefault("seen_count", 0)
    record.setdefault("success_count", 0)
    record.setdefault("failure_count", 0)
    record.setdefault("contexts", [])
    if not isinstance(record["contexts"], list):
        record["contexts"] = []
    if not isinstance(record["term"], str) or not record["term"].strip():
        record["term"] = term_label
    return record


def trim_contexts(values: list[str], limit: int = SEMANTIC_CONTEXT_LIMIT) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        clean = normalize_term(value)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    if len(out) > limit:
        out = out[-limit:]
    return out


def extract_candidate_context(text: str, term: str, window: int = 80) -> str:
    pattern = re.compile(re.escape(term), flags=re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return ""
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    snippet = text[start:end]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


def tokenize_for_similarity(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9']+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def cosine_similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = 0.0
    for token, count in a.items():
        dot += float(count * b.get(token, 0))
    if dot <= 0:
        return 0.0
    norm_a = math.sqrt(sum(float(v * v) for v in a.values()))
    norm_b = math.sqrt(sum(float(v * v) for v in b.values()))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def compute_semantic_similarity(current_context: str, history_contexts: list[str]) -> float:
    if not current_context or not history_contexts:
        return 0.0
    current_tokens = tokenize_for_similarity(current_context)
    if not current_tokens:
        return 0.0
    current_vec = Counter(current_tokens)
    best = 0.0
    for snippet in history_contexts:
        tokens = tokenize_for_similarity(snippet)
        if not tokens:
            continue
        score = cosine_similarity(current_vec, Counter(tokens))
        if score > best:
            best = score
    return best


def compute_recency_weight(last_date: str | None, reference_date: str | None) -> float:
    dt_last = parse_iso_date(last_date)
    dt_ref = parse_iso_date(reference_date) or parse_iso_date(today_date_str())
    if not dt_last or not dt_ref:
        return 0.0
    days = max(0, (dt_ref - dt_last).days)
    return math.exp(-RECENCY_LAMBDA * days)


def collect_recent_topic_activity(
    journal_dir: str | None,
    reference_date: str | None,
    lookback_days: int = BURST_LOOKBACK_DAYS,
) -> dict[str, int]:
    if not journal_dir:
        return {}
    reference_dt = parse_iso_date(reference_date)
    if not reference_dt:
        return {}
    journal_path = Path(journal_dir)
    if not journal_path.exists():
        return {}

    topic_activity: dict[str, int] = {}
    for days_ago in range(max(1, lookback_days)):
        day = (reference_dt - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        note_path = journal_path / f"{day}.md"
        if not note_path.exists():
            continue
        try:
            note_text = note_path.read_text(encoding="utf-8")
        except Exception:
            continue
        for topic in extract_wikilink_terms(note_text):
            topic_activity[topic] = topic_activity.get(topic, 0) + 1
    return topic_activity


def compute_burst_weight(activity_count: int) -> float:
    if activity_count <= 0:
        return 0.0
    return float(min(activity_count, 3)) * BURST_WEIGHT


def apply_previous_day_feedback(
    learning: dict,
    current_entry_text: str,
    journal_dir: str | None,
    current_date_override: str | None = None,
) -> str | None:
    current_date = current_date_override or parse_journal_date(current_entry_text)
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
    run_contexts = run.get("term_contexts", {})
    if not isinstance(run_contexts, dict):
        run_contexts = {}

    weights = learning.setdefault("term_weights", {})
    term_memory = learning.setdefault("term_memory", {})
    for term in suggested:
        if not isinstance(term, str):
            continue
        key = normalize_term(term).lower()
        if not key:
            continue

        matched = key in actual_links
        current = float(weights.get(key, 0.0))
        if matched:
            current += POSITIVE_DELTA
        else:
            current += NEGATIVE_DELTA
        current = max(-MAX_TERM_WEIGHT, min(MAX_TERM_WEIGHT, current))
        if abs(current) < 0.001:
            weights.pop(key, None)
        else:
            weights[key] = round(current, 3)

        record = build_term_record(term_memory, key, term)
        record["seen_count"] = int(record.get("seen_count", 0)) + 1
        if matched:
            record["success_count"] = int(record.get("success_count", 0)) + 1
            record["last_success_date"] = yesterday
        else:
            record["failure_count"] = int(record.get("failure_count", 0)) + 1
        record["last_seen_date"] = yesterday
        reinforcement = float(record.get("reinforcement", 0.0))
        reinforcement += POSITIVE_DELTA if matched else NEGATIVE_DELTA
        reinforcement = max(-MAX_TERM_WEIGHT, min(MAX_TERM_WEIGHT, reinforcement))
        record["reinforcement"] = round(reinforcement, 3)
        if matched:
            candidate_context = run_contexts.get(key)
            if isinstance(candidate_context, str) and candidate_context.strip():
                existing = record.get("contexts", [])
                if not isinstance(existing, list):
                    existing = []
                record["contexts"] = trim_contexts(existing + [candidate_context])

    return current_date


def record_daily_suggestions(
    learning: dict,
    current_date: str | None,
    ranked_terms: list[str],
    source_text: str,
) -> None:
    if not current_date:
        return

    term_contexts: dict[str, str] = {}
    for term in ranked_terms:
        if not isinstance(term, str):
            continue
        key = normalize_term(term).lower()
        if not key:
            continue
        context = extract_candidate_context(source_text, term)
        if context:
            term_contexts[key] = context

    learning.setdefault("runs", {})
    learning["runs"][current_date] = {
        "suggested_terms": ranked_terms,
        "term_contexts": term_contexts,
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


def rank_link_candidates(
    original: str,
    terms: list[str],
    learning: dict,
    max_links: int = 45,
    current_date: str | None = None,
    journal_dir: str | None = None,
    embedder: LocalEmbeddingCache | None = None,
) -> list[str]:
    weights = learning.get("term_weights", {})
    term_memory = learning.get("term_memory", {})
    embedder = embedder or LocalEmbeddingCache(learning)
    parsed_current_date = current_date or parse_journal_date(original)
    reference_date = parsed_current_date or today_date_str()
    recent_topic_activity = collect_recent_topic_activity(
        journal_dir=journal_dir,
        reference_date=parsed_current_date,
        lookback_days=BURST_LOOKBACK_DAYS,
    )

    seen: set[str] = set()
    normalized_terms: list[str] = []
    semantic_inputs: list[str] = []
    for raw in terms:
        if not isinstance(raw, str):
            continue
        term = normalize_term(raw)
        if len(term) < 3:
            continue

        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_terms.append(term)
        candidate_context = extract_candidate_context(original, term)
        semantic_basis = term if not candidate_context else f"{term} {candidate_context}"
        semantic_inputs.append(normalize_embedding_text(semantic_basis, max_chars=240))

    embedding_scores: dict[str, float] = {}
    if semantic_inputs:
        vectors = embedder.embed_many([normalize_embedding_text(original, max_chars=1400)] + semantic_inputs, max_chars=1400)
        source_vector = vectors[0] if vectors else None
        for term, vector in zip(normalized_terms, vectors[1:]):
            if not source_vector or not vector:
                continue
            key = term.lower()
            embedding_scores[key] = max(0.0, embedding_cosine_similarity(source_vector, vector))

    scored: list[tuple[float, int, int, int, str]] = []
    seen.clear()
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
        score += compute_burst_weight(recent_topic_activity.get(key, 0))

        if isinstance(term_memory, dict):
            record = term_memory.get(key, {})
        else:
            record = {}
        if isinstance(record, dict):
            reinforcement = float(record.get("reinforcement", 0.0))
            successes = max(0, int(record.get("success_count", 0)))
            failures = max(0, int(record.get("failure_count", 0)))
            last_success = record.get("last_success_date") or record.get("last_seen_date")
            recency = compute_recency_weight(last_success, reference_date)
            history_contexts = record.get("contexts", [])
            if not isinstance(history_contexts, list):
                history_contexts = []
            semantic = compute_semantic_similarity(extract_candidate_context(original, term), history_contexts)

            usage = math.log1p(successes) - (0.4 * math.log1p(failures))
            score += reinforcement * 1.5
            score += usage * 4.0
            score += recency * 8.0
            score += semantic * 10.0
            score += embedding_scores.get(key, 0.0) * 7.0

        # Keep some influence from the model's original ordering.
        score += max(0.0, 8.0 - (idx * 0.15))

        scored.append((score, freq, len(term), -idx, term))

    scored.sort(reverse=True)
    return [term for _, _, _, _, term in scored[:max_links]]


def is_boundary_ok(text: str, s: int, e: int) -> bool:
    left_ok = s == 0 or not text[s - 1].isalnum()
    right_ok = e == len(text) or not text[e].isalnum()
    return left_ok and right_ok


_MD_FENCE_OPEN = re.compile(r"^\s*```[\w.-]*\s*$")
_MD_FENCE_CLOSE = re.compile(r"^\s*```\s*$")


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    out: list[tuple[int, int]] = [intervals[0]]
    for s, e in intervals[1:]:
        ps, pe = out[-1]
        if s <= pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def markdown_fenced_code_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges inside ``` fenced blocks (opening/closing lines included)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        line_end = nl if nl != -1 else n
        line = text[pos:line_end]
        if _MD_FENCE_OPEN.match(line):
            fence_start = pos
            pos = (nl + 1) if nl != -1 else n
            while pos < n:
                nl2 = text.find("\n", pos)
                line_end2 = nl2 if nl2 != -1 else n
                line2 = text[pos:line_end2]
                if _MD_FENCE_CLOSE.match(line2):
                    span_end = (nl2 + 1) if nl2 != -1 else line_end2
                    spans.append((fence_start, span_end))
                    pos = span_end
                    break
                pos = (nl2 + 1) if nl2 != -1 else n
            else:
                spans.append((fence_start, n))
            continue
        pos = (nl + 1) if nl != -1 else n
    return spans


def iter_wikilink_candidate_ranges(text: str):
    """Ranges where new wikilinks may appear: outside existing [[ ]] and fenced ``` blocks."""
    excluded: list[tuple[int, int]] = []
    for m in re.finditer(r"\[\[.*?\]\]", text, flags=re.DOTALL):
        excluded.append((m.start(), m.end()))
    excluded.extend(markdown_fenced_code_spans(text))
    excluded = _merge_intervals(excluded)
    last = 0
    for s, e in excluded:
        if s > last:
            yield last, s
        last = max(last, e)
    if last < len(text):
        yield last, len(text)


def paragraph_looks_like_shell_or_transcript(block: str) -> bool:
    """Heuristic: pasted terminal / launchd recipes — do not add wikilinks here."""
    if not block.strip():
        return False
    if re.search(r"~/Library/LaunchAgents", block):
        return True
    if re.search(r"~/Library/Logs/", block) and re.search(
        r"\b(readlink|tail|head|ls|cat|open|ln)\s",
        block,
        re.I,
    ):
        return True
    if re.search(
        r"launchctl\s+(bootstrap|kickstart|bootout|load|unload|print|list)\b",
        block,
        re.I,
    ):
        return True
    # Common CLI verbs at line start (macOS / homebrew workflows)
    _cli_verb = (
        r"^\s*(cp|mv|chmod|mkdir|launchctl|grep|egrep|fgrep|cat|tail|head|ls|sudo|brew|"
        r"plutil|python3|readlink|ln|rm|open|xattr|dirname|basename|which)\s"
    )
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) == 1:
        ln = lines[0]
        if re.match(r"^\s*just\s+", ln, re.I):
            return True
        if re.match(_cli_verb, ln, re.I):
            return True
    if len(lines) < 2:
        return False
    score = 0
    for ln in lines:
        if re.match(r"^\s*(%|\$\s+)", ln):
            score += 2
        elif re.match(_cli_verb, ln, re.I):
            score += 1
        elif re.search(r"@\S+:\s*~", ln):
            score += 2
        elif re.search(r"/Users/\S+.*\.(plist|sh)\b", ln):
            score += 1
    return score >= 2


def find_unlinked_span(text: str, term: str) -> tuple[int, int] | None:
    patterns = [
        re.compile(re.escape(term)),
        re.compile(re.escape(term), flags=re.IGNORECASE),
    ]
    for pattern in patterns:
        for start, end in iter_wikilink_candidate_ranges(text):
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


def insert_wikilinks_by_paragraph(text: str, ranked_terms: list[str]) -> str:
    chunks = re.split(r"(\n\s*\n)", text)
    for term in ranked_terms:
        for i, chunk in enumerate(chunks):
            if re.fullmatch(r"\n\s*\n", chunk):
                continue
            if paragraph_looks_like_shell_or_transcript(chunk):
                continue
            span = find_unlinked_span(chunk, term)
            if not span:
                continue
            s, e = span
            chunks[i] = chunk[:s] + "[[" + chunk[s:e] + "]]" + chunk[e:]
            break
    return "".join(chunks)


def insert_ranked_wikilinks(
    original: str,
    terms: list[str],
    learning: dict,
    max_links: int = 45,
    current_date: str | None = None,
    journal_dir: str | None = None,
) -> tuple[str, list[str]]:
    ranked_terms = rank_link_candidates(
        original,
        terms,
        learning,
        max_links=max_links,
        current_date=current_date,
        journal_dir=journal_dir,
    )
    frontmatter, body = split_frontmatter(original)
    linked_body = insert_wikilinks_by_paragraph(body, ranked_terms)
    return frontmatter + linked_body, ranked_terms


def main() -> int:
    run_started_at = datetime.now()
    MODEL, NUM_CTX, JOURNAL_DIR, RESET_LEARNING, ACTIVE_DATE, ACTIVE_FILE, WRITE_BACK, remaining_args = parse_cli()
    input_text, input_origin = get_input_text(remaining_args)
    input_text = strip_html_if_needed(input_text)
    (
        resolved_current_date,
        resolved_note_path,
        resolved_note_text,
        active_context_source,
    ) = resolve_current_journal_context(
        input_text=input_text,
        journal_dir=JOURNAL_DIR,
        active_date_override=ACTIVE_DATE,
        active_file_override=ACTIVE_FILE,
    )
    # launchd / empty pipe / TTY often leave clipboard = terminal junk; prefer disk note.
    input_body_source = input_origin
    if input_origin in ("stdin_pipe_empty", "clipboard") and resolved_note_text is not None:
        input_text = strip_html_if_needed(resolved_note_text)
        input_body_source = f"{input_origin}->journal_file"
    elif not input_text.strip() and resolved_note_text and resolved_note_text.strip():
        input_text = strip_html_if_needed(resolved_note_text)
        input_body_source = f"{input_origin}->journal_file"
    elif not input_text.strip() and input_origin == "stdin_pipe_empty":
        input_text = strip_html_if_needed(get_clipboard_text())
        input_body_source = "stdin_pipe_empty->clipboard"
    report_base_dir = derive_report_base_dir(JOURNAL_DIR, resolved_note_path)

    has_embedded_date = parse_journal_date(input_text) is not None
    file_nav_sync_applied = False
    previous_file_nav_sync_applied = False
    previous_path: Path | None = None
    prompt: str | None = None
    t0: float | None = None
    t1: float | None = None
    t2: float | None = None
    eval_duration_ns: int | None = None
    suggested_terms: list[str] = []
    ranked_terms: list[str] = []
    out: str | None = None
    touched_files: list[Path] = []
    actions: list[dict[str, str]] = []

    if has_embedded_date and resolved_current_date:
        input_text = sync_daily_navigation_links(
            input_text,
            JOURNAL_DIR,
            current_date_override=resolved_current_date,
        )
    elif resolved_current_date and resolved_note_path:
        file_nav_sync_applied = sync_navigation_links_in_file(
            note_path=resolved_note_path,
            journal_dir=JOURNAL_DIR,
            current_date=resolved_current_date,
        )
        actions.append(
            {
                "action": "Sync current note navigation links",
                "result": "updated" if file_nav_sync_applied else "no change",
                "target": str(resolved_note_path),
            }
        )
        if file_nav_sync_applied:
            touched_files.append(resolved_note_path)

    if resolved_current_date and JOURNAL_DIR:
        previous_existing_date = find_previous_existing_journal_date(JOURNAL_DIR, resolved_current_date)
        if previous_existing_date:
            previous_path = Path(JOURNAL_DIR) / f"{previous_existing_date}.md"
            if previous_path.exists():
                previous_file_nav_sync_applied = sync_navigation_links_in_file(
                    note_path=previous_path,
                    journal_dir=JOURNAL_DIR,
                    current_date=previous_existing_date,
                )
                actions.append(
                    {
                        "action": "Sync previous note navigation links",
                        "result": "updated" if previous_file_nav_sync_applied else "no change",
                        "target": str(previous_path),
                    }
                )
                if previous_file_nav_sync_applied:
                    touched_files.append(previous_path)

    if not input_text.strip():
        error_message = (
            "Error: No input provided. Use stdin, a CLI argument, clipboard, or SCRIBE_JOURNAL_DIR "
            "(latest YYYY-MM-DD.md / --active-date / --active-file)."
        )
        actions.append(
            {
                "action": "Read input",
                "result": "failed",
                "target": "stdin/argv/clipboard",
            }
        )
        report_path = write_run_report(
            base_dir=report_base_dir,
            started_at=run_started_at,
            finished_at=datetime.now(),
            status="error",
            model=MODEL,
            num_ctx=NUM_CTX,
            journal_dir=JOURNAL_DIR,
            active_context_source=active_context_source,
            active_date=resolved_current_date,
            active_file=resolved_note_path,
            input_text=input_text,
            prompt=prompt,
            suggested_terms=suggested_terms,
            ranked_terms=ranked_terms,
            output_text=out,
            file_nav_sync_applied=file_nav_sync_applied,
            previous_file_nav_sync_applied=previous_file_nav_sync_applied,
            actions=actions,
            touched_files=touched_files,
            error_message=error_message,
            traceback_text=None,
        )
        if report_path is not None:
            print(f"[Scribe] report={report_path}", file=sys.stderr)
        print(
            error_message,
            file=sys.stderr,
        )
        return 2

    prompt = build_prompt(input_text)
    actions.append(
        {
            "action": "Build prompt",
            "result": "completed",
            "target": f"{len(prompt)} chars",
        }
    )

    # MODEL = "deepseek-r1:32b"  # removed as per instructions
    # Use string "5m" per API docs so model stays loaded; run "ollama ps" while Scribe runs or right after
    KEEP_ALIVE = "5m"

    try:
        if RESET_LEARNING and MEMORY_STORE_FILE.exists():
            MEMORY_STORE_FILE.unlink(missing_ok=True)
            actions.append(
                {
                    "action": "Reset learning store",
                    "result": "deleted",
                    "target": str(MEMORY_STORE_FILE),
                }
            )

        memory_store_data = load_memory_store(MEMORY_STORE_FILE)
        current_date = apply_previous_day_feedback(
            memory_store_data,
            input_text,
            JOURNAL_DIR,
            current_date_override=resolved_current_date,
        )
        actions.append(
            {
                "action": "Apply previous-day feedback",
                "result": "completed" if current_date else "skipped",
                "target": current_date or "no active date",
            }
        )

        t0 = time.perf_counter()
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0, "num_ctx": NUM_CTX},
            keep_alive=KEEP_ALIVE,
        )
        t1 = time.perf_counter()
        eval_duration_ns = response.get("eval_duration")
        actions.append(
            {
                "action": "Request candidate links from model",
                "result": "completed",
                "target": MODEL,
            }
        )
        data = extract_json_obj(response["message"]["content"])
        terms = data.get("links", [])
        if not isinstance(terms, list):
            raise ValueError("Model JSON must include a list at key 'links'.")
        suggested_terms = [term for term in terms if isinstance(term, str)]
        out, ranked_terms = insert_ranked_wikilinks(
            input_text,
            terms,
            memory_store_data,
            current_date=current_date,
            journal_dir=JOURNAL_DIR,
        )
        actions.append(
            {
                "action": "Insert ranked wikilinks",
                "result": "completed",
                "target": f"{len(ranked_terms)} ranked terms",
            }
        )
        record_daily_suggestions(memory_store_data, current_date, ranked_terms, input_text)
        actions.append(
            {
                "action": "Record daily suggestions",
                "result": "completed" if current_date else "skipped",
                "target": current_date or "no active date",
            }
        )
        save_memory_store(MEMORY_STORE_FILE, memory_store_data)
        touched_files.append(MEMORY_STORE_FILE)
        actions.append(
            {
                "action": "Save learning store",
                "result": "updated",
                "target": str(MEMORY_STORE_FILE),
            }
        )
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
        print(
            f"[Scribe] active_context_source={active_context_source} "
            f"input_body_source={input_body_source} "
            f"active_date={resolved_current_date or 'unset'} "
            f"active_file={str(resolved_note_path) if resolved_note_path else 'unset'} "
            f"file_nav_sync_applied={file_nav_sync_applied} "
            f"previous_file_nav_sync_applied={previous_file_nav_sync_applied}",
            file=sys.stderr,
        )
        if eval_duration_ns:
            print(f"[Scribe] eval_duration_ns={eval_duration_ns}", file=sys.stderr)
        report_path = write_run_report(
            base_dir=report_base_dir,
            started_at=run_started_at,
            finished_at=datetime.now(),
            status="success",
            model=MODEL,
            num_ctx=NUM_CTX,
            journal_dir=JOURNAL_DIR,
            active_context_source=active_context_source,
            active_date=resolved_current_date,
            active_file=resolved_note_path,
            input_text=input_text,
            prompt=prompt,
            suggested_terms=suggested_terms,
            ranked_terms=ranked_terms,
            output_text=out,
            file_nav_sync_applied=file_nav_sync_applied,
            previous_file_nav_sync_applied=previous_file_nav_sync_applied,
            actions=actions,
            touched_files=touched_files,
            error_message=None,
            traceback_text=None,
            ollama_sec=(t1 - t0) if t0 is not None and t1 is not None else None,
            postprocess_sec=(t2 - t1) if t1 is not None and t2 is not None else None,
            eval_duration_ns=eval_duration_ns,
        )
        if report_path is not None:
            print(f"[Scribe] report={report_path}", file=sys.stderr)

        if WRITE_BACK and resolved_note_path is not None and "journal_file" in input_body_source:
            try:
                resolved_note_path.write_text(out, encoding="utf-8")
                touched_files.append(resolved_note_path)
                print(f"[Scribe] write_back={resolved_note_path}", file=sys.stderr)
            except Exception as wb_err:
                print(f"[Scribe] write_back failed: {wb_err}", file=sys.stderr)

        print(out)
        return 0
    except Exception as e:
        actions.append(
            {
                "action": "Run journal linker",
                "result": "failed",
                "target": str(resolved_note_path) if resolved_note_path else "current input",
            }
        )
        report_path = write_run_report(
            base_dir=report_base_dir,
            started_at=run_started_at,
            finished_at=datetime.now(),
            status="error",
            model=MODEL,
            num_ctx=NUM_CTX,
            journal_dir=JOURNAL_DIR,
            active_context_source=active_context_source,
            active_date=resolved_current_date,
            active_file=resolved_note_path,
            input_text=input_text,
            prompt=prompt,
            suggested_terms=suggested_terms,
            ranked_terms=ranked_terms,
            output_text=out,
            file_nav_sync_applied=file_nav_sync_applied,
            previous_file_nav_sync_applied=previous_file_nav_sync_applied,
            actions=actions,
            touched_files=touched_files,
            error_message=str(e),
            traceback_text=traceback.format_exc(),
            ollama_sec=(t1 - t0) if t0 is not None and t1 is not None else None,
            postprocess_sec=(t2 - t1) if t1 is not None and t2 is not None else None,
            eval_duration_ns=eval_duration_ns,
        )
        if report_path is not None:
            print(f"[Scribe] report={report_path}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
