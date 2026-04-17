import argparse
import contextlib
import hashlib
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path

from journal_linker_env import bootstrap_journal_linker_env
from weekly_insights import (
    DEFAULT_MEMORY_STORE_FILE,
    MIN_SUBSTANTIVE_ENTRY_WORDS,
    build_entry_excerpt,
    clean_daily_journal_text,
    extract_json_obj,
    extract_keyword_tokens,
    load_memory_store,
    strip_think,
)

try:
    import ollama
except Exception:  # pragma: no cover - import availability depends on local runtime
    ollama = None


DEFAULT_STATE_FILE = Path(__file__).with_name("daily_reflection_state.json")
DEFAULT_PUSHOVER_SERVER = "https://api.pushover.net"
DEFAULT_WINDOW_START = "16:00"
DEFAULT_WINDOW_END = "21:00"
DEFAULT_PUSHOVER_PRIORITY = "0"
KEEP_ALIVE = "5m"
MIN_DAILY_WORDS = 45
MIN_CONFIDENCE_TO_SEND = 0.45
MAX_BODY_CHARS = 360
MAX_TITLE_CHARS = 80
MEMORY_HIT_LIMIT = 6
DATE_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
DEFAULT_DAILY_REFLECTION_MODEL = "llama3.1:8b"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    bootstrap_journal_linker_env(repo_root=Path(__file__).resolve().parent)

    parser = argparse.ArgumentParser(description="Generate and send a once-daily day-behind Pushover reflection.")
    parser.add_argument("--journal-dir", default=os.getenv("SCRIBE_JOURNAL_DIR"))
    parser.add_argument("--learning-file", default=str(DEFAULT_MEMORY_STORE_FILE))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--date", help="Reflected date in YYYY-MM-DD. Defaults to yesterday in local time.")
    parser.add_argument("--dry-run", action="store_true", help="Print the generated notification without sending it.")
    parser.add_argument("--force-send", action="store_true", help="Bypass duplicate-send protection for manual testing.")
    return parser.parse_args(argv)


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def resolve_reflection_date(now: datetime, override: str | None = None) -> date:
    override_date = parse_iso_date(override)
    if override_date is not None:
        return override_date
    return now.date() - timedelta(days=1)


def parse_window_time(value: str, fallback: str) -> time:
    raw = (value or fallback).strip() or fallback
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except Exception:
        return datetime.strptime(fallback, "%H:%M").time()


def resolve_window() -> tuple[time, time]:
    start = parse_window_time(os.getenv("SCRIBE_DAILY_REFLECTION_WINDOW_START", DEFAULT_WINDOW_START), DEFAULT_WINDOW_START)
    end = parse_window_time(os.getenv("SCRIBE_DAILY_REFLECTION_WINDOW_END", DEFAULT_WINDOW_END), DEFAULT_WINDOW_END)
    if datetime.combine(date.today(), end) <= datetime.combine(date.today(), start):
        start = datetime.strptime(DEFAULT_WINDOW_START, "%H:%M").time()
        end = datetime.strptime(DEFAULT_WINDOW_END, "%H:%M").time()
    return start, end


def compute_target_send_time(run_date: date, start: time, end: time, seed: str = "") -> datetime:
    start_dt = datetime.combine(run_date, start)
    end_dt = datetime.combine(run_date, end)
    span_seconds = int((end_dt - start_dt).total_seconds())
    if span_seconds <= 0:
        return start_dt

    digest = hashlib.sha256(f"{seed}|{run_date.isoformat()}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    offset_seconds = rng.randint(0, span_seconds)
    return start_dt + timedelta(seconds=offset_seconds)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}}
    if not isinstance(data, dict):
        return {"days": {}}
    days = data.get("days", {})
    if not isinstance(days, dict):
        days = {}
    return {"days": days}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def prepare_state_record(state: dict, reflection_date: date, target_send_at: datetime) -> dict:
    days = state.setdefault("days", {})
    record = days.get(reflection_date.isoformat())
    if not isinstance(record, dict):
        record = {}
        days[reflection_date.isoformat()] = record
    record.setdefault("target_send_at", target_send_at.isoformat(timespec="seconds"))
    if record.get("target_send_at") != target_send_at.isoformat(timespec="seconds"):
        record["target_send_at"] = target_send_at.isoformat(timespec="seconds")
    record.setdefault("sent", False)
    record.setdefault("attempt_count", 0)
    record.setdefault("last_error", "")
    record.setdefault("last_attempt_at", "")
    record.setdefault("sent_at", "")
    record.setdefault("context_hash", "")
    return record


