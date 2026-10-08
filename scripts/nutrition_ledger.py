#!/usr/bin/env python3
"""Nutrition ledger for voice callouts.

Reads `> [!voice] HH:MM` blocks, asks a local model what was eaten or drunk,
looks up branded names on the web, and appends one JSONL row per intake.
A separate `--summary` command sends one end-of-day Pushover and scores the
previous journal day against nutrition_memory.json.

This file does not write the intent delivery ledger or scribe_learning.json.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from text_normalize import strip_wikilinks  # noqa: E402

LEDGER_FILENAME = "nutrition_ledger.jsonl"
PROCESSED_FILENAME = "nutrition_processed.jsonl"
CACHE_FILENAME = "nutrition_lookup_cache.json"
MEMORY_FILENAME = "nutrition_memory.json"
SUMMARY_STATE_FILENAME = "nutrition_summary_state.json"

DEFAULT_GATE_MODEL = "qwen2.5:14b"
DEFAULT_LOOKUP_MODEL = "gpt-4o-mini"
DEFAULT_PUSHOVER_SERVER = "https://api.pushover.net"
PUSHOVER_MESSAGE_LIMIT = 1024
PUSHOVER_TITLE_LIMIT = 250
LOOKUP_TIMEOUT_SEC = 20.0
ERROR_LOOKUP_RETRY_CAP = 3
KNOWN_ITEM_CAP = 20
KNOWN_ITEM_MIN_SEEN = 3
JOURNAL_SNIPPET_DAYS = 14
JOURNAL_SNIPPET_CAP = 5
MEMORY_CONTEXT_CAP = 5
MEMORY_AMOUNT_CAP = 10
REINFORCE_UP = 0.2
REINFORCE_DOWN = 0.5
REINFORCE_MIN = -2.0
REINFORCE_MAX = 2.0
KEEP_ALIVE = "5m"

MEAL_SLOTS = {"breakfast", "lunch", "dinner", "snack", "drink", "unknown"}
IDENTITIES = {"generic", "branded", "unresolved"}
KINDS = {"food", "drink"}
SERVING_BASES = {"stated", "default_serving", "approximate", "remembered"}
LOOKUP_STATUSES = {"ok", "miss", "ambiguous", "error", "cached", "skipped"}
COUNT_UNITS = {
    "serving", "glass", "cup", "bowl", "bottle", "can", "piece", "count",
    "plate", "slice", "container", "item", "drink", "carton", "pouch",
    "bar", "packet", "bag",
}

_VOICE_HEADER_RE = re.compile(r"^> \[!voice\]\s+(\d{1,2}:\d{2})\s*$")
_DATE_NOTE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

NUTRITION_LEDGER_PROMPT = (
    "You are a nutrition ledger clerk for one spoken journal utterance.\n"
    "Decide whether the speaker is reporting food or drink they are consuming or already consumed.\n"
    "Then extract every such item. Reason about vague speech. Do not invent a stricter phrase than they used.\n"
    "\n"
    "Log it when they report intake, including present tense (\"I'm drinking...\"), past tense (\"I had...\"), "
    "and bare fragments (\"two glasses of milk\", \"fourteen grams of chicken\").\n"
    "Do not log plans, wishes, or advice (\"I should eat more protein\", \"I need to buy milk\").\n"
    "Do not log other people's meals unless the speaker clearly ate or drank it too "
    "(\"we had pizza\" counts as one share for the speaker).\n"
    "A mention of food that is only a story, with no intake, is not a log.\n"
    "\n"
    "Quantity rules, in order:\n"
    "- A stated count or measure wins: \"eleven carrots\" is 11 carrots; \"fourteen grams of chicken\" is 14 g; "
    "\"two glasses\" is 2 glasses. Never replace a stated amount with a standard serving.\n"
    "- Number words and digits are the same (two, eleven, 14).\n"
    "- \"A glass\", \"a cup\", \"a bowl\", \"a bottle\", \"a serving\", \"some\", \"a bit\", or no amount at all, "
    "and the food or drink is named: quantity 1, unit \"serving\" or the container they named, "
    "serving_basis \"default_serving\". Use one ordinary single-person portion of that item "
    "(one plate of chicken alfredo, one 8 fl oz glass, one medium piece). Say what you assumed.\n"
    "- The food itself is only \"this\", \"that\", or \"it\", with no name in the utterance: still log it, "
    "label \"unresolved\", identity \"unresolved\", unresolved true, needs_lookup false, "
    "keep any stated quantity and container (\"a glass of this\" is 1 glass). "
    "Leave calories and macros null. Do not guess the food.\n"
    "- If preparation changes the estimate and they did not say (fried vs grilled, sweetened vs unsweetened), "
    "pick the common plain version and record that in assumption.\n"
    "- Keep the speaker's name. \"Bev\" stays \"Bev\". Do not expand a brand, product, or proper noun "
    "into a generic food you happen to know.\n"
    "\n"
    "Identity:\n"
    "- generic: ordinary foods and dishes a plain foods table would cover "
    "(carrots, milk, chicken, coffee, chicken alfredo). needs_lookup false. You may estimate.\n"
    "- branded: a brand, product line, packaged name, restaurant item, or any niche name you would not "
    "trust from memory alone. That includes short proper nouns like \"Bev\". identity \"branded\", "
    "needs_lookup true, and all four estimate fields null. Do not guess brand nutrition from memory.\n"
    "- unresolved: identity \"unresolved\", needs_lookup false, estimates null.\n"
    "\n"
    "Known items:\n"
    "The user message may include a Known items block. Prefer a stated amount over that block.\n"
    "When the utterance names a known item and gives no amount, set serving_basis to \"remembered\" "
    "and use the stored typical quantity and unit.\n"
    "\n"
    "Estimates (generic only):\n"
    "- Fill calories_est, protein_g_est, carbs_g_est, fat_g_est with rough USDA-like numbers for the "
    "quantity you settled on. These are estimates, not measurements.\n"
    "- Scale estimates to the stated quantity (14 g is not a full chicken breast).\n"
    "\n"
    "meal_slot is breakfast, lunch, dinner, snack, or drink only when the utterance or the clock time "
    "makes it plain. Otherwise \"unknown\". A drink can be meal_slot \"drink\".\n"
    "\n"
    "Respond with one JSON object and no other text.\n"
    "If this utterance is not intake, return {\"is_intake\": false, \"items\": [], \"notes\": \"\"}.\n"
)

NUTRITION_LOOKUP_PROMPT = (
    "Look up nutrition facts for this exact product name: \"{label}\".\n"
    "Use web search. Do not substitute a different brand or a generic food.\n"
    "If you cannot find that product, or several different products share the name, do not pick one.\n"
    "Return one JSON object and no other text. Numbers are for one serving of the matched product, "
    "not for a quantity the speaker stated.\n"
    '{"status":"ok|ambiguous|miss","matched_name":"","serving_description":"",'
    '"calories":null,"protein_g":null,"carbs_g":null,"fat_g":null,"source_urls":[]}\n'
)

CORRECTION_PROMPT = (
    "You compare a nutrition log to the journal note for that same day.\n"
    "Return corrections only when the speaker says a logged amount or a logged identity was wrong.\n"
    "Plans and wishes (\"I should drink less Bev\") are not corrections.\n"
    "If the note does not contradict the log, return an empty list.\n"
    "Respond with one JSON object and no other text.\n"
    '{"corrections":[{"label":"Bev","quantity":2,"unit":"can","resolved_name":"","reason":"speaker said two, not one"}]}\n'
)


def _log(msg: str) -> None:
    print(f"[nutrition] {msg}", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _extract_json_obj(text: str) -> dict:
    cleaned = _strip_think(text)
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
    raise ValueError(f"No valid JSON object found in model output: {cleaned[:200]!r}")


def nutrition_model() -> str:
    return os.getenv("INTENT_NUTRITION_MODEL", "").strip() or os.getenv("INTENT_GATE_MODEL", "").strip() or DEFAULT_GATE_MODEL


def lookup_model() -> str:
    return os.getenv("INTENT_NUTRITION_LOOKUP_MODEL", "").strip() or DEFAULT_LOOKUP_MODEL


def default_state_dir() -> Path:
    raw = os.getenv("INTENT_STATE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state" / "journal-linker" / "intents"


def default_journal_dir() -> Path | None:
    raw = os.getenv("SCRIBE_JOURNAL_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def normalize_label(label: str) -> str:
    text = str(label or "").strip().lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_unit(unit: str) -> str:
    text = str(unit or "").strip().lower()
    aliases = {
        "glasses": "glass",
        "grams": "g",
        "gram": "g",
        "g": "g",
        "ounces": "oz",
        "ounce": "oz",
        "cans": "can",
        "cups": "cup",
        "bowls": "bowl",
        "bottles": "bottle",
        "pieces": "piece",
        "plates": "plate",
        "slices": "slice",
        "servings": "serving",
    }
    if text in aliases:
        return aliases[text]
    if text.endswith("s") and text[:-1] in COUNT_UNITS:
        return text[:-1]
    return text


def _as_number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _clean_qty(number: float):
    if abs(number - round(number)) < 1e-6:
        return int(round(number))
    return round(number, 3)


def fmt_qty(number) -> str:
    try:
        value = float(number)
    except (TypeError, ValueError):
        return str(number)
    return str(_clean_qty(value))


_DISPLAY_PLURAL = {
    "glass": "glasses",
    "can": "cans",
    "cup": "cups",
    "bowl": "bowls",
    "bottle": "bottles",
    "piece": "pieces",
    "plate": "plates",
    "slice": "slices",
    "serving": "servings",
    "container": "containers",
    "item": "items",
    "drink": "drinks",
    "carton": "cartons",
    "pouch": "pouches",
    "bar": "bars",
    "packet": "packets",
    "bag": "bags",
}
_UNCOUNTED_UNITS = {"g", "oz", "ml", "kg", "lb"}


def display_unit(quantity, unit: str) -> str:
    """Pluralize a count unit when the quantity is not 1. Grams stay grams."""
    singular = normalize_unit(unit) or str(unit or "").strip()
    number = _as_number(quantity)
    if number is None or abs(number - 1.0) < 1e-6:
        return singular
    if singular in _UNCOUNTED_UNITS:
        return singular
    if singular in _DISPLAY_PLURAL:
        return _DISPLAY_PLURAL[singular]
    return singular


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return data if isinstance(data, dict) else fallback


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _clamp_reinforcement(value: float) -> float:
    return round(max(REINFORCE_MIN, min(REINFORCE_MAX, value)), 3)


# ---------------------------------------------------------------------------
# Voice callouts and span identity
# ---------------------------------------------------------------------------

def extract_voice_callouts(note_text: str) -> list[dict]:
    """Return {spoken_at, raw_text} for each voice callout, wikilinks stripped."""
    callouts: list[dict] = []
    lines = (note_text or "").splitlines()
    index = 0
    while index < len(lines):
        match = _VOICE_HEADER_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue
        hour, minute = match.group(1).split(":")
        spoken_at = f"{int(hour):02d}:{minute}"
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].startswith(">"):
            if _VOICE_HEADER_RE.match(lines[index].strip()):
                break
            content = lines[index][1:]
            if content.startswith(" "):
                content = content[1:]
            body.append(content)
            index += 1
        raw = strip_wikilinks(" ".join(body))
        raw = re.sub(r"\s+", " ", raw).strip()
        if raw:
            callouts.append({"spoken_at": spoken_at, "raw_text": raw})
    return callouts


def span_key(source_path: Path | str, spoken_at: str, raw_text: str) -> str:
    normalized = re.sub(r"\s+", " ", raw_text).strip().lower()
    payload = f"{source_path}|{spoken_at}|{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ledger, processed spans, cache, memory
# ---------------------------------------------------------------------------

def ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_FILENAME


def processed_path(state_dir: Path) -> Path:
    return state_dir / PROCESSED_FILENAME


def cache_path(state_dir: Path) -> Path:
    return state_dir / CACHE_FILENAME


def memory_path(state_dir: Path) -> Path:
    return state_dir / MEMORY_FILENAME


def summary_state_path(state_dir: Path) -> Path:
    return state_dir / SUMMARY_STATE_FILENAME


def load_ledger_rows(state_dir: Path) -> list[dict]:
    path = ledger_path(state_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    return rows


def rewrite_ledger_rows(state_dir: Path, rows: list[dict]) -> None:
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_processed_keys(state_dir: Path) -> set[str]:
    path = processed_path(state_dir)
    keys: set[str] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(record.get("idempotency_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def append_processed(state_dir: Path, record: dict) -> None:
    _append_jsonl(processed_path(state_dir), record)


def load_cache(state_dir: Path) -> dict:
    data = _read_json(cache_path(state_dir), {"labels": {}})
    labels = data.get("labels")
    if not isinstance(labels, dict):
        data["labels"] = {}
    return data


def save_cache(state_dir: Path, cache: dict) -> None:
    _atomic_write_json(cache_path(state_dir), cache)


def empty_memory() -> dict:
    return {"last_reinforced_date": "", "labels": {}}


def load_memory(state_dir: Path) -> dict:
    data = _read_json(memory_path(state_dir), empty_memory())
    if not isinstance(data.get("labels"), dict):
        data["labels"] = {}
    data.setdefault("last_reinforced_date", "")
    return data


def save_memory(state_dir: Path, memory: dict) -> None:
    _atomic_write_json(memory_path(state_dir), memory)


def _blank_memory_record(display: str) -> dict:
    return {
        "display": display,
        "seen_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "reinforcement": 0.0,
        "last_seen_date": "",
        "last_success_date": "",
        "typical": None,
        "resolved_name": "",
        "per_serving": None,
        "contexts": [],
        "recent_amounts": [],
    }


def _recompute_typical(amounts: list[dict]) -> dict | None:
    if not amounts:
        return None
    counts: dict[tuple, int] = {}
    for amount in amounts:
        key = (amount.get("quantity"), amount.get("unit"))
        counts[key] = counts.get(key, 0) + 1
    best = None
    best_count = -1
    best_index = -1
    for index, amount in enumerate(amounts):
        key = (amount.get("quantity"), amount.get("unit"))
        count = counts[key]
        if count > best_count or (count == best_count and index > best_index):
            best = amount
            best_count = count
            best_index = index
    if best is None:
        return None
    return {"quantity": best.get("quantity"), "unit": best.get("unit")}


def _maybe_unlock_prior(record: dict) -> None:
    typical = record.get("typical")
    if not isinstance(typical, dict):
        return
    if int(record.get("seen_count") or 0) < KNOWN_ITEM_MIN_SEEN:
        return
    if int(record.get("failure_count") or 0) > 0:
        return
    if float(record.get("reinforcement") or 0) > 0:
        return
    matches = 0
    for amount in record.get("recent_amounts") or []:
        if amount.get("quantity") == typical.get("quantity") and amount.get("unit") == typical.get("unit"):
            matches += 1
    if matches >= KNOWN_ITEM_MIN_SEEN:
        record["reinforcement"] = REINFORCE_UP


def _store_resolved_product(memory: dict, item: dict) -> None:
    lookup = item.get("lookup") if isinstance(item.get("lookup"), dict) else {}
    if lookup.get("status") not in {"ok", "cached"} or not isinstance(lookup.get("per_serving"), dict):
        return
    key = normalize_label(str(item.get("label") or ""))
    if not key:
        return
    labels = memory.setdefault("labels", {})
    record = labels.get(key)
    if not isinstance(record, dict):
        record = _blank_memory_record(str(item.get("label") or key))
        labels[key] = record
    matched = str(lookup.get("matched_name") or "").strip()
    if matched:
        record["resolved_name"] = matched
    record["per_serving"] = lookup.get("per_serving")


def update_memory_from_row(memory: dict, row: dict) -> None:
    """Fold one intake row into per-label memory. Stated amounts build `typical`."""
    labels = memory.setdefault("labels", {})
    source_date = str(row.get("source_date") or "")
    context = str(row.get("raw_text") or "").strip()[:160]
    for item in row.get("items") or []:
        if not isinstance(item, dict):
            continue
        display = str(item.get("label") or "").strip()
        key = normalize_label(display)
        if not key or item.get("unresolved"):
            continue
        record = labels.get(key)
        if not isinstance(record, dict):
            record = _blank_memory_record(display)
            labels[key] = record
        record["display"] = display or record.get("display") or key
        record["seen_count"] = int(record.get("seen_count") or 0) + 1
        record["last_seen_date"] = source_date or record.get("last_seen_date") or ""
        if str(item.get("serving_basis") or "") == "stated":
            quantity = _as_number(item.get("quantity"))
            unit = normalize_unit(str(item.get("unit") or ""))
            if quantity is not None and quantity > 0 and unit:
                amounts = record.setdefault("recent_amounts", [])
                amounts.append({"quantity": _clean_qty(quantity), "unit": unit})
                del amounts[:-MEMORY_AMOUNT_CAP]
                record["typical"] = _recompute_typical(amounts)
        lookup = item.get("lookup") if isinstance(item.get("lookup"), dict) else {}
        if lookup.get("status") in {"ok", "cached"} and lookup.get("per_serving"):
            matched = str(lookup.get("matched_name") or "").strip()
            if matched:
                record["resolved_name"] = matched
            record["per_serving"] = lookup.get("per_serving")
        if context:
            contexts = record.setdefault("contexts", [])
            if context not in contexts:
                contexts.append(context)
            del contexts[:-MEMORY_CONTEXT_CAP]
        _maybe_unlock_prior(record)


def known_item_records(memory: dict) -> list[dict]:
    labels = memory.get("labels") if isinstance(memory.get("labels"), dict) else {}
    chosen: list[dict] = []
    for record in labels.values():
        if not isinstance(record, dict):
            continue
        if int(record.get("seen_count") or 0) < KNOWN_ITEM_MIN_SEEN:
            continue
        if float(record.get("reinforcement") or 0) <= 0:
            continue
        chosen.append(record)
    chosen.sort(key=lambda rec: str(rec.get("last_seen_date") or ""), reverse=True)
    return chosen[:KNOWN_ITEM_CAP]


def format_known_items(memory: dict) -> str:
    lines: list[str] = []
    for record in known_item_records(memory):
        display = str(record.get("display") or "").strip()
        if not display:
            continue
        typical = record.get("typical") if isinstance(record.get("typical"), dict) else {}
        parts = [display]
        if typical.get("quantity") is not None and typical.get("unit"):
            parts.append(f"usual {fmt_qty(typical['quantity'])} {typical['unit']}")
        resolved = str(record.get("resolved_name") or "").strip()
        if resolved:
            parts.append(f"product {resolved}")
        lines.append(": ".join(parts[:1]) + ("; " + "; ".join(parts[1:]) if len(parts) > 1 else ""))
    return "\n".join(lines)


def apply_memory_priors(items: list[dict], memory: dict) -> None:
    """Vague amounts for a learned item take the stored typical quantity and unit."""
    by_key = {}
    for record in known_item_records(memory):
        key = normalize_label(str(record.get("display") or ""))
        if key:
            by_key[key] = record
    for item in items:
        if str(item.get("serving_basis") or "") == "stated":
            continue
        record = by_key.get(normalize_label(str(item.get("label") or "")))
        if not record:
            continue
        typical = record.get("typical") if isinstance(record.get("typical"), dict) else None
        if not typical or typical.get("quantity") is None or not typical.get("unit"):
            continue
        changed = (
            item.get("quantity") != typical.get("quantity")
            or normalize_unit(str(item.get("unit") or "")) != typical.get("unit")
        )
        item["quantity"] = typical["quantity"]
        item["unit"] = typical["unit"]
        item["serving_basis"] = "remembered"
        item["assumption"] = f"your usual: {fmt_qty(typical['quantity'])} {typical['unit']}"
        per_serving = record.get("per_serving") if isinstance(record.get("per_serving"), dict) else None
        if changed and not per_serving:
            item.update(_null_estimates())
        if per_serving:
            _apply_scaled_estimates(item, per_serving, "")


# ---------------------------------------------------------------------------
# Validation and portion scaling
# ---------------------------------------------------------------------------

def _null_estimates() -> dict:
    return {
        "calories_est": None,
        "protein_g_est": None,
        "carbs_g_est": None,
        "fat_g_est": None,
    }


def validate_extract(payload: dict) -> dict:
    """Type-check model JSON. Stated numbers are kept. Brand macros are cleared."""
    if not isinstance(payload, dict):
        raise ValueError("nutrition extract was not an object")
    is_intake = bool(payload.get("is_intake"))
    meal_slot = str(payload.get("meal_slot") or "unknown").strip().lower()
    if meal_slot not in MEAL_SLOTS:
        meal_slot = "unknown"
    notes = str(payload.get("notes") or "").strip()
    items_in = payload.get("items") if isinstance(payload.get("items"), list) else []
    items: list[dict] = []
    if is_intake:
        for raw in items_in:
            item = _validate_item(raw)
            if item is not None:
                items.append(item)
    if not items:
        is_intake = False
    return {
        "is_intake": is_intake,
        "meal_slot": meal_slot if is_intake else "unknown",
        "items": items,
        "notes": notes,
    }


def _validate_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    quantity = _as_number(raw.get("quantity"))
    if quantity is None or quantity <= 0:
        return None
    unresolved = bool(raw.get("unresolved")) or normalize_label(label) in {"unresolved", "this", "that", "it"}
    if unresolved:
        identity = "unresolved"
        label = "unresolved" if normalize_label(label) in {"this", "that", "it", "unresolved"} else label
    else:
        identity = str(raw.get("identity") or "").strip().lower()
        if identity not in IDENTITIES or identity == "unresolved":
            identity = "branded" if bool(raw.get("needs_lookup")) else "generic"
    kind = str(raw.get("kind") or "food").strip().lower()
    if kind not in KINDS:
        kind = "drink" if identity == "unresolved" and "glass" in str(raw.get("unit") or "").lower() else "food"
    unit = normalize_unit(str(raw.get("unit") or "serving")) or "serving"
    serving_basis = str(raw.get("serving_basis") or "").strip().lower()
    if serving_basis not in SERVING_BASES:
        serving_basis = "approximate"
    needs_lookup = identity == "branded"
    estimates = _null_estimates()
    if identity == "generic":
        for key in estimates:
            estimates[key] = _as_number(raw.get(key))
    item = {
        "label": "unresolved" if identity == "unresolved" and normalize_label(label) in {"this", "that", "it"} else label,
        "kind": kind,
        "identity": identity,
        "quantity": _clean_qty(quantity),
        "unit": unit,
        "serving_basis": serving_basis,
        "unresolved": identity == "unresolved",
        "needs_lookup": needs_lookup,
        "assumption": str(raw.get("assumption") or "").strip(),
    }
    item.update(estimates)
    return item


def _scale_factor(quantity, unit: str, serving_description: str) -> float | None:
    number = _as_number(quantity)
    if number is None or number <= 0:
        return None
    normalized = normalize_unit(unit)
    if normalized in COUNT_UNITS:
        return number
    description = serving_description or ""
    if normalized == "g" and re.search(r"100\s*g", description, flags=re.I):
        return number / 100.0
    if normalized == "oz" and re.search(r"\b1\s*oz\b|\bper\s+oz\b", description, flags=re.I):
        return number
    return None


def scale_estimates(quantity, unit: str, per_serving: dict | None, serving_description: str = "") -> dict:
    blank = _null_estimates()
    if not isinstance(per_serving, dict):
        return blank
    factor = _scale_factor(quantity, unit, serving_description)
    if factor is None:
        return blank
    mapping = (
        ("calories", "calories_est"),
        ("protein_g", "protein_g_est"),
        ("carbs_g", "carbs_g_est"),
        ("fat_g", "fat_g_est"),
    )
    scaled = {}
    for source, dest in mapping:
        value = _as_number(per_serving.get(source))
        scaled[dest] = None if value is None else round(value * factor, 1)
    return scaled


def _apply_scaled_estimates(item: dict, per_serving: dict, serving_description: str) -> None:
    scaled = scale_estimates(item.get("quantity"), str(item.get("unit") or ""), per_serving, serving_description)
    item.update(scaled)


def _per_serving_from_lookup(data: dict) -> dict | None:
    calories = _as_number(data.get("calories"))
    protein = _as_number(data.get("protein_g"))
    carbs = _as_number(data.get("carbs_g"))
    fat = _as_number(data.get("fat_g"))
    if calories is None and protein is None and carbs is None and fat is None:
        return None
    return {
        "calories": calories,
        "protein_g": protein,
        "carbs_g": carbs,
        "fat_g": fat,
    }


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

def call_nutrition_model(system: str, user: str, model: str | None = None) -> dict:
    try:
        import ollama  # type: ignore
    except ImportError as exc:
        raise RuntimeError("The 'ollama' package is required for nutrition extract.") from exc
    chosen = model or nutrition_model()
    _log(f"calling model={chosen}")
    response = ollama.chat(
        model=chosen,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        options={"temperature": 0.0, "num_ctx": 4096},
        keep_alive=KEEP_ALIVE,
    )
    raw = response["message"]["content"]
    return _extract_json_obj(raw)


def build_extract_user(spoken_at: str, raw_text: str, memory: dict) -> str:
    parts = [f"Time: {spoken_at}", "Utterance:", raw_text]
    known = format_known_items(memory)
    if known:
        parts.extend(["Known items:", known])
    return "\n".join(parts)


def _walk_urls(node, found: list[str]) -> None:
    if isinstance(node, dict):
        for key in ("url", "uri"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("http") and value not in found:
                found.append(value)
        for value in node.values():
            _walk_urls(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk_urls(value, found)
    elif hasattr(node, "__dict__"):
        _walk_urls(vars(node), found)


def _response_text(response) -> str:
    if isinstance(response, dict):
        text = response.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        chunks: list[str] = []
        _collect_text(response.get("output"), chunks)
        return "\n".join(chunks)
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chunks = []
    _collect_text(getattr(response, "output", None), chunks)
    return "\n".join(chunks)


def _collect_text(node, chunks: list[str]) -> None:
    if isinstance(node, str):
        if node.strip():
            chunks.append(node)
        return
    if isinstance(node, dict):
        for key in ("text", "output_text", "content"):
            if key in node:
                _collect_text(node.get(key), chunks)
        return
    if isinstance(node, (list, tuple)):
        for value in node:
            _collect_text(value, chunks)
        return
    for attr in ("text", "content"):
        if hasattr(node, attr):
            _collect_text(getattr(node, attr), chunks)


def citation_urls(response) -> list[str]:
    found: list[str] = []
    if isinstance(response, dict):
        _walk_urls(response, found)
    else:
        _walk_urls(getattr(response, "output", None), found)
        if hasattr(response, "model_dump"):
            try:
                _walk_urls(response.model_dump(), found)
            except Exception:
                pass
    return found


def _normalize_lookup_payload(data: dict, urls: list[str], query: str) -> dict:
    status = str(data.get("status") or "").strip().lower()
    if status not in {"ok", "ambiguous", "miss"}:
        status = "error"
    source_urls = []
    raw_urls = data.get("source_urls") if isinstance(data.get("source_urls"), list) else []
    for url in list(raw_urls) + list(urls):
        text = str(url or "").strip()
        if text.startswith("http") and text not in source_urls:
            source_urls.append(text)
    if status == "ok" and not source_urls:
        status = "miss"
    per_serving = _per_serving_from_lookup(data) if status == "ok" else None
    if status != "ok":
        per_serving = None
    return {
        "status": status,
        "query": query,
        "matched_name": str(data.get("matched_name") or "").strip() if status == "ok" else "",
        "serving_description": str(data.get("serving_description") or "").strip() if status == "ok" else "",
        "source_urls": source_urls if status == "ok" else [],
        "per_serving": per_serving,
    }


def lookup_product(label: str, *, extra_context: str = "", model: str | None = None) -> dict:
    """One web_search call for a product label. Never sends the journal note."""
    query = f"{label} nutrition facts"
    user = NUTRITION_LOOKUP_PROMPT.format(label=label)
    if extra_context.strip():
        user += "\nContext:\n" + extra_context.strip()
        query = f"{label} nutrition facts\n{extra_context.strip()}"
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "error",
            "query": query,
            "matched_name": "",
            "serving_description": "",
            "source_urls": [],
            "per_serving": None,
            "error": "OPENAI_API_KEY is not set",
        }
    try:
        import openai  # type: ignore
    except ImportError as exc:
        return {
            "status": "error",
            "query": query,
            "matched_name": "",
            "serving_description": "",
            "source_urls": [],
            "per_serving": None,
            "error": str(exc),
        }
    try:
        client = openai.OpenAI(api_key=api_key, timeout=LOOKUP_TIMEOUT_SEC)
        response = client.responses.create(
            model=model or lookup_model(),
            tools=[{"type": "web_search"}],
            input=user,
        )
        parsed = _extract_json_obj(_response_text(response))
        urls = citation_urls(response)
    except Exception as exc:
        return {
            "status": "error",
            "query": query,
            "matched_name": "",
            "serving_description": "",
            "source_urls": [],
            "per_serving": None,
            "error": str(exc),
        }
    result = _normalize_lookup_payload(parsed, urls, query)
    return result


def recent_journal_paths(journal_dir: Path, limit: int = JOURNAL_SNIPPET_DAYS) -> list[Path]:
    if not journal_dir or not journal_dir.is_dir():
        return []
    notes = [path for path in journal_dir.glob("*.md") if _DATE_NOTE_RE.match(path.name)]
    notes.sort(key=lambda path: path.name, reverse=True)
    return notes[:limit]


def snippets_for_label(journal_dir: Path | None, label: str, memory: dict) -> list[str]:
    found: list[str] = []
    key = normalize_label(label)
    record = {}
    labels = memory.get("labels") if isinstance(memory.get("labels"), dict) else {}
    if key in labels and isinstance(labels[key], dict):
        record = labels[key]
    for context in record.get("contexts") or []:
        text = str(context or "").strip()
        if text and text not in found:
            found.append(text)
        if len(found) >= JOURNAL_SNIPPET_CAP:
            return found
    if not journal_dir or not label.strip():
        return found
    pattern = re.compile(rf"\b{re.escape(label.strip())}\b", re.IGNORECASE)
    for path in recent_journal_paths(journal_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            plain = re.sub(r"\s+", " ", line.lstrip("> ").strip())
            if not plain or not pattern.search(plain):
                continue
            if plain not in found:
                found.append(plain)
            if len(found) >= JOURNAL_SNIPPET_CAP:
                return found
    return found


def _downgrade_ok_without_url(result: dict) -> dict:
    if result.get("status") == "ok" and not list(result.get("source_urls") or []):
        downgraded = dict(result)
        downgraded["status"] = "miss"
        downgraded["matched_name"] = ""
        downgraded["serving_description"] = ""
        downgraded["source_urls"] = []
        downgraded["per_serving"] = None
        return downgraded
    return result


def _lookup_view(result: dict, *, status: str | None = None) -> dict:
    return {
        "status": status or result.get("status") or "error",
        "query": result.get("query") or "",
        "matched_name": result.get("matched_name") or "",
        "serving_description": result.get("serving_description") or "",
        "source_urls": list(result.get("source_urls") or []),
        "per_serving": result.get("per_serving"),
    }


def _apply_lookup_result(item: dict, result: dict, *, status: str | None = None) -> None:
    view = _lookup_view(result, status=status)
    item["lookup"] = view
    item.update(_null_estimates())
    if view["status"] in {"ok", "cached"} and isinstance(view.get("per_serving"), dict):
        _apply_scaled_estimates(item, view["per_serving"], view.get("serving_description") or "")


def resolve_branded_item(
    item: dict,
    cache: dict,
    memory: dict,
    journal_dir: Path | None,
    *,
    dry_run: bool,
    model: str | None = None,
) -> None:
    if item.get("identity") != "branded":
        return
    key = normalize_label(str(item.get("label") or ""))
    labels = cache.setdefault("labels", {})
    cached = labels.get(key) if key else None
    if isinstance(cached, dict) and cached.get("status") in {"ok", "miss", "ambiguous"}:
        status = "cached" if cached.get("status") == "ok" else cached.get("status")
        _apply_lookup_result(item, cached, status=status)
        return
    if dry_run:
        item["lookup"] = {
            "status": "skipped",
            "query": f"{item.get('label')} nutrition facts",
            "matched_name": "",
            "serving_description": "",
            "source_urls": [],
            "per_serving": None,
        }
        item.update(_null_estimates())
        return
    result = lookup_product(str(item.get("label") or ""), model=model)
    if result.get("status") in {"miss", "ambiguous"}:
        snippets = snippets_for_label(journal_dir, str(item.get("label") or ""), memory)
        if snippets:
            result = lookup_product(
                str(item.get("label") or ""),
                extra_context="\n".join(snippets),
                model=model,
            )
    result = _downgrade_ok_without_url(result)
    if result.get("status") != "error" and key:
        labels[key] = {
            "status": result.get("status"),
            "query": result.get("query") or "",
            "matched_name": result.get("matched_name") or "",
            "serving_description": result.get("serving_description") or "",
            "source_urls": list(result.get("source_urls") or []),
            "per_serving": result.get("per_serving"),
            "fetched_at": _now_iso(),
        }
    _apply_lookup_result(item, result)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def _process_callout(
    callout: dict,
    *,
    source_path: Path,
    source_date: str,
    state_dir: Path,
    memory: dict,
    cache: dict,
    dry_run: bool,
    journal_dir: Path | None,
    model: str | None,
) -> tuple[bool, dict | None]:
    """Return (wrote_or_would_write_intake, row). Raises if the extract model fails."""
    user = build_extract_user(callout["spoken_at"], callout["raw_text"], memory)
    parsed = validate_extract(call_nutrition_model(NUTRITION_LEDGER_PROMPT, user, model=model))
    apply_memory_priors(parsed["items"], memory)
    for item in parsed["items"]:
        resolve_branded_item(
            item, cache, memory, journal_dir, dry_run=dry_run, model=model,
        )
    key = span_key(source_path, callout["spoken_at"], callout["raw_text"])
    row = {
        "idempotency_key": key,
        "logged_at": _now_iso(),
        "source_path": str(source_path),
        "source_date": source_date,
        "spoken_at": callout["spoken_at"],
        "raw_text": callout["raw_text"],
        "is_intake": parsed["is_intake"],
        "meal_slot": parsed["meal_slot"],
        "items": parsed["items"],
        "notes": parsed["notes"],
    }
    if dry_run:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return parsed["is_intake"], row
    if parsed["is_intake"]:
        _append_jsonl(ledger_path(state_dir), row)
        update_memory_from_row(memory, row)
        save_memory(state_dir, memory)
        save_cache(state_dir, cache)
    append_processed(state_dir, {
        "idempotency_key": key,
        "is_intake": parsed["is_intake"],
        "source_path": str(source_path),
        "spoken_at": callout["spoken_at"],
    })
    return parsed["is_intake"], row


def retry_error_lookups(
    state_dir: Path,
    journal_dir: Path | None,
    *,
    limit: int = ERROR_LOOKUP_RETRY_CAP,
    model: str | None = None,
) -> int:
    """Re-resolve branded items whose last lookup status is error. Caps the run."""
    rows = load_ledger_rows(state_dir)
    if not rows:
        return 0
    cache = load_cache(state_dir)
    memory = load_memory(state_dir)
    fixed = 0
    changed = False
    for row in rows:
        for item in row.get("items") or []:
            if fixed >= limit:
                break
            lookup = item.get("lookup") if isinstance(item.get("lookup"), dict) else {}
            if lookup.get("status") != "error":
                continue
            item["identity"] = "branded"
            item["needs_lookup"] = True
            resolve_branded_item(item, cache, memory, journal_dir, dry_run=False, model=model)
            fixed += 1
            changed = True
            _store_resolved_product(memory, item)
        if fixed >= limit:
            break
    if changed:
        rewrite_ledger_rows(state_dir, rows)
        save_cache(state_dir, cache)
        save_memory(state_dir, memory)
    return fixed


def run_nutrition_stage(
    note_text: str,
    source_path: Path,
    source_date: str,
    state_dir: Path,
    dry_run: bool = False,
    journal_dir: Path | None = None,
    model: str | None = None,
) -> int:
    """Parse new voice callouts. Failures stay inside this function.

    Returns the number of intake rows written, or that dry-run would write.
    """
    journal_dir = journal_dir or Path(source_path).parent
    chosen_model = os.getenv("INTENT_NUTRITION_MODEL", "").strip() or model or nutrition_model()
    logged = 0
    if not dry_run:
        try:
            retry_error_lookups(state_dir, journal_dir, model=chosen_model)
        except Exception as exc:
            _log(f"lookup retry failed: {exc}")
    try:
        callouts = extract_voice_callouts(note_text)
    except Exception as exc:
        _log(f"callout parse failed: {exc}")
        return 0
    processed = load_processed_keys(state_dir)
    memory = load_memory(state_dir)
    cache = load_cache(state_dir)
    for callout in callouts:
        key = span_key(source_path, callout["spoken_at"], callout["raw_text"])
        if key in processed:
            continue
        try:
            wrote, _row = _process_callout(
                callout,
                source_path=source_path,
                source_date=source_date,
                state_dir=state_dir,
                memory=memory,
                cache=cache,
                dry_run=dry_run,
                journal_dir=journal_dir,
                model=chosen_model,
            )
        except Exception as exc:
            _log(f"extract failed for {callout['spoken_at']}: {exc}")
            continue
        if wrote:
            logged += 1
        if not dry_run:
            processed.add(key)
    return logged


# ---------------------------------------------------------------------------
# Day summary
# ---------------------------------------------------------------------------

def flatten_day_items(rows: list[dict], source_date: str) -> list[dict]:
    matched = [
        row for row in rows
        if str(row.get("source_date") or "") == source_date and row.get("is_intake")
    ]
    matched.sort(key=lambda row: (str(row.get("spoken_at") or ""), str(row.get("logged_at") or "")))
    items: list[dict] = []
    for row in matched:
        for item in row.get("items") or []:
            if isinstance(item, dict):
                items.append(item)
    return items


def format_day_summary(rows: list[dict], source_date: str) -> tuple[str, str] | None:
    """Title is the calorie total. Body is one line per item. None when empty."""
    items = flatten_day_items(rows, source_date)
    if not items:
        return None
    total = 0.0
    omitted = 0
    lines: list[str] = []
    for item in items:
        label = str(item.get("label") or "").strip() or "item"
        unit = display_unit(item.get("quantity"), str(item.get("unit") or "serving"))
        quantity = item.get("quantity")
        lines.append(f"{fmt_qty(quantity)} {unit} {label}")
        calories = _as_number(item.get("calories_est"))
        if calories is None:
            omitted += 1
        else:
            total += calories
    title = f"{int(round(total))} kcal"
    footer = f"Total omits {omitted} items with no estimate" if omitted else ""
    body = _fit_summary_body(lines, footer)
    return title, body


def _fit_summary_body(lines: list[str], footer: str) -> str:
    def assemble(kept: list[str], hidden: int) -> str:
        parts = list(kept)
        if hidden:
            parts.append(f"and {hidden} more")
        if footer:
            parts.append(footer)
        return "\n".join(parts)

    full = assemble(lines, 0)
    if len(full) <= PUSHOVER_MESSAGE_LIMIT:
        return full
    for count in range(len(lines), -1, -1):
        hidden = len(lines) - count
        candidate = assemble(lines[:count], hidden)
        if len(candidate) <= PUSHOVER_MESSAGE_LIMIT:
            return candidate
    return assemble([], len(lines))[:PUSHOVER_MESSAGE_LIMIT]


def publish_nutrition_summary(title: str, message: str) -> tuple[int, str]:
    app_token = os.getenv("SCRIBE_PUSHOVER_APP_TOKEN", "").strip() or os.getenv("PUSHOVER_TOKEN", "").strip()
    user_key = os.getenv("SCRIBE_PUSHOVER_USER_KEY", "").strip() or os.getenv("PUSHOVER_KEY", "").strip()
    if not app_token:
        raise ValueError("SCRIBE_PUSHOVER_APP_TOKEN is required for Pushover delivery.")
    if not user_key:
        raise ValueError("SCRIBE_PUSHOVER_USER_KEY is required for Pushover delivery.")
    server = os.getenv("SCRIBE_PUSHOVER_SERVER", DEFAULT_PUSHOVER_SERVER).strip().rstrip("/") or DEFAULT_PUSHOVER_SERVER
    form = {
        "token": app_token,
        "user": user_key,
        "title": title[:PUSHOVER_TITLE_LIMIT],
        "message": message[:PUSHOVER_MESSAGE_LIMIT],
        "priority": "0",
    }
    device = os.getenv("SCRIBE_PUSHOVER_DEVICE", "").strip()
    if device:
        form["device"] = device
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(f"{server}/1/messages.json", data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with contextlib.closing(urllib.request.urlopen(request, timeout=15)) as response:
        status = getattr(response, "status", 200)
        body = response.read().decode("utf-8", errors="replace")
        return status, body


def load_summary_state(state_dir: Path) -> dict:
    return _read_json(summary_state_path(state_dir), {})


def save_summary_state(state_dir: Path, state: dict) -> None:
    _atomic_write_json(summary_state_path(state_dir), state)


# ---------------------------------------------------------------------------
# Reinforcement
# ---------------------------------------------------------------------------

def _labels_in_rows(rows: list[dict]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        for item in row.get("items") or []:
            if not isinstance(item, dict) or item.get("unresolved"):
                continue
            key = normalize_label(str(item.get("label") or ""))
            if key and key not in seen:
                seen.append(key)
    return seen


def apply_reinforcement(memory: dict, rows: list[dict], corrections: list[dict], source_date: str) -> None:
    labels = memory.setdefault("labels", {})
    corrected: dict[str, dict] = {}
    for correction in corrections:
        if not isinstance(correction, dict):
            continue
        key = normalize_label(str(correction.get("label") or ""))
        if key:
            corrected[key] = correction
    for key in _labels_in_rows(rows):
        record = labels.get(key)
        if not isinstance(record, dict):
            record = _blank_memory_record(key)
            labels[key] = record
        if key in corrected:
            correction = corrected[key]
            record["failure_count"] = int(record.get("failure_count") or 0) + 1
            record["reinforcement"] = _clamp_reinforcement(float(record.get("reinforcement") or 0) - REINFORCE_DOWN)
            quantity = _as_number(correction.get("quantity"))
            unit = normalize_unit(str(correction.get("unit") or ""))
            if quantity is not None and quantity > 0 and unit:
                record["typical"] = {"quantity": _clean_qty(quantity), "unit": unit}
                amounts = record.setdefault("recent_amounts", [])
                amounts.append({"quantity": _clean_qty(quantity), "unit": unit})
                del amounts[:-MEMORY_AMOUNT_CAP]
            resolved = str(correction.get("resolved_name") or "").strip()
            if resolved:
                record["resolved_name"] = resolved
        else:
            record["success_count"] = int(record.get("success_count") or 0) + 1
            record["reinforcement"] = _clamp_reinforcement(float(record.get("reinforcement") or 0) + REINFORCE_UP)
            record["last_success_date"] = source_date
    memory["last_reinforced_date"] = source_date


def reinforce_closed_day(
    state_dir: Path,
    journal_dir: Path | None,
    source_date: str,
    *,
    model: str | None = None,
) -> bool:
    """Score one closed journal date. Returns False when the model is not called."""
    memory = load_memory(state_dir)
    if str(memory.get("last_reinforced_date") or "") == source_date:
        return False
    rows = [
        row for row in load_ledger_rows(state_dir)
        if str(row.get("source_date") or "") == source_date and row.get("is_intake")
    ]
    if not rows:
        memory["last_reinforced_date"] = source_date
        save_memory(state_dir, memory)
        return False
    note_text = ""
    if journal_dir is not None:
        note_path = journal_dir / f"{source_date}.md"
        if note_path.is_file():
            try:
                note_text = note_path.read_text(encoding="utf-8")
            except OSError:
                note_text = ""
    logged_lines = []
    summary = format_day_summary(rows, source_date)
    if summary:
        logged_lines.append(summary[0])
        logged_lines.append(summary[1])
    user = "Logged items:\n" + "\n".join(logged_lines) + "\n\nJournal note:\n" + note_text
    parsed = call_nutrition_model(CORRECTION_PROMPT, user, model=model)
    corrections = parsed.get("corrections") if isinstance(parsed.get("corrections"), list) else []
    apply_reinforcement(memory, rows, corrections, source_date)
    save_memory(state_dir, memory)
    return True


def run_day_summary(
    *,
    state_dir: Path,
    journal_dir: Path | None,
    source_date: str,
    dry_run: bool = False,
    model: str | None = None,
) -> int:
    """Reinforce the previous day, then send today's summary once."""
    try:
        current = date.fromisoformat(source_date)
        yesterday = (current - timedelta(days=1)).isoformat()
    except ValueError:
        _log(f"bad summary date: {source_date}")
        return 1
    reinforced = False
    if not dry_run:
        try:
            reinforced = reinforce_closed_day(state_dir, journal_dir, yesterday, model=model)
        except Exception as exc:
            _log(f"reinforcement failed: {exc}")
    rows = load_ledger_rows(state_dir)
    formatted = format_day_summary(rows, source_date)
    if formatted is None:
        _log(f"no intake for {source_date}")
        _write_summary_payload(source_date, sent=False, items=0, reinforced=reinforced)
        return 0
    title, body = formatted
    state = load_summary_state(state_dir)
    if str(state.get("last_sent_date") or "") == source_date:
        _log(f"summary already sent for {source_date}")
        _write_summary_payload(source_date, sent=False, items=body.count("\n") + 1, reinforced=reinforced, skipped="already_sent")
        return 0
    if dry_run:
        print(title)
        print()
        print(body)
        return 0
    try:
        status, _response = publish_nutrition_summary(title, body)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        _log(f"pushover failed: {exc}")
        _write_summary_payload(source_date, sent=False, items=0, reinforced=reinforced, error=str(exc))
        return 1
    save_summary_state(state_dir, {"last_sent_date": source_date, "sent_at": _now_iso(), "status": status})
    _log(f"sent summary for {source_date} status={status}")
    _write_summary_payload(source_date, sent=True, items=flatten_day_items(rows, source_date).__len__(), reinforced=reinforced)
    return 0


