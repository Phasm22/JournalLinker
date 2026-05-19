import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "journal_linker_telemetry.py"
spec = importlib.util.spec_from_file_location("journal_linker_telemetry", SCRIPT_PATH)
telemetry = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(telemetry)


class TestJournalLinkerTelemetry(unittest.TestCase):
    def test_finalize_merges_payload_and_prints_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            telemetry.write_job_payload(payload, outcome="sent", reason="sent")
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf):
                telemetry.finalize_job_event(
                    service="daily-reflection",
                    run_id="20260519-120000-1",
                    exit_code=0,
                    duration_sec=1.5,
                    payload_file=payload,
                )
            line = buf.getvalue().strip()
            self.assertTrue(line.startswith(telemetry.EVENT_PREFIX))
            event = telemetry.parse_event_line(line)
            assert event is not None
            self.assertEqual(event["event"], "job.completed")
            self.assertEqual(event["service"], "daily-reflection")
            self.assertEqual(event["run_id"], "20260519-120000-1")
            self.assertEqual(event["exit_code"], 0)
            self.assertEqual(event["duration_sec"], 1.5)
            self.assertEqual(event["outcome"], "sent")
            self.assertFalse(payload.exists())

    def test_finalize_without_payload_file(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            telemetry.finalize_job_event(
                service="scribe",
                run_id="run-1",
                exit_code=1,
                duration_sec=3,
                payload_file=None,
                skipped=True,
                skip_reason="lock_held",
            )
        event = telemetry.parse_event_line(buf.getvalue().strip())
        assert event is not None
        self.assertTrue(event["skipped"])
        self.assertEqual(event["skip_reason"], "lock_held")
        self.assertEqual(event["exit_code"], 1)

    def test_emit_health_probe(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            telemetry.emit_health_probe(
                "feedback-sender",
                uptime_sec=120,
                poll_offset=5,
                updates_last_cycle=0,
                feedback_queue_pending=2,
            )
        event = telemetry.parse_event_line(buf.getvalue().strip())
        assert event is not None
        self.assertEqual(event["event"], "health.probe")
        self.assertEqual(event["service"], "feedback-sender")
        self.assertEqual(event["uptime_sec"], 120)
        self.assertIn("ts", event)

    def test_cli_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "p.json"
            telemetry.write_job_payload(payload, items_processed=2)
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf):
                code = telemetry.main(
                    [
                        "finalize",
                        "--service",
                        "voice",
                        "--run-id",
                        "r1",
                        "--exit-code",
                        "0",
                        "--duration-sec",
                        "9",
                        "--payload-file",
                        str(payload),
                    ]
                )
            self.assertEqual(code, 0)
            event = telemetry.parse_event_line(buf.getvalue().strip())
            assert event is not None
            self.assertEqual(event["items_processed"], 2)


if __name__ == "__main__":
    unittest.main()
