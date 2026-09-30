"""Pytest hooks — keep intent sink env vars from leaking from the developer shell."""

import os

import pytest

# Mark the env bootstrap as already done before any test module is imported, so
# no import-time or parse_args() call to bootstrap_journal_linker_env() can load
# the developer's real ~/.config/journal-linker/journal-linker.env into the
# shared pytest process (it leaked e.g. INTENT_HOTSEAT_AUTOINDEX=off and live
# API keys into unrelated tests). test_journal_linker_env.py clears os.environ
# around its own cases, so it still exercises the real bootstrap logic.
os.environ["JOURNAL_LINKER_ENV_BOOTSTRAPPED"] = "1"


@pytest.fixture(autouse=True)
def _isolate_intent_sink_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset capture-only toggles so defaults match tests unless a case patches them.

    These can leak from the developer shell; clearing them per-test keeps the
    routing/delivery defaults deterministic regardless of local config.
    """
    monkeypatch.delenv("INTENT_DIGEST_MODE", raising=False)
    monkeypatch.delenv("INTENT_FEEDBACK_MODE", raising=False)
    monkeypatch.delenv("INTENT_PUSHOVER_URGENCIES", raising=False)
    monkeypatch.delenv("INTENT_COMMANDS_MODE", raising=False)
    monkeypatch.delenv("INTENT_COMMAND_WAKE_WORDS", raising=False)
    monkeypatch.delenv("INTENT_HOTSEAT_AUTOINDEX", raising=False)