def extract_date_from_journal_filename(path: Path) -> str | None:
    match = DATE_FILENAME_RE.match(path.name)
    if not match:
        return None
    return match.group(1)


def find_latest_modified_journal_note(journal_dir: Path) -> Path | None:
    if not journal_dir.exists():
        return None

    latest_path: Path | None = None
    latest_mtime: float | None = None
    for note_path in journal_dir.glob("*.md"):
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


def resolve_entry_target(journal_dir: Path, reflection_date: date) -> tuple[date, str]:
    direct_path = journal_dir / f"{reflection_date.isoformat()}.md"
    if direct_path.exists():
        return reflection_date, "yesterday"

    latest_note = find_latest_modified_journal_note(journal_dir)
    if latest_note is None:
        return reflection_date, "yesterday"

    latest_date = parse_iso_date(extract_date_from_journal_filename(latest_note))
    if latest_date is None:
        return reflection_date, "yesterday"
    return latest_date, "latest_valid"


def find_memory_hits(cleaned_text: str, memory_store: dict) -> list[str]:
    text = cleaned_text.lower()
    hits: list[str] = []
    term_memory = memory_store.get("term_memory", {})
    if not isinstance(term_memory, dict):
        return hits
    for raw_term in term_memory:
        if not isinstance(raw_term, str):
            continue
        term = raw_term.strip().lower()
        if not term:
            continue
        if " " in term:
            matched = term in text
        else:
            matched = re.search(rf"\b{re.escape(term)}\b", text) is not None
        if matched:
            hits.append(term)
    hits.sort()
    return hits[:MEMORY_HIT_LIMIT]


def collect_daily_entry(journal_dir: Path, reflection_date: date) -> dict:
    note_path = journal_dir / f"{reflection_date.isoformat()}.md"
    if not note_path.exists():
        return {
            "date": reflection_date.isoformat(),
            "path": note_path,
            "exists": False,
            "raw_text": "",
            "cleaned_text": "",
            "word_count": 0,
            "is_substantive": False,
        }

    try:
        raw_text = note_path.read_text(encoding="utf-8")
    except Exception:
        raw_text = ""
    cleaned_text = clean_daily_journal_text(raw_text)
    word_count = len(re.findall(r"[A-Za-z][A-Za-z'-]{1,}", cleaned_text))
    return {
        "date": reflection_date.isoformat(),
        "path": note_path,
        "exists": True,
        "raw_text": raw_text,
        "cleaned_text": cleaned_text,
        "word_count": word_count,
        "is_substantive": word_count >= MIN_SUBSTANTIVE_ENTRY_WORDS,
    }


