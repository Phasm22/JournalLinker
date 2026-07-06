"""Tests for scripts/process_voice.py — Whisper vocabulary-bias prompt.

Focused on extract_whisper_prompt()'s guaranteed-vocab merge: the wake
word(s) and previously-logged voice-anomaly corrections should always bias
the prompt, independent of scribe_learning.json success counts.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_voice.py"
spec = importlib.util.spec_from_file_location("process_voice", SCRIPT_PATH)
pv = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pv)


class TestExtractWhisperPrompt(unittest.TestCase):
    def test_ranked_terms_from_learning_file(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06", guaranteed_terms=[],
            )
            self.assertIn("Ford", prompt)

    def test_missing_learning_file_still_returns_guaranteed_terms(self):
        # Regression: the old implementation returned "" immediately if the
        # learning file was missing, which would have silently dropped
        # guaranteed terms (wake word, corrections) too.
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist.json"
            prompt = pv.extract_whisper_prompt(
                missing, guaranteed_terms=["Palindrome", "OMC"],
            )
            self.assertIn("Palindrome", prompt)
            self.assertIn("OMC", prompt)

    def test_learning_file_with_no_term_memory_still_returns_guaranteed_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({"runs": {}}), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, guaranteed_terms=["Palindrome"],
            )
            self.assertIn("Palindrome", prompt)

    def test_guaranteed_terms_come_before_ranked_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06",
                guaranteed_terms=["Palindrome"],
            )
            self.assertLess(prompt.index("Palindrome"), prompt.index("Ford"))

    def test_dedupes_guaranteed_and_ranked_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06",
                guaranteed_terms=["Ford"],
            )
            self.assertEqual(prompt.count("Ford"), 1)

    def test_default_guaranteed_terms_include_wake_word_and_logged_corrections(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            (state_dir / pv.VOICE_ANOMALY_LOG_FILENAME).write_text(
                json.dumps({"corrected_term": "OMC"}) + "\n", encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"INTENT_STATE_DIR": str(state_dir)}):
                learning_file = Path(td) / "does_not_exist.json"
                prompt = pv.extract_whisper_prompt(learning_file)
            self.assertIn("Palindrome", prompt)
            self.assertIn("OMC", prompt)

    def test_empty_everything_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist.json"
            prompt = pv.extract_whisper_prompt(missing, guaranteed_terms=[])
            self.assertEqual(prompt, "")


class TestLoadAnomalyCorrectedTerms(unittest.TestCase):
    def test_reads_distinct_corrected_terms_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            log = state_dir / pv.VOICE_ANOMALY_LOG_FILENAME
            log.write_text(
                "\n".join([
                    json.dumps({"corrected_term": "OMC"}),
                    json.dumps({"corrected_term": "pull"}),
                    json.dumps({"corrected_term": "OMC"}),  # duplicate, dropped
                    json.dumps({"corrected_term": ""}),  # empty, dropped
                ]) + "\n",
                encoding="utf-8",
            )
            terms = pv._load_anomaly_corrected_terms(state_dir)
            self.assertEqual(terms, ["OMC", "pull"])

    def test_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(pv._load_anomaly_corrected_terms(Path(td)), [])


if __name__ == "__main__":
    unittest.main()
