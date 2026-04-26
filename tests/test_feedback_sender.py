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

    def _update(self, action, key="a" * 64, include_message=False, message_id=123):
        callback_query = {
            "id": "callback-1",
            "data": f"{action}:{key[:16]}",
        }
        if include_message:
            callback_query["message"] = {
                "message_id": message_id,
                "chat": {"id": 456},
            }
        return {
            "update_id": 1,
            "callback_query": callback_query,
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

    def test_confirm_removes_keyboard_and_writes_tap_trace(self):
        key = "a" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch.object(fs, "answer_callback") as answer_mock,
                mock.patch.object(fs, "edit_message_reply_markup") as edit_mock,
            ):
                fs.process_callback_updates(
                    [self._update("confirm", key, include_message=True)],
                    entries,
                    ledger,
                    "token",
                    state_dir=state_dir,
                )
            traces = [
                json.loads(line)
                for line in (state_dir / fs.FEEDBACK_TAP_TRACE_FILENAME).read_text().splitlines()
            ]

        edit_mock.assert_called_once_with("token", "456", 123)
        answer_mock.assert_called_once_with("token", "callback-1", "✓ Noted")
        self.assertEqual(traces[0]["claude_idempotency_key"], key)
        self.assertTrue(traces[0]["accepted"])
        self.assertEqual(traces[0]["resulting_signal"], "confirmed")

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
        patterns = learning["suppressed"]["purchase_intent"]["patterns"]
        self.assertIn("call marcus", patterns)
        self.assertEqual(patterns["call marcus"]["count"], 1)
        self.assertEqual(patterns["call marcus"]["sample_phrase"], "call Marcus")

    def test_second_tap_is_duplicate_and_does_not_change_learning(self):
        key = "b" * 64
        entry = self._entry(key)
        entry["intent_class"] = "purchase_intent"
        entries = [entry]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch.object(fs, "answer_callback") as answer_mock,
                mock.patch.object(fs, "edit_message_reply_markup"),
            ):
                fs.process_callback_updates(
                    [
                        self._update("confirm", key, include_message=True),
                        self._update("reject", key, include_message=True),
                    ],
                    entries,
                    ledger,
                    "token",
                    state_dir=state_dir,
                )
            learning = fs.load_feedback_learning(state_dir)
            traces = [
                json.loads(line)
                for line in (state_dir / fs.FEEDBACK_TAP_TRACE_FILENAME).read_text().splitlines()
            ]

        self.assertEqual(entries[0]["state"], "responded")
        self.assertEqual(entries[0]["feedback_signal"], "confirmed")
        self.assertEqual(ledger[key]["feedback_signal"], "confirmed")
        self.assertEqual(learning["approved"]["purchase_intent"]["count"], 1)
        self.assertEqual(learning["suppressed"], {})
        self.assertEqual(answer_mock.call_args_list[1].args[2], fs.DUPLICATE_ACKS[2])
        self.assertFalse(traces[1]["accepted"])
        self.assertEqual(traces[1]["resulting_signal"], "confirmed")

    def test_third_total_tap_uses_special_acknowledgement(self):
        key = "e" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch.object(fs, "answer_callback") as answer_mock,
                mock.patch.object(fs, "edit_message_reply_markup"),
            ):
                fs.process_callback_updates(
                    [
                        self._update("confirm", key, include_message=True),
                        self._update("reject", key, include_message=True),
                        self._update("defer", key, include_message=True),
                    ],
                    entries,
                    ledger,
                    "token",
                    state_dir=state_dir,
                )

        self.assertEqual(answer_mock.call_args_list[2].args[2], fs.THIRD_TAP_ACK)
        self.assertEqual(entries[0]["feedback_signal"], "confirmed")

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

    def test_defer_removes_old_keyboard_and_writes_tap_trace(self):
        key = "c" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch.object(fs, "answer_callback"),
                mock.patch.object(fs, "edit_message_reply_markup") as edit_mock,
            ):
                fs.process_callback_updates(
                    [self._update("defer", key, include_message=True)],
                    entries,
                    ledger,
                    "token",
                    state_dir=state_dir,
                )
            trace = json.loads((state_dir / fs.FEEDBACK_TAP_TRACE_FILENAME).read_text().splitlines()[0])

        edit_mock.assert_called_once_with("token", "456", 123)
        self.assertEqual(entries[0]["state"], "pending")
        self.assertEqual(trace["resulting_signal"], "deferred")
        self.assertTrue(trace["accepted"])

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

    def test_keyboard_removal_failure_does_not_block_callback(self):
        key = "f" * 64
        entries = [self._entry(key)]
        ledger = {key: {"claude_idempotency_key": key}}
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(fs, "answer_callback") as answer_mock,
                mock.patch.object(fs, "edit_message_reply_markup", side_effect=RuntimeError("boom")),
            ):
                fs.process_callback_updates(
                    [self._update("confirm", key, include_message=True)],
                    entries,
                    ledger,
                    "token",
                    state_dir=Path(tmpdir),
                )

        self.assertEqual(entries[0]["feedback_signal"], "confirmed")
        answer_mock.assert_called_once_with("token", "callback-1", "✓ Noted")


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
                mock.patch.object(fs, "send_due_messages", side_effect=lambda entries, token, chat, **kwargs: entries),
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