def compute_context_hash(entry: dict) -> str:
    cleaned_text = str(entry.get("cleaned_text", "")).strip()
    payload = f"{entry.get('date', '')}\n{cleaned_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_daily_reflection_signals(entry: dict, memory_store: dict) -> dict:
    if not entry["exists"]:
        return {
            "date": entry["date"],
            "missing": True,
            "word_count": 0,
            "confidence": 0.0,
            "memory_hits": [],
            "top_keywords": [],
            "excerpt": "",
        }

    cleaned_text = str(entry["cleaned_text"])
    keywords = extract_keyword_tokens(cleaned_text)
    unique_keywords = sorted(set(keywords))
    memory_hits = find_memory_hits(cleaned_text, memory_store)
    excerpt = build_entry_excerpt(cleaned_text, max_chars=700)

    confidence = (
        min(1.0, entry["word_count"] / 160.0) * 0.55
        + min(1.0, len(unique_keywords) / 10.0) * 0.20
        + min(1.0, len(memory_hits) / 3.0) * 0.25
    )
    confidence = round(min(1.0, confidence), 2)

    return {
        "date": entry["date"],
        "missing": False,
        "word_count": entry["word_count"],
        "confidence": confidence,
        "memory_hits": memory_hits,
        "top_keywords": unique_keywords[:8],
        "excerpt": excerpt,
        "full_text": cleaned_text,
    }


def evaluate_skip_reason(entry: dict, signals: dict) -> str | None:
    if not entry["exists"]:
        return "missing daily note"
    if not entry["is_substantive"]:
        return "insufficient daily signal"
    if signals["word_count"] < MIN_DAILY_WORDS:
        return "insufficient daily signal"
    if signals["confidence"] < MIN_CONFIDENCE_TO_SEND:
        return "insufficient daily signal"
    return None


def build_daily_reflection_prompt(signals: dict) -> str:
    return f"""
Return JSON only.

Write a calm, grounded reflection about a completed day from journal evidence.
The goal is not to summarize the facts. The goal is to identify what the facts suggest about the shape of the day.

Rules:
- Output exactly this shape: {{"title":"...","body":"...","confidence":0.0,"should_send":true,"reason":"..."}}
- "title" must be short, natural, and under 80 characters.
- "body" must be 1 short paragraph, maximum 3 sentences.
- Include the date implicitly in the perspective of a completed day, not as urgent or current.
- Keep the tone restrained, reflective, and concrete.
- Do not use therapy-speak, motivational filler, or advice-list language.
- Base everything on the provided evidence only, but you may make light interpretive inferences about what seemed central, charged, or desired.
- Prefer the underlying pattern over the surface detail. Treat concrete details as signals of taste, mood, desire, avoidance, momentum, or restlessness.
- Do not produce a bullet-list summary of events.
- Avoid naming proper nouns or one-off specifics unless they are clearly central to the meaning of the day.
- Do not over-psychologize. Avoid words like "struggle", "escape", "avoidance", or "issue" unless the evidence clearly supports them.
- Prefer language like "drawn toward", "ready for", "wanting more of", "leaning toward", or "circling around" when the evidence is subtle.
- If a detail seems culturally specific or taste-specific, translate it into what it implies about mood or appetite rather than just repeating the noun.
- Ask yourself: what were these details pointing toward?
- Good reflections sound like they noticed the thread connecting the day, not like they copied the journal.
- Avoid stock phrasing such as "the day was marked by", "followed by", or "there was a sense of".
- Prefer writing that begins with the felt center of the day rather than a recap of events.

Interpretation examples:
- If the note mentions wanting a short, efficient "power hour", the reflection should usually talk about wanting concentrated momentum, competence, or clean focus, not just repeat "power hour".
- If the note mentions a specific artist, studio, genre, or media fixation, the reflection should usually name the kind of aesthetic or emotional appetite it points to, not just repeat the proper noun.
- If the note mentions a season, trip, or upcoming change, the reflection should usually talk about anticipation, lightness, restlessness, or readiness for a shift, not just restate the event.

Bad reflection:
- "The day was productive, then there was interest in A24, and then excitement for summer."

Better reflection:
- "The day seemed to want two things at once: a clean pocket of competence and a sense of movement toward something lighter and more alive."
- If the evidence is too thin for a real reflection, set "should_send" to false and "reason" to "insufficient daily signal".
- Confidence should be a float from 0.0 to 1.0.

DAILY SIGNALS:
{json.dumps(signals, ensure_ascii=True, indent=2)}
""".strip()


