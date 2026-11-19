"""Fix link: the page does not write, the button does, once."""

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("fix_link", SCRIPTS / "fix_link.py")
fl = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["fix_link"] = fl
spec.loader.exec_module(fl)


class TestFixLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "fix-proposals.json"
        self.note = Path(self.tmp.name) / "2026-11-09.md"
        self.note.write_text("with the just bear spicy\n", encoding="utf-8")
        self.url = fl.mint_fix_link(
            path=self.note,
            old="just bear",
            new="Just Bare",
            heard="just bear",
            suggestion="Just Bare",
            excerpt="…with the just bear spicy…",
            base_url="http://127.0.0.1:8798",
            store=self.store,
        )
        self.token = self.url.rsplit("/", 1)[-1]

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_does_not_write_and_post_writes_once(self):
        status, body = fl.render_get(self.token, self.store)
        self.assertEqual(status, 200)
        self.assertIn(b"Fix", body)
        self.assertIn(b"font-size:3rem", body)
        self.assertIn(b"with the just bear spicy", body)
        self.assertLess(body.index(b"with the just bear spicy"), body.index("just bear →".encode()))
        self.assertIn("just bear", self.note.read_text(encoding="utf-8"))

        status, body = fl.render_post(self.token, self.store)
        self.assertEqual(status, 200)
        self.assertIn(b"Fixed.", body)
        self.assertIn("Just Bare", self.note.read_text(encoding="utf-8"))
        self.assertNotIn("just bear", self.note.read_text(encoding="utf-8"))

        status, body = fl.render_post(self.token, self.store)
        self.assertIn(b"Already fixed.", body)

    def test_two_copies_are_left_alone(self):
        self.note.write_text("just bear and just bear\n", encoding="utf-8")
        status, body = fl.render_post(self.token, self.store)
        self.assertIn(b"more than once", body)
        self.assertEqual(self.note.read_text(encoding="utf-8"), "just bear and just bear\n")
        _, page = fl.render_get(self.token, self.store)
        self.assertIn(b"Fix", page)

    def test_expired_link_does_not_write(self):
        later = datetime.now(timezone.utc) + timedelta(days=4)
        status, body = fl.render_get(self.token, self.store, now=later)
        self.assertIn(b"expired", body)
        status, body = fl.render_post(self.token, self.store, now=later)
        self.assertIn(b"expired", body)
        self.assertIn("just bear", self.note.read_text(encoding="utf-8"))
