#!/usr/bin/env python3
"""process_intents.py — Intent-capture pipeline for journalLinker.

Reads a daily note, gates actionable intent via a local Ollama model,
packages context, calls an OpenAI model for structured routing output, then delivers
to Pushover / Obsidian cortex/ / JSON digest queue.

Usage:
    python3 scripts/process_intents.py --file /path/to/2026-04-16.md
    python3 scripts/process_intents.py --file note.md --dry-run
    python3 scripts/process_intents.py --retry
    python3 scripts/process_intents.py --reset-ledger
    python3 scripts/process_intents.py --prune-ledger --older-than 30d

Exit codes (stable contract for shell wrappers):
    0   success or clean skip (has_intent=false, or already delivered)
    10  permanent failure (bad credentials, schema error, corrupt note)
    20  transient gate failure (Ollama down/timeout)
    30  transient routing model failure (rate limit, network timeout)
    40  transient delivery failure (Pushover HTTP error)
    50  partial success (some sinks succeeded, some failed — retry delivery only)

Env vars (from .env or environment):
    SCRIBE_JOURNAL_DIR           journal directory (required)
    INTENT_GATE_MODEL            Ollama gate model (default: phi4:14b)
    INTENT_GATE_STYLE            auto|phi4|qwen25 (default: auto)
    INTENT_ROUTING_MODEL         OpenAI model ID (default: gpt-4o-mini)
    INTENT_CORTEX_DIR            Obsidian cortex write target (default: <journal_dir>/cortex)
    INTENT_STATE_DIR             local state directory
                                 (default: ~/.local/state/journal-linker/intents)
    INTENT_CLAUDE_IN_FLIGHT_TTL  seconds before in-flight entry is treated as stale
                                 (default: 300)
    INTENT_ENRICHMENT_MODE       off|llmlib|mcp (default: llmlib; llmlib uses MCP)
    LLMLIBRARIAN_MCP_HOST        llmLibrarian MCP host (default: 127.0.0.1)
    LLMLIBRARIAN_MCP_PORT        llmLibrarian MCP port (default: 8765)
    LLMLIBRARIAN_MCP_PATH        llmLibrarian MCP path (default: /mcp)
    LLMLIBRARIAN_MCP_TIMEOUT     health/MCP timeout seconds (default: 2)
    LLMLIBRARIAN_MCP_N_RESULTS   retrieval count for enrichment (default: 5)
    LLMLIBRARIAN_MCP_SILO        optional silo slug/name; unset uses unified search
    INTENT_MAX_INTENTS_PER_NOTE  max intents extracted per note (default: 5)
    INTENT_FEEDBACK_DELAY_TODAY  seconds before feedback prompt fires for today/immediate urgency (default: 21600 = 6h)
    INTENT_FEEDBACK_DELAY_SOON   seconds before feedback prompt fires for soon urgency (default: 86400 = 24h)
    INTENT_PUSHOVER_URGENCIES    comma-separated urgencies that may trigger Pushover (default: immediate,today,soon).
                                 Set e.g. immediate,today to skip Pushover for urgency=soon.
    OPENAI_API_KEY               OpenAI API key (required for routing calls)
    SCRIBE_PUSHOVER_APP_TOKEN    Pushover app token (or PUSHOVER_TOKEN)
    SCRIBE_PUSHOVER_USER_KEY     Pushover user key  (or PUSHOVER_KEY)
    SCRIBE_PUSHOVER_DEVICE       optional device name
    SCRIBE_PUSHOVER_PRIORITY     Pushover priority, default 0
    SCRIBE_PUSHOVER_SERVER       Pushover API base URL
    TELEGRAM_BOT_TOKEN           Telegram bot token (for feedback sender)
    TELEGRAM_CHAT_ID             Telegram chat ID (for feedback sender)
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time as _time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_PERMANENT = 10
EXIT_GATE_TRANSIENT = 20
EXIT_CLAUDE_TRANSIENT = 30
EXIT_DELIVERY_TRANSIENT = 40
EXIT_PARTIAL = 50

DEFAULT_GATE_MODEL = "phi4:14b"
DEFAULT_GATE_STYLE = "auto"
DEFAULT_ROUTING_MODEL = "gpt-4o-mini"
DEFAULT_IN_FLIGHT_TTL = 300  # seconds
DEFAULT_MAX_INTENTS = 5
DEFAULT_PUSHOVER_SERVER = "https://api.pushover.net"
DEFAULT_PUSHOVER_PRIORITY = "0"
DEFAULT_ENRICHMENT_MODE = "llmlib"
DEFAULT_LLMLIBRARIAN_MCP_HOST = "127.0.0.1"
DEFAULT_LLMLIBRARIAN_MCP_PORT = "8765"
DEFAULT_LLMLIBRARIAN_MCP_PATH = "/mcp"
DEFAULT_LLMLIBRARIAN_MCP_TIMEOUT = 2.0
DEFAULT_LLMLIBRARIAN_MCP_N_RESULTS = 5
INTENT_IDEMPOTENCY_VERSION = "3"
KEEP_ALIVE = "5m"

LEDGER_FILENAME = "intent_delivery_ledger.jsonl"
RUN_HISTORY_FILENAME = "intent_run_history.jsonl"
RETRY_QUEUE_FILENAME = "intent_retry_queue.jsonl"
DIGEST_QUEUE_FILENAME = "intent_digest_queue.jsonl"
FEEDBACK_QUEUE_FILENAME = "intent_feedback_queue.jsonl"
ACTION_QUEUE_FILENAME = "intent_action_queue.jsonl"
CONSENT_FILENAME = "intent_consent.json"


# ---------------------------------------------------------------------------
# Environment loader (mirrors existing scripts)
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


def bootstrap_env(repo_root: Path) -> None:
    """Load shared JournalLinker environment config before resolving defaults."""
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    try:
        from journal_linker_env import bootstrap_journal_linker_env
    except Exception:
        load_local_env(repo_root / ".env")
        return
    bootstrap_journal_linker_env(repo_root=repo_root)


# ---------------------------------------------------------------------------
# Tiny helpers (inlined to keep this script self-contained)
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------

def get_state_dir() -> Path:
    raw = os.getenv("INTENT_STATE_DIR", "").strip()
    if raw:
        d = Path(raw).expanduser()
    else:
        d = Path.home() / ".local" / "state" / "journal-linker" / "intents"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Gate: prompt templates + Ollama call
# ---------------------------------------------------------------------------

def _infer_gate_style(model_name: str) -> str:
    lname = model_name.lower()
    if "qwen" in lname:
        return "qwen25"
    return "phi4"


def _build_gate_prompt_phi4(text_excerpt: str) -> str:
    return (
        "You are a concise intent extractor. "
        "Read the following journal excerpt and extract ALL actionable intents: "
        "concrete tasks, commitments, reminders, or plans the author intends to act on. "
        "Do not extract items that the author says are already handled, already in another reminder system, "
        "or mentioned only as praise/retrospective narration.\n\n"
        "Excerpt:\n"
        "---\n"
        f"{text_excerpt}\n"
        "---\n\n"
        "Respond with JSON only, no extra text. Return an empty list if nothing actionable is found.\n"
        "Use category for note organization and intent_class for downstream behavior.\n"
        "Valid intent_class values: task_intent, purchase_intent, latent_interest.\n"
        '{"intents": [{"intent_raw": "<short phrase>", "category": "<task|reminder|commitment|plan>", "intent_class": "<task_intent|purchase_intent|latent_interest>"}]}'
    )


def _build_gate_prompt_qwen25(text_excerpt: str) -> str:
    return (
        "<|im_start|>system\n"
        "You are an intent extractor for personal journal notes. "
        "Extract ALL actionable intents (tasks, reminders, commitments, plans). "
        "Do not extract already-completed items, reminders the author says another system already handles, "
        "or retrospective praise/observations with no remaining action. "
        "Also classify the downstream intent class. "
        "Output only a JSON object — no markdown fences, no extra text.\n"
        'Schema: {"intents": [{"intent_raw": string, "category": string, "intent_class": string}]}\n'
        "Valid categories: task, reminder, commitment, plan\n"
        "Valid intent_class: task_intent, purchase_intent, latent_interest\n"
        "Return an empty intents list if nothing actionable is found.\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        "Extract all actionable intents from this journal excerpt:\n\n"
        f"{text_excerpt}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def build_gate_prompt(text_excerpt: str, style: str) -> str:
    if style == "qwen25":
        return _build_gate_prompt_qwen25(text_excerpt)
    return _build_gate_prompt_phi4(text_excerpt)


def resolve_gate_style(model_name: str) -> str:
    raw = os.getenv("INTENT_GATE_STYLE", DEFAULT_GATE_STYLE).strip().lower()
    if raw in ("phi4", "qwen25"):
        return raw
    return _infer_gate_style(model_name)


def _normalize_intent_text(text: str) -> str:
    lowered = str(text or "").strip().lower()
    lowered = re.sub(r"^did you\s+", "", lowered)
    lowered = re.sub(r"[^\w\s]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _paragraph_chunks(note_text: str, max_chars: int = 2000, top_n: int = 3) -> list[str]:
    body = re.sub(r"^---\n.*?\n---\n", "", note_text, flags=re.DOTALL).strip()
    if not body:
        return []
    raw_parts = [p.strip() for p in re.split(r"\n\s*\n+", body) if p.strip()]
    parts: list[str] = []
    for part in raw_parts:
        if len(part) <= max_chars:
            parts.append(part)
            continue
        # Fall back to sentence-ish segmentation for very long paragraphs.
        sents = re.split(r"(?<=[.!?])\s+", part)
        buff = ""
        for sent in sents:
            sent = sent.strip()
            if not sent:
                continue
            candidate = f"{buff} {sent}".strip() if buff else sent
            if len(candidate) <= max_chars:
                buff = candidate
                continue
            if buff:
                parts.append(buff)
            buff = sent[:max_chars].strip()
        if buff:
            parts.append(buff)
    if not parts:
        return []

    cue_re = re.compile(
        r"\b(need to|should|must|remember|remind|buy|order|schedule|call|email|book|plan|tomorrow|today|by\s+\w+)\b",
        re.IGNORECASE,
    )

    scored: list[tuple[int, int, str]] = []
    for idx, part in enumerate(parts):
        score = 0
        score += min(5, len(cue_re.findall(part)))
        score += 2 if any(ch in part for ch in ("[ ]", "TODO", "- [")) else 0
        score += 1 if len(part) > 300 else 0
        scored.append((score, idx, part[:max_chars]))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = [text for _, _, text in scored[:max(1, top_n)]]
    return best


def _dedupe_intents(intents: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in intents:
        intent_raw = str(item.get("intent_raw", "")).strip()
        category = str(item.get("category", "none")).strip().lower()
        norm = _normalize_intent_text(intent_raw)
        if not norm:
            continue
        key = (norm, category)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    max_n = int(os.getenv("INTENT_MAX_INTENTS_PER_NOTE", str(DEFAULT_MAX_INTENTS)))
    return out[:max_n]


def call_gate(note_text: str, model: str, style: str, num_ctx: int = 4096) -> list[dict]:
    """Run the gate model via Ollama. Returns a list of {intent_raw, category} dicts."""
    try:
        import ollama  # type: ignore
    except ImportError:
        raise RuntimeError(
            "The 'ollama' Python package is required for intent gate. "
            "Install it with: pip install ollama"
        )

    chunks = _paragraph_chunks(note_text, max_chars=2000, top_n=3)
    if not chunks:
        chunks = [note_text[:2000].strip()]
    merged: list[dict] = []
    for idx, excerpt in enumerate(chunks):
        prompt = build_gate_prompt(excerpt, style)
        _log("gate", f"calling model={model} style={style} chunk={idx + 1}/{len(chunks)}")
        response = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_ctx": num_ctx},
            keep_alive=KEEP_ALIVE,
        )
        raw_content = response["message"]["content"]
        gate_output = _extract_json_obj(raw_content)
        merged.extend(_parse_gate_output(gate_output))
    return _dedupe_intents(merged)


_VALID_CATEGORIES = {"task", "reminder", "commitment", "plan", "none"}
_VALID_INTENT_CLASSES = {"task_intent", "purchase_intent", "latent_interest", "none"}
_VALID_ACTIONS = {
    "notification",
    "note_capture",
    "digest_capture",
    "future_action_search_enqueue",
}


def _fallback_intent_class(category: str) -> str:
    if category in {"task", "reminder", "commitment", "plan"}:
        return "task_intent"
    return "none"


def _parse_gate_output(data: dict) -> list[dict]:
    """Parse gate model output into a validated list of intent dicts."""
    raw_list = data.get("intents", [])
    if not isinstance(raw_list, list):
        raw_list = []
    max_n = int(os.getenv("INTENT_MAX_INTENTS_PER_NOTE", str(DEFAULT_MAX_INTENTS)))
    valid: list[dict] = []
    for item in raw_list[:max_n]:
        if not isinstance(item, dict):
            continue
        intent_raw = str(item.get("intent_raw", "")).strip()
        category = str(item.get("category", "none")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "none"
        intent_class = str(item.get("intent_class", "")).strip().lower()
        if not intent_class:
            intent_class = _fallback_intent_class(category)
        if intent_class not in _VALID_INTENT_CLASSES:
            intent_class = "none"
        if intent_raw:
            valid.append({
                "intent_raw": intent_raw,
                "category": category,
                "intent_class": intent_class,
            })
    return valid


# ---------------------------------------------------------------------------
# Context extraction from note
# ---------------------------------------------------------------------------

def extract_surrounding_context(note_text: str, intent_raw: str, window_chars: int = 600) -> str:
    """Return text around the first occurrence of intent_raw, or a centered excerpt."""
    if intent_raw and intent_raw in note_text:
        idx = note_text.index(intent_raw)
        start = max(0, idx - window_chars // 2)
        end = min(len(note_text), idx + len(intent_raw) + window_chars // 2)
        return note_text[start:end].strip()
    # Fallback: strip frontmatter and return middle portion
    body = re.sub(r"^---\n.*?\n---\n", "", note_text, flags=re.DOTALL).strip()
    return body[:window_chars].strip()


def infer_journal_timestamp(source_path: Path) -> str:
    """Derive ISO timestamp from note filename (YYYY-MM-DD.md) or mtime."""
    stem = source_path.stem
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
        return f"{stem}T00:00:00"
    mtime = source_path.stat().st_mtime
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def compute_idempotency_key(
    source_path: Path,
    source_date: str,
    intent_raw: str,
    category: str,
    gate_model: str,
    gate_style: str,
    intent_class: str = "task_intent",
) -> str:
    """Return lowercase hex SHA-256 of a fixed-order fingerprint document.

    Keyed on intent content only — stable across note edits (e.g. wikilink
    insertion by Scribe) that don't change the intent itself.
    """
    fingerprint = "\n".join([
        f"intent_idempotency_version={INTENT_IDEMPOTENCY_VERSION}",
        f"source_path={source_path.expanduser().resolve().as_posix()}",
        f"source_date={source_date}",
        f"intent_raw={intent_raw}",
        f"category={category}",
        f"intent_class={intent_class}",
        f"gate_model={gate_model}",
        f"gate_style={gate_style}",
    ])
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def build_envelope(
    source_path: Path,
    journal_timestamp: str,
    source_stat_str: str,
    intent_raw: str,
    category: str,
    enrichment_mode: str,
    intent_class: str = "task_intent",
    note_text: str | None = None,
) -> dict:
    """Build the context envelope for a single intent."""
    text = note_text if note_text is not None else source_path.read_text(encoding="utf-8")
    surrounding = extract_surrounding_context(text, intent_raw)
    envelope: dict = {
        "intent_raw": intent_raw,
        "surrounding_context": surrounding,
        "inferred_category": category,
        "intent_class": intent_class,
        "timestamp": journal_timestamp,
        "source_file": source_path.expanduser().resolve().as_posix(),
        "source_stat": source_stat_str,
        "enrichment_mode": enrichment_mode,
        "prompt_version": "2",
    }
    return envelope


# ---------------------------------------------------------------------------
# llmLibrarian enrichment (best-effort, MCP-over-HTTP)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _llmlibrarian_mcp_base_url() -> str:
    host = os.getenv("LLMLIBRARIAN_MCP_HOST", DEFAULT_LLMLIBRARIAN_MCP_HOST).strip()
    port = os.getenv("LLMLIBRARIAN_MCP_PORT", DEFAULT_LLMLIBRARIAN_MCP_PORT).strip()
    return f"http://{host}:{port}"


def _llmlibrarian_mcp_endpoint() -> str:
    path = os.getenv("LLMLIBRARIAN_MCP_PATH", DEFAULT_LLMLIBRARIAN_MCP_PATH).strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{_llmlibrarian_mcp_base_url()}{path}"


def _llmlibrarian_health_check(timeout: float | None = None) -> tuple[bool, str]:
    """Return (ok, detail) for the llmLibrarian HTTP liveness endpoint."""
    timeout = timeout if timeout is not None else _env_float(
        "LLMLIBRARIAN_MCP_TIMEOUT",
        DEFAULT_LLMLIBRARIAN_MCP_TIMEOUT,
    )
    url = f"{_llmlibrarian_mcp_base_url()}/healthz"
    try:
        request = urllib.request.Request(url, method="GET")
        with contextlib.closing(urllib.request.urlopen(request, timeout=timeout)) as resp:
            status = getattr(resp, "status", resp.getcode())
            if 200 <= int(status) < 300:
                return True, "ok"
            return False, f"healthz returned HTTP {status}"
    except Exception as exc:
        return False, f"healthz failed: {exc}"


def _mcp_tool_payload(tool_result: object) -> dict:
    """Extract the JSON dict payload from an MCP CallToolResult."""
    structured = getattr(tool_result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(tool_result, "content", None)
    if isinstance(content, dict):
        return content
    if content is None:
        return {"error": "MCP response had no content", "chunks": []}

    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if text is None and item.get("type") == "json":
                return item
        else:
            text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return {"error": "MCP response content did not contain a JSON object", "chunks": []}


def _resolve_llmlibrarian_silo(list_silos_payload: dict, configured_silo: str) -> str:
    silos = list_silos_payload.get("silos")
    if not isinstance(silos, list):
        raise ValueError("list_silos response did not include silos[]")
    target = configured_silo.strip().lower()
    for row in silos:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug", "")).strip()
        candidates = {
            slug,
            str(row.get("name", "")).strip(),
            str(row.get("display_name", "")).strip(),
            str(row.get("displayName", "")).strip(),
        }
        if target in {c.lower() for c in candidates if c}:
            return slug or configured_silo
    raise ValueError(f"configured LLMLIBRARIAN_MCP_SILO not found: {configured_silo}")


async def _query_llmlibrarian_mcp_async(query: str) -> dict:
    from mcp import ClientSession  # type: ignore
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore

    timeout = _env_float("LLMLIBRARIAN_MCP_TIMEOUT", DEFAULT_LLMLIBRARIAN_MCP_TIMEOUT)
    n_results = _env_int("LLMLIBRARIAN_MCP_N_RESULTS", DEFAULT_LLMLIBRARIAN_MCP_N_RESULTS)
    configured_silo = os.getenv("LLMLIBRARIAN_MCP_SILO", "").strip()
    headers: dict[str, str] | None = None
    token = os.getenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "").strip()
    if token:
        headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(
        _llmlibrarian_mcp_endpoint(),
        headers=headers,
        timeout=timeout,
        sse_read_timeout=timeout,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            list_result = await session.call_tool("list_silos", {"check_staleness": False})
            list_payload = _mcp_tool_payload(list_result)
            if list_payload.get("error"):
                return {"error": f"list_silos failed: {list_payload.get('error')}", "chunks": []}

            silo = _resolve_llmlibrarian_silo(list_payload, configured_silo) if configured_silo else None
            args: dict = {"query": query, "n_results": n_results}
            if silo:
                args["silo"] = silo
            query_result = await session.call_tool("query_personal_knowledge", args)
            payload = _mcp_tool_payload(query_result)
            if not isinstance(payload.get("chunks", []), list):
                payload["chunks"] = []
            return payload


def query_llmlibrarian_mcp(query: str) -> dict:
    """Query llmLibrarian through MCP HTTP. Returns an error payload on failure."""
    ok, detail = _llmlibrarian_health_check()
    if not ok:
        return {"error": detail, "chunks": []}
    try:
        timeout = _env_float("LLMLIBRARIAN_MCP_TIMEOUT", DEFAULT_LLMLIBRARIAN_MCP_TIMEOUT)
        return asyncio.run(asyncio.wait_for(_query_llmlibrarian_mcp_async(query), timeout=timeout + 3.0))
    except asyncio.TimeoutError:
        return {"error": f"MCP query timed out after {timeout:g}s", "chunks": []}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "chunks": []}


def _chunk_title(chunk: dict) -> str:
    return str(
        chunk.get("title")
        or chunk.get("source")
        or chunk.get("silo")
        or ""
    )


def enrich_envelope(envelope: dict) -> dict:
    """Query llmLibrarian for related hits. Never raises — failure is logged only."""
    intent_raw = envelope.get("intent_raw", "")
    if not intent_raw:
        return envelope
    result = query_llmlibrarian_mcp(str(intent_raw))
    if result.get("error"):
        detail = str(result.get("error"))
        _log("enrich", f"best-effort enrichment failed (continuing): {detail}")
        envelope["_enrichment_error"] = detail
        return envelope

    chunks = result.get("chunks") or []
    hits = [
        {"title": _chunk_title(c), "snippet": str(c.get("text", ""))[:120]}
        for c in chunks[:3]
        if isinstance(c, dict)
    ]
    envelope["related_silo_hits"] = hits
    envelope["recurrence_signal"] = len(chunks) >= 3
    _log("enrich", f"enriched: {len(hits)} hits, recurrence={envelope['recurrence_signal']}")
    return envelope


# ---------------------------------------------------------------------------
# Claude API call + response validation
# ---------------------------------------------------------------------------

ROUTING_SYSTEM_PROMPT = (
    "You are a structured intent router for a personal journal system. "
    "You receive an intent envelope extracted from a daily note and decide how to route it. "
    "Always respond with valid JSON only — no markdown, no prose.\n"
    "The envelope includes inferred_category for organization and intent_class for behavior. "
    "Supported intent_class values are task_intent, purchase_intent, and latent_interest.\n"
    "Routing discipline (avoid noisy real-time notifications):\n"
    "- Use urgency=low and format=digest for nice-to-remember items, vague ideas, speculative or observational thoughts about the future, or anything without a hard deadline or concrete commitment.\n"
    "- Use urgency=soon only when timing matters within days but it is not worth interrupting the user now; prefer format=note or digest unless it is truly time-sensitive.\n"
    "- Reserve urgency=immediate or today for concrete, time-bound, or high-consequence actions (deadlines, appointments, money, people waiting).\n"
    "- Use format=notification only when a short ping is strictly better than a capturable note.\n"
    "- Use action=notification for immediate actions that should interrupt the user.\n"
    "- Use action=future_action_search_enqueue for purchasable/searchable intent, such as buying, comparing, finding, or researching an item or service.\n"
    "- Use action=digest_capture or note_capture for latent or long-horizon interest; do not notify for latent_interest unless there is an explicit time-bound commitment.\n"
    "- For purchase_intent, prefer future_action_search_enqueue or note_capture over notification unless money, deadline, or another person is waiting today.\n"
    "- Set feedback_prompt to empty string when urgency=low.\n"
    "- When urgency is not low, feedback_prompt MUST be a yes/no completion question phrased as 'Did you X?' — never an open-ended question starting with What, How, Why, Which, or Who. The user answers with Done, Skip, or Later buttons, so the question must have a binary yes/no answer.\n"
    "Feedback calibration: if feedback_context is present in the envelope, it contains confirmed and rejected "
    "tap counts from the user's Telegram responses, keyed by intent_class. Use this to calibrate routing:\n"
    "- High rejected count relative to confirmed for this intent_class: the user often dismisses these — "
    "prefer lower urgency (soon or low) and digest_capture over notification.\n"
    "- High confirmed count: the user engages with these — normal routing discipline applies.\n"
    "- When rejected >= 3 and rejected > confirmed for this intent_class, avoid urgency=immediate or today "
    "unless the intent has a clear concrete deadline, appointment, or named person waiting.\n"
    "Required output schema:\n"
    '{"urgency": "immediate|today|soon|low", '
    '"format": "notification|note|digest|draft", '
    '"action": "notification|note_capture|digest_capture|future_action_search_enqueue", '
    '"title": "<short title under 80 chars>", '
    '"body": "<1-2 sentence summary>", '
    '"defer_to": "<ISO date or empty string>", '
    '"feedback_prompt": "<yes/no completion question as \'Did you X?\', or empty string if urgency=low>"}'
)


def _feedback_summary(state_dir: Path) -> dict:
    """Return confirmed/rejected counts per intent_class from Telegram tap history."""
    path = state_dir / "intent_feedback_learning.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    summary: dict[str, dict[str, int]] = {}
    for cls, info in data.get("approved", {}).items():
        if cls == "unknown":
            continue
        summary.setdefault(cls, {"confirmed": 0, "rejected": 0})["confirmed"] = int(info.get("count", 0))
    for cls, info in data.get("suppressed", {}).items():
        if cls == "unknown":
            continue
        summary.setdefault(cls, {"confirmed": 0, "rejected": 0})["rejected"] = int(info.get("count", 0))
    return summary


def _suppression_match(state_dir: Path, intent_class: str, intent_raw: str) -> dict | None:
    path = state_dir / "intent_feedback_learning.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    suppressed = data.get("suppressed", {})
    if not isinstance(suppressed, dict):
        return None
    class_data = suppressed.get(intent_class, {})
    if not isinstance(class_data, dict):
        return None
    patterns = class_data.get("patterns", {})
    if not isinstance(patterns, dict):
        return None
    norm_intent = _normalize_intent_text(intent_raw)
    if not norm_intent:
        return None
    for pattern, meta in patterns.items():
        pattern_norm = _normalize_intent_text(pattern)
        if len(pattern_norm) < 6:
            continue
        if pattern_norm in norm_intent or norm_intent in pattern_norm:
            md = meta if isinstance(meta, dict) else {}
            return {
                "pattern": pattern_norm,
                "count": int(md.get("count", 0)),
                "first_seen": str(md.get("first_seen", "")),
                "last_seen": str(md.get("last_seen", "")),
                "sample_phrase": str(md.get("sample_phrase", "")),
            }
    return None


def call_routing_model(envelope: dict, model: str, dry_run: bool = False) -> dict:
    """Call OpenAI with the context envelope. Returns parsed response dict."""
    if dry_run:
        _log("routing", "dry-run: skipping routing model call")
        return {
            "urgency": "today",
            "format": "note",
            "action": "note_capture",
            "title": f"[dry-run] {envelope.get('intent_raw', 'Intent')[:60]}",
            "body": "[dry-run] No routing model call made.",
            "defer_to": "",
            "feedback_prompt": f"[dry-run] Did you follow through on: {envelope.get('intent_raw', '')[:50]}?",
            "_dry_run": True,
        }

    try:
        import openai  # type: ignore
    except ImportError:
        raise RuntimeError(
            "The 'openai' package is required. Install: pip install openai"
        )

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to .env or the environment."
        )

    client = openai.OpenAI(api_key=api_key)
    user_content = json.dumps(envelope, ensure_ascii=False, sort_keys=True)

    _log("routing", f"calling model={model}")
    completion = client.chat.completions.create(
        model=model,
        max_tokens=512,
        temperature=0,
        messages=[
            {"role": "system", "content": ROUTING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
    )
    raw_text = completion.choices[0].message.content or ""
    finish_reason = completion.choices[0].finish_reason
    _log("routing", f"finish_reason={finish_reason} id={completion.id}")
    parsed = _validate_claude_response(_extract_json_obj(raw_text))
    parsed["_message_id"] = completion.id
    parsed["_stop_reason"] = finish_reason
    return parsed


def _validate_claude_response(data: dict) -> dict:
    valid_urgencies = {"immediate", "today", "soon", "low"}
    valid_formats = {"notification", "note", "digest", "draft"}

    urgency = str(data.get("urgency", "low")).strip().lower()
    if urgency not in valid_urgencies:
        raise ValueError(f"Invalid urgency value: {urgency!r}")

    fmt = str(data.get("format", "digest")).strip().lower()
    if fmt not in valid_formats:
        raise ValueError(f"Invalid format value: {fmt!r}")

    action_raw = str(data.get("action", "")).strip().lower()
    action = action_raw or _legacy_action_for_response({"urgency": urgency, "format": fmt})
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Invalid action value: {action!r}")

    title = str(data.get("title", "")).strip()[:80]
    body = str(data.get("body", "")).strip()
    defer_to = str(data.get("defer_to", "")).strip()
    feedback_prompt = str(data.get("feedback_prompt", "")).strip()

    return {
        "urgency": urgency,
        "format": fmt,
        "action": action,
        "title": title,
        "body": body,
        "defer_to": defer_to,
        "feedback_prompt": feedback_prompt,
    }


# ---------------------------------------------------------------------------
# Delivery: Pushover
# ---------------------------------------------------------------------------

def _get_pushover_token() -> str:
    return (
        os.getenv("SCRIBE_PUSHOVER_APP_TOKEN", "").strip()
        or os.getenv("PUSHOVER_TOKEN", "").strip()
    )


def _get_pushover_key() -> str:
    return (
        os.getenv("SCRIBE_PUSHOVER_USER_KEY", "").strip()
        or os.getenv("PUSHOVER_KEY", "").strip()
    )


def send_pushover(title: str, body: str, urgency: str = "today") -> tuple[int, str]:
    app_token = _get_pushover_token()
    user_key = _get_pushover_key()
    if not app_token:
        raise ValueError("SCRIBE_PUSHOVER_APP_TOKEN is required for Pushover delivery.")
    if not user_key:
        raise ValueError("SCRIBE_PUSHOVER_USER_KEY is required for Pushover delivery.")

    # Map urgency to Pushover priority
    priority_map = {"immediate": "1", "today": "0", "soon": "0", "low": "-1"}
    priority = priority_map.get(urgency, os.getenv("SCRIBE_PUSHOVER_PRIORITY", DEFAULT_PUSHOVER_PRIORITY))

    server = (
        os.getenv("SCRIBE_PUSHOVER_SERVER", DEFAULT_PUSHOVER_SERVER).rstrip("/")
    )
    url = f"{server}/1/messages.json"
    form_data: dict = {
        "token": app_token,
        "user": user_key,
        "title": title[:250],
        "message": body[:1024],
        "priority": priority,
    }
    device = os.getenv("SCRIBE_PUSHOVER_DEVICE", "").strip()
    if device:
        form_data["device"] = device

    data = urllib.parse.urlencode(form_data).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with contextlib.closing(urllib.request.urlopen(request, timeout=15)) as resp:
        status = getattr(resp, "status", 200)
        body_resp = resp.read().decode("utf-8", errors="replace")
        return status, body_resp


def prior_sink_delivered_ok(ledger_entry: dict | None, sink: str) -> bool:
    """True if any past delivery attempt recorded success for this sink."""
    if not ledger_entry:
        return False
    for att in ledger_entry.get("delivery_attempts") or []:
        res = (att.get("results") or {}).get(sink) or {}
        if res.get("ok") is True:
            return True
    return False


def parse_pushover_urgencies_allowed() -> set[str]:
    """Urgencies that may trigger Pushover. Unset env => legacy immediate|today|soon."""
    raw = os.getenv("INTENT_PUSHOVER_URGENCIES", "").strip()
    all_u = frozenset({"immediate", "today", "soon", "low"})
    if not raw:
        return {"immediate", "today", "soon"}
    parsed = {u.strip().lower() for u in raw.split(",") if u.strip()}
    valid = parsed & all_u
    return valid if valid else {"immediate", "today", "soon"}


def resolve_enrichment_mode() -> str:
    raw = os.getenv("INTENT_ENRICHMENT_MODE", DEFAULT_ENRICHMENT_MODE).strip().lower()
    if raw not in {"off", "llmlib", "mcp"}:
        return DEFAULT_ENRICHMENT_MODE
    return raw


def _legacy_action_for_response(response: dict) -> str:
    """Map pre-action routing responses onto the new action vocabulary.

    This preserves the old urgency-driven notification behavior for stored ledger
    rows or mocked tests that do not yet include an explicit action.
    """
    fmt = str(response.get("format", "digest")).strip().lower()
    urgency = str(response.get("urgency", "low")).strip().lower()
    if fmt == "digest" or urgency == "low":
        return "digest_capture"
    if fmt == "draft":
        return "note_capture"
    if fmt == "notification" or urgency in parse_pushover_urgencies_allowed():
        return "notification"
    return "note_capture"


def normalize_action(response: dict) -> str:
    action = str(response.get("action", "")).strip().lower()
    if action in _VALID_ACTIONS:
        return action
    return _legacy_action_for_response(response)


def _consent_path(state_dir: Path) -> Path:
    return state_dir / CONSENT_FILENAME


def load_consent_state(state_dir: Path) -> dict:
    path = _consent_path(state_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_consent_state(state_dir: Path, state: dict) -> None:
    path = _consent_path(state_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_intent_class_consent(state_dir: Path, intent_class: str, allowed: bool = True) -> None:
    state = load_consent_state(state_dir)
    state[intent_class] = {
        "allowed": bool(allowed),
        "updated_at": _now_iso(),
    }
    save_consent_state(state_dir, state)


def intent_class_has_consent(state_dir: Path, intent_class: str) -> bool:
    if intent_class in {"task_intent", "none", ""}:
        return True
    state = load_consent_state(state_dir)
    entry = state.get(intent_class)
    return bool(isinstance(entry, dict) and entry.get("allowed") is True)


def compute_feedback_timing(
    urgency: str,
    action: str,
    intent_class: str,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    if urgency == "low" or action in {"digest_capture", "future_action_search_enqueue"}:
        return {"policy": "do_not_surface_yet", "send_after": ""}
    if intent_class in {"purchase_intent", "latent_interest"} and action != "notification":
        return {"policy": "do_not_surface_yet", "send_after": ""}
    if urgency == "immediate":
        return {"policy": "show_now", "send_after": now.isoformat(timespec="seconds")}
    delay_today = int(os.getenv("INTENT_FEEDBACK_DELAY_TODAY", "21600"))
    delay_soon = int(os.getenv("INTENT_FEEDBACK_DELAY_SOON", "86400"))
    if urgency == "today":
        send_after = now + timedelta(seconds=delay_today)
        return {"policy": "later_today", "send_after": send_after.isoformat(timespec="seconds")}
    send_after = now + timedelta(seconds=delay_soon)
    return {"policy": "tomorrow_or_later", "send_after": send_after.isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# Delivery: Obsidian cortex write
# ---------------------------------------------------------------------------

def write_cortex_note(
    cortex_dir: Path,
    title: str,
    body: str,
    source_file: str,
    timestamp: str,
    category: str,
    claude_idempotency_key: str,
    surrounding_context: str = "",
    defer_to: str = "",
    feedback_prompt: str = "",
    intent_class: str = "task_intent",
    action: str = "note_capture",
    consent_required: bool = False,
) -> Path:
    safe_title = re.sub(r'[<>:"/\\|?*]', "-", title).strip("-").strip() or "Intent"
    date_prefix = timestamp[:10] if len(timestamp) >= 10 else datetime.now().strftime("%Y-%m-%d")
    # Organize into per-category subdirectory
    subdir = cortex_dir / (category or "other")
    subdir.mkdir(parents=True, exist_ok=True)
    note_path = subdir / f"{date_prefix} {safe_title}.md"

    # Source as Obsidian wikilink from timestamp date
    source_link = f"[[{date_prefix}]]"

    # Build frontmatter lines
    fm_lines = [
        "---",
        f'source: "{source_link}"',
        f"category: {category}",
        f"intent_class: {intent_class}",
        f"action: {action}",
        f"status: open",
        f"tags: [intent, {category}]",
        f"created: {timestamp}",
    ]
    if defer_to:
        fm_lines.append(f"defer_to: {defer_to}")
    if feedback_prompt:
        fm_lines.append(f'feedback_prompt: "{feedback_prompt}"')
    if consent_required:
        fm_lines.append("consent_required: true")
    fm_lines.append(f"intent_key: {claude_idempotency_key[:16]}")
    fm_lines.append("---")

    # Build body
    body_lines = ["", f"# {title}", "", body, ""]

    # Append journal excerpt callout if context is available
    if surrounding_context.strip():
        body_lines.append("> [!journal] Source excerpt")
        for line in surrounding_context.strip().splitlines():
            body_lines.append(f"> {line}")
        body_lines.append("")

    content = "\n".join(fm_lines) + "\n" + "\n".join(body_lines)
    note_path.write_text(content, encoding="utf-8")
    _log("cortex", f"wrote {note_path}")
    return note_path


# ---------------------------------------------------------------------------
# Delivery: digest queue append
# ---------------------------------------------------------------------------

def append_digest_queue(state_dir: Path, entry: dict) -> None:
    queue_path = state_dir / DIGEST_QUEUE_FILENAME
    with open(queue_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _log("digest", f"queued entry for {entry.get('title', '?')!r}")


def append_action_queue(state_dir: Path, entry: dict) -> None:
    queue_path = state_dir / ACTION_QUEUE_FILENAME
    with open(queue_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _log("action", f"queued future action for {entry.get('title', '?')!r}")


def append_feedback_queue(state_dir: Path, entry: dict) -> None:
    queue_path = state_dir / FEEDBACK_QUEUE_FILENAME
    with open(queue_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _log("feedback", f"queued check-in for {entry.get('title', '?')!r} send_after={entry.get('send_after','?')}")


# ---------------------------------------------------------------------------
# Delivery router
# ---------------------------------------------------------------------------

def route_delivery(
    claude_response: dict,
    envelope: dict,
    cortex_dir: Path,
    state_dir: Path,
    claude_idempotency_key: str,
    dry_run: bool = False,
    ledger_entry: dict | None = None,
) -> dict:
    """Route claude_response to the appropriate sinks. Returns per-sink results.

    ledger_entry: optional prior ledger row for this idempotency key; used to skip
    sinks that already succeeded (prevents repeat Pushover on watcher re-fire).
    """
    urgency = claude_response["urgency"]
    fmt = claude_response["format"]
    action = normalize_action(claude_response)
    title = claude_response["title"]
    body = claude_response["body"]
    feedback_prompt = claude_response.get("feedback_prompt", "")
    source_file = envelope["source_file"]
    timestamp = envelope["timestamp"]
    category = envelope["inferred_category"]
    intent_class = envelope.get("intent_class", "task_intent")

    results: dict = {"planned_route": [], "results_per_sink": {}, "errors": []}

    pushover_urgencies = parse_pushover_urgencies_allowed()
    # Determine planned route
    has_class_consent = intent_class_has_consent(state_dir, intent_class)
    consent_required = not has_class_consent
    push_via_pushover = action == "notification" and urgency in pushover_urgencies and has_class_consent
    write_to_cortex = True  # always capture to cortex regardless of format/urgency
    append_to_digest = (
        action == "digest_capture"
        or fmt == "digest"
        or urgency == "low"
        or fmt == "draft"
        or consent_required
    )
    enqueue_future_action = action == "future_action_search_enqueue" and has_class_consent

    if push_via_pushover:
        results["planned_route"].append("pushover")
    if write_to_cortex:
        results["planned_route"].append("cortex")
    if append_to_digest:
        results["planned_route"].append("digest")
    if enqueue_future_action:
        results["planned_route"].append("action_queue")
    if consent_required:
        results["results_per_sink"]["consent"] = {
            "ok": False,
            "required": True,
            "intent_class": intent_class,
        }

    _log(
        "router",
        f"route={results['planned_route']} urgency={urgency} format={fmt} "
        f"action={action} intent_class={intent_class}",
    )

    if dry_run:
        _log("router", "dry-run: skipping all sink writes")
        results["results_per_sink"]["dry_run"] = "skipped"
        return results

    # Pushover
    if push_via_pushover:
        if prior_sink_delivered_ok(ledger_entry, "pushover"):
            results["results_per_sink"]["pushover"] = {
                "ok": True,
                "skipped": "already_delivered",
            }
            _log("pushover", "skip: already delivered for this intent key")
        else:
            try:
                status_code, resp_body = send_pushover(title, body, urgency)
                results["results_per_sink"]["pushover"] = {
                    "ok": True,
                    "status_code": status_code,
                    "response": resp_body[:200],
                }
                _log("pushover", f"sent: {status_code}")
            except Exception as exc:
                results["errors"].append({"sink": "pushover", "error": str(exc)})
                results["results_per_sink"]["pushover"] = {"ok": False, "error": str(exc)}
                _log("pushover", f"ERROR: {exc}")

    # Cortex write
    if write_to_cortex:
        if prior_sink_delivered_ok(ledger_entry, "cortex"):
            results["results_per_sink"]["cortex"] = {
                "ok": True,
                "skipped": "already_delivered",
            }
            _log("cortex", "skip: already delivered for this intent key")
        else:
            try:
                note_path = write_cortex_note(
                    cortex_dir, title, body, source_file, timestamp,
                    category, claude_idempotency_key,
                    surrounding_context=envelope.get("surrounding_context", ""),
                    defer_to=claude_response.get("defer_to", ""),
                    feedback_prompt=feedback_prompt,
                    intent_class=intent_class,
                    action=action,
                    consent_required=consent_required,
                )
                results["results_per_sink"]["cortex"] = {"ok": True, "path": str(note_path)}
            except Exception as exc:
                results["errors"].append({"sink": "cortex", "error": str(exc)})
                results["results_per_sink"]["cortex"] = {"ok": False, "error": str(exc)}
                _log("cortex", f"ERROR: {exc}")

    # Digest queue
    if append_to_digest:
        if prior_sink_delivered_ok(ledger_entry, "digest"):
            results["results_per_sink"]["digest"] = {
                "ok": True,
                "skipped": "already_delivered",
            }
            _log("digest", "skip: already queued for this intent key")
        else:
            try:
                queue_entry = {
                    "claude_idempotency_key": claude_idempotency_key,
                    "title": title,
                    "body": body,
                    "urgency": urgency,
                    "format": fmt,
                    "action": action,
                    "source_file": source_file,
                    "timestamp": timestamp,
                    "category": category,
                    "intent_class": intent_class,
                    "queued_at": _now_iso(),
                    "defer_to": claude_response.get("defer_to", ""),
                    "consent_required": consent_required,
                }
                append_digest_queue(state_dir, queue_entry)
                results["results_per_sink"]["digest"] = {"ok": True}
            except Exception as exc:
                results["errors"].append({"sink": "digest", "error": str(exc)})
                results["results_per_sink"]["digest"] = {"ok": False, "error": str(exc)}
                _log("digest", f"ERROR: {exc}")

    # Future action/search queue
    if enqueue_future_action:
        if prior_sink_delivered_ok(ledger_entry, "action_queue"):
            results["results_per_sink"]["action_queue"] = {
                "ok": True,
                "skipped": "already_delivered",
            }
            _log("action", "skip: already queued for this intent key")
        else:
            try:
                queue_entry = {
                    "claude_idempotency_key": claude_idempotency_key,
                    "intent_raw": envelope.get("intent_raw", ""),
                    "title": title,
                    "body": body,
                    "urgency": urgency,
                    "format": fmt,
                    "action": action,
                    "source_file": source_file,
                    "timestamp": timestamp,
                    "category": category,
                    "intent_class": intent_class,
                    "related_silo_hits": envelope.get("related_silo_hits", []),
                    "recurrence_signal": envelope.get("recurrence_signal", False),
                    "queued_at": _now_iso(),
                    "state": "pending",
                }
                append_action_queue(state_dir, queue_entry)
                results["results_per_sink"]["action_queue"] = {"ok": True}
            except Exception as exc:
                results["errors"].append({"sink": "action_queue", "error": str(exc)})
                results["results_per_sink"]["action_queue"] = {"ok": False, "error": str(exc)}
                _log("action", f"ERROR: {exc}")

    # Feedback queue — schedule a Telegram check-in for today/soon/immediate urgency
    if feedback_prompt and urgency != "low":
        if prior_sink_delivered_ok(ledger_entry, "feedback_queue"):
            results["results_per_sink"]["feedback_queue"] = {
                "ok": True,
                "skipped": "already_delivered",
            }
            _log("feedback", "skip: already enqueued for this intent key")
        else:
            try:
                now = datetime.now(timezone.utc)
                timing = compute_feedback_timing(urgency, action, intent_class, now=now)
                send_after = timing.get("send_after", "")
                if not send_after:
                    results["results_per_sink"]["feedback_queue"] = {
                        "ok": True,
                        "skipped": timing["policy"],
                    }
                    _log("feedback", f"skip: timing_policy={timing['policy']}")
                    return results
                fb_entry = {
                    "claude_idempotency_key": claude_idempotency_key,
                    "feedback_prompt": feedback_prompt,
                    "title": title,
                    "urgency": urgency,
                    "format": fmt,
                    "action": action,
                    "category": category,
                    "intent_class": intent_class,
                    "source_file": source_file,
                    "captured_at": _now_iso(),
                    "send_after": send_after,
                    "timing_policy": timing["policy"],
                    "state": "pending",
                    "telegram_message_id": None,
                    "feedback_signal": None,
                    "defer_count": 0,
                    "expires_at": "",
                }
                append_feedback_queue(state_dir, fb_entry)
                results["results_per_sink"]["feedback_queue"] = {"ok": True, "send_after": send_after}
            except Exception as exc:
                results["errors"].append({"sink": "feedback_queue", "error": str(exc)})
                results["results_per_sink"]["feedback_queue"] = {"ok": False, "error": str(exc)}
                _log("feedback", f"ERROR: {exc}")

    return results


# ---------------------------------------------------------------------------
# Delivery ledger
# ---------------------------------------------------------------------------

def _ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_FILENAME


def load_ledger(state_dir: Path) -> dict:
    """Load ledger as {claude_idempotency_key: record_dict}."""
    path = _ledger_path(state_dir)
    ledger: dict = {}
    if not path.exists():
        return ledger
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            key = record.get("claude_idempotency_key")
            if key:
                ledger[key] = record
        except json.JSONDecodeError:
            pass
    return ledger


def save_ledger(state_dir: Path, ledger: dict) -> None:
    """Atomically rewrite the ledger JSONL from the in-memory dict."""
    path = _ledger_path(state_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            for record in ledger.values():
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def upsert_ledger_entry(state_dir: Path, ledger: dict, record: dict) -> None:
    """Update ledger dict and persist atomically."""
    key = record["claude_idempotency_key"]
    ledger[key] = record
    save_ledger(state_dir, ledger)


def reconcile_stale_inflight(state_dir: Path, ledger: dict, ttl_seconds: int) -> int:
    """Mark stale in-flight entries as failed_transient. Returns number reconciled."""
    now = datetime.now(timezone.utc)
    count = 0
    for key, record in ledger.items():
        if record.get("claude_status") != "in_flight":
            continue
        since_raw = record.get("claude_in_flight_since", "")
        if not since_raw:
            continue
        try:
            since_dt = datetime.fromisoformat(since_raw)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age = (now - since_dt).total_seconds()
        if age > ttl_seconds:
            record["claude_status"] = "failed_transient"
            record["claude_stale_reconciled_at"] = _now_iso()
            count += 1
            _log("ledger", f"reconciled stale in-flight key={key[:16]}… age={age:.0f}s")
    if count:
        save_ledger(state_dir, ledger)
    return count


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

def _run_history_path(state_dir: Path) -> Path:
    return state_dir / RUN_HISTORY_FILENAME


def append_run_record(state_dir: Path, record: dict) -> None:
    path = _run_history_path(state_dir)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Retry queue
# ---------------------------------------------------------------------------

def _retry_queue_path(state_dir: Path) -> Path:
    return state_dir / RETRY_QUEUE_FILENAME


def append_retry_queue(state_dir: Path, entry: dict) -> None:
    path = _retry_queue_path(state_dir)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_retry_queue(state_dir: Path) -> list[dict]:
    path = _retry_queue_path(state_dir)
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def clear_retry_queue(state_dir: Path) -> None:
    path = _retry_queue_path(state_dir)
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

class PipelineError(Exception):
    def __init__(self, message: str, exit_code: int, stage: str, kind: str):
        super().__init__(message)
        self.exit_code = exit_code
        self.stage = stage
        self.kind = kind


def _intent_exit_code(delivery_result: dict, dry_run: bool) -> tuple[str, int]:
    """Compute (delivery_status, exit_code) from a route_delivery result."""
    errors = delivery_result.get("errors", [])
    planned = delivery_result.get("planned_route", [])
    results_per_sink = delivery_result.get("results_per_sink", {})
    failed_sinks = [e["sink"] for e in errors]
    ok_sinks = [s for s in planned if results_per_sink.get(s, {}).get("ok")]

    if not planned or dry_run:
        return "succeeded", EXIT_SUCCESS
    if failed_sinks and ok_sinks:
        return "partial", EXIT_PARTIAL
    if failed_sinks and not ok_sinks:
        status = "failed_permanent" if all(
            "permission" in e.get("error", "").lower() or "disk" in e.get("error", "").lower()
            for e in errors
        ) else "failed_transient"
        return status, EXIT_DELIVERY_TRANSIENT
    return "succeeded", EXIT_SUCCESS


def run_intent_pipeline(
    source_path: Path,
    *,
    gate_model: str,
    gate_style: str,
    routing_model: str,
    cortex_dir: Path,
    state_dir: Path,
    enrichment_mode: str,
    in_flight_ttl: int,
    dry_run: bool,
    verbose: bool,
    existing_idempotency_key: str | None = None,
) -> int:
    """Run the full intent pipeline for one note. Returns an exit code."""
    run_id = str(uuid.uuid4())
    created_at = _now_iso()
    _log("intent", f"run_id={run_id} file={source_path.name}")

    # Load and reconcile ledger
    ledger = load_ledger(state_dir)
    reconciled = reconcile_stale_inflight(state_dir, ledger, in_flight_ttl)
    if reconciled:
        _log("intent", f"reconciled {reconciled} stale in-flight ledger entries")

    # Read note
    try:
        note_text = source_path.read_text(encoding="utf-8")
    except Exception as exc:
        _log("intent", f"cannot read note: {exc}")
        append_run_record(state_dir, {
            "run_id": run_id, "created_at": created_at,
            "source_path": str(source_path), "status": "failed_permanent",
            "failure": {"stage": "read", "kind": "permanent", "detail": str(exc)},
        })
        return EXIT_PERMANENT

    # Stat for idempotency
    try:
        st = source_path.stat()
        source_stat_str = f"{st.st_mtime_ns}:{st.st_size}"
    except Exception as exc:
        _log("intent", f"cannot stat note: {exc}")
        return EXIT_PERMANENT

    journal_timestamp = infer_journal_timestamp(source_path)
    source_date = journal_timestamp[:10]  # YYYY-MM-DD

    # ── Gate ──────────────────────────────────────────────────────────────
    gate_run_record: dict = {
        "run_id": run_id,
        "created_at": created_at,
        "source_path": str(source_path),
        "journal_timestamp": journal_timestamp,
        "source_stat": source_stat_str,
        "status": "in_progress",
    }
    append_run_record(state_dir, gate_run_record)

    try:
        intents = call_gate(note_text, gate_model, gate_style)
    except Exception as exc:
        _log("gate", f"ERROR: {exc}")
        if verbose:
            traceback.print_exc(file=sys.stderr)
        gate_run_record["status"] = "failed_transient"
        gate_run_record["failure"] = {"stage": "gate", "kind": "transient", "detail": str(exc)}
        append_run_record(state_dir, gate_run_record)
        return EXIT_GATE_TRANSIENT

    _log("gate", f"found {len(intents)} intent(s)")
    if verbose:
        for item in intents:
            _log("gate", f"  [{item['category']}/{item.get('intent_class', 'task_intent')}] {item['intent_raw']!r}")

    gate_run_record["gate"] = {"model": gate_model, "style": gate_style, "intent_count": len(intents)}

    if not intents:
        _log("intent", "no intents detected — clean skip")
        gate_run_record["status"] = "skipped_no_intent"
        append_run_record(state_dir, gate_run_record)
        return EXIT_SUCCESS

    gate_run_record["status"] = "gate_complete"
    append_run_record(state_dir, gate_run_record)

    # ── Per-intent loop ───────────────────────────────────────────────────
    worst_exit = EXIT_SUCCESS

    for intent_index, intent_item in enumerate(intents):
        intent_raw = intent_item["intent_raw"]
        category = intent_item["category"]
        intent_class = intent_item.get("intent_class", _fallback_intent_class(category))
        claude_idempotency_key = compute_idempotency_key(
            source_path, source_date,
            intent_raw, category,
            gate_model, gate_style,
            intent_class=intent_class,
        )

        _log(
            "intent",
            f"[{intent_index + 1}/{len(intents)}] category={category!r} "
            f"intent_class={intent_class!r} raw={intent_raw!r}",
        )

        # If retrying with a specific key, skip intents that don't match
        if existing_idempotency_key and existing_idempotency_key != claude_idempotency_key:
            continue

        suppression = _suppression_match(state_dir, intent_class, intent_raw)
        if suppression:
            _log(
                "intent",
                f"[{intent_index + 1}/{len(intents)}] suppressed by feedback pattern={suppression['pattern']!r}",
            )
            ledger_entry = ledger.get(claude_idempotency_key, {})
            prior_delivery_attempts = list(ledger_entry.get("delivery_attempts") or [])
            prior_feedback_fields = {
                k: ledger_entry[k]
                for k in ("feedback_signal", "feedback_received_at", "defer_count")
                if k in ledger_entry
            }
            suppressed_entry = {
                "claude_idempotency_key": claude_idempotency_key,
                "source_path": str(source_path),
                "journal_timestamp": journal_timestamp,
                "source_stat": source_stat_str,
                "intent_raw": intent_raw,
                "category": category,
                "intent_class": intent_class,
                "claude_status": "skipped_suppressed",
                "claude_in_flight_since": "",
                "claude_response": {},
                "delivery_status": "suppressed_by_feedback",
                "delivery_attempts": prior_delivery_attempts,
                "latest_run_id": run_id,
                "suppression": suppression,
            }
            suppressed_entry.update(prior_feedback_fields)
            upsert_ledger_entry(state_dir, ledger, suppressed_entry)
            append_run_record(state_dir, {
                "run_id": run_id,
                "intent_index": intent_index,
                "intent_total": len(intents),
                "claude_idempotency_key": claude_idempotency_key,
                "created_at": created_at,
                "source_path": str(source_path),
                "journal_timestamp": journal_timestamp,
                "source_stat": source_stat_str,
                "intent_raw": intent_raw,
                "category": category,
                "intent_class": intent_class,
                "status": "skipped_suppressed",
                "suppression": suppression,
            })
            continue

        envelope = build_envelope(
            source_path, journal_timestamp, source_stat_str,
            intent_raw, category, enrichment_mode,
            intent_class=intent_class,
            note_text=note_text,
        )
        if enrichment_mode in {"llmlib", "mcp"}:
            envelope = enrich_envelope(envelope)
        feedback_ctx = _feedback_summary(state_dir)
        if feedback_ctx:
            envelope["feedback_context"] = feedback_ctx

        _log("intent", f"idempotency_key={claude_idempotency_key[:16]}…")

        ledger_entry = ledger.get(claude_idempotency_key, {})

        # Per-intent run record
        run_record: dict = {
            "run_id": run_id,
            "intent_index": intent_index,
            "intent_total": len(intents),
            "claude_idempotency_key": claude_idempotency_key,
            "created_at": created_at,
            "source_path": str(source_path),
            "journal_timestamp": journal_timestamp,
            "source_stat": source_stat_str,
            "intent_raw": intent_raw,
            "category": category,
            "intent_class": intent_class,
            "status": "in_progress_claude",
            "envelope": {
                "intent_class": intent_class,
                "related_silo_hits": envelope.get("related_silo_hits", []),
                "recurrence_signal": envelope.get("recurrence_signal", False),
                "_enrichment_error": envelope.get("_enrichment_error", ""),
            },
        }

        # ── Claude call (with dedup) ──────────────────────────────────────
        claude_response: dict | None = None

        if ledger_entry.get("claude_status") == "succeeded":
            stored = ledger_entry.get("claude_response", {})
            claude_response = stored.get("parsed_json")
            _log("claude", "reusing stored response from ledger (delivery retry)")
        else:
            append_run_record(state_dir, run_record)
            prior_delivery_attempts = list(ledger_entry.get("delivery_attempts") or [])
            prior_delivery_status = str(ledger_entry.get("delivery_status") or "pending")
            prior_feedback_fields = {
                k: ledger_entry[k]
                for k in (
                    "feedback_signal",
                    "feedback_received_at",
                    "defer_count",
                    "title",
                    "urgency",
                    "format",
                    "action",
                )
                if k in ledger_entry
            }
            ledger_entry = {
                "claude_idempotency_key": claude_idempotency_key,
                "source_path": str(source_path),
                "journal_timestamp": journal_timestamp,
                "source_stat": source_stat_str,
                "intent_raw": intent_raw,
                "category": category,
                "intent_class": intent_class,
                "claude_status": "in_flight",
                "claude_in_flight_since": _now_iso(),
                "claude_response": {},
                "delivery_status": prior_delivery_status,
                "delivery_attempts": prior_delivery_attempts,
                "latest_run_id": run_id,
            }
            ledger_entry.update(prior_feedback_fields)
            upsert_ledger_entry(state_dir, ledger, ledger_entry)

            try:
                claude_response = call_routing_model(envelope, routing_model, dry_run=dry_run)
            except Exception as exc:
                _log("claude", f"ERROR (intent {intent_index + 1}): {exc}")
                if verbose:
                    traceback.print_exc(file=sys.stderr)
                ledger_entry["claude_status"] = "failed"
                ledger_entry["claude_response"] = {"error": str(exc)}
                upsert_ledger_entry(state_dir, ledger, ledger_entry)
                run_record["status"] = "failed_transient"
                run_record["failure"] = {"stage": "claude", "kind": "transient", "detail": str(exc)}
                append_run_record(state_dir, run_record)
                append_retry_queue(state_dir, {
                    "run_id": run_id, "claude_idempotency_key": claude_idempotency_key,
                    "source_path": str(source_path), "stage": "claude",
                    "kind": "transient", "enqueued_at": _now_iso(),
                })
                worst_exit = max(worst_exit, EXIT_CLAUDE_TRANSIENT)
                continue

            run_record["claude"] = {
                "model": routing_model,
                "response_message_id": claude_response.get("_message_id", ""),
                "stop_reason": claude_response.get("_stop_reason", ""),
            }
            ledger_entry["claude_status"] = "succeeded"
            ledger_entry["claude_response"] = {
                "message_id": claude_response.get("_message_id", ""),
                "raw_text": "",
                "parsed_json": {k: v for k, v in claude_response.items() if not k.startswith("_")},
                "error": "",
            }
            upsert_ledger_entry(state_dir, ledger, ledger_entry)

        if claude_response is None:
            _log("intent", f"no Claude response for intent {intent_index + 1} — skipping")
            worst_exit = max(worst_exit, EXIT_PERMANENT)
            continue

        clean_response = {k: v for k, v in claude_response.items() if not k.startswith("_")}
        if verbose:
            _log("claude", f"response: {json.dumps(clean_response)}")

        # ── Delivery ──────────────────────────────────────────────────────
        ledger_entry = ledger.get(claude_idempotency_key, ledger_entry)
        delivery_result = route_delivery(
            clean_response, envelope, cortex_dir, state_dir,
            claude_idempotency_key, dry_run=dry_run, ledger_entry=ledger_entry,
        )

        delivery_status, intent_exit = _intent_exit_code(delivery_result, dry_run)
        errors = delivery_result.get("errors", [])
        planned = delivery_result.get("planned_route", [])
        results_per_sink = delivery_result.get("results_per_sink", {})

        run_record["delivery"] = {
            "planned_route": planned,
            "results_per_sink": results_per_sink,
            "errors": errors,
        }
        run_record["status"] = (
            "success" if intent_exit == EXIT_SUCCESS else
            "partial" if intent_exit == EXIT_PARTIAL else
            "failed_transient"
        )
        append_run_record(state_dir, run_record)

        ledger_entry["delivery_status"] = delivery_status
        ledger_entry["title"] = clean_response.get("title", "")
        ledger_entry["urgency"] = clean_response.get("urgency", "")
        ledger_entry["format"] = clean_response.get("format", "")
        ledger_entry["intent_class"] = intent_class
        ledger_entry["action"] = normalize_action(clean_response)
        ledger_entry["enrichment"] = {
            "mode": enrichment_mode,
            "related_silo_hits": envelope.get("related_silo_hits", []),
            "recurrence_signal": envelope.get("recurrence_signal", False),
            "_enrichment_error": envelope.get("_enrichment_error", ""),
        }
        ledger_entry["delivery_attempts"].append({
            "run_id": run_id,
            "intent_index": intent_index,
            "attempted_at": _now_iso(),
            "results": results_per_sink,
            "errors": errors,
        })
        ledger_entry["latest_run_id"] = run_id
        upsert_ledger_entry(state_dir, ledger, ledger_entry)

        if intent_exit in (EXIT_PARTIAL, EXIT_DELIVERY_TRANSIENT):
            append_retry_queue(state_dir, {
                "run_id": run_id, "claude_idempotency_key": claude_idempotency_key,
                "source_path": str(source_path), "stage": "delivery",
                "kind": "transient", "enqueued_at": _now_iso(),
            })

        worst_exit = max(worst_exit, intent_exit)

    _log("intent", f"done exit_code={worst_exit}")
    return worst_exit


# ---------------------------------------------------------------------------
# Retry mode
# ---------------------------------------------------------------------------

def run_retry(
    *,
    gate_model: str,
    gate_style: str,
    routing_model: str,
    cortex_dir: Path,
    state_dir: Path,
    enrichment_mode: str,
    in_flight_ttl: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Replay pending transient failures from the retry queue."""
    entries = load_retry_queue(state_dir)
    if not entries:
        _log("retry", "no entries in retry queue")
        return EXIT_SUCCESS

    _log("retry", f"{len(entries)} entr{'y' if len(entries)==1 else 'ies'} in retry queue")
    # Deduplicate: only latest entry per idempotency key
    seen: dict[str, dict] = {}
    for entry in entries:
        key = entry.get("claude_idempotency_key", "")
        if key:
            seen[key] = entry

    clear_retry_queue(state_dir)

    worst_exit = EXIT_SUCCESS
    for key, entry in seen.items():
        source_path = Path(entry.get("source_path", "")).expanduser()
        if not source_path.exists():
            _log("retry", f"source gone, skipping key={key[:16]}… path={source_path}")
            continue
        _log("retry", f"replaying key={key[:16]}… stage={entry.get('stage')} source={source_path.name}")
        exit_code = run_intent_pipeline(
            source_path,
            gate_model=gate_model,
            gate_style=gate_style,
            routing_model=routing_model,
            cortex_dir=cortex_dir,
            state_dir=state_dir,
            enrichment_mode=enrichment_mode,
            in_flight_ttl=in_flight_ttl,
            dry_run=dry_run,
            verbose=verbose,
            existing_idempotency_key=key,
        )
        if exit_code != EXIT_SUCCESS:
            worst_exit = exit_code

    return worst_exit


