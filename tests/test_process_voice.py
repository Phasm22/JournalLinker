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
from types import SimpleNamespace
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


def _fake_proc(returncode, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class TestProbeDecodableClassification(unittest.TestCase):
    """Disposition of the probe's failure modes, stubbing the child process so
    no fixtures are needed and every branch — including a crash — is covered."""

    def test_short_decode_vs_declared_is_truncation_permanent(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_OK, "[2.0, 30.0]")):
            ok, kind, reason = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_PERMANENT)
        self.assertIn("truncated", reason)

    def test_full_decode_within_slack_passes(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_OK, "[29.8, 30.0]")):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertTrue(ok)
        self.assertEqual(kind, "")

    def test_no_declared_duration_skips_reconciliation(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_OK, "[5.0, 0.0]")):
            ok, _, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertTrue(ok)

    def test_content_exit_is_permanent(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_CONTENT)):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_PERMANENT)

    def test_env_exit_is_transient(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_ENV)):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)

    def test_signal_death_survives_and_is_transient(self):
        # The whole reason the probe is isolated: a libav SIGSEGV (-11) must not
        # crash the parent — it becomes a bounded transient, not a batch kill.
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(-11)):
            ok, kind, reason = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)
        self.assertIn("signal 11", reason)

    def test_timeout_is_transient(self):
        with mock.patch.object(pv.subprocess, "run",
                               side_effect=pv.subprocess.TimeoutExpired("cmd", 120)):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)

    def test_malformed_worker_output_defaults_transient(self):
        with mock.patch.object(pv.subprocess, "run",
                               return_value=_fake_proc(pv.PROBE_EXIT_OK, "not json")):
            ok, kind, _ = pv.probe_decodable(Path("x.m4a"))
        self.assertFalse(ok)
        self.assertEqual(kind, pv.FAIL_TRANSIENT)


@unittest.skipUnless(_HAVE_AV, "PyAV not available")
class TestProbeWorker(unittest.TestCase):
    """The child-process entry point's exit codes, run in-process on fixtures."""

    def test_valid_returns_ok_with_json(self):
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "g.m4a"
            _synth_m4a(good)
            from contextlib import redirect_stdout
            import io
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pv._probe_worker(str(good))
            self.assertEqual(rc, pv.PROBE_EXIT_OK)
            decoded, declared = json.loads(buf.getvalue())
            self.assertGreater(decoded, 0.0)

    def test_corrupt_returns_content_exit(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "b.m4a"
            bad.write_bytes(b"nonsense" * 8)
            self.assertEqual(pv._probe_worker(str(bad)), pv.PROBE_EXIT_CONTENT)


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


class TestBoundedRetry(unittest.TestCase):
    """Transient failures accrue an attempt count and escalate to permanent at
    the cap, firing exactly one notification."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS", None)

    def tearDown(self):
        self._env.stop()

    def _audio(self, td):
        p = Path(td) / "2026-07-08-0900.m4a"
        p.write_bytes(b"placeholder")
        return p

    def test_permanent_failure_is_not_counted(self):
        with tempfile.TemporaryDirectory() as td:
            audio = self._audio(td)
            with mock.patch.object(pv, "_notify") as notify:
                pv.mark_failed(audio, "corrupt", kind=pv.FAIL_PERMANENT)
            self.assertFalse(pv.is_transient_failed(audio))
            self.assertEqual(pv._read_marker_attempts(audio), 0)
            notify.assert_not_called()

    def test_transient_increments_attempts(self):
        os.environ["SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS"] = "5"
        with tempfile.TemporaryDirectory() as td:
            audio = self._audio(td)
            with mock.patch.object(pv, "_notify"):
                pv.mark_failed(audio, "ollama down", kind=pv.FAIL_TRANSIENT)
                self.assertEqual(pv._read_marker_attempts(audio), 1)
                self.assertTrue(pv.is_transient_failed(audio))
                pv.mark_failed(audio, "ollama down", kind=pv.FAIL_TRANSIENT)
                self.assertEqual(pv._read_marker_attempts(audio), 2)
                self.assertTrue(pv.is_transient_failed(audio))

    def test_escalates_to_permanent_at_cap_and_notifies_once(self):
        os.environ["SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS"] = "3"
        with tempfile.TemporaryDirectory() as td:
            audio = self._audio(td)
            with mock.patch.object(pv, "_notify") as notify:
                pv.mark_failed(audio, "reason", kind=pv.FAIL_TRANSIENT)  # 1
                pv.mark_failed(audio, "reason", kind=pv.FAIL_TRANSIENT)  # 2
                self.assertTrue(pv.is_transient_failed(audio))
                notify.assert_not_called()
                pv.mark_failed(audio, "reason", kind=pv.FAIL_TRANSIENT)  # 3 -> cap
                # Escalated: now permanent, so the retry loop will skip it.
                self.assertFalse(pv.is_transient_failed(audio))
                self.assertTrue(pv.is_failed(audio))
                notify.assert_called_once()
                marker = audio.with_suffix(audio.suffix + pv.FAILED_SUFFIX)
                self.assertIn("escalated", marker.read_text())

    def test_default_cap_is_ten(self):
        self.assertEqual(pv._max_transient_attempts(), 10)

    def test_bad_cap_env_falls_back_to_default(self):
        os.environ["SCRIBE_VOICE_MAX_TRANSIENT_ATTEMPTS"] = "not-a-number"
        self.assertEqual(pv._max_transient_attempts(), 10)

    def test_legacy_marker_without_attempts_reads_zero(self):
        with tempfile.TemporaryDirectory() as td:
            audio = self._audio(td)
            marker = audio.with_suffix(audio.suffix + pv.FAILED_SUFFIX)
            marker.write_text("kind: transient\nreason: legacy\n", encoding="utf-8")
            self.assertEqual(pv._read_marker_attempts(audio), 0)
            # Next transient failure resumes counting from 1.
            with mock.patch.object(pv, "_notify"):
                pv.mark_failed(audio, "again", kind=pv.FAIL_TRANSIENT)
            self.assertEqual(pv._read_marker_attempts(audio), 1)


class TestNotifyBestEffort(unittest.TestCase):
    def test_no_pnotify_falls_back_to_log_without_raising(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(pv.os, "access", return_value=False):
            pv._notify("title", "body")  # must not raise

    def test_pnotify_invoked_when_present(self):
        with mock.patch("shutil.which", return_value="/usr/bin/pnotify"), \
                mock.patch.object(pv.subprocess, "run") as run:
            pv._notify("title", "body")
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/pnotify")


if __name__ == "__main__":
    unittest.main()


class TestResolveTargetDate(unittest.TestCase):
    def test_dropbox_era_name(self):
        self.assertEqual(pv.resolve_target_date(Path("2026-09-30-0753.m4a")), ("2026-09-30", "07:53"))

    def test_tailscale_upload_name(self):
        # Regression: this format fell through to mtime (= upload time), so
        # every upload in a batch got the same date/time and all but one were
        # skipped as duplicate callouts.
        self.assertEqual(pv.resolve_target_date(Path("2026-09-30_09-26-57.m4a")), ("2026-09-30", "09:26"))
        self.assertEqual(pv.resolve_target_date(Path("2026-09-29_23-28-41.m4a")), ("2026-09-29", "23:28"))

    def test_before_night_cutoff_rolls_to_previous_day(self):
        self.assertEqual(pv.resolve_target_date(Path("2026-09-30_01-30-00.m4a")), ("2026-09-29", "01:30"))
