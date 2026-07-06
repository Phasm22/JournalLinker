"""Tests for scripts/text_normalize.py — wikilink stripping and spelled-letter
collapsing shared by journal_commands.py and hot_seat_fetch.py.
"""

import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "text_normalize.py"
spec = importlib.util.spec_from_file_location("text_normalize", SCRIPT_PATH)
tn = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(tn)


class TestStripWikilinks(unittest.TestCase):
    def test_simple_wikilink(self):
        self.assertEqual(tn.strip_wikilinks("pull the 10-K for [[Ford]]"), "pull the 10-K for Ford")

    def test_multiple_wikilinks(self):
        self.assertEqual(
            tn.strip_wikilinks("[[Palindrome]], pull the 10K for ticker [[ocm]]"),
            "Palindrome, pull the 10K for ticker ocm",
        )

    def test_alias_syntax_prefers_alias(self):
        self.assertEqual(tn.strip_wikilinks("pull [[OMC|Omnicom Group]]"), "pull Omnicom Group")

    def test_section_suffix_dropped(self):
        self.assertEqual(tn.strip_wikilinks("see [[Ford#history]]"), "see Ford")

    def test_alias_with_section_suffix_dropped(self):
        self.assertEqual(tn.strip_wikilinks("see [[Ford#history|the company]]"), "see the company")

    def test_no_wikilinks_passthrough(self):
        self.assertEqual(tn.strip_wikilinks("pull the 10-K for Ford"), "pull the 10-K for Ford")

    def test_empty_string(self):
        self.assertEqual(tn.strip_wikilinks(""), "")


class TestCollapseSpelledLetters(unittest.TestCase):
    def test_hyphen_separated(self):
        self.assertEqual(tn.collapse_spelled_letters("ticker O-C-M"), "ticker OCM")

    def test_space_separated(self):
        self.assertEqual(tn.collapse_spelled_letters("ticker O C M"), "ticker OCM")

    def test_dot_separated(self):
        # Trailing "." after the last letter is ambiguous (sentence end vs.
        # spelling punctuation) so it's left alone; the letters still collapse.
        self.assertEqual(tn.collapse_spelled_letters("ticker O.C.M."), "ticker OCM.")

    def test_mixed_case_letters(self):
        self.assertEqual(tn.collapse_spelled_letters("ticker o-c-m"), "ticker OCM")

    def test_does_not_touch_hyphenated_words(self):
        self.assertEqual(
            tn.collapse_spelled_letters("state-of-the-art design"),
            "state-of-the-art design",
        )

    def test_does_not_touch_single_letter_alone(self):
        self.assertEqual(tn.collapse_spelled_letters("grab a coffee"), "grab a coffee")

    def test_does_not_touch_ordinary_word(self):
        self.assertEqual(tn.collapse_spelled_letters("pull the 10-K for Ford"), "pull the 10-K for Ford")

    def test_already_contiguous_token_is_noop(self):
        self.assertEqual(tn.collapse_spelled_letters("ticker ocm"), "ticker ocm")


class TestNormalizeCommandText(unittest.TestCase):
    def test_wikilinked_spelled_letters(self):
        # Letters spelled out inside a wikilink, e.g. "[[O C M]]"
        self.assertEqual(
            tn.normalize_command_text("pull the 10-K for ticker [[O C M]]"),
            "pull the 10-K for ticker OCM",
        )

    def test_realistic_full_command(self):
        self.assertEqual(
            tn.normalize_command_text("[[Palindrome]], pull the 10K for ticker [[ocm]]"),
            "Palindrome, pull the 10K for ticker ocm",
        )

    def test_dash_letters_outside_wikilink(self):
        self.assertEqual(
            tn.normalize_command_text("[[Palindrome]], pull the ticker, O-C-M."),
            "Palindrome, pull the ticker, OCM.",
        )

    def test_empty_string(self):
        self.assertEqual(tn.normalize_command_text(""), "")


if __name__ == "__main__":
    unittest.main()
