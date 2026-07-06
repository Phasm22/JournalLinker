"""Tests for scripts/journal_commands.py — wake-word command recognition.

Pure functions; no external services. Verifies that directives are recognized
with high precision and that normal journaling never triggers a route.
"""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "journal_commands.py"
spec = importlib.util.spec_from_file_location("journal_commands", SCRIPT_PATH)
jc = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(jc)


class TestWakeWords(unittest.TestCase):
    def test_default_wake_word(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INTENT_COMMAND_WAKE_WORDS", None)
            self.assertIn("palindrome", jc.get_wake_words())

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"INTENT_COMMAND_WAKE_WORDS": "computer, jarvis"}):
            self.assertEqual(jc.get_wake_words(), ["computer", "jarvis"])


class TestExtraction(unittest.TestCase):
    def test_extracts_command_after_wake_word(self):
        text = "Woke up. Palindrome, pull the 10-K for Ford. Then coffee."
        spans = jc.extract_command_spans(text, ["palindrome"])
        self.assertEqual(len(spans), 1)
        self.assertIn("pull the 10-K for Ford", spans[0]["command_text"])

    def test_hey_prefix_and_question_terminator(self):
        text = "Hey Palindrome, can you pull the latest ten K for Ford?"
        spans = jc.extract_command_spans(text)
        self.assertEqual(len(spans), 1)
        self.assertFalse(spans[0]["command_text"].endswith("?"))

    def test_no_wake_word_no_spans(self):
        self.assertEqual(jc.extract_command_spans("Just a normal note.", ["palindrome"]), [])


class TestFormDetection(unittest.TestCase):
    def test_variants_map_to_10k(self):
        for phrase in ("10-K", "10 K", "10k", "ten K", "ten-k", "annual report", "annual filing"):
            form, span = jc.detect_form(f"pull the {phrase} for ford")
            self.assertEqual(form, "10-K", phrase)
            self.assertIsNotNone(span)

    def test_no_form(self):
        form, span = jc.detect_form("pull something for ford")
        self.assertIsNone(form)
        self.assertIsNone(span)


class TestParseCommand(unittest.TestCase):
    def test_parses_fetch_for_company(self):
        cmd = jc.parse_command("can you pull the latest ten K for Ford")
        self.assertEqual(cmd["route"], "hot_seat_fetch")
        self.assertEqual(cmd["form"], "10-K")
        self.assertEqual(cmd["company"], "ford")

    def test_annual_report_possessive(self):
        cmd = jc.parse_command("pull up the Apple annual report please")
        self.assertEqual(cmd["company"], "apple")

    def test_multiword_company(self):
        cmd = jc.parse_command("grab the 10-K for Berkshire Hathaway")
        self.assertEqual(cmd["company"], "berkshire hathaway")

    def test_the_prefix_company(self):
        cmd = jc.parse_command("get me the annual report on The Home Depot")
        self.assertEqual(cmd["company"], "home depot")

    def test_requires_verb(self):
        # Mentions a 10-K but issues no fetch verb -> not a command.
        self.assertIsNone(jc.parse_command("I read Ford's 10-K yesterday"))

    def test_requires_form(self):
        self.assertIsNone(jc.parse_command("pull the thing for ford"))

    def test_empty(self):
        self.assertIsNone(jc.parse_command(""))


class TestFindCommands(unittest.TestCase):
    def test_full_note_single_command(self):
        text = "Morning pages. Palindrome, can you pull the latest ten K for Ford? Gym after."
        cmds = jc.find_commands(text)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["company"], "ford")
        self.assertEqual(cmds[0]["key"], "hot_seat_fetch|10-K|ford")

    def test_ignores_non_command_wake_sentences(self):
        text = "Palindrome, remind me to call the dentist."
        self.assertEqual(jc.find_commands(text), [])

    def test_dedupes_same_command(self):
        text = ("Palindrome, pull the 10-K for Ford. "
                "Later: Palindrome grab the annual report for Ford.")
        cmds = jc.find_commands(text)
        self.assertEqual(len(cmds), 1)

    def test_two_distinct_commands(self):
        text = ("Palindrome, pull the 10-K for Ford. "
                "Palindrome, pull the 10-K for Apple.")
        cmds = jc.find_commands(text)
        companies = sorted(c["company"] for c in cmds)
        self.assertEqual(companies, ["apple", "ford"])


class TestStripSpans(unittest.TestCase):
    def test_strips_recognized_span(self):
        text = "Before. Palindrome, pull the 10-K for Ford. After."
        cmds = jc.find_commands(text)
        stripped = jc.strip_command_spans(text, cmds)
        self.assertNotIn("pull the 10-K for Ford", stripped)
        self.assertIn("Before.", stripped)
        self.assertIn("After.", stripped)

    def test_no_commands_returns_original(self):
        text = "Nothing here."
        self.assertEqual(jc.strip_command_spans(text, []), text)


if __name__ == "__main__":
    unittest.main()
