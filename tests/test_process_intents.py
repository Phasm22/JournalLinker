"""Tests for scripts/process_intents.py — intent capture pipeline.

All external services (Ollama, Anthropic API, Pushover) are mocked.
Tests use tempfile.TemporaryDirectory for isolation.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import urllib.parse

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_intents.py"
spec = importlib.util.spec_from_file_location("process_intents", SCRIPT_PATH)
pi = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pi)

FIXTURE_NOTE = Path(__file__).resolve().parent / "fixtures" / "intent_sample.md"
LIVE_OLLAMA_SMOKE = os.getenv("JOURNAL_LINKER_LIVE_SMOKE") == "1"


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

class TestGatePromptTemplates(unittest.TestCase):
    def test_phi4_prompt_contains_json_schema(self):
        prompt = pi.build_gate_prompt("I need to call the doctor.", "phi4")
        self.assertIn("intents", prompt)
        self.assertIn("intent_raw", prompt)
        self.assertIn("category", prompt)
        self.assertIn("intent_class", prompt)
        self.assertIn("already handled", prompt)

    def test_qwen25_prompt_uses_chatml(self):
        prompt = pi.build_gate_prompt("I need to call the doctor.", "qwen25")
        self.assertIn("<|im_start|>", prompt)
        self.assertIn("intents", prompt)
        self.assertIn("another system already handles", prompt)

    def test_resolve_gate_style_auto_phi4(self):
        style = pi.resolve_gate_style("phi4:14b")
        self.assertEqual(style, "phi4")

    def test_resolve_gate_style_auto_qwen(self):
        style = pi.resolve_gate_style("qwen2.5:32b")
        self.assertEqual(style, "qwen25")

    def test_resolve_gate_style_explicit_override(self):
        with mock.patch.dict(os.environ, {"INTENT_GATE_STYLE": "qwen25"}):
            style = pi.resolve_gate_style("phi4:14b")
        self.assertEqual(style, "qwen25")


class TestParseGateOutput(unittest.TestCase):
    def test_single_intent(self):
        out = pi._parse_gate_output({
            "intents": [{"intent_raw": "call the doctor", "category": "task", "intent_class": "task_intent"}],
        })
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["intent_raw"], "call the doctor")
        self.assertEqual(out[0]["category"], "task")
        self.assertEqual(out[0]["intent_class"], "task_intent")

    def test_new_intent_classes_are_accepted(self):
        out = pi._parse_gate_output({
            "intents": [
                {"intent_raw": "look into a new backpack", "category": "plan", "intent_class": "purchase_intent"},
                {"intent_raw": "learn more about ceramics", "category": "plan", "intent_class": "latent_interest"},
            ],
        })
        self.assertEqual(out[0]["intent_class"], "purchase_intent")
        self.assertEqual(out[1]["intent_class"], "latent_interest")

    def test_missing_intent_class_falls_back_from_category(self):
        out = pi._parse_gate_output({
            "intents": [{"intent_raw": "finish the report", "category": "commitment"}],
        })
        self.assertEqual(out[0]["intent_class"], "task_intent")

    def test_invalid_intent_class_falls_back_to_none(self):
        out = pi._parse_gate_output({
            "intents": [{"intent_raw": "x", "category": "task", "intent_class": "INVALID"}],
        })
        self.assertEqual(out[0]["intent_class"], "none")

    def test_multiple_intents(self):
        out = pi._parse_gate_output({
            "intents": [
                {"intent_raw": "call the doctor", "category": "task"},
                {"intent_raw": "finish the report", "category": "commitment"},
            ],
        })
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["intent_raw"], "finish the report")

    def test_empty_intents_list(self):
        out = pi._parse_gate_output({"intents": []})
        self.assertEqual(out, [])

    def test_missing_intents_key_returns_empty(self):
        out = pi._parse_gate_output({})
        self.assertEqual(out, [])

    def test_invalid_category_falls_back_to_none(self):
        out = pi._parse_gate_output({"intents": [{"intent_raw": "x", "category": "INVALID"}]})
        self.assertEqual(out[0]["category"], "none")

    def test_items_with_empty_intent_raw_are_skipped(self):
        out = pi._parse_gate_output({"intents": [{"intent_raw": "", "category": "task"}]})
        self.assertEqual(out, [])

    def test_max_intents_cap_is_respected(self):
        many = [{"intent_raw": f"intent {i}", "category": "task"} for i in range(20)]
        with mock.patch.dict(os.environ, {"INTENT_MAX_INTENTS_PER_NOTE": "3"}):
            out = pi._parse_gate_output({"intents": many})
        self.assertEqual(len(out), 3)


class TestGateChunking(unittest.TestCase):
    def test_paragraph_chunks_prioritize_actionable_late_content(self):
        note = (
            "Just reflecting on the week and writing a lot of background text.\n\n"
            + ("filler words " * 300)
            + "\n\nNeed to call the dentist tomorrow morning."
        )
        chunks = pi._paragraph_chunks(note, max_chars=2000, top_n=2)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertTrue(any("call the dentist" in chunk.lower() for chunk in chunks))

    def test_call_gate_merges_and_dedupes_intents_across_chunks(self):
        note = "Filler.\n\nNeed to buy bike shoes.\n\nAlso need to buy bike shoes."
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.side_effect = [
            {"message": {"content": '{"intents":[{"intent_raw":"buy bike shoes","category":"task","intent_class":"purchase_intent"}]}'}},
            {"message": {"content": '{"intents":[{"intent_raw":"buy bike shoes","category":"task","intent_class":"purchase_intent"}]}'}},
        ]
        with (
            mock.patch.dict("sys.modules", {"ollama": mock_ollama}),
            mock.patch.object(pi, "_paragraph_chunks", return_value=["Need to buy bike shoes.", "Also need to buy bike shoes."]),
        ):
            intents = pi.call_gate(note, model="phi4:14b", style="phi4")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["intent_raw"], "buy bike shoes")


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

class TestIdempotencyKey(unittest.TestCase):
    def _key(self, **overrides):
        defaults = dict(
            source_path=Path("/tmp/2026-04-16.md"),
            source_date="2026-04-16",
            intent_raw="call doctor",
            category="task",
            gate_model="phi4:14b",
            gate_style="phi4",
            intent_class="task_intent",
        )
        defaults.update(overrides)
        return pi.compute_idempotency_key(**defaults)

    def test_key_is_64_hex_chars(self):
        key = self._key()
        self.assertEqual(len(key), 64)
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_key_is_deterministic(self):
        self.assertEqual(self._key(), self._key())

    def test_key_changes_when_intent_changes(self):
        k1 = self._key(intent_raw="call doctor")
        k2 = self._key(intent_raw="email accountant")
        self.assertNotEqual(k1, k2)

    def test_key_stable_across_stat_changes(self):
        # Same intent on same day should dedup even if file is re-saved
        k1 = self._key()
        k2 = self._key()  # no stat in key anymore
        self.assertEqual(k1, k2)

    def test_key_changes_when_model_changes(self):
        k1 = self._key(gate_model="phi4:14b")
        k2 = self._key(gate_model="qwen2.5:32b")
        self.assertNotEqual(k1, k2)

    def test_key_changes_when_intent_class_changes(self):
        k1 = self._key(intent_class="task_intent")
        k2 = self._key(intent_class="purchase_intent")
        self.assertNotEqual(k1, k2)


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------

class TestEnvelopeBuilder(unittest.TestCase):
    def test_required_fields_present(self):
        env = pi.build_envelope(
            FIXTURE_NOTE, "2026-04-16T00:00:00", "123:456",
            "follow up with accountant", "task", "off",
        )
        for field in ("intent_raw", "surrounding_context", "inferred_category", "timestamp",
                      "source_file", "source_stat", "enrichment_mode", "prompt_version",
                      "intent_class"):
            self.assertIn(field, env, f"missing field: {field}")

    def test_mcp_fields_absent_when_off(self):
        env = pi.build_envelope(FIXTURE_NOTE, "2026-04-16T00:00:00", "1:1", "x", "task", "off")
        self.assertNotIn("recurrence_signal", env)
        self.assertNotIn("related_silo_hits", env)

    def test_llmlib_fields_not_in_base_envelope(self):
        # build_envelope never adds enrichment fields; enrich_envelope() does that
        env = pi.build_envelope(FIXTURE_NOTE, "2026-04-16T00:00:00", "1:1", "x", "task", "llmlib")
        self.assertNotIn("recurrence_signal", env)
        self.assertNotIn("related_silo_hits", env)


# ---------------------------------------------------------------------------
# Claude response validation
# ---------------------------------------------------------------------------

class TestClaudeResponseValidation(unittest.TestCase):
    def test_valid_response(self):
        resp = pi._validate_claude_response({
            "urgency": "today", "format": "notification",
            "action": "notification",
            "title": "Follow up with accountant",
            "body": "Send the Q1 tax email before Friday.",
            "defer_to": "",
        })
        self.assertEqual(resp["urgency"], "today")
        self.assertEqual(resp["format"], "notification")
        self.assertEqual(resp["action"], "notification")

    def test_invalid_urgency_raises(self):
        with self.assertRaises(ValueError):
            pi._validate_claude_response({"urgency": "ASAP", "format": "note", "title": "x", "body": "y", "defer_to": ""})

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            pi._validate_claude_response({"urgency": "low", "format": "email", "title": "x", "body": "y", "defer_to": ""})

    def test_invalid_action_raises(self):
        with self.assertRaises(ValueError):
            pi._validate_claude_response({
                "urgency": "low", "format": "digest", "action": "email",
                "title": "x", "body": "y", "defer_to": "",
            })

    def test_title_truncated_to_80(self):
        long_title = "A" * 200
        resp = pi._validate_claude_response({
            "urgency": "low", "format": "digest",
            "title": long_title, "body": "b", "defer_to": "",
        })
        self.assertLessEqual(len(resp["title"]), 80)


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------

class TestRoutingDecisions(unittest.TestCase):
    def _route(self, urgency, fmt, dry_run=True, ledger_entry=None, action=None, intent_class="task_intent", consent=False):
        claude_response = {
            "urgency": urgency, "format": fmt,
            "title": "Test", "body": "Body text.", "defer_to": "",
        }
        if action:
            claude_response["action"] = action
        envelope = {
            "intent_raw": "x", "surrounding_context": "ctx",
            "inferred_category": "task", "intent_class": intent_class,
            "timestamp": "2026-04-16T00:00:00",
            "source_file": "/tmp/2026-04-16.md", "source_stat": "1:1",
            "enrichment_mode": "off",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            cortex_dir = Path(tmpdir) / "cortex"
            if consent:
                pi.record_intent_class_consent(state_dir, intent_class)
            result = pi.route_delivery(
                claude_response, envelope, cortex_dir, state_dir,
                "deadbeef" * 8, dry_run=dry_run, ledger_entry=ledger_entry,
            )
        return result

    def test_notification_urgency_immediate_routes_to_pushover(self):
        result = self._route("immediate", "notification")
        self.assertIn("pushover", result["planned_route"])

    def test_notification_urgency_today_routes_to_pushover(self):
        result = self._route("today", "notification")
        self.assertIn("pushover", result["planned_route"])

    def test_note_format_routes_to_cortex(self):
        result = self._route("soon", "note")
        self.assertIn("cortex", result["planned_route"])
        self.assertIn("pushover", result["planned_route"])  # soon urgency also pings

    def test_digest_format_routes_to_digest(self):
        result = self._route("low", "digest")
        self.assertIn("digest", result["planned_route"])

    def test_draft_format_routes_to_cortex_and_digest(self):
        result = self._route("soon", "draft")
        self.assertIn("cortex", result["planned_route"])
        self.assertIn("digest", result["planned_route"])

    def test_low_urgency_always_routes_to_digest(self):
        result = self._route("low", "note")
        self.assertIn("digest", result["planned_route"])

    def test_soon_note_excludes_pushover_when_env_restricts(self):
        with mock.patch.dict(os.environ, {"INTENT_PUSHOVER_URGENCIES": "immediate,today"}):
            result = self._route("soon", "note", dry_run=True)
            self.assertIn("cortex", result["planned_route"])
            self.assertNotIn("pushover", result["planned_route"])

    def test_purchase_action_with_consent_routes_to_action_queue(self):
        result = self._route(
            "soon", "note", dry_run=True,
            action="future_action_search_enqueue",
            intent_class="purchase_intent",
            consent=True,
        )
        self.assertIn("action_queue", result["planned_route"])
        self.assertNotIn("pushover", result["planned_route"])

    def test_purchase_action_without_consent_blocks_action_queue(self):
        result = self._route(
            "soon", "note", dry_run=True,
            action="future_action_search_enqueue",
            intent_class="purchase_intent",
            consent=False,
        )
        self.assertNotIn("action_queue", result["planned_route"])
        self.assertIn("digest", result["planned_route"])
        self.assertTrue(result["results_per_sink"]["consent"]["required"])

    def test_latent_interest_digest_action_does_not_notify(self):
        result = self._route(
            "low", "digest", dry_run=True,
            action="digest_capture",
            intent_class="latent_interest",
        )
        self.assertIn("digest", result["planned_route"])
        self.assertNotIn("pushover", result["planned_route"])


class TestEnrichmentModeResolution(unittest.TestCase):
    def test_default_is_llmlib(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pi.resolve_enrichment_mode(), "llmlib")

    def test_explicit_off_is_respected(self):
        with mock.patch.dict(os.environ, {"INTENT_ENRICHMENT_MODE": "off"}):
            self.assertEqual(pi.resolve_enrichment_mode(), "off")

    def test_explicit_mcp_alias_is_respected(self):
        with mock.patch.dict(os.environ, {"INTENT_ENRICHMENT_MODE": "mcp"}):
            self.assertEqual(pi.resolve_enrichment_mode(), "mcp")

    def test_parse_cli_loads_xdg_config_before_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config"
            env_dir = cfg / "journal-linker"
            env_dir.mkdir(parents=True)
            (env_dir / "journal-linker.env").write_text(
                "INTENT_ENRICHMENT_MODE=off\n",
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg)}, clear=True),
                mock.patch.object(sys, "argv", ["process_intents.py"]),
            ):
                pi.parse_cli()
                self.assertEqual(os.environ.get("INTENT_ENRICHMENT_MODE"), "off")


# ---------------------------------------------------------------------------
# Prior delivery dedupe + Pushover suppression
# ---------------------------------------------------------------------------

class TestPriorSinkDeliveredOk(unittest.TestCase):
    def test_false_when_empty_ledger(self):
        self.assertFalse(pi.prior_sink_delivered_ok(None, "pushover"))
        self.assertFalse(pi.prior_sink_delivered_ok({}, "pushover"))

    def test_true_when_prior_attempt_ok(self):
        entry = {
            "delivery_attempts": [
                {"results": {"pushover": {"ok": True, "status_code": 200}}},
            ],
        }
        self.assertTrue(pi.prior_sink_delivered_ok(entry, "pushover"))
        self.assertFalse(pi.prior_sink_delivered_ok(entry, "cortex"))


class TestParsePushoverUrgenciesAllowed(unittest.TestCase):
    def test_default_includes_soon(self):
        with mock.patch.dict(os.environ, {"INTENT_PUSHOVER_URGENCIES": ""}):
            u = pi.parse_pushover_urgencies_allowed()
            self.assertEqual(u, {"immediate", "today", "soon"})

    def test_custom_list(self):
        with mock.patch.dict(os.environ, {"INTENT_PUSHOVER_URGENCIES": "immediate, today"}):
            self.assertEqual(pi.parse_pushover_urgencies_allowed(), {"immediate", "today"})


class TestSendPushoverContract(unittest.TestCase):
    def test_builds_expected_request_and_returns_response(self):
        fake_resp = mock.Mock()
        fake_resp.status = 200
        fake_resp.read.return_value = b'{"status":"ok"}'
        fake_resp.close.return_value = None

        with mock.patch.dict(
            os.environ,
            {
                "SCRIBE_PUSHOVER_APP_TOKEN": "app-token",
                "SCRIBE_PUSHOVER_USER_KEY": "user-key",
                "SCRIBE_PUSHOVER_SERVER": "https://api.pushover.net",
                "SCRIBE_PUSHOVER_DEVICE": "phone-1",
            },
            clear=False,
        ), mock.patch.object(pi.urllib.request, "urlopen", return_value=fake_resp) as urlopen_mock:
            status, body = pi.send_pushover("Call doctor", "Follow up.", urgency="today")

        self.assertEqual(status, 200)
        self.assertEqual(body, '{"status":"ok"}')
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.pushover.net/1/messages.json")
        parsed = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(parsed["token"], ["app-token"])
        self.assertEqual(parsed["user"], ["user-key"])
        self.assertEqual(parsed["title"], ["Call doctor"])
        self.assertEqual(parsed["message"], ["Follow up."])
        self.assertEqual(parsed["priority"], ["0"])
        self.assertEqual(parsed["device"], ["phone-1"])


class TestPipelinePushoverDedupe(unittest.TestCase):
    """Second watcher run must not call Pushover again for the same idempotency key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.cortex_dir = Path(self._tmp.name) / "cortex"

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_run_skips_pushover_when_prior_delivery_succeeded(self):
        gate = {"intents": [{"intent_raw": "call doctor", "category": "task"}]}
        claude = {
            "urgency": "today",
            "format": "notification",
            "title": "Call doctor",
            "body": "Follow up.",
            "defer_to": "",
            "feedback_prompt": "",
        }
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {"message": {"content": json.dumps(gate)}}
        with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
            with mock.patch.object(pi, "call_routing_model", return_value=claude):
                with mock.patch.object(pi, "send_pushover", return_value=(200, "ok")) as mock_po:
                    e1 = pi.run_intent_pipeline(
                        FIXTURE_NOTE,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=self.cortex_dir,
                        state_dir=self.state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=False,
                        verbose=False,
                    )
                    e2 = pi.run_intent_pipeline(
                        FIXTURE_NOTE,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=self.cortex_dir,
                        state_dir=self.state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=False,
                        verbose=False,
                    )
        self.assertEqual(e1, pi.EXIT_SUCCESS)
        self.assertEqual(e2, pi.EXIT_SUCCESS)
        self.assertEqual(mock_po.call_count, 1)

    def test_rejected_pattern_suppresses_future_repeat_before_routing(self):
        gate = {"intents": [{"intent_raw": "buy bike shoes", "category": "task", "intent_class": "purchase_intent"}]}
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {"message": {"content": json.dumps(gate)}}
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / "intent_feedback_learning.json").write_text(
                json.dumps(
                    {
                        "approved": {},
                        "suppressed": {
                            "purchase_intent": {
                                "count": 1,
                                "phrases": {"Did you buy bike shoes?": 1},
                                "patterns": {
                                    "buy bike shoes": {
                                        "count": 1,
                                        "first_seen": "2026-04-24T12:00:00+00:00",
                                        "last_seen": "2026-04-24T12:00:00+00:00",
                                        "sample_phrase": "buy bike shoes",
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
                with (
                    mock.patch.object(pi, "call_routing_model") as routing_mock,
                    mock.patch.object(pi, "send_pushover") as pushover_mock,
                ):
                    exit_code = pi.run_intent_pipeline(
                        FIXTURE_NOTE,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=Path(tmpdir) / "cortex",
                        state_dir=state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=False,
                        verbose=False,
                    )
            ledger = pi.load_ledger(state_dir)

        self.assertEqual(exit_code, pi.EXIT_SUCCESS)
        routing_mock.assert_not_called()
        pushover_mock.assert_not_called()
        only = next(iter(ledger.values()))
        self.assertEqual(only["delivery_status"], "suppressed_by_feedback")
        self.assertEqual(only["claude_status"], "skipped_suppressed")

    @unittest.skipUnless(LIVE_OLLAMA_SMOKE, "set JOURNAL_LINKER_LIVE_SMOKE=1 to run live Ollama smoke tests")
    def test_live_ollama_smoke_for_intent_gate(self):
        import ollama as live_ollama

        prompt = pi.build_gate_prompt("I need to call the doctor.", "phi4")
        response = live_ollama.chat(
            model=os.getenv("INTENT_GATE_MODEL", pi.DEFAULT_GATE_MODEL),
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_ctx": 128},
            keep_alive=pi.KEEP_ALIVE,
        )
        self.assertIn("message", response)
        self.assertTrue(str(response["message"].get("content", "")).strip())


class TestPipelinePartialRetrySkipsPushover(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.cortex_dir = Path(self._tmp.name) / "cortex"

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_run_retries_cortex_only(self):
        gate = {"intents": [{"intent_raw": "call doctor", "category": "task"}]}
        claude = {
            "urgency": "today",
            "format": "note",
            "title": "Call doctor",
            "body": "Follow up.",
            "defer_to": "",
            "feedback_prompt": "",
        }
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {"message": {"content": json.dumps(gate)}}
        orig_write = pi.write_cortex_note
        call_count = {"n": 0}

        def write_maybe_fail(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated cortex failure")
            return orig_write(*args, **kwargs)

        with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
            with mock.patch.object(pi, "call_routing_model", return_value=claude):
                with mock.patch.object(pi, "send_pushover", return_value=(200, "ok")) as mock_po:
                    with mock.patch.object(pi, "write_cortex_note", side_effect=write_maybe_fail):
                        e1 = pi.run_intent_pipeline(
                            FIXTURE_NOTE,
                            gate_model="phi4:14b",
                            gate_style="phi4",
                            routing_model="gpt-4o-mini",
                            cortex_dir=self.cortex_dir,
                            state_dir=self.state_dir,
                            enrichment_mode="off",
                            in_flight_ttl=300,
                            dry_run=False,
                            verbose=False,
                        )
                        e2 = pi.run_intent_pipeline(
                            FIXTURE_NOTE,
                            gate_model="phi4:14b",
                            gate_style="phi4",
                            routing_model="gpt-4o-mini",
                            cortex_dir=self.cortex_dir,
                            state_dir=self.state_dir,
                            enrichment_mode="off",
                            in_flight_ttl=300,
                            dry_run=False,
                            verbose=False,
                        )
        self.assertEqual(e1, pi.EXIT_PARTIAL)
        self.assertEqual(e2, pi.EXIT_SUCCESS)
        self.assertEqual(mock_po.call_count, 1)
        self.assertEqual(call_count["n"], 2)


# ---------------------------------------------------------------------------
# Ledger operations
# ---------------------------------------------------------------------------

class TestLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_entry(self, key="abc123", status="succeeded"):
        return {
            "claude_idempotency_key": key,
            "source_path": "/tmp/2026-04-16.md",
            "journal_timestamp": "2026-04-16T00:00:00",
            "source_stat": "1:1",
            "claude_status": status,
            "claude_in_flight_since": "",
            "claude_response": {},
            "delivery_status": "pending",
            "delivery_attempts": [],
            "latest_run_id": "run-1",
        }

    def test_round_trip_load_save(self):
        entry = self._make_entry()
        ledger = {"abc123": entry}
        pi.save_ledger(self.state_dir, ledger)
        loaded = pi.load_ledger(self.state_dir)
        self.assertEqual(loaded["abc123"]["claude_status"], "succeeded")

    def test_upsert_adds_new_entry(self):
        ledger = {}
        entry = self._make_entry(key="key1")
        pi.upsert_ledger_entry(self.state_dir, ledger, entry)
        reloaded = pi.load_ledger(self.state_dir)
        self.assertIn("key1", reloaded)

    def test_upsert_updates_existing_entry(self):
        ledger = {}
        entry = self._make_entry(key="key1", status="in_flight")
        pi.upsert_ledger_entry(self.state_dir, ledger, entry)
        entry["claude_status"] = "succeeded"
        pi.upsert_ledger_entry(self.state_dir, ledger, entry)
        reloaded = pi.load_ledger(self.state_dir)
        self.assertEqual(reloaded["key1"]["claude_status"], "succeeded")

    def test_reconcile_stale_inflight(self):
        ledger = {}
        entry = self._make_entry(key="stale", status="in_flight")
        entry["claude_in_flight_since"] = "2020-01-01T00:00:00+00:00"  # very old
        pi.upsert_ledger_entry(self.state_dir, ledger, entry)
        count = pi.reconcile_stale_inflight(self.state_dir, ledger, ttl_seconds=300)
        self.assertEqual(count, 1)
        self.assertEqual(ledger["stale"]["claude_status"], "failed_transient")

    def test_reconcile_fresh_inflight_not_touched(self):
        ledger = {}
        entry = self._make_entry(key="fresh", status="in_flight")
        entry["claude_in_flight_since"] = datetime.now(timezone.utc).isoformat()
        pi.upsert_ledger_entry(self.state_dir, ledger, entry)
        count = pi.reconcile_stale_inflight(self.state_dir, ledger, ttl_seconds=300)
        self.assertEqual(count, 0)
        self.assertEqual(ledger["fresh"]["claude_status"], "in_flight")


# ---------------------------------------------------------------------------
# Exit code mapping via mocked pipeline
# ---------------------------------------------------------------------------

class TestExitCodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.cortex_dir = Path(self._tmp.name) / "cortex"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, gate_output, claude_response=None):
        """Run pipeline with mocked gate and Claude."""
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {
            "message": {"content": json.dumps(gate_output)}
        }
        with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
            with mock.patch.object(pi, "call_routing_model") as mock_claude:
                if claude_response is not None:
                    mock_claude.return_value = claude_response
                return pi.run_intent_pipeline(
                    FIXTURE_NOTE,
                    gate_model="phi4:14b",
                    gate_style="phi4",
                    routing_model="gpt-4o-mini",
                    cortex_dir=self.cortex_dir,
                    state_dir=self.state_dir,
                    enrichment_mode="off",
                    in_flight_ttl=300,
                    dry_run=True,
                    verbose=False,
                )

    def test_no_intent_returns_success(self):
        gate = {"intents": []}
        exit_code = self._run(gate)
        self.assertEqual(exit_code, pi.EXIT_SUCCESS)

    def test_intent_dry_run_returns_success(self):
        gate = {"intents": [{"intent_raw": "call doctor", "category": "task"}]}
        claude = {
            "urgency": "today", "format": "notification",
            "title": "Call doctor", "body": "Follow up.", "defer_to": "",
            "_dry_run": True,
        }
        exit_code = self._run(gate, claude_response=claude)
        self.assertEqual(exit_code, pi.EXIT_SUCCESS)

    def test_gate_ollama_error_returns_gate_transient(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            mock_ollama = mock.MagicMock()
            mock_ollama.chat.side_effect = ConnectionError("Ollama not running")
            with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
                exit_code = pi.run_intent_pipeline(
                    FIXTURE_NOTE,
                    gate_model="phi4:14b",
                    gate_style="phi4",
                    routing_model="gpt-4o-mini",
                    cortex_dir=Path(tmpdir) / "cortex",
                    state_dir=state_dir,
                    enrichment_mode="off",
                    in_flight_ttl=300,
                    dry_run=False,
                    verbose=False,
                )
        self.assertEqual(exit_code, pi.EXIT_GATE_TRANSIENT)

    def test_claude_error_returns_claude_transient(self):
        gate = {"intents": [{"intent_raw": "call doctor", "category": "task"}]}
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {
            "message": {"content": json.dumps(gate)}
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
                with mock.patch.object(pi, "call_routing_model", side_effect=ConnectionError("routing model timeout")):
                    exit_code = pi.run_intent_pipeline(
                        FIXTURE_NOTE,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=Path(tmpdir) / "cortex",
                        state_dir=state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=False,
                        verbose=False,
                    )
        self.assertEqual(exit_code, pi.EXIT_CLAUDE_TRANSIENT)

    def test_claude_retry_preserves_prior_delivery_attempts(self):
        gate = {"intents": [{"intent_raw": "rename file", "category": "task"}]}
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {
            "message": {"content": json.dumps(gate)}
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            note_path = state_dir / "2026-04-16.md"
            note_path.write_text("I need to rename file.\n", encoding="utf-8")
            key = pi.compute_idempotency_key(
                note_path,
                "2026-04-16",
                "rename file",
                "task",
                "phi4:14b",
                "phi4",
                intent_class="task_intent",
            )
            pi.save_ledger(state_dir, {
                key: {
                    "claude_idempotency_key": key,
                    "claude_status": "failed",
                    "delivery_status": "succeeded",
                    "delivery_attempts": [
                        {"results": {"feedback_queue": {"ok": True}}},
                    ],
                    "feedback_signal": "confirmed",
                },
            })
            with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
                with mock.patch.object(pi, "call_routing_model", side_effect=ConnectionError("routing timeout")):
                    exit_code = pi.run_intent_pipeline(
                        note_path,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=state_dir / "cortex",
                        state_dir=state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=False,
                        verbose=False,
                    )
            reloaded = pi.load_ledger(state_dir)[key]

        self.assertEqual(exit_code, pi.EXIT_CLAUDE_TRANSIENT)
        self.assertEqual(reloaded["delivery_attempts"][0]["results"]["feedback_queue"]["ok"], True)
        self.assertEqual(reloaded["feedback_signal"], "confirmed")


# ---------------------------------------------------------------------------
# Ledger maintenance commands
# ---------------------------------------------------------------------------

class TestLedgerMaintenance(unittest.TestCase):
    def test_reset_ledger_removes_state_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / pi.LEDGER_FILENAME).write_text("{}\n")
            (state_dir / pi.RUN_HISTORY_FILENAME).write_text("{}\n")
            pi.cmd_reset_ledger(state_dir)
            self.assertFalse((state_dir / pi.LEDGER_FILENAME).exists())
            self.assertFalse((state_dir / pi.RUN_HISTORY_FILENAME).exists())

    def test_prune_ledger_removes_old_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            ledger = {
                "old-key": {
                    "claude_idempotency_key": "old-key",
                    "claude_in_flight_since": "2020-01-01T00:00:00+00:00",
                    "claude_status": "failed_transient",
                },
                "new-key": {
                    "claude_idempotency_key": "new-key",
                    "claude_in_flight_since": datetime.now(timezone.utc).isoformat(),
                    "claude_status": "in_flight",
                },
            }
            pi.save_ledger(state_dir, ledger)
            pi.cmd_prune_ledger(state_dir, older_than_days=30)
            reloaded = pi.load_ledger(state_dir)
            self.assertNotIn("old-key", reloaded)
            self.assertIn("new-key", reloaded)

    def test_parse_older_than(self):
        self.assertEqual(pi._parse_older_than("30d"), 30)
        self.assertEqual(pi._parse_older_than("7"), 7)
        with self.assertRaises(ValueError):
            pi._parse_older_than("two weeks")


# ---------------------------------------------------------------------------
# Dry-run end-to-end (no side effects)
# ---------------------------------------------------------------------------

class TestDryRunWorkflow(unittest.TestCase):
    def test_dry_run_full_pipeline(self):
        gate_output = {"intents": [{"intent_raw": "follow up with accountant", "category": "task"}]}
        claude_response = {
            "urgency": "today", "format": "notification",
            "title": "Follow up with accountant",
            "body": "Send Q1 tax email before Friday.",
            "defer_to": "",
            "_dry_run": True,
        }
        mock_ollama = mock.MagicMock()
        mock_ollama.chat.return_value = {
            "message": {"content": json.dumps(gate_output)}
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            cortex_dir = Path(tmpdir) / "cortex"
            with mock.patch.dict("sys.modules", {"ollama": mock_ollama}):
                with mock.patch.object(pi, "call_routing_model", return_value=claude_response):
                    exit_code = pi.run_intent_pipeline(
                        FIXTURE_NOTE,
                        gate_model="phi4:14b",
                        gate_style="phi4",
                        routing_model="gpt-4o-mini",
                        cortex_dir=cortex_dir,
                        state_dir=state_dir,
                        enrichment_mode="off",
                        in_flight_ttl=300,
                        dry_run=True,
                        verbose=False,
                    )
            # Dry-run: cortex dir should NOT be created with notes
            self.assertEqual(exit_code, pi.EXIT_SUCCESS)
            self.assertFalse(cortex_dir.exists())
            # Run history should have a record
            history_path = state_dir / pi.RUN_HISTORY_FILENAME
            self.assertTrue(history_path.exists())


# ---------------------------------------------------------------------------
# Cortex note format + organization
# ---------------------------------------------------------------------------

class TestCortexNoteFormat(unittest.TestCase):
    def _write(self, **kwargs):
        defaults = dict(
            cortex_dir=None,  # set per test
            title="Follow up with accountant",
            body="Send Q1 tax email before Friday.",
            source_file="/home/tj/notes/2026-04-16.md",
            timestamp="2026-04-16T00:00:00",
            category="task",
            claude_idempotency_key="abcdef1234567890",
            surrounding_context="",
            defer_to="",
        )
        defaults.update(kwargs)
        return pi.write_cortex_note(**defaults)

    def test_note_written_into_category_subdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir)
            # Should be cortex/task/... not cortex/...
            self.assertEqual(path.parent.name, "task")
            self.assertEqual(path.parent.parent, cortex_dir)

    def test_source_is_wikilink_not_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir)
            content = path.read_text()
            self.assertIn('source: "[[2026-04-16]]"', content)
            self.assertNotIn("/home/tj/notes", content)

    def test_defer_to_omitted_when_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir, defer_to="")
            content = path.read_text()
            self.assertNotIn("defer_to", content)

    def test_defer_to_present_when_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir, defer_to="2026-04-20")
            content = path.read_text()
            self.assertIn("defer_to: 2026-04-20", content)

    def test_journal_callout_present_when_context_given(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir, surrounding_context="Need to call the accountant.")
            content = path.read_text()
            self.assertIn("> [!journal] Source excerpt", content)
            self.assertIn("> Need to call the accountant.", content)

    def test_journal_callout_absent_when_no_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir, surrounding_context="")
            content = path.read_text()
            self.assertNotIn("[!journal]", content)

    def test_status_open_in_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir)
            content = path.read_text()
            self.assertIn("status: open", content)

    def test_tags_include_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cortex_dir = Path(tmpdir) / "cortex"
            path = self._write(cortex_dir=cortex_dir, category="reminder")
            content = path.read_text()
            self.assertIn("tags: [intent, reminder]", content)
            # Should also be in the reminder/ subdir
            self.assertEqual(path.parent.name, "reminder")


# ---------------------------------------------------------------------------
# llmLibrarian enrichment (mocked)
# ---------------------------------------------------------------------------

class TestEnrichEnvelope(unittest.TestCase):
    def _base_envelope(self):
        return {
            "intent_raw": "follow up with accountant",
            "surrounding_context": "some context",
            "inferred_category": "task",
            "timestamp": "2026-04-16T00:00:00",
            "source_file": "/tmp/2026-04-16.md",
            "source_stat": "1:1",
            "enrichment_mode": "llmlib",
            "prompt_version": "1",
        }

    def test_enrich_populates_hits_on_success(self):
        fake_chunks = [
            {"source": "Note A", "text": "Some related content here"},
            {"source": "Note B", "text": "More related content"},
            {"source": "Note C", "text": "Even more content"},
            {"source": "Note D", "text": "Fourth result"},
        ]
        with mock.patch.object(pi, "query_llmlibrarian_mcp", return_value={"chunks": fake_chunks}) as query_mock:
            envelope = self._base_envelope()
            result = pi.enrich_envelope(envelope)

        query_mock.assert_called_once_with("follow up with accountant")
        self.assertIn("related_silo_hits", result)
        self.assertLessEqual(len(result["related_silo_hits"]), 3)
        self.assertEqual(result["related_silo_hits"][0]["title"], "Note A")
        self.assertTrue(result.get("recurrence_signal"))

    def test_enrich_continues_on_failure(self):
        """An exception inside enrich_envelope must not propagate."""
        envelope = self._base_envelope()
        with mock.patch.object(pi, "query_llmlibrarian_mcp", return_value={"error": "healthz failed", "chunks": []}):
            result = pi.enrich_envelope(envelope)
        # Pipeline should continue; error is recorded but envelope returned
        self.assertIsInstance(result, dict)
        self.assertIn("_enrichment_error", result)

    def test_enrich_noop_when_intent_empty(self):
        envelope = self._base_envelope()
        envelope["intent_raw"] = ""
        result = pi.enrich_envelope(envelope)
        self.assertNotIn("related_silo_hits", result)
        self.assertNotIn("_enrichment_error", result)

    def test_query_skips_mcp_when_health_fails(self):
        with (
            mock.patch.object(pi, "_llmlibrarian_health_check", return_value=(False, "connection refused")),
            mock.patch.object(pi, "_query_llmlibrarian_mcp_async") as async_mock,
        ):
            result = pi.query_llmlibrarian_mcp("anything")
        async_mock.assert_not_called()
        self.assertEqual(result["chunks"], [])
        self.assertIn("connection refused", result["error"])

    def test_mcp_tool_payload_parses_text_content(self):
        tool_result = mock.Mock()
        tool_result.structuredContent = None
        tool_result.content = [mock.Mock(text=json.dumps({"chunks": [{"text": "hit"}]}))]
        payload = pi._mcp_tool_payload(tool_result)
        self.assertEqual(payload["chunks"][0]["text"], "hit")

    def test_resolve_configured_silo_from_list_silos(self):
        payload = {
            "silos": [
                {"slug": "much-thinks-f018e152", "display_name": "Much Thinks"},
                {"slug": "project-palindrome-f8ea9ab4", "display_name": "Project Palindrome"},
            ]
        }
        self.assertEqual(
            pi._resolve_llmlibrarian_silo(payload, "Project Palindrome"),
            "project-palindrome-f8ea9ab4",
        )

    def test_unknown_configured_silo_fails_closed(self):
        with self.assertRaises(ValueError):
            pi._resolve_llmlibrarian_silo({"silos": []}, "missing")


# ---------------------------------------------------------------------------
# Feedback queue
# ---------------------------------------------------------------------------

class TestFeedbackQueue(unittest.TestCase):
    def _route(self, urgency: str, fmt: str, feedback_prompt: str = "Did you follow through?") -> dict:
        """Call route_delivery in dry_run=False against a temp state_dir, return results."""
        claude_response = {
            "urgency": urgency,
            "format": fmt,
            "title": "Test intent",
            "body": "Body text.",
            "defer_to": "",
            "feedback_prompt": feedback_prompt,
        }
        envelope = {
            "intent_raw": "test intent",
            "surrounding_context": "",
            "inferred_category": "task",
            "intent_class": "task_intent",
            "timestamp": "2026-04-16T10:00:00+00:00",
            "source_file": "/tmp/2026-04-16.md",
            "source_stat": "1:1",
            "enrichment_mode": "off",
            "prompt_version": "1",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            cortex_dir = Path(tmpdir) / "cortex"
            with mock.patch.object(pi, "send_pushover", return_value=(200, "ok")):
                results = pi.route_delivery(
                    claude_response, envelope, cortex_dir, state_dir, "a" * 64,
                    dry_run=False,
                )
            queue_path = state_dir / pi.FEEDBACK_QUEUE_FILENAME
            entries = []
            if queue_path.exists():
                for line in queue_path.read_text().splitlines():
                    if line.strip():
                        entries.append(json.loads(line))
            return {"results": results, "entries": entries}

    def test_feedback_queued_for_today_urgency(self):
        out = self._route("today", "note")
        self.assertEqual(len(out["entries"]), 1)
        self.assertEqual(out["entries"][0]["state"], "pending")
        self.assertEqual(out["entries"][0]["urgency"], "today")

    def test_feedback_queued_for_soon_urgency(self):
        out = self._route("soon", "note")
        self.assertEqual(len(out["entries"]), 1)
        self.assertEqual(out["entries"][0]["urgency"], "soon")

    def test_feedback_queued_for_immediate_urgency(self):
        out = self._route("immediate", "notification")
        self.assertEqual(len(out["entries"]), 1)

    def test_feedback_not_queued_for_low_urgency(self):
        out = self._route("low", "digest")
        self.assertEqual(len(out["entries"]), 0)

    def test_feedback_not_queued_when_prompt_empty(self):
        out = self._route("today", "note", feedback_prompt="")
        self.assertEqual(len(out["entries"]), 0)

    def test_send_after_offset_today(self):
        from datetime import datetime, timezone
        out = self._route("today", "note")
        entry = out["entries"][0]
        send_after = datetime.fromisoformat(entry["send_after"])
        captured_at = datetime.fromisoformat(entry["captured_at"])
        delta = (send_after - captured_at).total_seconds()
        self.assertAlmostEqual(delta, 21600, delta=10)

    def test_send_after_offset_soon(self):
        from datetime import datetime, timezone
        out = self._route("soon", "note")
        entry = out["entries"][0]
        send_after = datetime.fromisoformat(entry["send_after"])
        captured_at = datetime.fromisoformat(entry["captured_at"])
        delta = (send_after - captured_at).total_seconds()
        self.assertAlmostEqual(delta, 86400, delta=10)

    def test_feedback_prompt_in_queue_entry(self):
        out = self._route("today", "note", feedback_prompt="Did you call Marcus?")
        self.assertEqual(out["entries"][0]["feedback_prompt"], "Did you call Marcus?")

    def test_feedback_entry_includes_class_action_and_timing(self):
        out = self._route("today", "note")
        entry = out["entries"][0]
        self.assertEqual(entry["intent_class"], "task_intent")
        self.assertEqual(entry["action"], "notification")
        self.assertEqual(entry["timing_policy"], "later_today")
        self.assertEqual(entry["defer_count"], 0)
        self.assertIn("expires_at", entry)

    def test_future_action_queue_includes_enrichment_fields(self):
        claude_response = {
            "urgency": "soon",
            "format": "note",
            "action": "future_action_search_enqueue",
            "title": "Find a backpack",
            "body": "Compare lightweight backpacks.",
            "defer_to": "",
            "feedback_prompt": "",
        }
        envelope = {
            "intent_raw": "look for a backpack",
            "surrounding_context": "",
            "inferred_category": "plan",
            "intent_class": "purchase_intent",
            "timestamp": "2026-04-16T10:00:00+00:00",
            "source_file": "/tmp/2026-04-16.md",
            "source_stat": "1:1",
            "enrichment_mode": "llmlib",
            "related_silo_hits": [{"title": "Travel", "snippet": "old notes"}],
            "recurrence_signal": True,
            "prompt_version": "2",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            cortex_dir = Path(tmpdir) / "cortex"
            pi.record_intent_class_consent(state_dir, "purchase_intent")
            result = pi.route_delivery(
                claude_response, envelope, cortex_dir, state_dir, "b" * 64,
                dry_run=False,
            )
            queue_path = state_dir / pi.ACTION_QUEUE_FILENAME
            entries = [json.loads(line) for line in queue_path.read_text().splitlines() if line.strip()]

        self.assertIn("action_queue", result["planned_route"])
        self.assertEqual(entries[0]["intent_class"], "purchase_intent")
        self.assertEqual(entries[0]["related_silo_hits"], [{"title": "Travel", "snippet": "old notes"}])
        self.assertTrue(entries[0]["recurrence_signal"])


if __name__ == "__main__":
    unittest.main()
