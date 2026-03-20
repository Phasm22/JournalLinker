import importlib.util
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "Scribe.py"
spec = importlib.util.spec_from_file_location("scribe", SCRIPT_PATH)
scribe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(scribe)


class TestScribeLearning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.embedding_patcher = mock.patch.object(
            scribe.LocalEmbeddingCache,
            "embed_many",
            side_effect=lambda texts, max_chars=None: [None for _ in texts],
        )
        cls.embedding_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.embedding_patcher.stop()

    def test_find_latest_modified_journal_note_selects_newest_valid_date_file(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            old_note = journal_dir / "2026-03-01.md"
            new_note = journal_dir / "2026-03-02.md"
            ignored_non_date = journal_dir / "notes.md"

            old_note.write_text("old", encoding="utf-8")
            new_note.write_text("new", encoding="utf-8")
            ignored_non_date.write_text("ignore", encoding="utf-8")

            os.utime(old_note, (1000, 1000))
            os.utime(new_note, (2000, 2000))
            os.utime(ignored_non_date, (3000, 3000))

            latest = scribe.find_latest_modified_journal_note(str(journal_dir))
            self.assertEqual(latest, new_note)

    def test_resolve_current_journal_context_falls_back_to_latest_modified(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            note1 = journal_dir / "2026-03-04.md"
            note2 = journal_dir / "2026-03-06.md"
            note1.write_text("# Daily Log - 2026-03-04\n", encoding="utf-8")
            note2.write_text("# Daily Log - 2026-03-06\n", encoding="utf-8")
            os.utime(note1, (1000, 1000))
            os.utime(note2, (2000, 2000))

            current_date, note_path, note_text, source = scribe.resolve_current_journal_context(
                input_text="Body only input",
                journal_dir=str(journal_dir),
            )
            self.assertEqual(source, "latest_modified_file")
            self.assertEqual(current_date, "2026-03-06")
            self.assertEqual(note_path, note2)
            self.assertIn("2026-03-06", note_text or "")

    def test_find_previous_existing_journal_date_returns_latest_before_current(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-01.md").write_text("a", encoding="utf-8")
            (journal_dir / "2026-03-06.md").write_text("b", encoding="utf-8")
            (journal_dir / "2026-03-11.md").write_text("c", encoding="utf-8")
            got = scribe.find_previous_existing_journal_date(str(journal_dir), "2026-03-11")
            self.assertEqual(got, "2026-03-06")

    def test_load_local_env_sets_missing_values(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text('SCRIBE_JOURNAL_DIR="/tmp/journal-a"\n', encoding="utf-8")
            old = os.environ.pop("SCRIBE_JOURNAL_DIR", None)
            try:
                scribe.load_local_env(env_path)
                self.assertEqual(os.getenv("SCRIBE_JOURNAL_DIR"), "/tmp/journal-a")
            finally:
                if old is None:
                    os.environ.pop("SCRIBE_JOURNAL_DIR", None)
                else:
                    os.environ["SCRIBE_JOURNAL_DIR"] = old

    def test_load_local_env_does_not_override_existing_values(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text('SCRIBE_JOURNAL_DIR="/tmp/journal-from-file"\n', encoding="utf-8")
            old = os.environ.get("SCRIBE_JOURNAL_DIR")
            os.environ["SCRIBE_JOURNAL_DIR"] = "/tmp/journal-from-shell"
            try:
                scribe.load_local_env(env_path)
                self.assertEqual(os.getenv("SCRIBE_JOURNAL_DIR"), "/tmp/journal-from-shell")
            finally:
                if old is None:
                    os.environ.pop("SCRIBE_JOURNAL_DIR", None)
                else:
                    os.environ["SCRIBE_JOURNAL_DIR"] = old

    def test_renamed_api_surface_exists(self):
        self.assertTrue(hasattr(scribe, "MEMORY_STORE_FILE"))
        self.assertTrue(hasattr(scribe, "load_memory_store"))
        self.assertTrue(hasattr(scribe, "save_memory_store"))
        self.assertTrue(hasattr(scribe, "apply_previous_day_feedback"))
        self.assertTrue(hasattr(scribe, "record_daily_suggestions"))
        self.assertTrue(hasattr(scribe, "rank_link_candidates"))
        self.assertTrue(hasattr(scribe, "insert_ranked_wikilinks"))
        self.assertTrue(hasattr(scribe, "insert_wikilinks_by_paragraph"))
        self.assertTrue(hasattr(scribe, "extract_candidate_context"))
        self.assertTrue(hasattr(scribe, "compute_semantic_similarity"))
        self.assertTrue(hasattr(scribe, "compute_recency_weight"))
        self.assertFalse(hasattr(scribe, "load_learning"))
        self.assertFalse(hasattr(scribe, "save_learning"))
        self.assertFalse(hasattr(scribe, "rank_terms"))

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
                "term_memory": {},
                "runs": {
                    "2026-03-03": {
                        "suggested_terms": ["walk", "Dad", "laundry"],
                        "term_contexts": {
                            "walk": "I went on a walk after dinner.",
                            "dad": "I called Dad after my walk.",
                            "laundry": "I skipped laundry to rest.",
                        },
                        "updated_at": "2026-03-03T22:00:00",
                    }
                },
            }

            current_date = scribe.apply_previous_day_feedback(
                learning, current_entry_text, str(journal_dir)
            )

            self.assertEqual(current_date, "2026-03-04")
            self.assertGreater(learning["term_weights"]["walk"], 0)
            self.assertGreater(learning["term_weights"]["dad"], 0)
            self.assertLess(learning["term_weights"]["laundry"], 0)
            self.assertEqual(learning["term_memory"]["walk"]["success_count"], 1)
            self.assertEqual(learning["term_memory"]["dad"]["success_count"], 1)
            self.assertEqual(learning["term_memory"]["laundry"]["failure_count"], 1)
            self.assertEqual(learning["term_memory"]["walk"]["last_success_date"], "2026-03-03")
            self.assertIn("walk", " ".join(learning["term_memory"]["walk"]["contexts"]).lower())

    def test_rank_link_candidates_uses_learning_weights(self):
        original = "I went for a walk and called Dad."
        terms = ["walk", "Dad"]
        learning = {"term_weights": {"dad": 10.0, "walk": -5.0}, "runs": {}}

        ranked = scribe.rank_link_candidates(original, terms, learning, max_links=2)
        self.assertEqual(ranked[0].lower(), "dad")

    def test_rank_link_candidates_uses_temporal_semantic_memory(self):
        original = "I want to call Dad tonight and cook dinner tonight."
        terms = ["cook dinner tonight", "call Dad tonight"]
        learning = {
            "term_weights": {},
            "runs": {},
            "term_memory": {
                "call dad tonight": {
                    "reinforcement": 5.0,
                    "success_count": 6,
                    "failure_count": 0,
                    "last_success_date": "2026-03-09",
                    "contexts": [
                        "I want to call Dad tonight after dinner.",
                        "Calling Dad tonight helps me stay connected.",
                    ],
                },
                "cook dinner tonight": {
                    "reinforcement": -1.0,
                    "success_count": 0,
                    "failure_count": 3,
                    "last_success_date": "2025-01-01",
                    "contexts": [
                        "I should cook dinner tonight, but I often skip it.",
                    ],
                },
            },
        }

        ranked = scribe.rank_link_candidates(
            original,
            terms,
            learning,
            max_links=2,
            current_date="2026-03-10",
        )
        self.assertEqual(ranked[0].lower(), "call dad tonight")

    def test_rank_link_candidates_uses_embedding_similarity_when_words_are_equally_plain(self):
        original = "I felt calm by the ocean and the music helped me settle."
        terms = ["music", "ocean"]
        learning = {"term_weights": {}, "term_memory": {}, "runs": {}}

        class FakeEmbedder:
            dirty = False

            def embed_many(self, texts, max_chars=None):
                vectors = []
                for text in texts:
                    lowered = text.lower()
                    if lowered.startswith("ocean"):
                        vectors.append([1.0, 0.0])
                    elif lowered.startswith("music"):
                        vectors.append([0.0, 1.0])
                    else:
                        vectors.append([1.0, 0.0])
                return vectors

        ranked = scribe.rank_link_candidates(original, terms, learning, max_links=2, embedder=FakeEmbedder())

        self.assertEqual(ranked[0].lower(), "ocean")

    def test_burst_boosts_recently_active_topics(self):
        original = "# Daily Log - 2026-03-05\nI need to handle walk and laundry."
        terms = ["laundry", "walk"]
        learning = {"term_weights": {}, "term_memory": {}, "runs": {}}
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-04.md").write_text("I took a [[walk]].", encoding="utf-8")
            (journal_dir / "2026-03-03.md").write_text("Another [[walk]] today.", encoding="utf-8")
            ranked = scribe.rank_link_candidates(
                original,
                terms,
                learning,
                max_links=2,
                current_date="2026-03-05",
                journal_dir=str(journal_dir),
            )
        self.assertEqual(ranked[0].lower(), "walk")

    def test_burst_is_zero_without_parsable_date_or_journal(self):
        original = "I need to handle walk and laundry."
        terms = ["laundry", "walk"]
        learning = {"term_weights": {}, "term_memory": {}, "runs": {}}
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-04.md").write_text("I took a [[walk]].", encoding="utf-8")
            ranked = scribe.rank_link_candidates(
                original,
                terms,
                learning,
                max_links=2,
                current_date=None,
                journal_dir=str(journal_dir),
            )
        self.assertEqual(ranked[0].lower(), "laundry")

    def test_sync_daily_navigation_links_uses_adjacent_existing_notes(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-05.md").write_text("# Daily Log - 2026-03-05\n", encoding="utf-8")
            (journal_dir / "2026-03-09.md").write_text("# Daily Log - 2026-03-09\n", encoding="utf-8")
            text = (
                "# Daily Log - 2026-03-07\n\n"
                "[[2026-03-06|Yesterday]] | [[2026-03-08|Tomorrow]]\n\n"
                "Body"
            )
            synced = scribe.sync_daily_navigation_links(text, str(journal_dir))
            self.assertIn("[[2026-03-05|Yesterday]] | [[2026-03-09|Tomorrow]]", synced)

    def test_sync_daily_navigation_links_falls_back_when_neighbors_missing(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            text = (
                "# Daily Log - 2026-03-07\n\n"
                "[[2026-01-01|Yesterday]] | [[2026-01-02|Tomorrow]]\n\n"
                "Body"
            )
            synced = scribe.sync_daily_navigation_links(text, str(journal_dir))
            self.assertIn("[[2026-03-06|Yesterday]] | [[2026-03-08|Tomorrow]]", synced)

    def test_sync_navigation_links_in_file_updates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-05.md").write_text("# Daily Log - 2026-03-05\n", encoding="utf-8")
            (journal_dir / "2026-03-09.md").write_text("# Daily Log - 2026-03-09\n", encoding="utf-8")
            active = journal_dir / "2026-03-07.md"
            active.write_text(
                "# Daily Log - 2026-03-07\n\n[[2026-01-01|Yesterday]] | [[2026-01-02|Tomorrow]]\n\nBody",
                encoding="utf-8",
            )

            changed_first = scribe.sync_navigation_links_in_file(
                note_path=active,
                journal_dir=str(journal_dir),
                current_date="2026-03-07",
            )
            changed_second = scribe.sync_navigation_links_in_file(
                note_path=active,
                journal_dir=str(journal_dir),
                current_date="2026-03-07",
            )
            content = active.read_text(encoding="utf-8")
            self.assertTrue(changed_first)
            self.assertFalse(changed_second)
            self.assertIn("[[2026-03-05|Yesterday]] | [[2026-03-09|Tomorrow]]", content)
            self.assertEqual(content.count("|Tomorrow]]"), 1)

    def test_previous_existing_note_tomorrow_points_to_latest_note(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            previous = journal_dir / "2026-03-06.md"
            latest = journal_dir / "2026-03-11.md"
            previous.write_text(
                "# Daily Log - 2026-03-06\n\n[[2026-03-04|Yesterday]] | [[2026-03-07|Tomorrow]]\n",
                encoding="utf-8",
            )
            latest.write_text(
                "# Daily Log - 2026-03-11\n\n[[2026-03-10|Yesterday]] | [[2026-03-12|Tomorrow]]\n",
                encoding="utf-8",
            )
            previous_date = scribe.find_previous_existing_journal_date(str(journal_dir), "2026-03-11")
            self.assertEqual(previous_date, "2026-03-06")
            changed = scribe.sync_navigation_links_in_file(
                note_path=previous,
                journal_dir=str(journal_dir),
                current_date=previous_date,
            )
            self.assertTrue(changed)
            updated = previous.read_text(encoding="utf-8")
            self.assertIn("[[2026-03-11|Tomorrow]]", updated)

    def test_apply_previous_day_feedback_uses_current_date_override(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-03.md").write_text(
                "# Daily Log - 2026-03-03\n\n[[walk]]",
                encoding="utf-8",
            )
            learning = {
                "term_weights": {},
                "term_memory": {},
                "runs": {
                    "2026-03-03": {
                        "suggested_terms": ["walk"],
                        "term_contexts": {"walk": "I went on a walk."},
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                },
            }
            current_date = scribe.apply_previous_day_feedback(
                learning,
                "Body only, no header",
                str(journal_dir),
                current_date_override="2026-03-04",
            )
            self.assertEqual(current_date, "2026-03-04")
            self.assertGreater(learning["term_weights"]["walk"], 0)

    def test_rank_link_candidates_uses_explicit_current_date_for_body_only_input(self):
        original = "Need to do walk and laundry."
        terms = ["laundry", "walk"]
        learning = {"term_weights": {}, "term_memory": {}, "runs": {}}
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-03-04.md").write_text("I did a [[walk]].", encoding="utf-8")
            (journal_dir / "2026-03-03.md").write_text("Another [[walk]].", encoding="utf-8")
            ranked = scribe.rank_link_candidates(
                original,
                terms,
                learning,
                max_links=2,
                current_date="2026-03-05",
                journal_dir=str(journal_dir),
            )
        self.assertEqual(ranked[0].lower(), "walk")

    def test_write_run_report_prepends_newest_history_entry(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir = Path(d)
            first = scribe.write_run_report(
                base_dir=base_dir,
                started_at=datetime(2026, 3, 17, 9, 0, 0),
                finished_at=datetime(2026, 3, 17, 9, 0, 5),
                status="success",
                model="test-model",
                num_ctx=2048,
                journal_dir=str(base_dir),
                active_context_source="input_date",
                active_date="2026-03-17",
                active_file=base_dir / "2026-03-17.md",
                input_text="I went for a walk.",
                prompt="prompt",
                suggested_terms=["walk"],
                ranked_terms=["walk"],
                output_text="I went for a [[walk]].",
                file_nav_sync_applied=False,
                previous_file_nav_sync_applied=False,
                actions=[{"action": "Insert ranked wikilinks", "result": "completed", "target": "walk"}],
                touched_files=[],
                error_message=None,
                traceback_text=None,
            )
            second = scribe.write_run_report(
                base_dir=base_dir,
                started_at=datetime(2026, 3, 17, 10, 0, 0),
                finished_at=datetime(2026, 3, 17, 10, 0, 3),
                status="error",
                model="test-model",
                num_ctx=2048,
                journal_dir=str(base_dir),
                active_context_source="input_date",
                active_date="2026-03-17",
                active_file=base_dir / "2026-03-17.md",
                input_text="I went for a walk.",
                prompt="prompt",
                suggested_terms=[],
                ranked_terms=[],
                output_text=None,
                file_nav_sync_applied=False,
                previous_file_nav_sync_applied=False,
                actions=[{"action": "Run journal linker", "result": "failed", "target": "current input"}],
                touched_files=[],
                error_message="boom",
                traceback_text="traceback",
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            history_path = base_dir / scribe.RUN_REPORTS_DIRNAME / scribe.RUN_HISTORY_FILENAME
            history = history_path.read_text(encoding="utf-8")
            self.assertLess(history.find("2026-03-17 10:00:00"), history.find("2026-03-17 09:00:00"))

    def test_main_writes_success_report_in_journal_linker_folder(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            note_path = journal_dir / "2026-03-10.md"
            note_path.write_text(
                "# Daily Log - 2026-03-10\n\nI called Dad and went for a walk.\n",
                encoding="utf-8",
            )
            learning_path = Path(d) / "scribe_learning.json"
            model_response = {
                "message": {"content": '{"links":["Dad","walk"]}'},
                "eval_duration": 123,
            }

            with (
                mock.patch.object(
                    scribe,
                    "parse_cli",
                    return_value=("test-model", 2048, str(journal_dir), False, None, None, []),
                ),
                mock.patch.object(
                    scribe,
                    "get_input_text",
                    return_value=note_path.read_text(encoding="utf-8"),
                ),
                mock.patch.object(scribe, "MEMORY_STORE_FILE", learning_path),
                mock.patch.object(scribe.ollama, "chat", return_value=model_response),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = scribe.main()

            self.assertEqual(exit_code, 0)
            self.assertIn("[[Dad]]", stdout.getvalue())
            self.assertIn("report=", stderr.getvalue())

            report_dir = journal_dir / scribe.RUN_REPORTS_DIRNAME
            history_path = report_dir / scribe.RUN_HISTORY_FILENAME
            self.assertTrue(history_path.exists())
            report_files = sorted(report_dir.glob("Journal Linker Run - *.md"))
            self.assertEqual(len(report_files), 1)
            report_text = report_files[0].read_text(encoding="utf-8")
            self.assertIn("**Status:** Success", report_text)
            self.assertIn("Save learning store", report_text)
            self.assertIn(str(learning_path), report_text)

    def test_main_writes_error_report_in_journal_linker_folder(self):
        with tempfile.TemporaryDirectory() as d:
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            note_path = journal_dir / "2026-03-10.md"
            note_path.write_text(
                "# Daily Log - 2026-03-10\n\nI called Dad and went for a walk.\n",
                encoding="utf-8",
            )
            learning_path = Path(d) / "scribe_learning.json"

            with (
                mock.patch.object(
                    scribe,
                    "parse_cli",
                    return_value=("test-model", 2048, str(journal_dir), False, None, None, []),
                ),
                mock.patch.object(
                    scribe,
                    "get_input_text",
                    return_value=note_path.read_text(encoding="utf-8"),
                ),
                mock.patch.object(scribe, "MEMORY_STORE_FILE", learning_path),
                mock.patch.object(scribe.ollama, "chat", side_effect=RuntimeError("boom")),
                mock.patch("sys.stdout", new_callable=io.StringIO),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = scribe.main()

            self.assertEqual(exit_code, 1)
            self.assertIn("Error: boom", stderr.getvalue())

            report_dir = journal_dir / scribe.RUN_REPORTS_DIRNAME
            history_path = report_dir / scribe.RUN_HISTORY_FILENAME
            self.assertTrue(history_path.exists())
            report_files = sorted(report_dir.glob("Journal Linker Run - *.md"))
            self.assertEqual(len(report_files), 1)
            report_text = report_files[0].read_text(encoding="utf-8")
            self.assertIn("**Status:** Error", report_text)
            self.assertIn("boom", report_text)
            self.assertIn("## Traceback", report_text)


if __name__ == "__main__":
    unittest.main()
