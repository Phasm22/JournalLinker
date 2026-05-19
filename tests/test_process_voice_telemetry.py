import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_voice.py"
spec = importlib.util.spec_from_file_location("process_voice", SCRIPT_PATH)
process_voice = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(process_voice)


class TestProcessVoiceTelemetry(unittest.TestCase):
    def test_write_voice_payload_records_batch_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_file = Path(tmp) / "payload.json"
            with mock.patch.dict(
                os.environ,
                {"JOURNAL_LINKER_JOB_PAYLOAD_FILE": str(payload_file)},
                clear=False,
            ):
                process_voice._write_voice_payload(
                    items_processed=2,
                    items_failed=1,
                    items_skipped_placeholder=0,
                    items_skipped_already=3,
                    items_pending_at_start=3,
                )
            data = json.loads(payload_file.read_text(encoding="utf-8"))
            self.assertEqual(data["items_processed"], 2)
            self.assertEqual(data["items_failed"], 1)
            self.assertEqual(data["items_pending_at_start"], 3)


if __name__ == "__main__":
    unittest.main()