class TestFeedbackSenderMessages(unittest.TestCase):
    def test_freeform_text_routes_to_latest_unanswered_checkin(self):
        old = TestFeedbackSenderCallbacks()._entry("l" * 64)
        old["feedback_signal"] = "confirmed"
        old["telegram_message_id"] = 100
        old["sent_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        latest = TestFeedbackSenderCallbacks()._entry("m" * 64)
        latest["telegram_message_id"] = 200
        latest["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        update = {
            "update_id": 9,
            "message": {
                "text": "I meant beach shop",
                "chat": {"id": "chat"},
            },
        }
        with mock.patch.object(fs, "_apply_clarification") as apply_mock:
            fs.process_message_updates([update], [old, latest], "token", "chat")

        apply_mock.assert_called_once_with(latest, "I meant beach shop", "token", "chat")

    def test_freeform_text_with_multiple_unanswered_prompts_disambiguation(self):
        first = TestFeedbackSenderCallbacks()._entry("l" * 64)
        first["telegram_message_id"] = 100
        first["sent_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        second = TestFeedbackSenderCallbacks()._entry("m" * 64)
        second["telegram_message_id"] = 200
        second["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        update = {
            "update_id": 10,
            "message": {
                "text": "I meant beach shop",
                "chat": {"id": "chat"},
            },
        }
        with (
            mock.patch.object(fs, "_apply_clarification") as apply_mock,
            mock.patch.object(fs, "_send_plain") as send_plain_mock,
        ):
            fs.process_message_updates([update], [first, second], "token", "chat")

        apply_mock.assert_not_called()
        send_plain_mock.assert_called_once()
        self.assertIn("multiple active check-ins", send_plain_mock.call_args.args[2])


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
            with (
                mock.patch.object(fs, "_in_quiet_hours", return_value=False),
                mock.patch.object(
                    fs,
                    "send_message",
                    return_value={"result": {"message_id": 99}},
                ) as send_mock,
            ):
                fs.send_due_messages(entries, "token", "chat")

        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(entries[0]["state"], "sent")
        self.assertEqual(entries[1]["state"], "pending")
        self.assertIn("sent_at", entries[0])

    def test_unanswered_sent_entries_expire_after_silence_window(self):
        old_sent_at = datetime.now(timezone.utc) - timedelta(hours=9)
        key = "i" * 64
        entry = self._pending(key, "/tmp/2026-04-16.md")
        entry.update({
            "state": "sent",
            "telegram_message_id": 321,
            "telegram_chat_id": "chat",
            "sent_at": old_sent_at.isoformat(timespec="seconds"),
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            fs.save_ledger(state_dir, {key: {"claude_idempotency_key": key}})
            with (
                mock.patch.dict(os.environ, {"INTENT_FEEDBACK_SILENCE_EXPIRE_TASK_HOURS": "8"}),
                mock.patch.object(fs, "edit_message_reply_markup") as edit_mock,
            ):
                fs.send_due_messages([entry], "token", "chat", state_dir=state_dir)
            ledger = fs.load_ledger(state_dir)

        self.assertEqual(entry["state"], "expired")
        self.assertEqual(entry["feedback_signal"], "expired_silence")
        self.assertEqual(ledger[key]["feedback_signal"], "expired_silence")
        edit_mock.assert_called_once_with("token", "chat", 321)

    def test_unanswered_cap_delays_new_due_messages(self):
        recent_sent_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        sent = self._pending("j" * 64, "/tmp/2026-04-16.md")
        sent.update({
            "state": "sent",
            "sent_at": recent_sent_at.isoformat(timespec="seconds"),
            "telegram_message_id": 123,
        })
        pending = self._pending("k" * 64, "/tmp/2026-04-16.md")
        with (
            mock.patch.dict(os.environ, {"INTENT_FEEDBACK_MAX_UNANSWERED": "1"}),
            mock.patch.object(fs, "send_message") as send_mock,
        ):
            fs.send_due_messages([sent, pending], "token", "chat")

        send_mock.assert_not_called()
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["backpressure_reason"], "too_many_unanswered")
        self.assertEqual(pending["backpressure_count"], 1)

    def test_message_text_is_prompt_with_natural_title_footer(self):
        entry = self._pending("g" * 64, "/tmp/2026-04-16.md")
        entry["feedback_prompt"] = "Did you pick up the smoothie for Jill?"
        entry["title"] = "Pick up smoothie for Jill"
        entry["category"] = "task"
        entry["intent_class"] = "task_intent"
        with (
            mock.patch.object(fs, "_in_quiet_hours", return_value=False),
            mock.patch.object(
                fs,
                "send_message",
                return_value={"result": {"message_id": 99}},
            ) as send_mock,
        ):
            fs.send_due_messages([entry], "token", "chat")

        text = send_mock.call_args.args[2]
        self.assertNotIn("Intent check-in", text)
        self.assertNotIn("<i>", text)
        self.assertEqual(text, "Did you pick up the smoothie for Jill?")

    def test_message_text_html_escapes_user_content(self):
        entry = self._pending("h" * 64, "/tmp/2026-04-16.md")
        entry["feedback_prompt"] = "Did you pay Jill & Marcus?"
        entry["title"] = "Pay <Jill>"
        entry["category"] = "task > money"
        with (
            mock.patch.object(fs, "_in_quiet_hours", return_value=False),
            mock.patch.object(
                fs,
                "send_message",
                return_value={"result": {"message_id": 99}},
            ) as send_mock,
        ):
            fs.send_due_messages([entry], "token", "chat")

        text = send_mock.call_args.args[2]
        self.assertNotIn("<i>", text)
        self.assertEqual(text, "Did you pay Jill &amp; Marcus?")


if __name__ == "__main__":
    unittest.main()