def _write_summary_payload(source_date: str, *, sent: bool, items: int, reinforced: bool, **extra) -> None:
    try:
        from journal_linker_telemetry import maybe_write_job_payload
    except Exception:
        return
    maybe_write_job_payload(
        source_date=source_date,
        nutrition_summary_sent=sent,
        nutrition_summary_items=items,
        nutrition_reinforced=reinforced,
        **extra,
    )


def _bootstrap() -> None:
    try:
        from journal_linker_env import bootstrap_journal_linker_env
    except Exception:
        return
    bootstrap_journal_linker_env(repo_root=REPO_ROOT)


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    parser = argparse.ArgumentParser(description="Nutrition ledger day summary")
    parser.add_argument("--summary", action="store_true", help="Send the end-of-day nutrition summary")
    parser.add_argument("--dry-run", action="store_true", help="Print the summary and skip Pushover")
    parser.add_argument("--date", default="", help="Journal date YYYY-MM-DD (default: today, local)")
    parser.add_argument("--state-dir", default="", help="Override INTENT_STATE_DIR")
    parser.add_argument("--journal-dir", default="", help="Override SCRIBE_JOURNAL_DIR")
    args = parser.parse_args(argv)
    if not args.summary:
        parser.error("--summary is required")
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.journal_dir:
        journal_dir = Path(args.journal_dir).expanduser()
    else:
        journal_dir = default_journal_dir()
    source_date = args.date.strip() or date.today().isoformat()
    return run_day_summary(
        state_dir=state_dir,
        journal_dir=journal_dir,
        source_date=source_date,
        dry_run=args.dry_run,
        model=nutrition_model(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
