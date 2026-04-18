import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "feedback_sender.py"
spec = importlib.util.spec_from_file_location("feedback_sender", SCRIPT_PATH)
fs = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(fs)


class TestFeedbackSenderCallbacks(unittest.TestCase):
    def _entry(self, key="a" * 64):
        return {
            "claude_idempotency_key": key,
            "feedback_prompt": "Did you call Marcus?",
            "title": "Call Marcus",
            "intent_raw": "call Marcus",
            "urgency": "today",
            "format": "note",
            "action": "notification",
            "category": "task",
            "intent_class": "task_intent",
            "source_file": "/tmp/2026-04-16.md",
            "captured_at": "2026-04-16T10:00:00+00:00",
            "send_after": "2026-04-16T16:00:00+00:00",
            "timing_policy": "later_today",
            "state": "sent",
            "telegram_message_id": 123,
            "feedback_signal": None,
            "defer_count": 0,
            "expires_at": "",
        }

    def _update(self, action, key="a" * 64):
        return {
            "update_id": 1,
            "callback_query": {
                "id": "callback-1",
                "data": f"{action}:{key[:16]}",
            },
        }

    def test_confirm_records_approval_learning(self):
        key = "a" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with mock.patch.object(fs, "answer_callback"):
                entries, ledger = fs.process_callback_updates(
                    [self._update("confirm", key)], entries, ledger, "token", state_dir=state_dir
                )
            learning = fs.load_feedback_learning(state_dir)

        self.assertEqual(entries[0]["state"], "responded")
        self.assertEqual(entries[0]["feedback_signal"], "confirmed")
        self.assertEqual(ledger[key]["feedback_signal"], "confirmed")
        self.assertEqual(learning["approved"]["task_intent"]["count"], 1)

    def test_reject_records_suppression_learning(self):
        key = "b" * 64
        entry = self._entry(key)
        entry["intent_class"] = "purchase_intent"
        entries = [entry]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with mock.patch.object(fs, "answer_callback"):
                fs.process_callback_updates(
                    [self._update("reject", key)], entries, ledger, "token", state_dir=state_dir
                )
            learning = fs.load_feedback_learning(state_dir)

        self.assertEqual(learning["suppressed"]["purchase_intent"]["count"], 1)
        self.assertIn("Did you call Marcus?", learning["suppressed"]["purchase_intent"]["phrases"])

    def test_defer_increments_count_and_requeues(self):
        key = "c" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"INTENT_FEEDBACK_DEFER_LIMIT": "3"}):
                with mock.patch.object(fs, "answer_callback"):
                    fs.process_callback_updates(
                        [self._update("defer", key)], entries, ledger, "token", state_dir=Path(tmpdir)
                    )

        self.assertEqual(entries[0]["state"], "pending")
        self.assertEqual(entries[0]["feedback_signal"], "deferred")
        self.assertEqual(entries[0]["defer_count"], 1)
        self.assertEqual(ledger[key]["feedback_signal"], "deferred")

    def test_defer_expires_after_cap(self):
        key = "d" * 64
        entry = self._entry(key)
        entry["defer_count"] = 1
        entries = [entry]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"INTENT_FEEDBACK_DEFER_LIMIT": "1"}):
                with mock.patch.object(fs, "answer_callback"):
                    fs.process_callback_updates(
                        [self._update("defer", key)], entries, ledger, "token", state_dir=Path(tmpdir)
                    )

        self.assertEqual(entries[0]["state"], "expired")
        self.assertEqual(entries[0]["feedback_signal"], "expired_defer_limit")
        self.assertEqual(ledger[key]["feedback_signal"], "expired_defer_limit")


class TestFeedbackSenderLongPolling(unittest.TestCase):
    def test_get_updates_passes_long_poll_timeout(self):
        with mock.patch.object(fs, "_tg_request", return_value={"result": []}) as req_mock:
            got = fs.get_updates("token", offset=42, timeout=25)

        self.assertEqual(got, [])
        args, kwargs = req_mock.call_args
        self.assertEqual(args[1], "getUpdates")
        self.assertEqual(args[2]["offset"], 42)
        self.assertEqual(args[2]["timeout"], 25)
        self.assertEqual(kwargs["request_timeout"], 35)

    def test_run_daemon_processes_update_then_exits_on_interrupt(self):
        key = "a" * 64
        update = {
            "update_id": 7,
            "callback_query": {
                "id": "callback-1",
                "data": f"confirm:{key[:16]}",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            fs.save_feedback_queue(state_dir, [TestFeedbackSenderCallbacks()._entry(key)])
            fs.save_ledger(state_dir, {key: {"claude_idempotency_key": key}})
            with (
                mock.patch.object(fs, "get_updates", side_effect=[[update], KeyboardInterrupt]),
                mock.patch.object(fs, "answer_callback"),
                mock.patch.object(fs, "send_due_messages", side_effect=lambda entries, token, chat: entries),
            ):
                exit_code = fs.run_daemon(
                    state_dir,
                    "token",
                    "chat",
                    poll_timeout=1,
                    send_interval=1,
                )
            ledger = fs.load_ledger(state_dir)
            offset = fs.load_offset(state_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(ledger[key]["feedback_signal"], "confirmed")
        self.assertEqual(offset, 8)


class TestFeedbackSenderSendCaps(unittest.TestCase):
    def _pending(self, key, source_file):
        due = datetime.now(timezone.utc) - timedelta(seconds=60)
        return {
            "claude_idempotency_key": key,
            "feedback_prompt": "Did you do it?",
            "title": "Intent",
            "category": "task",
            "intent_class": "task_intent",
            "source_file": source_file,
            "send_after": due.isoformat(timespec="seconds"),
            "state": "pending",
        }

    def test_multiple_due_entries_from_same_source_are_capped(self):
        entries = [
            self._pending("e" * 64, "/tmp/2026-04-16.md"),
            self._pending("f" * 64, "/tmp/2026-04-16.md"),
        ]
        with mock.patch.dict(os.environ, {"INTENT_FEEDBACK_MAX_PER_SOURCE_PER_RUN": "1"}):
            with mock.patch.object(
                fs,
                "send_message",
                return_value={"result": {"message_id": 99}},
            ) as send_mock:
                fs.send_due_messages(entries, "token", "chat")

        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(entries[0]["state"], "sent")
        self.assertEqual(entries[1]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
