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

    def test_emit_health_probe_is_deprecated(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf), self.assertWarns(DeprecationWarning):
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

    def _events(self, buf: io.StringIO) -> list[dict]:
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        events = [telemetry.parse_event_line(line) for line in lines]
        self.assertTrue(all(e is not None for e in events), lines)
        return events  # type: ignore[return-value]

    def test_emit_service_state(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            telemetry.emit_service_state("voice-ingest", "ready", "listening")
            telemetry.emit_service_state("voice-ingest", "degraded", "write failed", queued=2)
        first, second = self._events(buf)
        self.assertEqual(first["event"], "service.state")
        self.assertEqual(first["service"], "voice-ingest")
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["reason"], "listening")
        self.assertIn("ts", first)
        self.assertNotIn("metrics", first)
        self.assertEqual(second["metrics"], {"queued": 2})

    def test_emit_service_state_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            telemetry.emit_service_state("svc", "fine", "whatever")

    def test_health_state_emits_only_on_transitions(self):
        health = telemetry.HealthState("feedback-sender")
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            health.set("ready", "polling")
            health.set("degraded", "poll failed: URLError")
            health.set("degraded", "poll failed: URLError")
            health.set("recovered", "polling")
        events = self._events(buf)
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [(e["event"], e["status"], e["reason"]) for e in events],
            [
                ("service.state", "ready", "polling"),
                ("service.state", "degraded", "poll failed: URLError"),
                ("service.state", "recovered", "polling"),
            ],
        )

    def test_health_state_new_reason_is_a_transition(self):
        health = telemetry.HealthState("svc")
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            health.set("degraded", "poll failed: URLError")
            health.set("degraded", "poll failed: TimeoutError")
        self.assertEqual(len(self._events(buf)), 2)

    def test_health_state_ok_maps_success_to_ready_or_recovered(self):
        health = telemetry.HealthState("svc")
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            self.assertTrue(health.ok("polling"))
            for _ in range(5):
                self.assertFalse(health.ok("polling"))
            health.set("degraded", "poll failed: URLError")
            self.assertTrue(health.ok("polling"))
            self.assertFalse(health.ok("polling"))
        self.assertEqual(
            [e["status"] for e in self._events(buf)], ["ready", "degraded", "recovered"]
        )

    def test_emit_usage_event(self):
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            telemetry.emit_usage_event(
                "usage.journal.entry_saved",
                {"date": "2026-07-08", "mode": "edit"},
            )
        event = telemetry.parse_event_line(buf.getvalue().strip())
        assert event is not None
        self.assertEqual(event["event"], "usage.journal.entry_saved")
        self.assertEqual(event["metrics"], {"date": "2026-07-08", "mode": "edit"})
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
