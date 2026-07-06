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

    def test_company_before_verb(self):
        # Company can precede the verb entirely, not just follow it as a
        # prepositional object.
        cmd = jc.parse_command("Ford's 10-K, can you pull it")
        self.assertEqual(cmd["company"], "ford")

    def test_company_before_verb_via_preposition(self):
        cmd = jc.parse_command("for Ford, pull the 10-K")
        self.assertEqual(cmd["company"], "ford")

    def test_after_still_wins_when_both_sides_present(self):
        # Company mentioned on both sides: prepositional-object handling
        # (highest confidence) still wins over the before/after fallback.
        cmd = jc.parse_command("Ford's 10-K, pull the 10-K for Apple")
        self.assertEqual(cmd["company"], "apple")

    def test_requires_form(self):
        self.assertIsNone(jc.parse_command("pull the thing for ford"))

    def test_empty(self):
        self.assertIsNone(jc.parse_command(""))

    def test_strips_wikilinks_from_company(self):
        cmd = jc.parse_command("pull the 10-K for [[Ford]]")
        self.assertEqual(cmd["company"], "ford")

    def test_wikilinked_wake_and_ticker(self):
        cmd = jc.parse_command("pull the 10K for ticker [[ocm]]")
        self.assertEqual(cmd["company"], "ocm")

    def test_collapses_spelled_out_letters(self):
        cmd = jc.parse_command("pull the 10-K for ticker O-C-M")
        self.assertEqual(cmd["company"], "ocm")

    def test_collapses_spelled_out_letters_space_separated(self):
        cmd = jc.parse_command("pull the 10-K for ticker O C M")
        self.assertEqual(cmd["company"], "ocm")

    def test_past_tense_verb_with_no_company_is_not_a_command(self):
        # Regression: "pulled the 10K" (no company at all) used to leak
        # "pulled" through as a bogus company because the filler list only
        # had the bare "pull", not "pulled". Real reported bug.
        self.assertIsNone(jc.parse_command("pulled the 10K"))
        # Same scenario via the full wake-word pipeline (parse_command()
        # only ever sees command_text *after* the wake word is stripped by
        # extract_command_spans(), so the wake word must not leak in here).
        self.assertEqual(jc.find_commands("[[Palindrome]] pulled the 10K"), [])

    def test_past_tense_verb_variants_still_recognized_with_company(self):
        for phrase in [
            "pulled the 10-K for Ford",
            "pulling up the 10-K for Ford",
            "fetched the 10-K for Ford",
            "grabbed the 10-K for Ford",
            "downloaded the 10-K for Ford",
            "got me the 10-K for Ford",
        ]:
            cmd = jc.parse_command(phrase)
            self.assertIsNotNone(cmd, phrase)
            self.assertEqual(cmd["company"], "ford", phrase)

    def test_pole_homophone_recognized_as_pull_with_company(self):
        # Real evidence (2026-07-04 journal): Whisper misheard "pull" as
        # "pole". With a company present it should still resolve.
        cmd = jc.parse_command("pole the 10-K for APH")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["company"], "aph")

    def test_pole_with_no_form_still_fails(self):
        # Matches the real utterance exactly: no form mentioned at all, so
        # this is out of scope regardless of verb recognition.
        self.assertIsNone(jc.parse_command("pole ticker APH and add it to the hot seat"))


class TestParseCommandDiagnostic(unittest.TestCase):
    def test_ok_result_matches_parse_command(self):
        result, diag = jc.parse_command_diagnostic("pull the 10-K for Ford")
        self.assertEqual(result, jc.parse_command("pull the 10-K for Ford"))
        self.assertEqual(diag["reason"], "ok")
        self.assertFalse(diag["is_artifact_verb"])
        self.assertFalse(diag["had_spelled_letters"])

    def test_no_form_reason(self):
        result, diag = jc.parse_command_diagnostic("pole ticker APH and add it to the hot seat")
        self.assertIsNone(result)
        self.assertEqual(diag["reason"], "no_form")
        # Verb detection still runs even though form detection is what
        # ultimately rejects it — this is what lets the anomaly-reporting
        # layer flag "pole" as a mis-transcription even in a no-form miss.
        self.assertTrue(diag["is_artifact_verb"])
        self.assertEqual(diag["verb_lemma"], "pull")

    def test_no_verb_reason(self):
        result, diag = jc.parse_command_diagnostic("I read Ford's 10-K yesterday")
        self.assertIsNone(result)
        self.assertEqual(diag["reason"], "no_verb")

    def test_no_company_reason_flags_the_real_bug(self):
        result, diag = jc.parse_command_diagnostic("pulled the 10K")
        self.assertIsNone(result)
        self.assertEqual(diag["reason"], "no_company")
        self.assertTrue(diag["is_artifact_verb"])
        self.assertEqual(diag["verb_lemma"], "pull")

    def test_artifact_verb_flagged_even_when_it_parses(self):
        result, diag = jc.parse_command_diagnostic("pole the 10-K for APH")
        self.assertIsNotNone(result)
        self.assertEqual(diag["reason"], "ok")
        self.assertTrue(diag["is_artifact_verb"])
        self.assertEqual(diag["verb_form"], "pole")
        self.assertEqual(diag["verb_lemma"], "pull")

    def test_spelled_letters_flagged(self):
        result, diag = jc.parse_command_diagnostic("pull the 10-K for ticker O-C-M")
        self.assertIsNotNone(result)
        self.assertTrue(diag["had_spelled_letters"])

    def test_empty_text(self):
        result, diag = jc.parse_command_diagnostic("")
        self.assertIsNone(result)
        self.assertEqual(diag["reason"], "no_form")


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


class TestFindCommandsWithDiagnostics(unittest.TestCase):
    def test_clean_command_produces_no_anomaly(self):
        text = "Palindrome, pull the 10-K for Ford."
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(len(commands), 1)
        self.assertEqual(anomalies, [])

    def test_real_bug_produces_zero_commands_and_one_anomaly(self):
        text = "Palindrome pulled the 10K"
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(commands, [])
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["reason"], "no_company")
        self.assertTrue(anomalies[0]["is_artifact_verb"])

    def test_pole_no_form_produces_zero_commands_and_one_anomaly(self):
        text = "Palindrome, pole ticker APH and add it to the hot seat."
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(commands, [])
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["reason"], "no_form")
        self.assertTrue(anomalies[0]["is_artifact_verb"])
        self.assertEqual(anomalies[0]["verb_lemma"], "pull")

    def test_pole_with_form_produces_command_and_anomaly(self):
        # Auto-fetch AND flag: it still executes, but is tagged for review.
        text = "Palindrome, pole the 10-K for APH."
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["company"], "aph")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["reason"], "ok")
        self.assertEqual(anomalies[0]["company"], "aph")

    def test_spelled_letters_produces_command_and_anomaly(self):
        text = "Palindrome, pull the 10-K for ticker O-C-M."
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["company"], "ocm")
        self.assertEqual(len(anomalies), 1)
        self.assertTrue(anomalies[0]["had_spelled_letters"])

    def test_non_command_wake_sentence_no_anomaly(self):
        # A wake-word hit with no fetch-shaped phrasing at all shouldn't be
        # reported as an anomaly — it's just normal journaling.
        text = "Palindrome, remind me to call the dentist."
        commands, anomalies = jc.find_commands_with_diagnostics(text)
        self.assertEqual(commands, [])
        self.assertEqual(anomalies, [])

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
