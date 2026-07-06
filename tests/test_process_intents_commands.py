"""Tests for the wake-word command stage in scripts/process_intents.py.

hot_seat_fetch and Pushover are mocked; filesystem uses tempdirs.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCRIPT_PATH = SCRIPTS_DIR / "process_intents.py"
spec = importlib.util.spec_from_file_location("process_intents", SCRIPT_PATH)
pi = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pi)

import hot_seat_fetch as hsf  # noqa: E402

OK_RESULT = {
    "ok": True, "query": "Ford", "ticker": "F", "cik": 37996,
    "title": "FORD MOTOR CO", "resolver": "edgar_local", "form": "10-K",
    "fy": "2025", "accession": "0000037996-26-000015",
    "filing_date": "2026-02-11", "report_date": "2025-12-31",
    "folder_name": "F_10K_FY2025", "folder": "/tmp/hs/F_10K_FY2025",
    "file": "/tmp/hs/F_10K_FY2025/f-20251231.htm", "downloaded": True, "bytes": 100,
}

NOTE = "Morning. Palindrome, can you pull the latest ten K for Ford? Then gym."


class TestCommandStage(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("INTENT_COMMANDS_MODE", None)
        os.environ.pop("SCRIBE_PUSHOVER_APP_TOKEN", None)
        os.environ.pop("SCRIBE_PUSHOVER_USER_KEY", None)
        os.environ.pop("PUSHOVER_TOKEN", None)
        os.environ.pop("PUSHOVER_KEY", None)

    def tearDown(self):
        self._env.stop()

    def _run(self, note_text, state_dir, cortex_dir):
        summary = pi.RunSummary()
        src = Path(state_dir) / "2026-07-04.md"
        src.write_text(note_text, encoding="utf-8")
        # Never fire a real notification or shell out to `pal` during tests.
        with mock.patch.object(pi, "_push_command_notification"), \
                mock.patch.object(pi, "index_hot_seat_dir", return_value={"ok": True}) as idx:
            exit_code, gate_text = pi.run_command_stage(
                note_text, src, "2026-07-04", Path(cortex_dir), Path(state_dir),
                summary, dry_run=False, verbose=False,
            )
        self._last_index_mock = idx
        return exit_code, gate_text, summary, src

    def test_executes_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            with mock.patch.object(hsf, "fetch_10k", return_value=OK_RESULT) as m:
                exit_code, gate_text, summary, src = self._run(NOTE, state, cortex)
                m.assert_called_once()
                self.assertEqual(m.call_args.args[0], "ford")
            self.assertEqual(exit_code, pi.EXIT_SUCCESS)
            self.assertEqual(summary.commands_executed, 1)
            # Command span stripped from gate text.
            self.assertNotIn("pull the latest ten K for Ford", gate_text)
            self.assertIn("Morning.", gate_text)
            # Ledger written.
            ledger = pi.load_command_ledger(state)
            self.assertEqual(len(ledger), 1)
            rec = next(iter(ledger.values()))
            self.assertEqual(rec["status"], "success")
            self.assertEqual(rec["ticker"], "F")
            # Cortex confirmation note written.
            notes = list((cortex / "command").glob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertIn("F", notes[0].read_text(encoding="utf-8"))
            # Auto-indexed into llmLibrarian after a fresh download.
            self._last_index_mock.assert_called_once()
            self.assertTrue(rec["indexed"])

    def test_idempotent_second_run(self):
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            with mock.patch.object(hsf, "fetch_10k", return_value=OK_RESULT) as m:
                self._run(NOTE, state, cortex)
                self._run(NOTE, state, cortex)
                self.assertEqual(m.call_count, 1)  # not re-fetched

    def test_disabled_mode(self):
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            with mock.patch.dict(os.environ, {"INTENT_COMMANDS_MODE": "off"}), \
                 mock.patch.object(hsf, "fetch_10k") as m:
                exit_code, gate_text, summary, src = self._run(NOTE, state, cortex)
                m.assert_not_called()
            self.assertEqual(gate_text, NOTE)
            self.assertEqual(summary.commands_executed, 0)

    def test_no_command_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            plain = "Just a normal journal entry with no directives."
            with mock.patch.object(hsf, "fetch_10k") as m:
                exit_code, gate_text, summary, src = self._run(plain, state, cortex)
                m.assert_not_called()
            self.assertEqual(gate_text, plain)

    def test_autoindex_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-04.md"; src.write_text(NOTE, encoding="utf-8")
            with mock.patch.dict(os.environ, {"INTENT_HOTSEAT_AUTOINDEX": "off"}), \
                    mock.patch.object(pi, "_push_command_notification"), \
                    mock.patch.object(hsf, "fetch_10k", return_value=OK_RESULT), \
                    mock.patch.object(pi, "index_hot_seat_dir") as idx:
                pi.run_command_stage(NOTE, src, "2026-07-04", cortex, state,
                                     pi.RunSummary(), dry_run=False, verbose=False)
                idx.assert_not_called()

    def test_no_reindex_when_already_present(self):
        already = dict(OK_RESULT, downloaded=False)
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-04.md"; src.write_text(NOTE, encoding="utf-8")
            with mock.patch.object(pi, "_push_command_notification"), \
                    mock.patch.object(hsf, "fetch_10k", return_value=already), \
                    mock.patch.object(pi, "index_hot_seat_dir") as idx:
                pi.run_command_stage(NOTE, src, "2026-07-04", cortex, state,
                                     pi.RunSummary(), dry_run=False, verbose=False)
                idx.assert_not_called()

    def test_autoindex_enabled_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INTENT_HOTSEAT_AUTOINDEX", None)
            self.assertTrue(pi.hot_seat_autoindex_enabled())
        with mock.patch.dict(os.environ, {"INTENT_HOTSEAT_AUTOINDEX": "0"}):
            self.assertFalse(pi.hot_seat_autoindex_enabled())

    def test_failed_download_marks_transient(self):
        fail = {"ok": False, "stage": "download", "error": "boom",
                "query": "ford", "form": "10-K"}
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            with mock.patch.object(hsf, "fetch_10k", return_value=fail):
                exit_code, gate_text, summary, src = self._run(NOTE, state, cortex)
            self.assertEqual(exit_code, pi.EXIT_DELIVERY_TRANSIENT)
            self.assertEqual(summary.commands_failed, 1)


class TestVoiceAnomalies(unittest.TestCase):
    """Real reported bug + real evidence from the user's own journal notes."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("INTENT_COMMANDS_MODE", None)
        os.environ.pop("SCRIBE_PUSHOVER_APP_TOKEN", None)
        os.environ.pop("SCRIBE_PUSHOVER_USER_KEY", None)
        os.environ.pop("PUSHOVER_TOKEN", None)
        os.environ.pop("PUSHOVER_KEY", None)

    def tearDown(self):
        self._env.stop()

    def test_past_tense_no_company_produces_no_note_but_logs_and_notifies(self):
        # The exact reported bug: "Palindrome pulled the 10K" (no company).
        note = "Palindrome pulled the 10K"
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-06.md"
            src.write_text(note, encoding="utf-8")
            with mock.patch.object(hsf, "fetch_10k") as fetch_mock, \
                    mock.patch.object(pi, "_push_command_notification") as push_mock:
                summary = pi.RunSummary()
                exit_code, gate_text = pi.run_command_stage(
                    note, src, "2026-07-06", cortex, state, summary,
                    dry_run=False, verbose=False,
                )
                fetch_mock.assert_not_called()
                push_mock.assert_called_once()
            self.assertEqual(summary.commands_executed, 0)
            self.assertEqual(summary.commands_failed, 0)
            self.assertEqual(list((cortex).glob("**/*.md")), [])
            log_path = state / pi.VOICE_ANOMALY_LOG_FILENAME
            self.assertTrue(log_path.exists())
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = __import__("json").loads(lines[0])
            self.assertEqual(record["tag"], "artifact_verb")
            self.assertEqual(record["corrected_term"], "pull")

    def test_pole_no_form_produces_no_note_but_logs_and_notifies(self):
        # Real evidence (2026-07-04.md): Whisper misheard "pull" as "pole".
        note = "Palindrome, pole ticker APH and add it to the hot seat."
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-04.md"
            src.write_text(note, encoding="utf-8")
            with mock.patch.object(hsf, "fetch_10k") as fetch_mock, \
                    mock.patch.object(pi, "_push_command_notification") as push_mock:
                summary = pi.RunSummary()
                pi.run_command_stage(
                    note, src, "2026-07-04", cortex, state, summary,
                    dry_run=False, verbose=False,
                )
                fetch_mock.assert_not_called()
                push_mock.assert_called_once()
            log_path = state / pi.VOICE_ANOMALY_LOG_FILENAME
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = __import__("json").loads(lines[0])
            self.assertEqual(record["reason"], "no_form")
            self.assertEqual(record["tag"], "artifact_verb")

    def test_working_command_with_ticker_word_still_executes(self):
        # The real working utterance: "pull the 10k for ticker [[ocm]]".
        note = "[[Palindrome]], pull the 10k for ticker [[ocm]]"
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-06.md"
            src.write_text(note, encoding="utf-8")
            with mock.patch.object(hsf, "fetch_10k", return_value=OK_RESULT) as fetch_mock, \
                    mock.patch.object(pi, "_push_command_notification"), \
                    mock.patch.object(pi, "index_hot_seat_dir", return_value={"ok": True}):
                summary = pi.RunSummary()
                pi.run_command_stage(
                    note, src, "2026-07-06", cortex, state, summary,
                    dry_run=False, verbose=False,
                )
                fetch_mock.assert_called_once()
                self.assertEqual(fetch_mock.call_args.args[0], "ocm")
            self.assertEqual(summary.commands_executed, 1)
            # No anomaly for a clean canonical-verb command with no artifacts.
            log_path = state / pi.VOICE_ANOMALY_LOG_FILENAME
            self.assertFalse(log_path.exists())

    def test_spelled_out_letters_execute_and_get_flagged(self):
        # Auto-fetch AND flag, per the approved plan.
        note = "Palindrome, pull the 10-K for ticker O-C-M."
        with tempfile.TemporaryDirectory() as td:
            state, cortex = Path(td) / "state", Path(td) / "cortex"
            state.mkdir(); cortex.mkdir()
            src = state / "2026-07-06.md"
            src.write_text(note, encoding="utf-8")
            with mock.patch.object(hsf, "fetch_10k", return_value=OK_RESULT) as fetch_mock, \
                    mock.patch.object(pi, "_push_command_notification") as push_mock, \
                    mock.patch.object(pi, "index_hot_seat_dir", return_value={"ok": True}):
                summary = pi.RunSummary()
                pi.run_command_stage(
                    note, src, "2026-07-06", cortex, state, summary,
                    dry_run=False, verbose=False,
                )
                fetch_mock.assert_called_once()
                self.assertEqual(fetch_mock.call_args.args[0], "ocm")
            self.assertEqual(summary.commands_executed, 1)
            # Two Pushover calls: one anomaly ping, one normal success ping.
            self.assertEqual(push_mock.call_count, 2)
            log_path = state / pi.VOICE_ANOMALY_LOG_FILENAME
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = __import__("json").loads(lines[0])
            self.assertEqual(record["tag"], "spelled_out_letters")
            self.assertEqual(record["corrected_term"], "OCM")


if __name__ == "__main__":
    unittest.main()
