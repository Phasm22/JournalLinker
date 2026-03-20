import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "vault_mapper.py"
spec = importlib.util.spec_from_file_location("vault_mapper", SCRIPT_PATH)
vault_mapper = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(vault_mapper)


class TestScanJournalLinks(unittest.TestCase):
    def test_extracts_wikilinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "2026-03-01.md"
            p.write_text("Today I worked on [[Python]] and [[llmLibrarian]].", encoding="utf-8")
            result = vault_mapper.scan_journal_links(Path(tmp))
        self.assertIn("2026-03-01", result)
        links = result["2026-03-01"]
        self.assertIn("python", links)
        self.assertIn("llmlibrarian", links)

    def test_ignores_non_date_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.md").write_text("[[Python]]", encoding="utf-8")
            (Path(tmp) / "2026-03-01.md").write_text("[[anxiety]]", encoding="utf-8")
            result = vault_mapper.scan_journal_links(Path(tmp))
        self.assertNotIn("notes", result)
        self.assertIn("2026-03-01", result)

    def test_strips_aliases_and_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "2026-03-01.md"
            p.write_text("See [[Python#section|Python Docs]] and [[sleep|rest]].", encoding="utf-8")
            result = vault_mapper.scan_journal_links(Path(tmp))
        links = result["2026-03-01"]
        self.assertIn("python", links)
        self.assertIn("sleep", links)

    def test_skips_date_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "2026-03-01.md"
            p.write_text("[[2026-03-02|Tomorrow]] but also [[anxiety]]", encoding="utf-8")
            result = vault_mapper.scan_journal_links(Path(tmp))
        links = result["2026-03-01"]
        self.assertNotIn("2026-03-02", links)
        self.assertIn("anxiety", links)


class TestBuildCooccurrence(unittest.TestCase):
    def test_counts_pairs(self):
        daily_links = {
            "2026-03-01": ["anxiety", "sleep"],
            "2026-03-02": ["anxiety", "sleep"],
        }
        result = vault_mapper.build_cooccurrence(daily_links, min_cooccurrence=1)
        self.assertEqual(result[("anxiety", "sleep")], 2)

    def test_min_threshold_filters(self):
        daily_links = {
            "2026-03-01": ["anxiety", "sleep"],
            "2026-03-02": ["anxiety", "gym"],
        }
        result = vault_mapper.build_cooccurrence(daily_links, min_cooccurrence=2)
        self.assertNotIn(("anxiety", "sleep"), result)
        self.assertNotIn(("anxiety", "gym"), result)

    def test_min_threshold_keeps_qualifying(self):
        daily_links = {
            "2026-03-01": ["anxiety", "sleep"],
            "2026-03-02": ["anxiety", "sleep"],
            "2026-03-03": ["anxiety", "gym"],
        }
        result = vault_mapper.build_cooccurrence(daily_links, min_cooccurrence=2)
        self.assertIn(("anxiety", "sleep"), result)
        self.assertNotIn(("anxiety", "gym"), result)


class TestClusterTerms(unittest.TestCase):
    def test_groups_connected_chain(self):
        # anxiety-sleep-gym chain: anxiety↔sleep, sleep↔gym
        cooccurrence = {
            ("anxiety", "sleep"): 3,
            ("gym", "sleep"): 2,
        }
        clusters = vault_mapper.cluster_terms(cooccurrence)
        flat = {term for cluster in clusters for term in cluster}
        self.assertIn("anxiety", flat)
        self.assertIn("sleep", flat)
        self.assertIn("gym", flat)
        # All three should be in the same cluster
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 3)

    def test_two_separate_clusters(self):
        cooccurrence = {
            ("anxiety", "sleep"): 3,
            ("python", "llmlibrarian"): 2,
        }
        clusters = vault_mapper.cluster_terms(cooccurrence)
        self.assertEqual(len(clusters), 2)


class TestRenderVaultMap(unittest.TestCase):
    def test_contains_expected_links(self):
        clusters = [["anxiety", "sleep"]]
        cooccurrence = {("anxiety", "sleep"): 5}
        memory_signals = {}
        daily_links = {"2026-03-01": ["anxiety", "sleep"]}
        output = vault_mapper.render_vault_map(clusters, cooccurrence, memory_signals, daily_links, "2026-03-20")
        self.assertIn("[[anxiety]]", output)
        self.assertIn("[[sleep]]", output)
        self.assertIn("5 notes", output)

    def test_header_contains_date(self):
        output = vault_mapper.render_vault_map([], {}, {}, {}, "2026-03-20")
        self.assertIn("2026-03-20", output)

    def test_empty_cooccurrence_no_strong_connections_section(self):
        output = vault_mapper.render_vault_map([], {}, {}, {}, "2026-03-20")
        self.assertNotIn("Strong Connections", output)


class TestLoadMemorySignals(unittest.TestCase):
    def test_returns_empty_when_file_missing(self):
        result = vault_mapper.load_memory_signals("/nonexistent/path/scribe_learning.json")
        self.assertEqual(result, {})

    def test_loads_term_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            lf = Path(tmp) / "scribe_learning.json"
            lf.write_text(json.dumps({
                "term_memory": {"Anxiety": {"success": 8, "failure": 2}},
                "term_weights": {},
                "runs": {},
            }), encoding="utf-8")
            result = vault_mapper.load_memory_signals(str(lf))
        self.assertIn("anxiety", result)
        self.assertEqual(result["anxiety"]["success"], 8)


class TestBuildVaultMap(unittest.TestCase):
    def test_writes_both_output_files(self):
        with tempfile.TemporaryDirectory() as journal_tmp, tempfile.TemporaryDirectory() as out_tmp:
            # Write two notes with overlapping wikilinks
            (Path(journal_tmp) / "2026-03-01.md").write_text("[[anxiety]] [[sleep]]", encoding="utf-8")
            (Path(journal_tmp) / "2026-03-02.md").write_text("[[anxiety]] [[sleep]]", encoding="utf-8")

            md_path, json_path = vault_mapper.build_vault_map(
                journal_dir=journal_tmp,
                learning_file="/nonexistent/scribe_learning.json",
                output_dir=out_tmp,
                min_cooccurrence=2,
            )

            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("anxiety", data["entities"])
            self.assertEqual(data["source_notes"], 2)


if __name__ == "__main__":
    unittest.main()
