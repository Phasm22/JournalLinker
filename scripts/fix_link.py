"""One-time Fix links for a transcript finding.

GET shows a plain page and does not write. POST replaces the stored span
when it still appears once. The token lasts three days and dies after a
successful write. A refresh says already fixed.

Bind 127.0.0.1 only. Publish it tailnet-only:

    tailscale serve --bg --https=8798 http://127.0.0.1:8798
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

TOKEN_TTL = timedelta(days=3)
DEFAULT_PORT = 8798
_STORE_NAME = "fix-proposals.json"


def proposals_path() -> Path:
    raw = os.getenv("INTENT_STATE_DIR", "").strip()
    state = Path(raw).expanduser() if raw else Path.home() / ".local" / "state" / "journal-linker"
    return state / _STORE_NAME


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def mint_fix_link(
    *,
    path: Path,
    old: str,
    new: str,
    heard: str,
    suggestion: str,
    base_url: str,
    excerpt: str = "",
    store: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Store the replacement and return the URL that shows the confirm page."""
    token = secrets.token_urlsafe(32)
    moment = now or datetime.now(timezone.utc)
    store_path = store or proposals_path()
    data = _load(store_path)
    data[_token_id(token)] = {
        "exp": (moment + TOKEN_TTL).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "used": False,
        "path": str(path),
        "old": old,
        "new": new,
        "heard": heard,
        "suggestion": suggestion,
        "excerpt": excerpt,
    }
    _save(store_path, data)
    return f"{base_url.rstrip('/')}/fix/{token}"


def lookup(token: str, store: Path | None = None, now: datetime | None = None) -> tuple[str, dict | None]:
    """Return (status, record). status is ok, missing, expired, or used."""
    data = _load(store or proposals_path())
    record = data.get(_token_id(token))
    if not isinstance(record, dict):
        return "missing", None
    if record.get("used"):
        return "used", record
    moment = now or datetime.now(timezone.utc)
    try:
        exp = datetime.strptime(str(record.get("exp")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "expired", record
    if moment > exp:
        return "expired", record
    return "ok", record


def apply_stored(token: str, store: Path | None = None, now: datetime | None = None) -> str:
    """Write the span if it appears once. Return fixed, missing, ambiguous, expired, used, or absent."""
    store_path = store or proposals_path()
    status, record = lookup(token, store_path, now)
    if status != "ok" or record is None:
        return status
    path = Path(str(record.get("path") or ""))
    old = str(record.get("old") or "")
    new = str(record.get("new") or "")
    if not old or old == new or not path.is_file():
        return "absent"
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "absent"
    if count > 1:
        return "ambiguous"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    data = _load(store_path)
    row = data.get(_token_id(token))
    if isinstance(row, dict):
        row["used"] = True
        data[_token_id(token)] = row
        _save(store_path, data)
    return "fixed"


def _page(title: str, paragraphs: list[str], button: bool, token: str) -> bytes:
    body = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title></head>",
        "<body style=\"font-family:sans-serif;font-size:3rem;line-height:1.3;padding:1.2rem\">",
    ]
    for paragraph in paragraphs:
        body.append(f"<p>{paragraph}</p>")
    if button:
        body.append(
            f"<form method=\"post\" action=\"/fix/{html.escape(token)}\">"
            "<button type=\"submit\" style=\"font:inherit;padding:0.35em 0.7em\">Fix</button></form>"
        )
    body.append("</body></html>\n")
    return "".join(body).encode("utf-8")


def render_get(token: str, store: Path | None = None, now: datetime | None = None) -> tuple[int, bytes]:
    status, record = lookup(token, store, now)
    if status == "used":
        return 200, _page("Already fixed", ["Already fixed."], False, token)
    if status == "expired":
        return 200, _page("Expired", ["This link expired. The note was not changed."], False, token)
    if status != "ok" or record is None:
        return 404, _page("Not found", ["This link is not valid."], False, token)
    name = html.escape(Path(str(record.get("path") or "")).name)
    heard = html.escape(str(record.get("heard") or ""))
    suggestion = html.escape(str(record.get("suggestion") or ""))
    excerpt = html.escape(str(record.get("excerpt") or ""))
    lines = [f"<b>{name}</b>"]
    if excerpt:
        lines.append(excerpt)
    lines.append(f"{heard} → {suggestion}")
    return 200, _page("Fix?", lines, True, token)


def render_post(token: str, store: Path | None = None, now: datetime | None = None) -> tuple[int, bytes]:
    status, record = lookup(token, store, now)
    if status == "used":
        return 200, _page("Already fixed", ["Already fixed."], False, token)
    if status == "expired":
        return 200, _page("Expired", ["This link expired. The note was not changed."], False, token)
    if status != "ok":
        return 404, _page("Not found", ["This link is not valid."], False, token)
    result = apply_stored(token, store, now)
    heard = html.escape(str((record or {}).get("heard") or ""))
    suggestion = html.escape(str((record or {}).get("suggestion") or ""))
    pair = f"{heard} → {suggestion}"
    if result == "fixed":
        return 200, _page("Fixed", ["Fixed.", pair], False, token)
    if result == "ambiguous":
        return 200, _page("Not changed", ["Found that span more than once. The note was not changed."], False, token)
    return 200, _page("Not changed", ["Couldn't find that span. The note was not changed."], False, token)


class _Handler(BaseHTTPRequestHandler):
    store: Path

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _token(self) -> str:
        path = unquote(self.path.split("?", 1)[0])
        if not path.startswith("/fix/"):
            return ""
        token = path[len("/fix/"):]
        if not token or "/" in token:
            return ""
        return token

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/healthz":
            raw = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        token = self._token()
        if not token:
            self._send(404, _page("Not found", ["This link is not valid."], False, ""))
            return
        status, body = render_get(token, self.store)
        self._send(status, body)

    def do_POST(self) -> None:  # noqa: N802
        token = self._token()
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        if not token:
            self._send(404, _page("Not found", ["This link is not valid."], False, ""))
            return
        status, body = render_post(token, self.store)
        self._send(status, body)


def serve(port: int = DEFAULT_PORT, store: Path | None = None) -> None:
    _Handler.store = store or proposals_path()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.serve_forever()


def main() -> None:
    port = int(os.getenv("FIX_LINK_PORT", str(DEFAULT_PORT)))
    serve(port)


if __name__ == "__main__":
    main()
