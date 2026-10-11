"""Tests for scripts/voice_ingest_server.py — Tailscale upload endpoint for VoiceDrop."""

import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "voice_ingest_server.py"
spec = importlib.util.spec_from_file_location("voice_ingest_server", SCRIPT_PATH)
vis = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(vis)

# Bytes that would break naive text/line-based multipart handling.
AUDIO = b"\x00\x01ftypM4A \r\n--not-a-boundary\r\n\xff\xfe" * 500
AUDIO_MD5 = hashlib.md5(AUDIO).hexdigest()


def multipart(data: bytes, field: str = "file", filename: str = "rec.m4a") -> tuple[str, bytes]:
    boundary = "----ShortcutsBoundary7d9f"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: audio/x-m4a\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


class TestValidation(unittest.TestCase):
    def test_accepts_shortcut_filename(self):
        self.assertEqual(vis.validate_filename(" 2026-09-30-0841.m4a "), "2026-09-30-0841.m4a")

    def test_rejects_traversal_hidden_and_wrong_extension(self):
        for bad in ["../x.m4a", "a/b.m4a", ".hidden.m4a", "note.txt", "2026-09-30-0841.M4A",
                    "rec.m4a", "2026-09-30.m4a", "", None]:
            with self.subTest(bad=bad), self.assertRaises(vis.IngestError):
                vis.validate_filename(bad)

    def test_accepts_upload_shortcut_filename(self):
        self.assertEqual(vis.validate_filename("2026-09-30_09-26-57.m4a"), "2026-09-30_09-26-57.m4a")

    def test_md5_normalized_and_validated(self):
        self.assertEqual(vis.normalize_md5(AUDIO_MD5.upper()), AUDIO_MD5)
        with self.assertRaises(vis.IngestError):
            vis.normalize_md5("abc")


class TestExtractFileBytes(unittest.TestCase):
    def test_multipart_binary_roundtrip(self):
        ctype, body = multipart(AUDIO)
        self.assertEqual(vis.extract_file_bytes(ctype, body), AUDIO)

    def test_raw_body_passthrough(self):
        self.assertEqual(vis.extract_file_bytes("audio/x-m4a", AUDIO), AUDIO)

    def test_missing_file_field(self):
        ctype, body = multipart(AUDIO, field="other")
        with self.assertRaises(vis.IngestError) as ctx:
            vis.extract_file_bytes(ctype, body)
        self.assertEqual(ctx.exception.status, 400)


@mock.patch.object(vis, "probe_audio", lambda path: None)
class TestStoreUpload(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.drop = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_stores_file_and_leaves_no_staging_debris(self):
        self.assertEqual(vis.store_upload(self.drop, "2026-09-30-0841.m4a", AUDIO, AUDIO_MD5), "stored")
        self.assertEqual((self.drop / "2026-09-30-0841.m4a").read_bytes(), AUDIO)
        self.assertEqual(list((self.drop / vis.INCOMING_SUBDIR).iterdir()), [])

    def test_md5_mismatch_rejected_and_nothing_written(self):
        with self.assertRaises(vis.IngestError) as ctx:
            vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, "0" * 32)
        self.assertEqual(ctx.exception.status, 400)
        self.assertFalse((self.drop / "2026-09-30_09-26-57.m4a").exists())

    def test_reupload_same_content_is_idempotent(self):
        vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, AUDIO_MD5)
        self.assertEqual(vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, AUDIO_MD5), "duplicate")

    def test_reupload_different_content_conflicts(self):
        vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, AUDIO_MD5)
        other = AUDIO + b"x"
        with self.assertRaises(vis.IngestError) as ctx:
            vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", other, hashlib.md5(other).hexdigest())
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual((self.drop / "2026-09-30_09-26-57.m4a").read_bytes(), AUDIO)

    def test_processed_marker_without_audio_counts_as_done(self):
        (self.drop / "2026-09-30_09-26-57.m4a.processed").touch()
        self.assertEqual(vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, AUDIO_MD5), "already-processed")
        self.assertFalse((self.drop / "2026-09-30_09-26-57.m4a").exists())

    def test_probe_rejection_leaves_no_file(self):
        def reject(path):
            raise vis.IngestError(422, "undecodable audio")

        with mock.patch.object(vis, "probe_audio", reject), self.assertRaises(vis.IngestError):
            vis.store_upload(self.drop, "2026-09-30_09-26-57.m4a", AUDIO, AUDIO_MD5)
        self.assertFalse((self.drop / "2026-09-30_09-26-57.m4a").exists())
        self.assertEqual(list((self.drop / vis.INCOMING_SUBDIR).iterdir()), [])


