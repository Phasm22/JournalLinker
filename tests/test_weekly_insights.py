import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "weekly_insights.py"
spec = importlib.util.spec_from_file_location("weekly_insights", SCRIPT_PATH)
weekly_insights = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(weekly_insights)


SUBSTANTIVE_DAY_ONE = """
---
tags: [journal]
---

# Daily Log - 2026-03-03

[[2026-03-02|Yesterday]] | [[2026-03-04|Tomorrow]]

> [!TIP] Memory Prompt
> What is one interaction from today that felt significant?

I felt wrung out by work again, but the bigger thing was how often my stomach kept hijacking the day.
I wanted quiet more than anything and kept noticing how much relief I got from slowing down.
The tension around effort and being seen at work was there in the background the whole time.

## Daily Questions
- Last night, after work, I...

---
## Portability Export
I felt wrung out by [[work]] again but my [[stomach]] was loud.
""".strip()

SUBSTANTIVE_DAY_TWO = """
---
tags: [journal]
---

# Daily Log - 2026-03-05

[[2026-03-04|Yesterday]] | [[2026-03-06|Tomorrow]]

I still wanted a lot of quiet today. Work felt heavy in a different way because I kept wondering whether the effort is visible.
My stomach was better, but I still mostly wanted rest and a little more room to slow down.

## Daily Questions
- Last night, after work, I...

---
## Portability Export
[[work]] [[effort]] [[rest]]
""".strip()

TEMPLATE_ONLY_DAY = """
---
tags: [journal, reflection]
---

# Daily Log - 2026-03-06

[[2026-03-05|Yesterday]] | [[2026-03-07|Tomorrow]]

> [!TIP] Memory Prompt
> What is one interaction from today that felt significant?

## Daily Questions
- Last night, after work, I...

---
## Portability Export
""".strip()


