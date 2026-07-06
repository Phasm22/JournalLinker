"""Pytest hooks — keep intent sink env vars from leaking from the developer shell."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_intent_sink_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset capture-only toggles so defaults match tests unless a case patches them.

    These can leak from the developer shell or the on-disk journal-linker.env
    (loaded via bootstrap during the session); clearing them per-test keeps the
    routing/delivery defaults deterministic regardless of local config.
    """
    monkeypatch.delenv("INTENT_DIGEST_MODE", raising=False)
    monkeypatch.delenv("INTENT_FEEDBACK_MODE", raising=False)
    monkeypatch.delenv("INTENT_PUSHOVER_URGENCIES", raising=False)
    monkeypatch.delenv("INTENT_COMMANDS_MODE", raising=False)
    monkeypatch.delenv("INTENT_COMMAND_WAKE_WORDS", raising=False)
    # Bootstrap-leaked toggle: weekly_insights.py calls
    # bootstrap_journal_linker_env() at import, which loads the on-disk
    # journal-linker.env (INTENT_HOTSEAT_AUTOINDEX=off) permanently into
    # os.environ. Without clearing it here, tests that assume the autoindex
    # default (on) fail only when a bootstrapping test file runs first —
    # the pre-existing test-order flake in test_process_intents_commands.py.
    monkeypatch.delenv("INTENT_HOTSEAT_AUTOINDEX", raising=False)