def request_daily_reflection(signals: dict) -> dict:
    if ollama is None:
        raise RuntimeError("The 'ollama' Python package is required for daily reflection generation.")

    model = (
        os.getenv("SCRIBE_DAILY_REFLECTION_MODEL", "").strip()
        or os.getenv("SCRIBE_MODEL", "").strip()
        or DEFAULT_DAILY_REFLECTION_MODEL
    )
    num_ctx_raw = os.getenv("SCRIBE_CTX", "8192")
    try:
        num_ctx = int(num_ctx_raw)
    except Exception:
        num_ctx = 8192

    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": build_daily_reflection_prompt(signals)}],
        options={"temperature": 0.45, "num_ctx": num_ctx},
        keep_alive=KEEP_ALIVE,
    )
    data = extract_json_obj(strip_think(response["message"]["content"]))
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    reason = str(data.get("reason", "")).strip() or "insufficient daily signal"
    try:
        confidence = float(data.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    should_send = bool(data.get("should_send", False))
    return {
        "title": title[:MAX_TITLE_CHARS].strip(),
        "body": body[:MAX_BODY_CHARS].strip(),
        "reason": reason,
        "confidence": max(0.0, min(1.0, confidence)),
        "should_send": should_send and bool(title) and bool(body),
    }


def build_notification_payload(reflection_date: date, reflection: dict) -> dict:
    title = reflection["title"].strip() or f"Reflection on {reflection_date.isoformat()}"
    body = reflection["body"].strip()
    message = f"{body}\n\nDate: {reflection_date.isoformat()}".strip()
    return {"title": title, "message": message}


def get_pushover_app_token() -> str:
    return os.getenv("SCRIBE_PUSHOVER_APP_TOKEN", "").strip() or os.getenv("PUSHOVER_TOKEN", "").strip()


def get_pushover_user_key() -> str:
    return os.getenv("SCRIBE_PUSHOVER_USER_KEY", "").strip() or os.getenv("PUSHOVER_KEY", "").strip()


def publish_pushover(payload: dict, server: str, app_token: str, user_key: str, device: str = "", priority: str = "") -> tuple[int, str]:
    if not app_token.strip():
        raise ValueError("SCRIBE_PUSHOVER_APP_TOKEN is required for Pushover delivery.")
    if not user_key.strip():
        raise ValueError("SCRIBE_PUSHOVER_USER_KEY is required for Pushover delivery.")

    base = server.strip().rstrip("/") or DEFAULT_PUSHOVER_SERVER
    url = f"{base}/1/messages.json"
    form_data = {
        "token": app_token.strip(),
        "user": user_key.strip(),
        "title": payload["title"],
        "message": payload["message"],
        "priority": (priority or DEFAULT_PUSHOVER_PRIORITY).strip() or DEFAULT_PUSHOVER_PRIORITY,
    }
    if device.strip():
        form_data["device"] = device.strip()

    data = urllib.parse.urlencode(form_data).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    with contextlib.closing(urllib.request.urlopen(request, timeout=15)) as response:
        status = getattr(response, "status", 200)
        body = response.read().decode("utf-8", errors="replace")
        return status, body


def run_daily_reflection(
    journal_dir: str,
    learning_file: str,
    state_file: str,
    now: datetime | None = None,
    date_override: str | None = None,
    dry_run: bool = False,
    force_send: bool = False,
) -> dict:
    current_time = now or datetime.now()
    reflection_date = resolve_reflection_date(current_time, override=date_override)
    start_time, end_time = resolve_window()
    target_send_at = compute_target_send_time(
        current_time.date(),
        start=start_time,
        end=end_time,
        seed=os.getenv("SCRIBE_DAILY_REFLECTION_SEED", ""),
    )
    window_end_at = datetime.combine(current_time.date(), end_time)

    state_path = Path(state_file)
    state = load_state(state_path)
    record = prepare_state_record(state, reflection_date, target_send_at)

    result = {
        "status": "skipped",
        "reason": "",
        "reflection_date": reflection_date.isoformat(),
        "target_send_at": target_send_at.isoformat(timespec="seconds"),
        "source": "yesterday",
        "sent": False,
        "dry_run": dry_run,
        "payload": None,
    }

    if record.get("sent") and not force_send:
        result["reason"] = "already sent"
        return result

    if current_time < target_send_at and not dry_run and not force_send:
        result["reason"] = "before target send time"
        return result

    if current_time > window_end_at and not dry_run and not force_send:
        result["reason"] = "window closed"
        return result

    journal_path = Path(journal_dir).expanduser()
    memory_store = load_memory_store(Path(learning_file))
    target_date, source = resolve_entry_target(journal_path, reflection_date)
    result["reflection_date"] = target_date.isoformat()
    result["source"] = source
    if target_date != reflection_date:
        record = prepare_state_record(state, target_date, target_send_at)
    entry = collect_daily_entry(journal_path, target_date)
    signals = build_daily_reflection_signals(entry, memory_store)
    skip_reason = evaluate_skip_reason(entry, signals)
    if skip_reason:
        result["reason"] = skip_reason
        return result

    context_hash = compute_context_hash(entry)
    if record.get("context_hash") == context_hash and not force_send:
        result["reason"] = "no new context since last run"
        return result

    reflection = request_daily_reflection(signals)
    if (not reflection["should_send"]) or reflection["confidence"] < MIN_CONFIDENCE_TO_SEND:
        result["reason"] = reflection["reason"] or "insufficient daily signal"
        return result

    payload = build_notification_payload(reflection_date, reflection)
    result["payload"] = payload

    if dry_run:
        result["status"] = "dry-run"
        result["reason"] = "dry run"
        return result

    record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
    record["last_attempt_at"] = current_time.isoformat(timespec="seconds")
    record["context_hash"] = context_hash
    try:
        status_code, response_body = publish_pushover(
            payload=payload,
            server=os.getenv("SCRIBE_PUSHOVER_SERVER", DEFAULT_PUSHOVER_SERVER),
            app_token=get_pushover_app_token(),
            user_key=get_pushover_user_key(),
            device=os.getenv("SCRIBE_PUSHOVER_DEVICE", ""),
            priority=os.getenv("SCRIBE_PUSHOVER_PRIORITY", DEFAULT_PUSHOVER_PRIORITY),
        )
    except Exception as exc:
        record["last_error"] = str(exc)
        save_state(state_path, state)
        result["status"] = "failed"
        result["reason"] = str(exc)
        return result

    record["sent"] = True
    record["sent_at"] = current_time.isoformat(timespec="seconds")
    record["last_error"] = ""
    record["last_response"] = response_body[:500]
    record["status_code"] = status_code
    save_state(state_path, state)

    result["status"] = "sent"
    result["reason"] = "sent"
    result["sent"] = True
    return result


def main() -> int:
    args = parse_args()
    if not args.journal_dir:
        print("Error: journal directory is required. Use --journal-dir or SCRIBE_JOURNAL_DIR.")
        return 2

    try:
        result = run_daily_reflection(
            journal_dir=args.journal_dir,
            learning_file=args.learning_file,
            state_file=args.state_file,
            date_override=args.date,
            dry_run=args.dry_run,
            force_send=args.force_send,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    if result["payload"]:
        payload = result["payload"]
        print(
            "[daily_reflection] "
            f"status={result['status']} "
            f"date={result['reflection_date']} "
            f"target={result['target_send_at']} "
            f"reason={result['reason']} "
            f"title={json.dumps(payload['title'])} "
            f"message={json.dumps(payload['message'])}"
        )
    else:
        print(
            "[daily_reflection] "
            f"status={result['status']} "
            f"date={result['reflection_date']} "
            f"target={result['target_send_at']} "
            f"reason={result['reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
