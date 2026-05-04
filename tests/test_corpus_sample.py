import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "corpus_sample.py"
spec = importlib.util.spec_from_file_location("corpus_sample", SCRIPT)
cs = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(cs)


class TestCorpusSample(unittest.TestCase):
    def test_discover_non_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2024-03-15.md").write_text("a", encoding="utf-8")
            (root / "not-a-date.md").write_text("b", encoding="utf-8")
            (root / "ignored.txt").write_text("c", encoding="utf-8")
            got = cs.discover_daily_notes([root], recursive=False)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].day, date(2024, 3, 15))

    def test_discover_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "nested"
            sub.mkdir()
            (sub / "2022-01-01.md").write_text("x", encoding="utf-8")
            got = cs.discover_daily_notes([root], recursive=True)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].day, date(2022, 1, 1))

    def test_filter_date_range(self):
        refs = [
            cs.NoteRef(date(2024, 1, 1), Path("/a")),
            cs.NoteRef(date(2024, 6, 1), Path("/b")),
        ]
        out = cs.filter_date_range(refs, from_date=date(2024, 1, 1), to_date=date(2024, 3, 1))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].day, date(2024, 1, 1))

    def test_shuffle_and_sample_reproducible(self):
        refs = [
            cs.NoteRef(date(2024, 1, d), Path(f"/{d}"))
            for d in range(1, 11)
        ]
        a = cs.shuffle_and_sample(refs, shuffle=False, sample_k=3, seed=42)
        b = cs.shuffle_and_sample(refs, shuffle=False, sample_k=3, seed=42)
        self.assertEqual([x.day for x in a], [x.day for x in b])

    def test_strip_frontmatter(self):
        text = "---\ntitle: x\n---\n\nBody here."
        self.assertEqual(cs.strip_frontmatter(text).strip(), "Body here.")
