from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime
from typing import Any, Mapping, Sequence

try:
    import ollama
except Exception:  # pragma: no cover - import availability depends on local runtime
    ollama = None


DEFAULT_EMBED_MODEL = "all-minilm"
DEFAULT_KEEP_ALIVE = "5m"
DEFAULT_CACHE_LIMIT = 512
CACHE_KEY = "embedding_cache"


def normalize_embedding_text(text: str, max_chars: int | None = None) -> str:
    if not isinstance(text, str):
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and max_chars > 0 and len(normalized) > max_chars:
        normalized = normalized[:max_chars].rsplit(" ", 1)[0].strip()
    return normalized


def resolve_embedding_model(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env_model = os.getenv("SCRIBE_EMBED_MODEL")
    if env_model and env_model.strip():
        return env_model.strip()
    return DEFAULT_EMBED_MODEL


def resolve_keep_alive(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env_keep_alive = os.getenv("SCRIBE_EMBED_KEEP_ALIVE")
    if env_keep_alive and env_keep_alive.strip():
        return env_keep_alive.strip()
    return DEFAULT_KEEP_ALIVE


def resolve_cache_limit(explicit: int | None = None) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    env_limit = os.getenv("SCRIBE_EMBED_CACHE_MAX_ITEMS")
    if env_limit:
        try:
            parsed = int(env_limit)
            if parsed > 0:
                return parsed
        except Exception:
            pass
    return DEFAULT_CACHE_LIMIT


def cache_key_for_text(model: str, text: str) -> str:
    normalized = normalize_embedding_text(text)
    digest = hashlib.sha1(f"{model}\0{normalized}".encode("utf-8")).hexdigest()
    return digest


def cosine_similarity(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    if not left or not right:
        return 0.0
    dot = 0.0
    for a, b in zip(left, right):
        dot += float(a) * float(b)
    return dot


def _normalize_vector(values: Sequence[float]) -> list[float]:
    vector = [float(v) for v in values]
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector
    return [v / norm for v in vector]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_cache_state(memory_store: dict, model: str) -> dict[str, Any]:
    cache = memory_store.get(CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
        memory_store[CACHE_KEY] = cache

    if cache.get("model") != model:
        cache.clear()
        cache.update(
            {
                "version": 1,
                "model": model,
                "updated_at": _now_iso(),
                "items": {},
            }
        )
    else:
        cache.setdefault("version", 1)
        cache.setdefault("model", model)
        cache.setdefault("updated_at", _now_iso())
        items = cache.get("items")
        if not isinstance(items, dict):
            cache["items"] = {}
    return cache


def _extract_embeddings(response: Any) -> list[list[float]]:
    embeddings = None
    if isinstance(response, Mapping):
        embeddings = response.get("embeddings")
        if embeddings is None and "embedding" in response:
            embeddings = [response["embedding"]]
    else:
        embeddings = getattr(response, "embeddings", None)
        if embeddings is None:
            embedding = getattr(response, "embedding", None)
            if embedding is not None:
                embeddings = [embedding]

    if embeddings is None:
        raise ValueError("No embeddings found in Ollama response.")

    vectors: list[list[float]] = []
    for embedding in embeddings:
        if embedding is None:
            continue
        vectors.append(_normalize_vector(embedding))
    return vectors


def _call_ollama_embed(model: str, inputs: list[str], keep_alive: str) -> list[list[float]]:
    if not inputs:
        return []
    if ollama is None:
        raise RuntimeError("The 'ollama' Python package is required for local embeddings.")

    if hasattr(ollama, "embed"):
        response = ollama.embed(model=model, input=inputs, keep_alive=keep_alive)
        return _extract_embeddings(response)

    if hasattr(ollama, "embeddings"):
        vectors: list[list[float]] = []
        for text in inputs:
            response = ollama.embeddings(model=model, prompt=text, keep_alive=keep_alive)
            vectors.extend(_extract_embeddings(response))
        return vectors

    raise RuntimeError("The installed Ollama package does not expose an embeddings API.")


class LocalEmbeddingCache:
    def __init__(
        self,
        memory_store: dict,
        model: str | None = None,
        keep_alive: str | None = None,
        cache_limit: int | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.model = resolve_embedding_model(model)
        self.keep_alive = resolve_keep_alive(keep_alive)
        self.cache_limit = resolve_cache_limit(cache_limit)
        self.cache = _ensure_cache_state(self.memory_store, self.model)
        self.dirty = False

    def _cache_items(self) -> dict[str, dict[str, Any]]:
        items = self.cache.get("items")
        if not isinstance(items, dict):
            items = {}
            self.cache["items"] = items
            self.dirty = True
        return items

    def _touch(self, key: str, normalized_text: str, embedding: Sequence[float]) -> None:
        items = self._cache_items()
        items[key] = {
            "text": normalized_text,
            "embedding": _normalize_vector(embedding),
            "last_used": _now_iso(),
        }
        self.cache["updated_at"] = _now_iso()
        self.dirty = True
        self._prune_items()

    def _prune_items(self) -> None:
        items = self._cache_items()
        if len(items) <= self.cache_limit:
            return

        ordered = sorted(
            items.items(),
            key=lambda item: (item[1].get("last_used", ""), item[0]),
        )
        trimmed = dict(ordered[-self.cache_limit :])
        if trimmed != items:
            self.cache["items"] = trimmed
            self.cache["updated_at"] = _now_iso()
            self.dirty = True

    def _cached_vector(self, normalized_text: str) -> list[float] | None:
        if not normalized_text:
            return None
        key = cache_key_for_text(self.model, normalized_text)
        items = self._cache_items()
        item = items.get(key)
        if not isinstance(item, dict):
            return None
        vector = item.get("embedding")
        if not isinstance(vector, list):
            return None
        item["last_used"] = _now_iso()
        self.cache["updated_at"] = item["last_used"]
        self.dirty = True
        return [float(v) for v in vector]

    def embed_many(self, texts: Sequence[str], max_chars: int | None = None) -> list[list[float] | None]:
        normalized_texts = [normalize_embedding_text(text, max_chars=max_chars) for text in texts]
        results: list[list[float] | None] = [None] * len(normalized_texts)

        missing: list[str] = []
        missing_keys: list[str] = []
        missing_positions: dict[str, list[int]] = {}

        for index, normalized_text in enumerate(normalized_texts):
            if not normalized_text:
                continue
            cached = self._cached_vector(normalized_text)
            if cached is not None:
                results[index] = cached
                continue
            key = cache_key_for_text(self.model, normalized_text)
            if key not in missing_positions:
                missing.append(normalized_text)
                missing_keys.append(key)
                missing_positions[key] = []
            missing_positions[key].append(index)

        if missing:
            try:
                fetched_vectors = _call_ollama_embed(self.model, missing, self.keep_alive)
            except Exception:
                return results

            for normalized_text, key, vector in zip(missing, missing_keys, fetched_vectors):
                if vector is None:
                    continue
                self._touch(key, normalized_text, vector)
                for index in missing_positions.get(key, []):
                    results[index] = [float(v) for v in vector]

        return results

    def similarity(self, left: str, right: str, max_chars: int | None = None) -> float:
        vectors = self.embed_many([left, right], max_chars=max_chars)
        if not vectors or vectors[0] is None or vectors[1] is None:
            return 0.0
        return cosine_similarity(vectors[0], vectors[1])


def average_vector(vectors: Sequence[Sequence[float] | None]) -> list[float] | None:
    kept = [list(map(float, vector)) for vector in vectors if vector]
    if not kept:
        return None
    width = min(len(vector) for vector in kept)
    if width <= 0:
        return None
    averaged = [0.0] * width
    for vector in kept:
        for index in range(width):
            averaged[index] += float(vector[index])
    count = float(len(kept))
    return _normalize_vector([value / count for value in averaged])
