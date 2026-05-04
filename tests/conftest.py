"""Pytest hooks — keep intent sink env vars from leaking from the developer shell."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_intent_sink_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset capture-only toggles so defaults match tests unless a case patches them."""
    monkeypatch.delenv("INTENT_DIGEST_MODE", raising=False)
    monkeypatch.delenv("INTENT_FEEDBACK_MODE", raising=False)
