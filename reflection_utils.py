"""Shared journal-text helpers for reflection workflows."""

import json
import re
from pathlib import Path


DEFAULT_MEMORY_STORE_FILE = Path(__file__).with_name("scribe_learning.json")
MIN_SUBSTANTIVE_ENTRY_WORDS = 35
MAX_ENTRY_EXCERPT_CHARS = 700
STOPWORDS = {
    "a", "about", "after", "all", "also", "am", "an", "and", "are", "as", "at",
    "be", "been", "but", "by", "did", "do", "feel", "felt", "for", "from", "good",
    "had", "has", "have", "i", "if", "in", "into", "is", "it", "its", "just",
    "know", "lately", "like", "lot", "maybe", "me", "my", "not", "of", "on", "or",
    "out", "putting", "really", "so", "something", "that", "the", "their", "them",
    "there", "they", "thing", "things", "this", "thought", "time", "to", "up", "very",
    "was", "we", "were", "what", "when", "with", "work", "would", "why", "you",
}


def load_memory_store(path: Path) -> dict:
    empty = {"term_weights": {}, "runs": {}, "term_memory": {}, "embedding_cache": {}}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    for key in empty:
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def strip_yaml_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("\n---", 1)
    return parts[1].lstrip("\n") if len(parts) == 2 else text


def clean_daily_journal_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = strip_yaml_frontmatter(normalized)
    normalized = re.split(r"\n##\s+Daily Questions\b", normalized, maxsplit=1)[0]
    normalized = re.sub(r"^#\s+Daily Log\s+-\s+\d{4}-\d{2}-\d{2}\s*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(
        r"^\[\[[^\]]+\|Yesterday\]\]\s*\|\s*\[\[[^\]]+\|Tomorrow\]\]\s*$",
        "",
        normalized,
        flags=re.MULTILINE,
    )
    normalized = re.sub(r"^>\s*\[!TIP\].*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(
        r"^>\s*What is one interaction from today that felt significant\?.*$",
        "",
        normalized,
        flags=re.MULTILINE,
    )
    normalized = re.sub(r"^---\s*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return "\n\n".join(line.strip() for line in normalized.splitlines() if line.strip()).strip()


def extract_keyword_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text.lower())
    return [token for token in tokens if len(token) >= 4 and token not in STOPWORDS]


def build_entry_excerpt(text: str, max_chars: int = MAX_ENTRY_EXCERPT_CHARS) -> str:
    clipped = text.strip()
    if len(clipped) <= max_chars:
        return clipped
    shortened = clipped[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{shortened}..."


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
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("No valid JSON object found in model output.")
