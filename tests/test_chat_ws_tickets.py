"""Behavior tests for WS auth tickets riding the coordination backend.

``_issue_ticket``/``_consume_ticket`` (app/api/chat.py) used to be backed by
a module-level dict (``_TICKETS``); they now go through
``coordination().kv_set``/``kv_delete``. These tests pin down the ticket
contract itself (single-use, TTL-bound, defensive against malformed/unknown
tickets) independent of the HTTP/WS plumbing already covered by
test_chat_api.py and test_copresence_ws_join.py.
"""

from __future__ import annotations

import time

import pytest

from app.coordination.factory import coordination, reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def test_issue_then_consume_roundtrip():
    from app.api.chat import _consume_ticket, _issue_ticket

    ticket = _issue_ticket("chat_1", "alice@test.com")
    assert _consume_ticket(ticket) == ("chat_1", "alice@test.com")


def test_consume_is_single_use():
    """A second consume of the same ticket must fail — atomic get-and-delete."""
    from app.api.chat import _consume_ticket, _issue_ticket

    ticket = _issue_ticket("chat_1", "alice@test.com")
    assert _consume_ticket(ticket) is not None
    assert _consume_ticket(ticket) is None


def test_consume_unknown_ticket_returns_none():
    from app.api.chat import _consume_ticket

    assert _consume_ticket("never-issued") is None


def test_consume_malformed_payload_returns_none():
    """A ``ws-ticket:`` key holding non-JSON garbage must be rejected, not
    raise — defensive against backend corruption or a future incompatible
    writer."""
    from app.api.chat import _TICKET_KEY_PREFIX, _consume_ticket

    coordination().kv_set(f"{_TICKET_KEY_PREFIX}garbage", "not-json{{", ttl_s=60)
    assert _consume_ticket("garbage") is None


def test_ticket_expiry_honored(monkeypatch):
    """Once the TTL elapses, the ticket is gone even though it was never
    explicitly consumed."""
    import app.api.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_TICKET_TTL_SEC", 1)
    ticket = chat_mod._issue_ticket("chat_1", "alice@test.com")
    time.sleep(1.3)
    assert chat_mod._consume_ticket(ticket) is None
