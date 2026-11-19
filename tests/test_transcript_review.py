"""Transcript checks. No Ollama and no Pushover."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("transcript_review", SCRIPTS / "transcript_review.py")
tr = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["transcript_review"] = tr
spec.loader.exec_module(tr)


NOTE = """# 2026-11-09

> [!voice] 20:26
> with the just bear spicy chicken.
> One constitutes a water tower.
"""


class TestPhraseAndSpan(unittest.TestCase):
    def test_known_mishear_and_unique_splice(self):
        findings = tr.check_known_mishear(NOTE, [("just bear", "Just Bare"), ("one constitutes", "what constitutes")])
        self.assertEqual([item["heard"] for item in findings], ["just bear", "one constitutes"])
        old, new = tr.locate_replacement(NOTE, findings[0])
        self.assertEqual(old, "just bear")
        self.assertEqual(new, "Just Bare")
        self.assertEqual(NOTE.replace(old, new, 1).count("Just Bare"), 1)

    def test_two_matches_are_not_a_fix(self):
        text = "just bear and just bear"
        self.assertIsNone(tr.locate_replacement(text, {
            "heard": "just bear",
            "suggestion": "Just Bare",
        }))

    def test_wikilink_keeps_the_brackets(self):
        text = "the [[just bear]] spicy"
        old, new = tr.splice_span(text, tr.find_phrase(text, "just bear")[0], "Just Bare")
        self.assertEqual(old, "just bear")
        self.assertEqual(new, "Just Bare")
        self.assertEqual(text.replace(old, new, 1), "the [[Just Bare]] spicy")

    def test_callout_wrap_keeps_the_marker(self):
        text = "> with the just\n> bear spicy"
        old, new = tr.splice_span(text, tr.find_phrase(text, "just bear")[0], "Just Bare")
        self.assertIn("just", old)
        self.assertIn("bear", old)
        replaced = text.replace(old, new, 1)
        self.assertIn("Just Bare", replaced)
        self.assertIn("> spicy", replaced)


class TestOtherChecks(unittest.TestCase):
    def test_vocab_near_miss_skips_plurals_and_common_words(self):
        findings = tr.check_vocab_near_miss(
            "Palindrom plants and there",
            ["Palindrome", "planes", "their"],
        )
        heard = [item["heard"].lower() for item in findings]
        self.assertIn("palindrom", heard)
        self.assertNotIn("plants", heard)
        self.assertNotIn("there", heard)

    def test_repeated_loop(self):
        text = "thank you so much " * 4
        findings = tr.check_repeated_loop(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["suggestion"], "")

    def test_uncertain_words_skip_a_lone_function_word(self):
        segments = [{
            "text": "the bear",
            "avg_logprob": -0.2,
            "compression_ratio": 1.0,
            "words": [("the", 0.2), ("bear", 0.3), ("sat", 0.9)],
        }]
        findings = tr.check_uncertain_words(segments)
        self.assertEqual([item["heard"] for item in findings], ["bear"])

    def test_weak_segment_dropped_when_the_table_explains_it(self):
        text = "the just bear spicy"
        segments = [{
            "text": "the just bear spicy",
            "avg_logprob": -1.4,
            "compression_ratio": 1.0,
            "words": [("just", 0.2), ("bear", 0.2)],
        }]
        findings = tr.run_checks(
            text,
            segments=segments,
            corrections=[("just bear", "Just Bare")],
            terms=[],
            ask_model=False,
        )
        checks = [item["check"] for item in findings]
        self.assertIn("known_mishear", checks)
        self.assertNotIn("uncertain_words", checks)
        self.assertNotIn("weak_segment", checks)

    def test_model_none_does_not_invent_a_fix(self):
        findings = tr.run_checks(
            "the blurble sat",
            segments=[{
                "text": "the blurble sat",
                "avg_logprob": -0.2,
                "compression_ratio": 1.0,
                "words": [("blurble", 0.1)],
            }],
            corrections=[],
            terms=[],
            ask_model=False,
        )
        unsure = [item for item in findings if item["check"] == "uncertain_words"]
        self.assertEqual(unsure[0]["suggestion"], "")
        self.assertIsNone(tr.locate_replacement("the blurble sat", unsure[0]))


class TestExcerpt(unittest.TestCase):
    def test_short_slice_around_the_span(self):
        text = "Earlier I came home and then sat with the just bear spicy chicken tenders after the gym."
        excerpt = tr.excerpt_around(text, "just bear")
        self.assertTrue(excerpt.startswith("…"))
        self.assertTrue(excerpt.endswith("…"))
        self.assertIn("just bear", excerpt)
        self.assertLess(len(excerpt), 90)


class TestPublishCap(unittest.TestCase):
    def test_three_pings_and_a_link_only_for_a_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / "2026-11-09.md"
            note.write_text("just bear. alpha beta. gamma delta. epsilon zeta.", encoding="utf-8")
            sent_bodies = []

            def _send(title, body, link):
                sent_bodies.append((title, body, link))

            findings = [
                {"check": "known_mishear", "heard": "just bear", "suggestion": "Just Bare", "why": ""},
                {"check": "uncertain_words", "heard": "alpha beta", "suggestion": "", "why": "unsure"},
                {"check": "uncertain_words", "heard": "gamma delta", "suggestion": "", "why": "unsure"},
                {"check": "uncertain_words", "heard": "epsilon zeta", "suggestion": "", "why": "unsure"},
            ]
            sent = tr.publish_findings(
                note, findings, when="Nov 9, 8:26 PM",
                state_dir=Path(td) / "state", base_url="http://127.0.0.1:8798",
                send=_send,
            )
            self.assertEqual(len(sent), 3)
            self.assertEqual(sent_bodies[0][0], "Saved phrase")
            excerpt = tr.excerpt_around(note.read_text(encoding="utf-8"), "just bear")
            self.assertIn(excerpt, sent_bodies[0][1])
            self.assertLess(sent_bodies[0][1].index(excerpt), sent_bodies[0][1].index("just bear → Just Bare"))
            self.assertTrue(sent_bodies[0][2].startswith("http://127.0.0.1:8798/fix/"))
            self.assertEqual(sent_bodies[1][2], "")
            again = tr.publish_findings(
                note, findings[:1], when="Nov 9, 8:26 PM",
                state_dir=Path(td) / "state", base_url="http://127.0.0.1:8798",
                send=_send,
            )
            self.assertEqual(again, [])
