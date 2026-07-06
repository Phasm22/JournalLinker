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


if __name__ == "__main__":
    unittest.main()