class TestWeeklyInsights(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.embedding_patcher = mock.patch.object(
            weekly_insights.LocalEmbeddingCache,
            "embed_many",
            side_effect=lambda texts, max_chars=None: [None for _ in texts],
        )
        cls.embedding_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.embedding_patcher.stop()

    def write_learning(self, path: Path) -> None:
        payload = {
            "term_weights": {},
            "runs": {},
            "term_memory": {
                "work": {
                    "reinforcement": 4.0,
                    "success_count": 6,
                    "failure_count": 1,
                    "last_success_date": "2026-03-08",
                },
                "stomach": {
                    "reinforcement": 2.0,
                    "success_count": 4,
                    "failure_count": 0,
                    "last_success_date": "2026-03-05",
                },
                "rest": {
                    "reinforcement": 1.5,
                    "success_count": 2,
                    "failure_count": 0,
                    "last_success_date": "2026-03-01",
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_extract_wikilink_terms_filters_date_and_nav_terms(self):
        text = (
            "[[2026-03-03|Yesterday]] [[2026-03-05|Tomorrow]] [[Today]] "
            "[[walk]] [[Laundry]] [[project#todo]]"
        )
        terms = weekly_insights.extract_wikilink_terms(text)
        self.assertIn("walk", terms)
        self.assertIn("laundry", terms)
        self.assertIn("project", terms)
        self.assertNotIn("2026-03-03", terms)
        self.assertNotIn("2026-03-05", terms)
        self.assertNotIn("today", terms)

    def test_clean_daily_journal_text_strips_template_and_export_sections(self):
        cleaned = weekly_insights.clean_daily_journal_text(SUBSTANTIVE_DAY_ONE)
        self.assertIn("I felt wrung out by work again", cleaned)
        self.assertNotIn("Daily Questions", cleaned)
        self.assertNotIn("Portability Export", cleaned)
        self.assertNotIn("[[work]]", cleaned)
        self.assertNotIn("Yesterday", cleaned)

    def test_skips_note_creation_for_low_signal_week(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(SUBSTANTIVE_DAY_ONE, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            output_path, stats = weekly_insights.generate_weekly_insight(
                journal_dir=str(journal_dir),
                learning_file=str(learning_file),
                week_label="2026-W10",
            )

            self.assertIsNone(output_path)
            self.assertTrue(stats["skipped"])
            self.assertEqual(stats["reason"], "insufficient weekly signal")
            self.assertFalse((journal_dir / "Insights" / "Weekly Insight - 2026-W10.md").exists())

    def test_template_only_entries_do_not_count_as_substantive(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(TEMPLATE_ONLY_DAY, encoding="utf-8")
            (journal_dir / "2026-03-04.md").write_text(TEMPLATE_ONLY_DAY.replace("2026-03-06", "2026-03-04"), encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            entries = weekly_insights.collect_weekly_entries(
                journal_dir=journal_dir,
                week_start=weekly_insights.date(2026, 3, 2),
                week_end=weekly_insights.date(2026, 3, 8),
            )
            signals = weekly_insights.build_weekly_reflection_signals(
                entries,
                weekly_insights.load_memory_store(learning_file),
                "2026-W10",
                weekly_insights.date(2026, 3, 2),
                weekly_insights.date(2026, 3, 8),
            )

            self.assertEqual(signals["entries_found"], 2)
            self.assertEqual(signals["substantive_entries"], 0)
            self.assertEqual(signals["total_words"], 0)

    def test_embedding_signal_identifies_the_semantic_anchor_entries(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            rest_day = """
---
tags: [journal]
---

# Daily Log - 2026-03-03

[[2026-03-02|Yesterday]] | [[2026-03-04|Tomorrow]]

I wanted quiet and rest today, and I kept noticing how much better the day felt when I stopped forcing effort.
There was still some work pressure in the background, but the strongest pull was toward slow, quiet time.
That made the whole day feel softer and easier to hold.
""".strip()
            work_day = """
---
tags: [journal]
---

# Daily Log - 2026-03-05

[[2026-03-04|Yesterday]] | [[2026-03-06|Tomorrow]]

Work felt heavy and the effort kept piling up throughout the day, especially when I tried to keep pace.
I noticed pressure more than momentum, and that tension made it hard to settle into anything for long.
The whole thing felt like carrying too much at once.
""".strip()
            (journal_dir / "2026-03-03.md").write_text(rest_day, encoding="utf-8")
            (journal_dir / "2026-03-05.md").write_text(work_day, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            class FakeEmbedder:
                dirty = False

                def embed_many(self, texts, max_chars=None):
                    vectors = []
                    for text in texts:
                        lowered = text.lower()
                        if "quiet" in lowered or "rest" in lowered:
                            vectors.append([1.0, 0.0])
                        elif "work" in lowered or "pressure" in lowered:
                            vectors.append([0.7, 0.3])
                        else:
                            vectors.append([0.0, 1.0])
                    return vectors

            entries = weekly_insights.collect_weekly_entries(
                journal_dir=journal_dir,
                week_start=weekly_insights.date(2026, 3, 2),
                week_end=weekly_insights.date(2026, 3, 8),
            )
            signals = weekly_insights.build_weekly_reflection_signals(
                entries,
                weekly_insights.load_memory_store(learning_file),
                "2026-W10",
                weekly_insights.date(2026, 3, 2),
                weekly_insights.date(2026, 3, 8),
                embedder=FakeEmbedder(),
            )

            self.assertGreater(signals["semantic_cohesion"], 0)
            self.assertTrue(signals["semantic_anchor_entries"])
            self.assertEqual(signals["semantic_anchor_entries"][0]["date"], "2026-03-03")

    def test_writes_calm_weekly_arc_for_high_signal_week(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(SUBSTANTIVE_DAY_ONE, encoding="utf-8")
            (journal_dir / "2026-03-05.md").write_text(SUBSTANTIVE_DAY_TWO, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            model_result = {
                "weekly_arc": (
                    "This week seems to circle around a mix of physical discomfort and the wish to slow down. "
                    "Work still matters, but a lot of the emotional weight seems to come from wondering whether effort is visible while energy stays low. "
                    "What seems needed next is a little more room for rest without turning that into another thing to perform well."
                ),
                "confidence": 0.81,
                "should_write": True,
                "reason": "",
            }

            with mock.patch.object(weekly_insights, "request_weekly_arc", return_value=model_result):
                output_path, stats = weekly_insights.generate_weekly_insight(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    week_label="2026-W10",
                )

            self.assertIsNotNone(output_path)
            assert output_path is not None
            self.assertTrue(output_path.exists())
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("# Weekly Insight - 2026-W10", content)
            self.assertIn("## Weekly Arc", content)
            self.assertIn("physical discomfort and the wish to slow down", content)
            self.assertNotIn("Top Active Topics", content)
            self.assertNotIn("Portability Export", content)
            self.assertFalse(stats["skipped"])
            self.assertGreaterEqual(float(stats["confidence"]), 0.45)

    def test_skip_leaves_existing_week_note_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            insights_dir = journal_dir / "Insights"
            insights_dir.mkdir(parents=True)
            existing_path = insights_dir / "Weekly Insight - 2026-W10.md"
            existing_path.write_text("KEEP-ME", encoding="utf-8")
            (journal_dir / "2026-03-03.md").write_text(SUBSTANTIVE_DAY_ONE, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            output_path, stats = weekly_insights.generate_weekly_insight(
                journal_dir=str(journal_dir),
                learning_file=str(learning_file),
                week_label="2026-W10",
            )

            self.assertIsNone(output_path)
            self.assertTrue(stats["skipped"])
            self.assertEqual(existing_path.read_text(encoding="utf-8"), "KEEP-ME")

    def test_overwrites_same_week_file_on_rerun_when_signal_is_sufficient(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(SUBSTANTIVE_DAY_ONE, encoding="utf-8")
            (journal_dir / "2026-03-05.md").write_text(SUBSTANTIVE_DAY_TWO, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            with mock.patch.object(
                weekly_insights,
                "request_weekly_arc",
                return_value={
                    "weekly_arc": "The week felt heavy but pointed toward rest.",
                    "confidence": 0.72,
                    "should_write": True,
                    "reason": "",
                },
            ):
                output_path, _ = weekly_insights.generate_weekly_insight(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    week_label="2026-W10",
                )

            assert output_path is not None
            output_path.write_text(output_path.read_text(encoding="utf-8") + "\nCUSTOM-MARKER\n", encoding="utf-8")

            with mock.patch.object(
                weekly_insights,
                "request_weekly_arc",
                return_value={
                    "weekly_arc": "The week kept returning to fatigue, work pressure, and the wish to slow down.",
                    "confidence": 0.78,
                    "should_write": True,
                    "reason": "",
                },
            ):
                output_path_2, _ = weekly_insights.generate_weekly_insight(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    week_label="2026-W10",
                )

            self.assertEqual(output_path, output_path_2)
            rerun_content = output_path_2.read_text(encoding="utf-8")
            self.assertNotIn("CUSTOM-MARKER", rerun_content)
            self.assertIn("fatigue, work pressure, and the wish to slow down", rerun_content)

    def test_uses_iso_week_boundaries_at_year_edge(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2025-12-29.md").write_text(SUBSTANTIVE_DAY_ONE.replace("2026-03-03", "2025-12-29"), encoding="utf-8")
            (journal_dir / "2026-01-04.md").write_text(SUBSTANTIVE_DAY_TWO.replace("2026-03-05", "2026-01-04"), encoding="utf-8")
            (journal_dir / "2026-01-05.md").write_text(SUBSTANTIVE_DAY_TWO.replace("2026-03-05", "2026-01-05"), encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            with mock.patch.object(
                weekly_insights,
                "request_weekly_arc",
                return_value={
                    "weekly_arc": "This week centered on work strain and the wish for more quiet.",
                    "confidence": 0.76,
                    "should_write": True,
                    "reason": "",
                },
            ):
                output_path, stats = weekly_insights.generate_weekly_insight(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    week_label="2026-W01",
                )

            assert output_path is not None
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("Week Window: 2025-12-29 to 2026-01-04", content)
            self.assertEqual(stats["entries_found"], 2)

    def test_main_emits_written_and_skipped_logs(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(SUBSTANTIVE_DAY_ONE, encoding="utf-8")
            (journal_dir / "2026-03-05.md").write_text(SUBSTANTIVE_DAY_TWO, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)

            skip_stdout = io.StringIO()
            with mock.patch("sys.argv", ["weekly_insights.py", "--journal-dir", str(journal_dir), "--learning-file", str(learning_file), "--week", "2026-W11"]), contextlib.redirect_stdout(skip_stdout):
                exit_code = weekly_insights.main()
            self.assertEqual(exit_code, 0)
            self.assertIn("[weekly_insights] skipped", skip_stdout.getvalue())

            write_stdout = io.StringIO()
            with mock.patch.object(
                weekly_insights,
                "request_weekly_arc",
                return_value={
                    "weekly_arc": "The week kept circling fatigue and the need for quiet.",
                    "confidence": 0.77,
                    "should_write": True,
                    "reason": "",
                },
            ), mock.patch(
                "sys.argv",
                ["weekly_insights.py", "--journal-dir", str(journal_dir), "--learning-file", str(learning_file), "--week", "2026-W10"],
            ), contextlib.redirect_stdout(write_stdout):
                exit_code = weekly_insights.main()
            self.assertEqual(exit_code, 0)
            self.assertIn("[weekly_insights] wrote=", write_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
