"""Dry-run collapse of wellness notes that share a date, type, and items."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_wellness_notes.py"
spec = importlib.util.spec_from_file_location("dedupe_wellness_notes", SCRIPT_PATH)
dedupe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(dedupe)


def _write_note(path: Path, *, source_date: str, cap_type: str, items: list[str], spoken: str, span: str) -> None:
    rendered = ", ".join(json.dumps(item) for item in items)
    path.write_text(
        "---\n"
        f"type: {cap_type}\n"
        f"items: [{rendered}]\n"
        f'time: "{spoken}"\n'
        f"source_date: {source_date}\n"
        "meal_slot: unknown\n"
        f"span_key: {span}\n"
        "---\n\n"
        "# capture\n",
        encoding="utf-8",
    )


class TestDedupeWellnessNotes(unittest.TestCase):
    def test_dry_run_collapses_same_date_type_and_items(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            wellness = vault / "wellness"
            wellness.mkdir()
            early = wellness / "2026-11-09 want_to_eat beef stew 11062377.md"
            later = wellness / "2026-11-09 want_to_eat beef stew 6750c868.md"
            shifted = wellness / "2026-11-09 want_to_eat beef stew 38b29404.md"
            other = wellness / "2026-11-09 want_to_eat espresso smoothie 11062377.md"
            for path, spoken, span in (
                (early, "20:26", "11062377aaaaaaaa"),
                (later, "20:26", "6750c868aaaaaaaa"),
                (shifted, "21:26", "38b29404aaaaaaaa"),
                (other, "20:26", "11062377bbbbbbbb"),
            ):
                label = "espresso smoothie" if "espresso" in path.name else "beef stew"
                _write_note(
                    path,
                    source_date="2026-11-09",
                    cap_type="want_to_eat",
                    items=[label],
                    spoken=spoken,
                    span=span,
                )
            os.utime(shifted, (1_700_000_000, 1_700_000_000))
            os.utime(later, (1_700_000_200, 1_700_000_200))
            os.utime(early, (1_700_000_100, 1_700_000_100))
            os.utime(other, (1_700_000_100, 1_700_000_100))
            rc, report = self._capture(vault, wellness)
            self.assertTrue(early.exists())
            self.assertTrue(shifted.exists())
            self.assertFalse((vault / ".trash-journal-linker").exists())
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", report)
        self.assertIn("groups: 1", report)
        self.assertIn(f"KEEP {early.name} time=20:26", report)
        self.assertIn(f"MOVE {later.name} time=20:26", report)
        self.assertIn(f"MOVE {shifted.name} time=21:26", report)
        self.assertNotIn("MOVE 2026-11-09 want_to_eat espresso", report)
        self.assertNotIn("apply", report.splitlines()[0])

    def _capture(self, vault: Path, wellness: Path) -> tuple[int, str]:
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = dedupe.main(["--vault", str(vault), "--wellness-dir", str(wellness)])
        return rc, buf.getvalue()

    def test_apply_moves_duplicates_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            wellness = vault / "journal" / "wellness"
            wellness.mkdir(parents=True)
            keep = wellness / "2026-11-12 preference the actual team f83dc6a9.md"
            extra = wellness / "2026-11-12 preference the actual team 6809407a.md"
            _write_note(keep, source_date="2026-11-12", cap_type="preference",
                        items=["the actual team"], spoken="14:29", span="f83dc6a9aaaaaaaa")
            _write_note(extra, source_date="2026-11-12", cap_type="preference",
                        items=["the actual team"], spoken="14:29", span="6809407aaaaaaaaa")
            os.utime(keep, (1_700_000_000, 1_700_000_000))
            os.utime(extra, (1_700_000_500, 1_700_000_500))
            from io import StringIO
            from contextlib import redirect_stdout
            buf = StringIO()
            with redirect_stdout(buf):
                rc = dedupe.main([
                    "--vault", str(vault),
                    "--journal-dir", str(vault / "journal"),
                    "--apply",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("apply", buf.getvalue())
            self.assertTrue(keep.exists())
            self.assertFalse(extra.exists())
            trash = list((vault / ".trash-journal-linker").rglob(extra.name))
            self.assertEqual(len(trash), 1)
            self.assertIn("the actual team", trash[0].read_text(encoding="utf-8"))
            manifest = next((vault / ".trash-journal-linker").rglob("manifest.json"))
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["kept"], [keep.name])
            self.assertEqual(data["moved"][0]["file"], extra.name)
            self.assertTrue(data["moved"][0]["to"].startswith(".trash-journal-linker/"))

    def test_dotdot_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td) / "vault"
            outside = Path(td) / "outside"
            vault.mkdir()
            outside.mkdir()
            secret = outside / "secret.md"
            secret.write_text("secret\n", encoding="utf-8")
            wellness = vault / "wellness"
            wellness.mkdir()
            link = wellness / "note.md"
            link.symlink_to(secret)
            rc = dedupe.main(["--vault", str(vault), "--wellness-dir", str(wellness)])
            self.assertEqual(rc, 2)
            self.assertTrue(secret.exists())
            self.assertEqual(secret.read_text(encoding="utf-8"), "secret\n")
            escaped = dedupe.main([
                "--vault", str(vault),
                "--wellness-dir", str(vault / ".." / "outside"),
            ])
            self.assertEqual(escaped, 2)
            self.assertTrue(secret.exists())


if __name__ == "__main__":
    unittest.main()