# ---------------------------------------------------------------------------
# Ledger maintenance
# ---------------------------------------------------------------------------

def cmd_reset_ledger(state_dir: Path) -> int:
    for fname in (
        LEDGER_FILENAME,
        RUN_HISTORY_FILENAME,
        RETRY_QUEUE_FILENAME,
        DIGEST_QUEUE_FILENAME,
        FEEDBACK_QUEUE_FILENAME,
        ACTION_QUEUE_FILENAME,
        CONSENT_FILENAME,
    ):
        p = state_dir / fname
        if p.exists():
            p.unlink()
            _log("ledger", f"removed {p.name}")
    _log("ledger", "reset complete")
    return EXIT_SUCCESS


def cmd_prune_ledger(state_dir: Path, older_than_days: int) -> int:
    ledger = load_ledger(state_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    before = len(ledger)
    pruned = {
        k: v for k, v in ledger.items()
        if not _entry_older_than(v, cutoff)
    }
    removed = before - len(pruned)
    if removed:
        save_ledger(state_dir, pruned)
        _log("ledger", f"pruned {removed} entries older than {older_than_days}d")
    else:
        _log("ledger", f"nothing to prune (all entries within {older_than_days}d)")
    return EXIT_SUCCESS


def _entry_older_than(entry: dict, cutoff: datetime) -> bool:
    for field in ("claude_in_flight_since",):
        raw = entry.get(field, "")
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt < cutoff
            except Exception:
                pass
    return False


def _parse_older_than(value: str) -> int:
    """Parse '30d', '7d', etc. into an integer number of days."""
    m = re.fullmatch(r"(\d+)d?", value.strip().lower())
    if not m:
        raise ValueError(f"Cannot parse --older-than value: {value!r} (expected e.g. '30d')")
    return int(m.group(1))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_cli() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    bootstrap_env(repo_root)

    parser = argparse.ArgumentParser(
        description="Intent-capture pipeline: gate → package → Claude → deliver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Run pipeline on a single note file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print envelope + planned route without side effects.",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Replay transient failures from the retry queue.",
    )
    parser.add_argument(
        "--reset-ledger",
        action="store_true",
        help="Delete local state files (ledger, run history, retry queue). Dangerous.",
    )
    parser.add_argument(
        "--prune-ledger",
        action="store_true",
        help="Remove ledger entries older than --older-than.",
    )
    parser.add_argument(
        "--older-than",
        default="30d",
        metavar="DAYS",
        help="TTL for --prune-ledger (e.g. 30d). Default: 30d.",
    )
    parser.add_argument(
        "--gate-model",
        default=os.getenv("INTENT_GATE_MODEL", DEFAULT_GATE_MODEL),
        help=f"Ollama gate model (default: $INTENT_GATE_MODEL or {DEFAULT_GATE_MODEL}).",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("INTENT_ROUTING_MODEL", DEFAULT_ROUTING_MODEL),
        help=f"OpenAI model ID (default: $INTENT_ROUTING_MODEL or {DEFAULT_ROUTING_MODEL}).",
    )
    parser.add_argument(
        "--journal-dir",
        default=os.getenv("SCRIBE_JOURNAL_DIR"),
        help="Journal directory (default: $SCRIBE_JOURNAL_DIR).",
    )
    parser.add_argument(
        "--cortex-dir",
        default=os.getenv("INTENT_CORTEX_DIR"),
        help="Obsidian cortex write target (default: $INTENT_CORTEX_DIR or <journal_dir>/cortex).",
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("INTENT_STATE_DIR"),
        help="Local state directory (default: $INTENT_STATE_DIR or ~/.local/state/journal-linker/intents).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed diagnostics.")
    return parser.parse_args()


def main() -> int:
    args = parse_cli()

    # State dir
    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
    else:
        state_dir = get_state_dir()

    # Maintenance modes
    if args.reset_ledger:
        return cmd_reset_ledger(state_dir)

    if args.prune_ledger:
        try:
            days = _parse_older_than(args.older_than)
        except ValueError as exc:
            print(f"[intent] {exc}", file=sys.stderr)
            return EXIT_PERMANENT
        return cmd_prune_ledger(state_dir, days)

    # Resolve parameters
    gate_model = args.gate_model
    gate_style = resolve_gate_style(gate_model)
    routing_model = args.model
    enrichment_mode = resolve_enrichment_mode()

    try:
        in_flight_ttl = int(os.getenv("INTENT_CLAUDE_IN_FLIGHT_TTL", str(DEFAULT_IN_FLIGHT_TTL)))
    except Exception:
        in_flight_ttl = DEFAULT_IN_FLIGHT_TTL

    # Cortex dir
    if args.cortex_dir:
        cortex_dir = Path(args.cortex_dir).expanduser()
    elif args.journal_dir:
        cortex_dir = Path(args.journal_dir).expanduser() / "cortex"
    else:
        cortex_dir = Path.home() / "cortex"

    pipeline_kwargs = dict(
        gate_model=gate_model,
        gate_style=gate_style,
        routing_model=routing_model,
        cortex_dir=cortex_dir,
        state_dir=state_dir,
        enrichment_mode=enrichment_mode,
        in_flight_ttl=in_flight_ttl,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    # Retry mode
    if args.retry:
        return run_retry(**pipeline_kwargs)

    # Single-file mode
    if args.file:
        source_path = Path(args.file).expanduser()
        if not source_path.exists():
            print(f"[intent] file not found: {source_path}", file=sys.stderr)
            return EXIT_PERMANENT
        return run_intent_pipeline(source_path, **pipeline_kwargs)

    print(
        "[intent] No action specified. Use --file PATH or --retry. See --help.",
        file=sys.stderr,
    )
    return EXIT_PERMANENT


if __name__ == "__main__":
    sys.exit(main())
