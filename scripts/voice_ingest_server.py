#!/usr/bin/env python3
"""voice_ingest_server.py — receive voice recordings from the iOS Shortcut over Tailscale.

Replaces the Dropbox hop: the phone POSTs each recording straight to this
host, and the file lands in SCRIBE_VOICEDROP_DIR, where the existing
journal-linker-voice-watcher.path → process_voice.py pipeline picks it up.

Protocol (matches the Shortcut):
    POST /ingest
      body:    multipart/form-data with a `file` field (raw body also accepted)
      headers: X-Filename  — e.g. 2026-09-30-0841.m4a (drives date attribution)
               X-MD5       — hex MD5 of the file as sent
    → 200 "OK" once the file is fully on disk. Anything else is a rejection;
      the Shortcut only writes its ledger on exactly "OK", so it retries later.

    GET /healthz → 200 "ok"

Durability: the upload is written to <drop>/.incoming/, fsynced, MD5-checked,
run through process_voice's crash-isolated decode probe, then hard-linked into <drop>/ (atomic, never
clobbers). The path unit only ever sees complete files.

Idempotent: re-uploading a file already in the drop dir (same MD5), or one
that already has a .processed marker, returns OK without rewriting it.

Exposure: binds 127.0.0.1 by default; front it with `tailscale serve` so it
is tailnet-only. Requests carrying Tailscale's Funnel header are refused, so
an accidental `tailscale funnel` on this port does not open it to the internet.

Env (from environment or ~/.config/journal-linker/journal-linker.env):
    SCRIBE_VOICEDROP_DIR   — destination folder (required)
    VOICE_INGEST_HOST      — bind address (default: 127.0.0.1)
    VOICE_INGEST_PORT      — bind port (default: 8797)
    VOICE_INGEST_MAX_MB    — per-upload size cap (default: 200)
"""

import argparse
import hashlib
import os
import re
import sys
import tempfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from journal_linker_env import bootstrap_journal_linker_env
import process_voice as pv

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8797
DEFAULT_MAX_MB = 200
INCOMING_SUBDIR = ".incoming"

# Lowercase extensions only: process_voice.py globs *.m4a etc. case-sensitively.
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}\.(m4a|wav|mp4|aac)$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")


class IngestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def validate_filename(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise IngestError(400, "missing X-Filename")
    if not FILENAME_RE.fullmatch(name):
        raise IngestError(400, f"bad filename: {name!r}")
    return name


def normalize_md5(raw: str | None) -> str:
    digest = (raw or "").strip().lower()
    if not MD5_RE.fullmatch(digest):
        raise IngestError(400, "missing or malformed X-MD5")
    return digest


def extract_file_bytes(content_type: str, body: bytes) -> bytes:
    """Return the `file` part of a multipart body, or the raw body otherwise."""
    if not content_type.lower().startswith("multipart/form-data"):
        return body
    header = f"Content-Type: {content_type}\r\n\r\n".encode("latin-1")
    msg = BytesParser(policy=HTTP).parsebytes(header + body)
    if not msg.is_multipart():
        raise IngestError(400, "malformed multipart body")
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            data = part.get_payload(decode=True)
            if data is None:
                raise IngestError(400, "empty file field")
            return data
    raise IngestError(400, "multipart body has no `file` field")


def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_audio(path: Path) -> None:
    """Run process_voice's crash-isolated decode probe before accepting a file.

    Same gate transcription applies (child-process decode, truncation check),
    so a libav segfault on a hostile file kills the probe child, not this
    server. Rejecting here matters because the phone still holds the intact
    recording and will retry; once accepted, a defect becomes a .failed marker.
    Content defects → 422 (retrying the same bytes won't help); environmental
    errors, timeouts and probe crashes → 503 (retry later).
    """
    ok, kind, reason = pv.probe_decodable(path)
    if ok:
        return
    raise IngestError(422 if kind == pv.FAIL_PERMANENT else 503, f"audio rejected: {reason}")


def store_upload(drop_dir: Path, filename: str, data: bytes, expected_md5: str) -> str:
    """Write `data` into drop_dir/filename atomically. Returns a short outcome tag."""
    if not data:
        raise IngestError(400, "empty file")
    actual = hashlib.md5(data).hexdigest()
    if actual != expected_md5:
        raise IngestError(400, f"md5 mismatch (got {actual}, expected {expected_md5})")

    dest = drop_dir / filename
    if dest.with_name(dest.name + pv.PROCESSED_SUFFIX).exists() and not dest.exists():
        return "already-processed"
    if dest.exists():
        if md5_of_file(dest) == actual:
            return "duplicate"
        raise IngestError(409, f"{filename} already exists with different content")

    # Staging lives in a subdirectory: the path unit watches drop_dir
    # non-recursively, so it only fires on the final rename.
    incoming = drop_dir / INCOMING_SUBDIR
    incoming.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".part", dir=incoming)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        probe_audio(tmp)
        # link() never clobbers, so a concurrent upload of the same name
        # can't overwrite a file the watcher may already be transcribing.
        try:
            os.link(tmp, dest)
        except FileExistsError:
            if md5_of_file(dest) == actual:
                return "duplicate"
            raise IngestError(409, f"{filename} already exists with different content")
    finally:
        tmp.unlink(missing_ok=True)
    dir_fd = os.open(drop_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return "stored"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(drop_dir: Path, max_bytes: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "journal-linker-voice-ingest/1"

        def _reply(self, status: int, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):  # route to journald via stderr
            print(f"[ingest] {self.address_string()} {fmt % args}", file=sys.stderr, flush=True)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self):
            if self.path != "/ingest":
                self._reply(404, "not found")
                return
            try:
                if self.headers.get("Tailscale-Funnel-Request"):
                    raise IngestError(403, "funnel requests are not accepted")
                filename = validate_filename(self.headers.get("X-Filename"))
                expected = normalize_md5(self.headers.get("X-MD5"))
                length_raw = self.headers.get("Content-Length")
                if length_raw is None:
                    raise IngestError(411, "Content-Length required")
                try:
                    length = int(length_raw)
                except ValueError:
                    raise IngestError(400, "bad Content-Length")
                if length > max_bytes:
                    raise IngestError(413, f"upload exceeds {max_bytes} bytes")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise IngestError(400, "truncated body")
                data = extract_file_bytes(self.headers.get("Content-Type", ""), body)
                outcome = store_upload(drop_dir, filename, data, expected)
            except IngestError as exc:
                print(f"[ingest] reject {exc.status}: {exc.message}", file=sys.stderr, flush=True)
                self._reply(exc.status, exc.message)
                return
            except Exception as exc:
                print(f"[ingest] error: {exc!r}", file=sys.stderr, flush=True)
                self._reply(500, "internal error")
                return
            print(f"[ingest] {outcome}: {filename} ({len(data)} bytes)", file=sys.stderr, flush=True)
            self._reply(200, "OK")

    return Handler


def parse_cli() -> argparse.Namespace:
    bootstrap_journal_linker_env(repo_root=_REPO_ROOT)
    parser = argparse.ArgumentParser(description="Receive voice recordings over Tailscale into VoiceDrop.")
    parser.add_argument("--drop-dir", default=os.getenv("SCRIBE_VOICEDROP_DIR"))
    parser.add_argument("--host", default=os.getenv("VOICE_INGEST_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("VOICE_INGEST_PORT", str(DEFAULT_PORT))))
    parser.add_argument(
        "--max-mb", type=int, default=int(os.getenv("VOICE_INGEST_MAX_MB", str(DEFAULT_MAX_MB)))
    )
    return parser.parse_args()


def main() -> int:
    args = parse_cli()
    if not args.drop_dir:
        print("[ingest] SCRIBE_VOICEDROP_DIR is not set (or pass --drop-dir)", file=sys.stderr)
        return 1
    drop_dir = Path(args.drop_dir).expanduser()
    if not drop_dir.is_dir():
        print(f"[ingest] VoiceDrop dir not found: {drop_dir}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), make_handler(drop_dir, args.max_mb * 1024 * 1024))
    print(f"[ingest] listening on http://{args.host}:{args.port} → {drop_dir}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
