"""Tests for scripts/voice_anomaly_report.py — confusion report over the
voice anomaly log. Pure aggregation; filesystem uses tempdirs."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCRIPT_PATH = SCRIPTS_DIR / "voice_anomaly_report.py"
spec = importlib.util.spec_from_file_location("voice_anomaly_report", SCRIPT_PATH)
var = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(var)


RECORDS = [
    {"timestamp": "2026-07-01T09:00:00", "tag": "artifact_verb", "verb_form": "pulled",
     "corrected_term": "pull", "route": "hot_seat_fetch"},
    {"timestamp": "2026-07-02T09:00:00", "tag": "artifact_verb", "verb_form": "pole",
     "corrected_term": "pull", "route": "hot_seat_fetch"},
    {"timestamp": "2026-07-03T09:00:00", "tag": "low_confidence", "verb_form": None,
     "corrected_term": "OMC", "route": "hot_seat_fetch", "confidence": 0.5},
    {"timestamp": "2026-07-04T09:00:00", "tag": "incomplete_command", "verb_form": "watch",
     "corrected_term": "", "route": "watchlist_add"},
    {"timestamp": "2026-07-05T09:00:00", "tag": "spelled_out_letters", "verb_form": None,
     "corrected_term": "OCM"},  # no route key -> "unknown"
]


class TestSummarize(unittest.TestCase):
    def _write_log(self, td, lines):
        path = Path(td) / "voice_anomalies.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_counts_by_tag_route_and_term(self):
        summary = var.summarize(RECORDS)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["by_tag"]["artifact_verb"], 2)
        self.assertEqual(summary["by_tag"]["low_confidence"], 1)
        self.assertEqual(summary["by_route"]["hot_seat_fetch"], 3)
        self.assertEqual(summary["by_route"]["watchlist_add"], 1)
        self.assertEqual(summary["by_route"]["unknown"], 1)  # missing route key
        self.assertEqual(summary["by_verb_form"]["pulled"], 1)
        self.assertEqual(summary["by_corrected_term"]["pull"], 2)  # most-recurring
        self.assertEqual(summary["oldest"], "2026-07-01T09:00:00")
        self.assertEqual(summary["newest"], "2026-07-05T09:00:00")

    def test_load_skips_blank_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            log = self._write_log(td, [
                json.dumps(RECORDS[0]),
                "",
                "{not valid json",
                "   ",
                json.dumps(RECORDS[2]),
                json.dumps(["not", "a", "dict"]),
            ])
            records = var.load_records(log)
            self.assertEqual(len(records), 2)
            self.assertEqual({r["tag"] for r in records},
                             {"artifact_verb", "low_confidence"})

    def test_main_json_output(self):
        with tempfile.TemporaryDirectory() as td:
            log = self._write_log(td, [json.dumps(r) for r in RECORDS])
            buf = []
            with mock.patch.object(var, "print", side_effect=buf.append, create=True):
                rc = var.main(["--log", str(log), "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf[0])
            self.assertEqual(payload["total"], 5)
            self.assertEqual(payload["by_tag"]["artifact_verb"], 2)

    def test_main_missing_log_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.jsonl"
            buf = []
            with mock.patch.object(var, "print", side_effect=buf.append, create=True):
                rc = var.main(["--log", str(missing)])
            self.assertEqual(rc, 0)
            self.assertIn("No anomaly log", buf[0])


if __name__ == "__main__":
    unittest.main()
