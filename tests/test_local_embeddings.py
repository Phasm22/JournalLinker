import importlib.util
from pathlib import Path
from unittest import mock
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "local_embeddings.py"
spec = importlib.util.spec_from_file_location("local_embeddings", SCRIPT_PATH)
local_embeddings = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(local_embeddings)


class TestLocalEmbeddings(unittest.TestCase):
    def test_batch_embeddings_use_cache_and_dedupe_missing_texts(self):
        store = {"embedding_cache": {}}
        cache = local_embeddings.LocalEmbeddingCache(store, model="test-embed", keep_alive="5m", cache_limit=10)
        cached_key = local_embeddings.cache_key_for_text("test-embed", "cached text")
        store["embedding_cache"]["items"][cached_key] = {
            "text": "cached text",
            "embedding": [1.0, 0.0],
            "last_used": "2026-03-20T00:00:00",
        }

        with mock.patch.object(
            local_embeddings.ollama,
            "embed",
            return_value={"embeddings": [[0.25, 0.75]]},
        ) as embed_mock:
            vectors = cache.embed_many(["cached text", "new text", "new text"], max_chars=120)

        self.assertEqual(embed_mock.call_count, 1)
        self.assertEqual(embed_mock.call_args.kwargs["input"], ["new text"])
        self.assertEqual(vectors[0], [1.0, 0.0])
        self.assertEqual(vectors[1], [0.31622776601683794, 0.9486832980505138])
        self.assertEqual(vectors[1], vectors[2])
        self.assertTrue(cache.dirty)

    def test_embedding_failures_fall_back_to_heuristic_path(self):
        store = {"embedding_cache": {}}
        cache = local_embeddings.LocalEmbeddingCache(store, model="test-embed", keep_alive="5m", cache_limit=10)

        with mock.patch.object(local_embeddings.ollama, "embed", side_effect=RuntimeError("boom")):
            vectors = cache.embed_many(["new text"], max_chars=120)

        self.assertEqual(vectors, [None])
        self.assertEqual(store["embedding_cache"].get("items", {}), {})

    def test_similarity_uses_batched_vectors(self):
        store = {"embedding_cache": {}}
        cache = local_embeddings.LocalEmbeddingCache(store, model="test-embed", keep_alive="5m", cache_limit=10)

        with mock.patch.object(
            local_embeddings.ollama,
            "embed",
            return_value={"embeddings": [[1.0, 0.0], [0.0, 1.0]]},
        ):
            similarity = cache.similarity("alpha", "beta", max_chars=120)

        self.assertEqual(similarity, 0.0)


if __name__ == "__main__":
    unittest.main()
