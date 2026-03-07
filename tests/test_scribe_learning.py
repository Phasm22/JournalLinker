import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "Scribe.py"
spec = importlib.util.spec_from_file_location("scribe", SCRIPT_PATH)
scribe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(scribe)


class TestScribeLearning(unittest.TestCase):
    def test_strip_html_if_needed_for_obsidian_clipboard(self):
        raw = "<meta charset='utf-8'><!-- obsidian --><p>Hello&nbsp;world</p><p>Line 2</p>"
        cleaned = scribe.strip_html_if_needed(raw)
        self.assertEqual(cleaned, "Hello world\nLine 2")

    def test_parse_journal_date_from_heading(self):
        text = "# Daily Log - 2026-03-04\n"
        self.assertEqual(scribe.parse_journal_date(text), "2026-03-04")

    def test_extract_wikilink_terms_handles_aliases_and_blocks(self):
        text = "[[Dad|My Dad]] [[project#todo]] [[walk]]"
        got = scribe.extract_wikilink_terms(text)
        self.assertIn("dad", got)
        self.assertIn("project", got)
        self.assertIn("walk", got)

    def test_yesterday_learning_applies_positive_and_negative_weights(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(
                (fixtures / "2026-03-03.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            current_entry_text = (fixtures / "2026-03-04.md").read_text(encoding="utf-8")

            learning = {
                "term_weights": {},
                "runs": {
                    "2026-03-03": {
                        "suggested_terms": ["walk", "Dad", "laundry"],
                        "updated_at": "2026-03-03T22:00:00",
                    }
                },
            }

            current_date = scribe.apply_yesterday_learning(
                learning, current_entry_text, str(journal_dir)
            )

            self.assertEqual(current_date, "2026-03-04")
            self.assertGreater(learning["term_weights"]["walk"], 0)
            self.assertGreater(learning["term_weights"]["dad"], 0)
            self.assertLess(learning["term_weights"]["laundry"], 0)

    def test_rank_terms_uses_learning_weights(self):
        original = "I went for a walk and called Dad."
        terms = ["walk", "Dad"]
        learning = {"term_weights": {"dad": 10.0, "walk": -5.0}, "runs": {}}

        ranked = scribe.rank_terms(original, terms, learning, max_links=2)
        self.assertEqual(ranked[0].lower(), "dad")


if __name__ == "__main__":
    unittest.main()