@mock.patch.object(vis, "probe_audio", lambda path: None)
class TestHttpEndpoint(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.drop = Path(self._td.name)
        self.health = vis.HealthState(vis.SERVICE_NAME)
        self.server = vis.ThreadingHTTPServer(
            ("127.0.0.1", 0), vis.make_handler(self.drop, 1 << 20, self.health)
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._td.cleanup()

    def post(self, body: bytes, headers: dict, path: str = "/ingest") -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        result = resp.status, resp.read().decode()
        conn.close()
        return result

    def test_shortcut_style_upload_returns_exact_ok(self):
        ctype, body = multipart(AUDIO)
        status, text = self.post(body, {
            "Content-Type": ctype, "X-Filename": "2026-09-30-0841.m4a", "X-MD5": AUDIO_MD5,
        })
        self.assertEqual((status, text), (200, "OK"))
        self.assertEqual((self.drop / "2026-09-30-0841.m4a").read_bytes(), AUDIO)

    def test_bad_hash_is_not_ok(self):
        ctype, body = multipart(AUDIO)
        status, text = self.post(body, {"Content-Type": ctype, "X-Filename": "2026-09-30_09-26-57.m4a", "X-MD5": "1" * 32})
        self.assertEqual(status, 400)
        self.assertNotEqual(text, "OK")

    def test_funnel_requests_refused(self):
        ctype, body = multipart(AUDIO)
        status, _ = self.post(body, {
            "Content-Type": ctype, "X-Filename": "2026-09-30_09-26-57.m4a", "X-MD5": AUDIO_MD5,
            "Tailscale-Funnel-Request": "?1",
        })
        self.assertEqual(status, 403)
        self.assertFalse((self.drop / "2026-09-30_09-26-57.m4a").exists())

    def test_oversize_rejected(self):
        big = b"x" * ((1 << 20) + 1)
        status, _ = self.post(big, {
            "Content-Type": "audio/x-m4a", "X-Filename": "2026-09-30_09-26-57.m4a", "X-MD5": hashlib.md5(big).hexdigest(),
        })
        self.assertEqual(status, 413)

    def _upload(self, name: str) -> tuple[int, str]:
        ctype, body = multipart(AUDIO)
        return self.post(body, {"Content-Type": ctype, "X-Filename": name, "X-MD5": AUDIO_MD5})

    def test_server_side_failure_degrades_then_recovers(self):
        self.health.set("ready", "listening")
        buf = io.StringIO()
        disk_full = OSError(28, "No space left on device")
        with mock.patch.object(sys, "stderr", buf):
            with mock.patch.object(vis, "store_upload", side_effect=disk_full):
                self.assertEqual(self._upload("2026-09-30_09-26-57.m4a")[0], 500)
                self.assertEqual(self._upload("2026-09-30_09-26-58.m4a")[0], 500)
            self.assertEqual(self._upload("2026-09-30_09-26-59.m4a"), (200, "OK"))
            self.assertEqual(self._upload("2026-09-30_09-27-00.m4a"), (200, "OK"))
        events = [
            json.loads(line.split("=", 1)[1])
            for line in buf.getvalue().splitlines()
            if line.startswith("JOURNAL_LINKER_EVENT=")
        ]
        self.assertEqual(
            [(e["status"], e["reason"]) for e in events],
            [("degraded", "write failed: OSError"), ("recovered", "write succeeded")],
        )

    def test_client_rejects_are_not_health_events(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            ctype, body = multipart(AUDIO)
            status, _ = self.post(body, {"Content-Type": ctype, "X-Filename": "nope.m4a", "X-MD5": AUDIO_MD5})
        self.assertEqual(status, 400)
        self.assertNotIn("JOURNAL_LINKER_EVENT=", buf.getvalue())
        self.assertIsNone(self.health.status)

    def test_healthz(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.read()), (200, b"ok"))
        conn.close()


class TestStartupHealth(unittest.TestCase):
    def _main_events(self, argv: list[str]) -> tuple[int, list[dict]]:
        buf = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["voice_ingest_server.py", *argv]),
            mock.patch.object(vis, "bootstrap_journal_linker_env"),
            mock.patch.dict(vis.os.environ, {}, clear=False),
            mock.patch.object(sys, "stderr", buf),
        ):
            vis.os.environ.pop("SCRIBE_VOICEDROP_DIR", None)
            code = vis.main()
        events = [
            json.loads(line.split("=", 1)[1])
            for line in buf.getvalue().splitlines()
            if line.startswith("JOURNAL_LINKER_EVENT=")
        ]
        return code, events

    def test_missing_drop_dir_config_is_error(self):
        code, events = self._main_events([])
        self.assertEqual(code, 1)
        self.assertEqual([(e["status"], e["reason"]) for e in events], [("error", "drop dir not configured")])

    def test_nonexistent_drop_dir_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, events = self._main_events(["--drop-dir", str(Path(tmp) / "missing")])
        self.assertEqual(code, 1)
        self.assertEqual([(e["status"], e["reason"]) for e in events], [("error", "drop dir missing")])

    def test_ready_once_then_stopping(self):
        class FakeServer:
            def __init__(self, *args, **kwargs):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(vis, "ThreadingHTTPServer", FakeServer), \
                mock.patch.object(vis.signal, "signal"):
            code, events = self._main_events(["--drop-dir", tmp, "--port", "0"])
        self.assertEqual(code, 0)
        self.assertEqual([(e["status"], e["reason"]) for e in events], [("ready", "listening"), ("stopping", "shutdown")])

    def test_bind_failure_is_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(vis, "ThreadingHTTPServer", side_effect=OSError(98, "Address in use")):
            code, events = self._main_events(["--drop-dir", tmp])
        self.assertEqual(code, 1)
        self.assertEqual([(e["status"], e["reason"]) for e in events], [("error", "bind failed")])


class TestProbeAudio(unittest.TestCase):
    def test_permanent_defect_is_422(self):
        with mock.patch.object(vis.pv, "probe_decodable",
                               return_value=(False, vis.pv.FAIL_PERMANENT, "truncated")):
            with self.assertRaises(vis.IngestError) as ctx:
                vis.probe_audio(Path("x.m4a"))
        self.assertEqual(ctx.exception.status, 422)

    def test_transient_or_crash_is_503(self):
        with mock.patch.object(vis.pv, "probe_decodable",
                               return_value=(False, vis.pv.FAIL_TRANSIENT, "decode crashed (signal 11)")):
            with self.assertRaises(vis.IngestError) as ctx:
                vis.probe_audio(Path("x.m4a"))
        self.assertEqual(ctx.exception.status, 503)

    def test_garbage_rejected_by_real_probe(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV not installed")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "junk.m4a"
            path.write_bytes(b"definitely not audio" * 100)
            with self.assertRaises(vis.IngestError) as ctx:
                vis.probe_audio(path)
            self.assertEqual(ctx.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
