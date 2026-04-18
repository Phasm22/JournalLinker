import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "daily_reflection.py"
spec = importlib.util.spec_from_file_location("daily_reflection", SCRIPT_PATH)
daily_reflection = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(daily_reflection)

LIVE_OLLAMA_SMOKE = os.getenv("JOURNAL_LINKER_LIVE_SMOKE") == "1"


SUBSTANTIVE_DAY = """
---
tags: [journal]
---

# Daily Log - 2026-04-08

[[2026-04-07|Yesterday]] | [[2026-04-09|Tomorrow]]

Honestly, I kept noticing how much better it felt to be a full day removed from things before trying to make meaning out of them.
Work still carried some friction, but by the evening it seemed clearer that the real thing I wanted was less pressure and a little more room to think.
The day felt better whenever I stopped chasing immediacy and let the emotional residue settle on its own.
""".strip()


SPARSE_DAY = """
---
tags: [journal]
---

# Daily Log - 2026-04-08

[[2026-04-07|Yesterday]] | [[2026-04-09|Tomorrow]]

Fine day.
""".strip()


class TestDailyReflection(unittest.TestCase):
    def write_learning(self, path: Path) -> None:
        payload = {
            "term_memory": {
                "work": {"success_count": 4, "last_success_date": "2026-04-07"},
                "pressure": {"success_count": 3, "last_success_date": "2026-04-07"},
                "room to think": {"success_count": 2, "last_success_date": "2026-04-06"},
            }
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def make_env(self) -> dict[str, str]:
        return {
            "SCRIBE_PUSHOVER_SERVER": "https://api.pushover.net",
            "SCRIBE_PUSHOVER_APP_TOKEN": "app-token",
            "SCRIBE_PUSHOVER_USER_KEY": "user-key",
            "SCRIBE_DAILY_REFLECTION_WINDOW_START": "16:00",
            "SCRIBE_DAILY_REFLECTION_WINDOW_END": "21:00",
            "SCRIBE_DAILY_REFLECTION_SEED": "seed-1",
        }

    def test_compute_target_send_time_is_deterministic_and_in_window(self):
        run_date = date(2026, 4, 9)
        start = datetime.strptime("16:00", "%H:%M").time()
        end = datetime.strptime("21:00", "%H:%M").time()

        first = daily_reflection.compute_target_send_time(run_date, start, end, seed="abc")
        second = daily_reflection.compute_target_send_time(run_date, start, end, seed="abc")

        self.assertEqual(first, second)
        self.assertGreaterEqual(first.time(), start)
        self.assertLessEqual(first.time(), end)

    def test_skips_when_note_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                result = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 0, 0),
                    date_override="2026-04-08",
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "missing daily note")

    def test_falls_back_to_latest_valid_note_when_yesterday_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            latest_path = journal_dir / "2026-04-06.md"
            latest_path.write_text(SUBSTANTIVE_DAY.replace("2026-04-08", "2026-04-06"), encoding="utf-8")
            os.utime(latest_path, (1712700000, 1712700000))
            older_path = journal_dir / "2026-04-04.md"
            older_path.write_text(SUBSTANTIVE_DAY.replace("2026-04-08", "2026-04-04"), encoding="utf-8")
            os.utime(older_path, (1712600000, 1712600000))
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            reflection = {
                "title": "A calmer read on yesterday",
                "body": "The day looks clearer with some distance. What stands out is the wish for less pressure and more room to think.",
                "confidence": 0.7,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "publish_pushover", return_value=(200, "ok")
            ), mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                result = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 0, 0),
                    date_override="2026-04-08",
                )

            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["reflection_date"], "2026-04-06")
            self.assertEqual(result["source"], "latest_valid")

    def test_skips_when_note_is_sparse(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SPARSE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                result = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 0, 0),
                    date_override="2026-04-08",
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "insufficient daily signal")

    def test_dry_run_prints_payload_without_sending(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            reflection = {
                "title": "A calmer read on yesterday",
                "body": "The day seems easier to understand from a little distance. What stands out is less the friction itself than the wish for less pressure and more room to think.",
                "confidence": 0.72,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "publish_pushover"
            ) as publish_mock:
                with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                    result = daily_reflection.run_daily_reflection(
                        journal_dir=str(journal_dir),
                        learning_file=str(learning_file),
                        state_file=str(state_file),
                        now=datetime(2026, 4, 9, 18, 0, 0),
                        date_override="2026-04-08",
                        dry_run=True,
                    )

            self.assertEqual(result["status"], "dry-run")
            self.assertIn("Date: 2026-04-08", result["payload"]["message"])
            publish_mock.assert_not_called()
            self.assertFalse(state_file.exists())

    def test_successful_send_records_state_and_dedupes(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            reflection = {
                "title": "A calmer read on yesterday",
                "body": "Distance makes the day feel less like a pile of friction and more like a clear wish for less pressure. The strongest thread is wanting a little more room to think.",
                "confidence": 0.74,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "publish_pushover", return_value=(200, "ok")
            ) as publish_mock:
                with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                    first = daily_reflection.run_daily_reflection(
                        journal_dir=str(journal_dir),
                        learning_file=str(learning_file),
                        state_file=str(state_file),
                        now=datetime(2026, 4, 9, 18, 0, 0),
                        date_override="2026-04-08",
                    )
                    second = daily_reflection.run_daily_reflection(
                        journal_dir=str(journal_dir),
                        learning_file=str(learning_file),
                        state_file=str(state_file),
                        now=datetime(2026, 4, 9, 18, 15, 0),
                        date_override="2026-04-08",
                    )

            self.assertEqual(first["status"], "sent")
            self.assertTrue(first["sent"])
            self.assertEqual(second["reason"], "already sent")
            publish_mock.assert_called_once()

            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["days"]["2026-04-08"]
            self.assertTrue(record["sent"])
            self.assertEqual(record["attempt_count"], 1)

    def test_failed_send_is_not_retried_without_new_context(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            reflection = {
                "title": "A calmer read on yesterday",
                "body": "The day looks clearer with a little distance. What remains is the wish for less pressure and more room to think.",
                "confidence": 0.7,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "publish_pushover", side_effect=RuntimeError("temporary error")
            ):
                with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                    first = daily_reflection.run_daily_reflection(
                        journal_dir=str(journal_dir),
                        learning_file=str(learning_file),
                        state_file=str(state_file),
                        now=datetime(2026, 4, 9, 18, 0, 0),
                        date_override="2026-04-08",
                    )
                    second = daily_reflection.run_daily_reflection(
                        journal_dir=str(journal_dir),
                        learning_file=str(learning_file),
                        state_file=str(state_file),
                        now=datetime(2026, 4, 9, 18, 15, 0),
                        date_override="2026-04-08",
                    )

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["reason"], "no new context since last run")

            state = json.loads(state_file.read_text(encoding="utf-8"))
            record = state["days"]["2026-04-08"]
            self.assertEqual(record["attempt_count"], 1)
            self.assertEqual(record["last_error"], "temporary error")

    def test_identical_context_skips_after_last_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            reflection = {
                "title": "A calmer read on yesterday",
                "body": "The day looks clearer with a little distance. What remains is the wish for less pressure and more room to think.",
                "confidence": 0.7,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "publish_pushover", side_effect=RuntimeError("temporary error")
            ), mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)):
                first = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 0, 0),
                    date_override="2026-04-08",
                )

            with mock.patch.object(daily_reflection, "request_daily_reflection") as reflection_mock, mock.patch.object(
                daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)
            ):
                second = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 15, 0),
                    date_override="2026-04-08",
                )

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["reason"], "no new context since last run")
            reflection_mock.assert_not_called()

    def test_before_target_send_time_skips_without_model_call(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            with mock.patch.object(daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 20, 0, 0)), mock.patch.object(
                daily_reflection, "request_daily_reflection"
            ) as reflection_mock:
                result = daily_reflection.run_daily_reflection(
                    journal_dir=str(journal_dir),
                    learning_file=str(learning_file),
                    state_file=str(state_file),
                    now=datetime(2026, 4, 9, 18, 0, 0),
                    date_override="2026-04-08",
                )

            self.assertEqual(result["reason"], "before target send time")
            reflection_mock.assert_not_called()

    def test_main_emits_dry_run_log(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.make_env(), clear=False):
            journal_dir = Path(d) / "journal"
            journal_dir.mkdir()
            (journal_dir / "2026-04-08.md").write_text(SUBSTANTIVE_DAY, encoding="utf-8")
            learning_file = Path(d) / "scribe_learning.json"
            self.write_learning(learning_file)
            state_file = Path(d) / "daily_reflection_state.json"

            stdout = io.StringIO()
            reflection = {
                "title": "A calmer read on yesterday",
                "body": "Distance made the day feel more legible. What stayed visible was the wish for less pressure and more room to think.",
                "confidence": 0.71,
                "should_send": True,
                "reason": "",
            }

            with mock.patch.object(daily_reflection, "request_daily_reflection", return_value=reflection), mock.patch.object(
                daily_reflection, "compute_target_send_time", return_value=datetime(2026, 4, 9, 17, 0, 0)
            ), mock.patch(
                "sys.argv",
                [
                    "daily_reflection.py",
                    "--journal-dir",
                    str(journal_dir),
                    "--learning-file",
                    str(learning_file),
                    "--state-file",
                    str(state_file),
                    "--date",
                    "2026-04-08",
                    "--dry-run",
                ],
            ), contextlib.redirect_stdout(stdout):
                exit_code = daily_reflection.main()

            self.assertEqual(exit_code, 0)
            self.assertIn("[daily_reflection] status=dry-run", stdout.getvalue())

    @unittest.skipUnless(LIVE_OLLAMA_SMOKE, "set JOURNAL_LINKER_LIVE_SMOKE=1 to run live Ollama smoke tests")
    def test_live_ollama_smoke_for_daily_reflection(self):
        signals = {
            "entry_date": "2026-04-08",
            "entry_text": "Work felt heavy but the evening was calmer.",
            "memory_hits": [],
            "keywords": ["work", "calm"],
            "confidence": 0.5,
        }
        response = daily_reflection.ollama.chat(
            model=os.getenv("SCRIBE_DAILY_REFLECTION_MODEL", os.getenv("SCRIBE_MODEL", "llama3.1:8b")),
            messages=[{"role": "user", "content": daily_reflection.build_daily_reflection_prompt(signals)}],
            options={"temperature": 0.45, "num_ctx": 128},
            keep_alive="5m",
        )
        self.assertIn("message", response)
        self.assertTrue(str(response["message"].get("content", "")).strip())


if __name__ == "__main__":
    unittest.main()
