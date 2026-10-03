"""Nutrition voice ledger: parse, lookup, memory, and the day summary."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import nutrition_ledger as nl  # noqa: E402


def _load_process_intents():
    path = SCRIPTS_DIR / "process_intents.py"
    spec = importlib.util.spec_from_file_location("process_intents", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


NOTE = """# 2026-10-02

> [!voice] 8:15
> I had [[fourteen]] grams of [[chicken]] and a glass of this.

> [!voice] 12:40
> I had a Bev.
"""


class TestVoiceCallouts(unittest.TestCase):
    def test_parses_times_and_strips_wikilinks(self):
        callouts = nl.extract_voice_callouts(NOTE)
        self.assertEqual(
            [(c["spoken_at"], c["raw_text"]) for c in callouts],
            [
                ("08:15", "I had fourteen grams of chicken and a glass of this."),
                ("12:40", "I had a Bev."),
            ],
        )


class TestValidation(unittest.TestCase):
    def test_stated_grams_are_kept(self):
        parsed = nl.validate_extract({
            "is_intake": True,
            "meal_slot": "lunch",
            "items": [{
                "label": "chicken",
                "kind": "food",
                "identity": "generic",
                "quantity": 14,
                "unit": "grams",
                "serving_basis": "stated",
                "calories_est": 23,
                "protein_g_est": 4,
                "carbs_g_est": 0,
                "fat_g_est": 0.5,
            }],
        })
        item = parsed["items"][0]
        self.assertEqual(item["quantity"], 14)
        self.assertEqual(item["unit"], "g")
        self.assertEqual(item["serving_basis"], "stated")
        self.assertEqual(item["calories_est"], 23)
        self.assertFalse(item["needs_lookup"])

    def test_unresolved_clears_macros_and_skips_lookup(self):
        parsed = nl.validate_extract({
            "is_intake": True,
            "meal_slot": "drink",
            "items": [{
                "label": "this",
                "kind": "drink",
                "identity": "unresolved",
                "quantity": 1,
                "unit": "glass",
                "serving_basis": "stated",
                "unresolved": True,
                "needs_lookup": True,
                "calories_est": 80,
            }],
        })
        item = parsed["items"][0]
        self.assertEqual(item["label"], "unresolved")
        self.assertTrue(item["unresolved"])
        self.assertFalse(item["needs_lookup"])
        self.assertIsNone(item["calories_est"])

    def test_branded_name_is_kept_and_macros_cleared(self):
        parsed = nl.validate_extract({
            "is_intake": True,
            "meal_slot": "unknown",
            "items": [{
                "label": "Bev",
                "kind": "drink",
                "identity": "branded",
                "quantity": 1,
                "unit": "can",
                "serving_basis": "default_serving",
                "needs_lookup": True,
                "calories_est": 150,
            }],
        })
        item = parsed["items"][0]
        self.assertEqual(item["label"], "Bev")
        self.assertEqual(item["identity"], "branded")
        self.assertTrue(item["needs_lookup"])
        self.assertIsNone(item["calories_est"])


class TestStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.journal = Path(self.tmp.name) / "journal"
        self.journal.mkdir()
        self.note = self.journal / "2026-10-02.md"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, text, payload, lookup=None):
        with mock.patch.object(nl, "call_nutrition_model", return_value=payload) as model, \
                mock.patch.object(nl, "lookup_product", side_effect=lookup) as web:
            count = nl.run_nutrition_stage(
                text, self.note, "2026-10-02", self.state,
                journal_dir=self.journal,
            )
        return count, model, web

    def test_unresolved_drink_does_not_search(self):
        text = "> [!voice] 09:00\n> a glass of this\n"
        payload = {
            "is_intake": True,
            "meal_slot": "drink",
            "items": [{
                "label": "this",
                "kind": "drink",
                "identity": "unresolved",
                "quantity": 1,
                "unit": "glass",
                "serving_basis": "stated",
                "unresolved": True,
                "calories_est": 90,
            }],
        }
        count, _model, web = self._run(text, payload)
        self.assertEqual(count, 1)
        web.assert_not_called()
        rows = nl.load_ledger_rows(self.state)
        self.assertIsNone(rows[0]["items"][0]["calories_est"])
        self.assertEqual(rows[0]["items"][0]["label"], "unresolved")

    def test_stated_grams_survive_the_stage(self):
        text = "> [!voice] 09:00\n> fourteen grams of chicken\n"
        payload = {
            "is_intake": True,
            "meal_slot": "unknown",
            "items": [{
                "label": "chicken",
                "kind": "food",
                "identity": "generic",
                "quantity": 14,
                "unit": "grams",
                "serving_basis": "stated",
                "calories_est": 23,
            }],
        }
        self._run(text, payload)
        item = nl.load_ledger_rows(self.state)[0]["items"][0]
        self.assertEqual(item["quantity"], 14)
        self.assertEqual(item["unit"], "g")
        self.assertEqual(item["serving_basis"], "stated")

    def test_non_intake_marks_processed_without_a_ledger_row(self):
        text = "> [!voice] 09:00\n> I should eat more protein\n"
        count, _model, web = self._run(text, {"is_intake": False, "items": []})
        self.assertEqual(count, 0)
        web.assert_not_called()
        self.assertFalse(nl.ledger_path(self.state).exists())
        self.assertTrue(nl.processed_path(self.state).exists())

    def test_second_run_does_not_extract_again(self):
        text = "> [!voice] 09:00\n> I had a banana\n"
        payload = {
            "is_intake": True,
            "meal_slot": "snack",
            "items": [{
                "label": "banana",
                "kind": "food",
                "identity": "generic",
                "quantity": 1,
                "unit": "serving",
                "serving_basis": "default_serving",
                "calories_est": 105,
            }],
        }
        with mock.patch.object(nl, "call_nutrition_model", return_value=payload) as model:
            nl.run_nutrition_stage(text, self.note, "2026-10-02", self.state)
            nl.run_nutrition_stage(text, self.note, "2026-10-02", self.state)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(nl.load_ledger_rows(self.state)), 1)

    def test_extract_failure_is_isolated(self):
        text = "> [!voice] 09:00\n> I had a banana\n"
        with mock.patch.object(nl, "call_nutrition_model", side_effect=RuntimeError("down")):
            count = nl.run_nutrition_stage(text, self.note, "2026-10-02", self.state)
        self.assertEqual(count, 0)
        self.assertFalse(nl.processed_path(self.state).exists())
        self.assertFalse(nl.ledger_path(self.state).exists())

    def test_dry_run_writes_nothing_and_does_not_search(self):
        text = "> [!voice] 09:00\n> I had a Bev\n"
        payload = {
            "is_intake": True,
            "meal_slot": "drink",
            "items": [{
                "label": "Bev",
                "kind": "drink",
                "identity": "branded",
                "quantity": 1,
                "unit": "can",
                "serving_basis": "default_serving",
                "needs_lookup": True,
                "calories_est": 100,
            }],
        }
        buf = StringIO()
        with mock.patch.object(nl, "call_nutrition_model", return_value=payload), \
                mock.patch.object(nl, "lookup_product") as web, \
                mock.patch("sys.stdout", buf):
            count = nl.run_nutrition_stage(
                text, self.note, "2026-10-02", self.state, dry_run=True,
            )
        self.assertEqual(count, 1)
        web.assert_not_called()
        self.assertFalse(nl.ledger_path(self.state).exists())
        self.assertFalse(nl.processed_path(self.state).exists())
        self.assertIn("Bev", buf.getvalue())

    def test_cache_hit_skips_web_and_scales(self):
        nl.save_cache(self.state, {"labels": {"bev": {
            "status": "ok",
            "query": "Bev nutrition facts",
            "matched_name": "Bev mango",
            "serving_description": "1 can",
            "source_urls": ["https://example.com/bev"],
            "per_serving": {"calories": 100, "protein_g": 1, "carbs_g": 20, "fat_g": 0},
        }}})
        text = "> [!voice] 09:00\n> two Bevs\n"
        payload = {
            "is_intake": True,
            "meal_slot": "drink",
            "items": [{
                "label": "Bev",
                "kind": "drink",
                "identity": "branded",
                "quantity": 2,
                "unit": "cans",
                "serving_basis": "stated",
                "needs_lookup": True,
            }],
        }
        _count, _model, web = self._run(text, payload)
        web.assert_not_called()
        item = nl.load_ledger_rows(self.state)[0]["items"][0]
        self.assertEqual(item["lookup"]["status"], "cached")
        self.assertEqual(item["calories_est"], 200)
        self.assertEqual(item["label"], "Bev")

    def test_ok_without_url_is_stored_as_miss(self):
        text = "> [!voice] 09:00\n> I had a Bev\n"
        payload = {
            "is_intake": True,
            "meal_slot": "unknown",
            "items": [{
                "label": "Bev",
                "kind": "drink",
                "identity": "branded",
                "quantity": 1,
                "unit": "can",
                "serving_basis": "stated",
                "needs_lookup": True,
                "calories_est": 80,
            }],
        }

        def _lookup(label, extra_context="", model=None):
            return {
                "status": "ok",
                "query": f"{label} nutrition facts",
                "matched_name": "Some Bev",
                "serving_description": "1 can",
                "source_urls": [],
                "per_serving": {"calories": 80, "protein_g": 0, "carbs_g": 20, "fat_g": 0},
            }

        self._run(text, payload, lookup=_lookup)
        item = nl.load_ledger_rows(self.state)[0]["items"][0]
        self.assertEqual(item["lookup"]["status"], "miss")
        self.assertIsNone(item["calories_est"])
        cached = nl.load_cache(self.state)["labels"]["bev"]
        self.assertEqual(cached["status"], "miss")

    def test_ambiguous_followup_uses_journal_and_still_misses_without_url(self):
        (self.journal / "2026-10-01.md").write_text("Bev is the mango soda\n", encoding="utf-8")
        text = "> [!voice] 09:00\n> I had a Bev\n"
        payload = {
            "is_intake": True,
            "meal_slot": "unknown",
            "items": [{
                "label": "Bev",
                "kind": "drink",
                "identity": "branded",
                "quantity": 1,
                "unit": "can",
                "serving_basis": "stated",
                "needs_lookup": True,
            }],
        }
        calls = []

        def _lookup(label, extra_context="", model=None):
            calls.append(extra_context)
            if len(calls) == 1:
                return {
                    "status": "ambiguous",
                    "query": label,
                    "matched_name": "",
                    "serving_description": "",
                    "source_urls": [],
                    "per_serving": None,
                }
            return {
                "status": "ok",
                "query": label,
                "matched_name": "Bev mango",
                "serving_description": "1 can",
                "source_urls": [],
                "per_serving": {"calories": 40, "protein_g": 0, "carbs_g": 10, "fat_g": 0},
            }

        self._run(text, payload, lookup=_lookup)
        self.assertEqual(len(calls), 2)
        self.assertIn("Bev is the mango soda", calls[1])
        item = nl.load_ledger_rows(self.state)[0]["items"][0]
        self.assertEqual(item["lookup"]["status"], "miss")
        self.assertIsNone(item["calories_est"])


class TestMemory(unittest.TestCase):
    def _stated_bev(self):
        return {
            "source_date": "2026-10-01",
            "raw_text": "I had a Bev",
            "items": [{
                "label": "Bev",
                "identity": "branded",
                "quantity": 1,
                "unit": "can",
                "serving_basis": "stated",
                "unresolved": False,
            }],
        }

    def test_three_agreeing_rows_unlock_typical_amount(self):
        memory = nl.empty_memory()
        for _ in range(3):
            nl.update_memory_from_row(memory, self._stated_bev())
        record = memory["labels"]["bev"]
        self.assertGreater(record["reinforcement"], 0)
        self.assertEqual(record["typical"], {"quantity": 1, "unit": "can"})
        message = nl.build_extract_user("09:00", "a Bev", memory)
        self.assertIn("Known items:", message)
        self.assertIn("Bev", message)
        self.assertIn("usual 1 can", message)
        item = {
            "label": "Bev",
            "quantity": 1,
            "unit": "serving",
            "serving_basis": "default_serving",
            "calories_est": 10,
            "protein_g_est": 0,
            "carbs_g_est": 0,
            "fat_g_est": 0,
        }
        nl.apply_memory_priors([item], memory)
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["unit"], "can")
        self.assertEqual(item["serving_basis"], "remembered")

    def test_stated_amount_beats_memory(self):
        memory = nl.empty_memory()
        for _ in range(3):
            nl.update_memory_from_row(memory, self._stated_bev())
        item = {
            "label": "chicken",
            "quantity": 14,
            "unit": "g",
            "serving_basis": "stated",
            "calories_est": 23,
        }
        memory["labels"]["chicken"] = {
            "display": "chicken",
            "seen_count": 5,
            "success_count": 4,
            "failure_count": 0,
            "reinforcement": 1.0,
            "last_seen_date": "2026-10-01",
            "last_success_date": "2026-10-01",
            "typical": {"quantity": 1, "unit": "serving"},
            "resolved_name": "",
            "per_serving": None,
            "contexts": [],
            "recent_amounts": [],
        }
        nl.apply_memory_priors([item], memory)
        self.assertEqual(item["quantity"], 14)
        self.assertEqual(item["unit"], "g")
        self.assertEqual(item["serving_basis"], "stated")

    def test_negative_reinforcement_is_left_out_of_known_items(self):
        memory = nl.empty_memory()
        memory["labels"]["bev"] = {
            "display": "Bev",
            "seen_count": 5,
            "success_count": 1,
            "failure_count": 2,
            "reinforcement": -0.5,
            "last_seen_date": "2026-10-01",
            "last_success_date": "",
            "typical": {"quantity": 1, "unit": "can"},
            "resolved_name": "",
            "per_serving": None,
            "contexts": [],
            "recent_amounts": [],
        }
        self.assertNotIn("Bev", nl.format_known_items(memory))
        item = {
            "label": "Bev",
            "quantity": 1,
            "unit": "serving",
            "serving_basis": "default_serving",
        }
        nl.apply_memory_priors([item], memory)
        self.assertEqual(item["serving_basis"], "default_serving")
        self.assertEqual(item["unit"], "serving")

    def test_correction_updates_typical_and_lowers_score(self):
        memory = nl.empty_memory()
        nl.update_memory_from_row(memory, self._stated_bev())
        memory["labels"]["bev"]["reinforcement"] = 0.2
        rows = [self._stated_bev()]
        nl.apply_reinforcement(
            memory,
            rows,
            [{"label": "Bev", "quantity": 2, "unit": "can", "resolved_name": "", "reason": "said two"}],
            "2026-10-01",
        )
        record = memory["labels"]["bev"]
        self.assertEqual(record["typical"], {"quantity": 2, "unit": "can"})
        self.assertEqual(record["reinforcement"], -0.3)
        self.assertEqual(record["failure_count"], 1)
        self.assertEqual(memory["last_reinforced_date"], "2026-10-01")

    def test_second_summary_does_not_score_the_same_day(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        journal = Path(tmp.name) / "notes"
        journal.mkdir()
        (journal / "2026-10-01.md").write_text("I had a Bev\n", encoding="utf-8")
        nl._append_jsonl(nl.ledger_path(state), {
            "source_date": "2026-10-01",
            "is_intake": True,
            "spoken_at": "09:00",
            "items": [{
                "label": "Bev",
                "quantity": 1,
                "unit": "can",
                "calories_est": 100,
                "unresolved": False,
            }],
        })
        with mock.patch.object(nl, "call_nutrition_model", return_value={"corrections": []}) as model:
            first = nl.reinforce_closed_day(state, journal, "2026-10-01")
            second = nl.reinforce_closed_day(state, journal, "2026-10-01")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(model.call_count, 1)


class TestDaySummary(unittest.TestCase):
    def _row(self, spoken_at, items):
        return {
            "source_date": "2026-10-02",
            "is_intake": True,
            "spoken_at": spoken_at,
            "logged_at": spoken_at,
            "items": items,
        }

    def test_orders_items_and_sums_known_calories(self):
        rows = [
            self._row("12:00", [{
                "label": "chicken alfredo",
                "quantity": 1,
                "unit": "serving",
                "calories_est": 560,
            }]),
            self._row("08:00", [
                {"label": "oat milk", "quantity": 2, "unit": "glass", "calories_est": 240},
                {"label": "chicken", "quantity": 14, "unit": "g", "calories_est": 23},
            ]),
            self._row("15:00", [{
                "label": "Bev",
                "quantity": 1,
                "unit": "can",
                "calories_est": None,
            }]),
        ]
        # spoken order is applied inside format, not input order
        title, body = nl.format_day_summary(rows, "2026-10-02")
        self.assertEqual(title, "823 kcal")
        self.assertEqual(
            body.splitlines(),
            [
                "2 glasses oat milk",
                "14 g chicken",
                "1 serving chicken alfredo",
                "1 can Bev",
                "Total omits 1 items with no estimate",
            ],
        )

    def test_empty_day_returns_no_message(self):
        self.assertIsNone(nl.format_day_summary([], "2026-10-02"))
        self.assertIsNone(nl.format_day_summary([
            {"source_date": "2026-10-01", "is_intake": True, "spoken_at": "09:00", "items": [
                {"label": "oat milk", "quantity": 1, "unit": "glass", "calories_est": 120},
            ]},
        ], "2026-10-02"))

    def test_long_list_truncates(self):
        items = [
            {"label": f"item-{index:03d}", "quantity": 1, "unit": "serving", "calories_est": 10}
            for index in range(80)
        ]
        title, body = nl.format_day_summary([self._row("09:00", items)], "2026-10-02")
        self.assertEqual(title, "800 kcal")
        self.assertLessEqual(len(body), nl.PUSHOVER_MESSAGE_LIMIT)
        self.assertIn("and ", body)
        self.assertTrue(body.strip().endswith("more") or "and " in body.splitlines()[-1])


class TestPipelineHook(unittest.TestCase):
    def test_voice_intake_is_counted_without_failing_the_run(self):
        pi = _load_process_intents()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        note = root / "2026-10-02.md"
        note.write_text("> [!voice] 09:00\n> I had a banana\n", encoding="utf-8")
        state = root / "state"
        state.mkdir()
        payload = root / "payload.json"
        payload_env = os.environ.get("JOURNAL_LINKER_JOB_PAYLOAD_FILE")
        os.environ["JOURNAL_LINKER_JOB_PAYLOAD_FILE"] = str(payload)
        extract = {
            "is_intake": True,
            "meal_slot": "snack",
            "items": [{
                "label": "banana",
                "kind": "food",
                "identity": "generic",
                "quantity": 1,
                "unit": "serving",
                "serving_basis": "default_serving",
                "calories_est": 105,
            }],
        }
        try:
            with mock.patch.object(nl, "call_nutrition_model", return_value=extract), \
                    mock.patch.object(pi, "call_gate", return_value=[]):
                code = pi.run_intent_pipeline(
                    note,
                    gate_model="test-gate",
                    gate_style="phi4",
                    routing_model="test-route",
                    cortex_dir=root / "cortex",
                    state_dir=state,
                    enrichment_mode="off",
                    in_flight_ttl=30,
                    dry_run=True,
                    verbose=False,
                )
        finally:
            if payload_env is None:
                os.environ.pop("JOURNAL_LINKER_JOB_PAYLOAD_FILE", None)
            else:
                os.environ["JOURNAL_LINKER_JOB_PAYLOAD_FILE"] = payload_env
        self.assertEqual(code, 0)
        saved = json.loads(payload.read_text(encoding="utf-8"))
        self.assertEqual(saved["nutrition_logged"], 1)
        self.assertEqual(pi.RunSummary().nutrition_logged, 0)
