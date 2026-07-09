"""Tests for scripts/process_voice.py — Whisper vocabulary-bias prompt.

Focused on extract_whisper_prompt()'s guaranteed-vocab merge: the wake
word(s) and previously-logged voice-anomaly corrections should always bias
the prompt, independent of scribe_learning.json success counts.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_voice.py"
spec = importlib.util.spec_from_file_location("process_voice", SCRIPT_PATH)
pv = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pv)


class TestExtractWhisperPrompt(unittest.TestCase):
    def test_ranked_terms_from_learning_file(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06", guaranteed_terms=[],
            )
            self.assertIn("Ford", prompt)

    def test_missing_learning_file_still_returns_guaranteed_terms(self):
        # Regression: the old implementation returned "" immediately if the
        # learning file was missing, which would have silently dropped
        # guaranteed terms (wake word, corrections) too.
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist.json"
            prompt = pv.extract_whisper_prompt(
                missing, guaranteed_terms=["Palindrome", "OMC"],
            )
            self.assertIn("Palindrome", prompt)
            self.assertIn("OMC", prompt)

    def test_learning_file_with_no_term_memory_still_returns_guaranteed_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({"runs": {}}), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, guaranteed_terms=["Palindrome"],
            )
            self.assertIn("Palindrome", prompt)

    def test_guaranteed_terms_come_before_ranked_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06",
                guaranteed_terms=["Palindrome"],
            )
            self.assertLess(prompt.index("Palindrome"), prompt.index("Ford"))

    def test_dedupes_guaranteed_and_ranked_terms(self):
        with tempfile.TemporaryDirectory() as td:
            learning_file = Path(td) / "scribe_learning.json"
            learning_file.write_text(json.dumps({
                "term_memory": {
                    "ford": {"term": "Ford", "success_count": 3,
                              "last_success_date": "2026-07-01"},
                }
            }), encoding="utf-8")
            prompt = pv.extract_whisper_prompt(
                learning_file, reference_date="2026-07-06",
                guaranteed_terms=["Ford"],
            )
            self.assertEqual(prompt.count("Ford"), 1)

    def test_default_guaranteed_terms_include_wake_word_and_logged_corrections(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            (state_dir / pv.VOICE_ANOMALY_LOG_FILENAME).write_text(
                json.dumps({"corrected_term": "OMC"}) + "\n", encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"INTENT_STATE_DIR": str(state_dir)}):
                learning_file = Path(td) / "does_not_exist.json"
                prompt = pv.extract_whisper_prompt(learning_file)
            self.assertIn("Palindrome", prompt)
            self.assertIn("OMC", prompt)

    def test_empty_everything_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist.json"
            prompt = pv.extract_whisper_prompt(missing, guaranteed_terms=[])
            self.assertEqual(prompt, "")


class TestLoadAnomalyCorrectedTerms(unittest.TestCase):
    def test_reads_distinct_corrected_terms_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            log = state_dir / pv.VOICE_ANOMALY_LOG_FILENAME
            log.write_text(
                "\n".join([
                    json.dumps({"corrected_term": "OMC"}),
                    json.dumps({"corrected_term": "pull"}),
                    json.dumps({"corrected_term": "OMC"}),  # duplicate, dropped
                    json.dumps({"corrected_term": ""}),  # empty, dropped
                ]) + "\n",
                encoding="utf-8",
            )
            terms = pv._load_anomaly_corrected_terms(state_dir)
            self.assertEqual(terms, ["OMC", "pull"])

    def test_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(pv._load_anomaly_corrected_terms(Path(td)), [])


try:
    import av  # noqa: F401
    import numpy as np
    _HAVE_AV = True
except Exception:  # pragma: no cover - av is a runtime dep, present in ScribeVenv
    _HAVE_AV = False


def _synth_m4a(path: Path, seconds: float = 0.6, sr: int = 16000) -> None:
    """Write a small valid silent AAC/m4a file for probe fixtures."""
    container = av.open(str(path), mode="w")
    stream = container.add_stream("aac", rate=sr)
    stream.layout = "mono"
    frame = av.AudioFrame.from_ndarray(
        np.zeros((1, int(seconds * sr))).astype("float32"), format="fltp", layout="mono"
    )
    frame.sample_rate = sr
    frame.pts = 0
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()


@unittest.skipUnless(_HAVE_AV, "PyAV not available")
class TestProbeDecodableIntegration(unittest.TestCase):
    """Real decode against synthesized fixtures (valid + truncations)."""

    def test_valid_file_passes(self):
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.m4a"
            _synth_m4a(good)
            ok, kind, reason = pv.probe_decodable(good)
            self.assertTrue(ok, reason)
            self.assertEqual(kind, "")

    def test_corrupt_bytes_are_permanent(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.m4a"
            bad.write_bytes(b"this is not a media container" * 4)
            ok, kind, _ = pv.probe_decodable(bad)
            self.assertFalse(ok)
            self.assertEqual(kind, pv.FAIL_PERMANENT)

    def test_empty_file_is_permanent(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty.m4a"
            empty.write_bytes(b"")
            ok, kind, _ = pv.probe_decodable(empty)
            self.assertFalse(ok)
            self.assertEqual(kind, pv.FAIL_PERMANENT)

    def test_truncated_container_is_permanent(self):
        # A container truncated hard enough to lose its audio stream / hit EOF is
        # deterministically bad -> permanent, not an endless transient retry.
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.m4a"
            _synth_m4a(good, seconds=1.0)
            data = good.read_bytes()
            for frac in (0.3, 0.6):
                trunc = Path(td) / f"trunc_{int(frac*100)}.m4a"
                trunc.write_bytes(data[: int(len(data) * frac)])
                ok, kind, reason = pv.probe_decodable(trunc)
                self.assertFalse(ok, f"{frac} unexpectedly ok")
                self.assertEqual(kind, pv.FAIL_PERMANENT, reason)


class TestProbeDecodableClassification(unittest.TestCase):
    """Disposition of the probe's failure modes, stubbing the decode itself so
    no fixtures are needed and every branch is covered deterministically."""

    def test_short_decode_vs_declared_is_truncation_permanent(self):
        # decode returned far fewer seconds than the container declared.
        with mock.patch.object(pv, "_probe_decode", return_value=(2.0, 30.0)):
            ok, kind, reason = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_PERMANENT)
        self.assertIn("truncated", reason)

    def test_full_decode_within_slack_passes(self):
        with mock.patch.object(pv, "_probe_decode", return_value=(29.8, 30.0)):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertTrue(ok)
        self.assertEqual(kind, "")

    def test_no_declared_duration_skips_reconciliation(self):
        # declared==0 -> can't reconcile; a clean decode still passes.
        with mock.patch.object(pv, "_probe_decode", return_value=(5.0, 0.0)):
            ok, _, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertTrue(ok)

    def test_no_audio_stream_is_permanent(self):
        with mock.patch.object(pv, "_probe_decode", return_value=(None, 0.0)):
            ok, kind, reason = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_PERMANENT)
        self.assertIn("no decodable audio stream", reason)

    def test_environmental_decode_error_is_transient(self):
        # OSError (I/O, missing binary, disk) -> environmental -> transient.
        with mock.patch.object(pv, "_probe_decode", side_effect=OSError("disk gone")):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)

    def test_unknown_error_defaults_to_transient(self):
        # Default-deny: an unrecognized error fails toward retry, never a pass.
        with mock.patch.object(pv, "_probe_decode", side_effect=RuntimeError("???")):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)

    def test_invalid_data_error_is_permanent(self):
        import av
        with mock.patch.object(pv, "_probe_decode",
                               side_effect=av.error.InvalidDataError(1, "bad")):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_PERMANENT)


class TestProcessFileGate(unittest.TestCase):
    """process_file must reject an incomplete file before transcription and
    disposition it with the probe's kind."""

    def _run(self, td, probe_return):
        audio = Path(td) / "2026-07-08-0900.m4a"
        audio.write_bytes(b"placeholder")
        model = mock.Mock()  # .transcribe must never be called
        with mock.patch.object(pv, "probe_decodable", return_value=probe_return), \
                mock.patch.object(pv, "transcribe_audio") as transcribe:
            result = pv.process_file(
                audio, journal_dir=Path(td), model=model, whisper_prompt="",
                python_bin=Path("/usr/bin/python3"), scribe_py=Path("scribe.py"),
                night_cutoff=4, dry_run=False, verbose=False,
            )
        return result, audio, transcribe

    def test_permanent_probe_failure_marks_permanent_and_skips_transcribe(self):
        with tempfile.TemporaryDirectory() as td:
            result, audio, transcribe = self._run(
                td, (False, pv.FAIL_PERMANENT, "no decodable audio stream in container"))
            self.assertFalse(result)
            transcribe.assert_not_called()
            self.assertTrue(pv.is_failed(audio))
            self.assertFalse(pv.is_transient_failed(audio))  # permanent

    def test_transient_probe_failure_marks_transient(self):
        with tempfile.TemporaryDirectory() as td:
            result, audio, transcribe = self._run(
                td, (False, pv.FAIL_TRANSIENT, "decode failed (OSError: disk gone)"))
            self.assertFalse(result)
            transcribe.assert_not_called()
            self.assertTrue(pv.is_transient_failed(audio))

    def test_dry_run_does_not_write_marker(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "2026-07-08-0900.m4a"
            audio.write_bytes(b"placeholder")
            with mock.patch.object(pv, "probe_decodable",
                                   return_value=(False, pv.FAIL_PERMANENT, "bad")), \
                    mock.patch.object(pv, "transcribe_audio"):
                pv.process_file(
                    audio, journal_dir=Path(td), model=mock.Mock(), whisper_prompt="",
                    python_bin=Path("/usr/bin/python3"), scribe_py=Path("scribe.py"),
                    night_cutoff=4, dry_run=True, verbose=False,
                )
            self.assertFalse(pv.is_failed(audio))


if __name__ == "__main__":
    unittest.main()
