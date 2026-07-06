"""Tests for scripts/hot_seat_fetch.py — SEC 10-K fetch into hot_seat.

Network (SEC EDGAR) and OpenAI are mocked. Filesystem uses tempdirs.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hot_seat_fetch.py"
spec = importlib.util.spec_from_file_location("hot_seat_fetch", SCRIPT_PATH)
hsf = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hsf)


TICKERS = [
    {"ticker": "F", "title": "FORD MOTOR CO", "cik_str": 37996},
    {"ticker": "FORD", "title": "FORWARD INDUSTRIES INC", "cik_str": 38264},
    {"ticker": "AAPL", "title": "Apple Inc.", "cik_str": 320193},
    {"ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC", "cik_str": 1067983},
]

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-Q"],
            "accessionNumber": ["0000037996-26-000001", "0000037996-26-000015", "0000037996-25-000009"],
            "primaryDocument": ["f-8k.htm", "f-20251231.htm", "f-10q.htm"],
            "filingDate": ["2026-03-01", "2026-02-11", "2025-10-20"],
            "reportDate": ["2026-02-28", "2025-12-31", "2025-09-30"],
        }
    }
}


class TestLocalResolver(unittest.TestCase):
    def test_common_name_beats_ticker_collision(self):
        # "Ford" (word) should resolve to the automaker F, not ticker FORD.
        res = hsf.local_resolve_ticker("Ford", TICKERS)
        self.assertEqual(res["ticker"], "F")

    def test_explicit_uppercase_ticker(self):
        res = hsf.local_resolve_ticker("FORD", TICKERS)
        self.assertEqual(res["ticker"], "FORD")

    def test_ticker_symbol(self):
        self.assertEqual(hsf.local_resolve_ticker("AAPL", TICKERS)["ticker"], "AAPL")

    def test_lowercase_name(self):
        self.assertEqual(hsf.local_resolve_ticker("apple", TICKERS)["ticker"], "AAPL")

    def test_multiword_name(self):
        self.assertEqual(
            hsf.local_resolve_ticker("berkshire hathaway", TICKERS)["ticker"], "BRK-B"
        )

    def test_unknown_returns_none(self):
        self.assertIsNone(hsf.local_resolve_ticker("zzzznotacompany", TICKERS))


class TestResolveTicker(unittest.TestCase):
    def test_openai_result_validated_against_edgar(self):
        with mock.patch.object(hsf, "openai_resolve_ticker", return_value="AAPL"):
            res = hsf.resolve_ticker("the iphone company", tickers=TICKERS, mode="auto")
        self.assertEqual(res["ticker"], "AAPL")
        self.assertEqual(res["source"], "openai")

    def test_openai_invalid_falls_back_to_local(self):
        with mock.patch.object(hsf, "openai_resolve_ticker", return_value="NOTREAL"):
            res = hsf.resolve_ticker("Ford", tickers=TICKERS, mode="auto")
        self.assertEqual(res["ticker"], "F")
        self.assertEqual(res["source"], "edgar_local")

    def test_local_mode_skips_openai(self):
        with mock.patch.object(hsf, "openai_resolve_ticker") as m:
            res = hsf.resolve_ticker("apple", tickers=TICKERS, mode="local")
            m.assert_not_called()
        self.assertEqual(res["ticker"], "AAPL")

    def test_strips_wikilinks_before_resolving(self):
        res = hsf.resolve_ticker("[[apple]]", tickers=TICKERS, mode="local")
        self.assertEqual(res["ticker"], "AAPL")

    def test_collapses_spelled_out_letters_before_resolving(self):
        res = hsf.resolve_ticker("A-A-P-L", tickers=TICKERS, mode="local")
        self.assertEqual(res["ticker"], "AAPL")

    def test_alias_wins_over_openai_and_local(self):
        tickers = TICKERS + [{"ticker": "OMC", "title": "OMNICOM GROUP INC", "cik_str": 30371}]
        with mock.patch.object(hsf, "load_ticker_aliases", return_value={"OCM": "OMC"}), \
                mock.patch.object(hsf, "openai_resolve_ticker", return_value="AAPL"):
            res = hsf.resolve_ticker("OCM", tickers=tickers, mode="auto")
        self.assertEqual(res["ticker"], "OMC")
        self.assertEqual(res["source"], "alias")

    def test_alias_miss_falls_through_to_local(self):
        with mock.patch.object(hsf, "load_ticker_aliases", return_value={"OCM": "OMC"}):
            res = hsf.resolve_ticker("apple", tickers=TICKERS, mode="local")
        self.assertEqual(res["ticker"], "AAPL")
        self.assertEqual(res["source"], "edgar_local")


class TestFuzzyResolveTicker(unittest.TestCase):
    def setUp(self):
        self.tickers_with_omc = TICKERS + [
            {"ticker": "OMC", "title": "OMNICOM GROUP INC", "cik_str": 30371},
        ]

    def test_catches_ticker_transposition(self):
        # Real case: "OCM" heard/spoken for the real ticker "OMC" (Omnicom).
        # Exact/token/prefix scoring in local_resolve_ticker can't catch a
        # single-letter transposition in a 3-char ticker.
        res = hsf.fuzzy_resolve_ticker("OCM", self.tickers_with_omc)
        self.assertIsNotNone(res)
        self.assertEqual(res["ticker"], "OMC")
        self.assertEqual(res["source"], "fuzzy")
        self.assertTrue(res["low_confidence"])

    def test_below_threshold_returns_none(self):
        self.assertIsNone(hsf.fuzzy_resolve_ticker("zzznotarealticker", TICKERS))

    def test_empty_query_returns_none(self):
        self.assertIsNone(hsf.fuzzy_resolve_ticker("", TICKERS))

    def test_wired_as_last_resort_in_resolve_ticker(self):
        # local_resolve_ticker finds nothing for "OCM" against these tickers
        # (no exact/token/prefix match), so resolve_ticker should fall
        # through to the fuzzy tier automatically. Alias tier disabled here
        # so this test isolates the fuzzy fallthrough specifically (the
        # alias seed table already has its own OCM->OMC entry, tested
        # separately in TestResolveTicker).
        with mock.patch.object(hsf, "load_ticker_aliases", return_value={}):
            res = hsf.resolve_ticker("OCM", tickers=self.tickers_with_omc, mode="local")
        self.assertEqual(res["ticker"], "OMC")
        self.assertEqual(res["source"], "fuzzy")

    def test_alias_and_local_take_priority_over_fuzzy(self):
        # A real local match should win outright, never falling to fuzzy.
        res = hsf.resolve_ticker("apple", tickers=TICKERS, mode="local")
        self.assertEqual(res["source"], "edgar_local")


class TestResolverConfidence(unittest.TestCase):
    def test_fuzzy_is_low(self):
        # The OCM->OMC transposition scores difflib ratio 0.667 (over the fuzzy
        # bar) but should still map to the shaky 0.5 tier so it gets confirmed.
        self.assertEqual(hsf.resolver_confidence("fuzzy"), 0.5)

    def test_confident_sources(self):
        for source in ("edgar_local", "alias", "openai", "exact", None):
            self.assertEqual(hsf.resolver_confidence(source), 1.0)


class TestLoadTickerAliases(unittest.TestCase):
    def test_loads_default_seed_file(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOT_SEAT_ALIAS_FILE", None)
            aliases = hsf.load_ticker_aliases()
        self.assertEqual(aliases.get("OCM"), "OMC")

    def test_env_override_file(self):
        with tempfile.TemporaryDirectory() as td:
            custom = Path(td) / "my_aliases.json"
            custom.write_text(json.dumps({"XYZ": {"ticker": "abc"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOT_SEAT_ALIAS_FILE": str(custom)}):
                aliases = hsf.load_ticker_aliases()
            self.assertEqual(aliases, {"XYZ": "ABC"})

    def test_missing_override_file_returns_empty(self):
        with mock.patch.dict(os.environ, {"HOT_SEAT_ALIAS_FILE": "/tmp/does-not-exist-xyz.json"}):
            self.assertEqual(hsf.load_ticker_aliases(), {})

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text("{not valid json", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOT_SEAT_ALIAS_FILE": str(bad)}):
                self.assertEqual(hsf.load_ticker_aliases(), {})


class TestResolveLatestFiling(unittest.TestCase):
    def test_picks_latest_10k_and_fy(self):
        with mock.patch.object(hsf, "_http_get_json", return_value=SUBMISSIONS):
            filing = hsf.resolve_latest_filing(37996, form="10-K")
        self.assertEqual(filing["accession"], "0000037996-26-000015")
        self.assertEqual(filing["primary_doc"], "f-20251231.htm")
        self.assertEqual(filing["fy"], "2025")
        self.assertEqual(filing["accession_nodash"], "000003799626000015")

    def test_no_matching_form(self):
        with mock.patch.object(hsf, "_http_get_json", return_value={"filings": {"recent": {"form": ["8-K"]}}}):
            self.assertIsNone(hsf.resolve_latest_filing(37996, form="10-K"))


class TestDownload(unittest.TestCase):
    def test_folder_name(self):
        self.assertEqual(hsf._folder_name("f", "10-K", "2025"), "F_10K_FY2025")

    def test_download_and_idempotency(self):
        filing = {
            "form": "10-K", "fy": "2025",
            "accession_nodash": "000003799626000015",
            "primary_doc": "f-20251231.htm",
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with mock.patch.object(hsf, "_http_get", return_value=b"<html>10-K</html>") as m:
                first = hsf.download_filing("F", 37996, filing, base_dir=base)
                self.assertTrue(first["downloaded"])
                self.assertTrue(Path(first["file"]).exists())
                self.assertEqual(m.call_count, 1)
                second = hsf.download_filing("F", 37996, filing, base_dir=base)
                self.assertFalse(second["downloaded"])
                self.assertEqual(m.call_count, 1)  # not re-fetched


class TestFetch10K(unittest.TestCase):
    def test_end_to_end_mocked(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with mock.patch.object(hsf, "_http_get_json", return_value=SUBMISSIONS), \
                 mock.patch.object(hsf, "_http_get", return_value=b"<html>10-K</html>"), \
                 mock.patch.object(hsf, "openai_resolve_ticker", return_value=None):
                res = hsf.fetch_10k("Ford", base_dir=base, resolver_mode="local", tickers=TICKERS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["ticker"], "F")
        self.assertEqual(res["fy"], "2025")
        self.assertEqual(res["folder_name"], "F_10K_FY2025")
        self.assertTrue(res["downloaded"])

    def test_dry_run_no_download(self):
        with mock.patch.object(hsf, "_http_get_json", return_value=SUBMISSIONS), \
             mock.patch.object(hsf, "_http_get") as m_dl, \
             mock.patch.object(hsf, "openai_resolve_ticker", return_value=None):
            res = hsf.fetch_10k("apple", resolver_mode="local", dry_run=True, tickers=TICKERS)
            m_dl.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["ticker"], "AAPL")

    def test_unresolvable_ticker(self):
        with mock.patch.object(hsf, "openai_resolve_ticker", return_value=None):
            res = hsf.fetch_10k("zzzzznope", resolver_mode="local", tickers=TICKERS)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "resolve")


if __name__ == "__main__":
    unittest.main()
