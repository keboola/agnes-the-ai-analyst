"""Tests for app/chat/routing.py (wave-2F task 1: session routing leases).

Two "simulated managers" (i.e. two gateway replicas) are modeled as two
distinct holder_id strings racing over the SAME shared coordination-backend
singleton — the exact convention tests/test_coordination_leases.py already
uses for the leader-lease helper. This works identically under the default
``memory`` backend (a single process's singleton IS the shared state) and a
real multi-process ``redis`` deployment, because both backends satisfy the
same CoordinationBackend contract (tests/test_coordination_contract.py).
"""

from __future__ import annotations

import time

import pytest

from app.chat import routing
from app.coordination.factory import coordination, reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def test_this_gateway_id_is_hostname_colon_pid():
    gw_id = routing.this_gateway_id()
    host, _, pid = gw_id.partition(":")
    assert host and pid.isdigit()


def test_two_holders_one_chat_id_exactly_one_claim_succeeds():
    """Two simulated gateways race to claim the same chat session — only
    one wins, matching single-owner routing semantics."""
    chat_id = "chat-1"
    gw_a, gw_b = "host-a:100", "host-b:200"

    assert routing.claim_session(chat_id, gw_a, ttl_s=60) is True
    assert routing.claim_session(chat_id, gw_b, ttl_s=60) is False
    # Even the winner can't "claim" a second time — must renew instead.
    assert routing.claim_session(chat_id, gw_a, ttl_s=60) is False


def test_memory_mode_first_claim_always_granted():
    """Default (memory) backend, single-process deployment: the very
    first claim attempt for a chat_id always succeeds — no functional
    change from today's single-owner behavior."""
    assert routing.claim_session("chat-solo", routing.this_gateway_id(), ttl_s=60) is True


def test_renew_by_holder_only():
    chat_id = "chat-2"
    gw_a, gw_b = "host-a:100", "host-b:200"
    assert routing.claim_session(chat_id, gw_a, ttl_s=60) is True

    assert routing.renew_session(chat_id, gw_b, ttl_s=60) is False
    assert routing.renew_session(chat_id, gw_a, ttl_s=60) is True


def test_owner_of_returns_current_holder():
    chat_id = "chat-3"
    gw_a = "host-a:100"
    assert routing.owner_of(chat_id) is None  # unclaimed
    routing.claim_session(chat_id, gw_a, ttl_s=60)
    assert routing.owner_of(chat_id) == gw_a


def test_owner_death_lease_expires_then_other_gateway_claims():
    """A holder that stops renewing (simulated: claim once, then never
    renew again) loses the lease on TTL expiry — a different gateway can
    then claim it."""
    chat_id = "chat-4"
    gw_a, gw_b = "host-a:100", "host-b:200"
    assert routing.claim_session(chat_id, gw_a, ttl_s=1) is True
    assert routing.owner_of(chat_id) == gw_a

    # Before expiry, the other gateway still can't take over.
    assert routing.claim_session(chat_id, gw_b, ttl_s=60) is False

    time.sleep(1.3)  # gw_a never renewed — lease has expired

    assert routing.claim_session(chat_id, gw_b, ttl_s=60) is True
    assert routing.owner_of(chat_id) == gw_b


def test_release_frees_the_lease_for_immediate_reclaim():
    chat_id = "chat-5"
    gw_a, gw_b = "host-a:100", "host-b:200"
    routing.claim_session(chat_id, gw_a, ttl_s=60)
    routing.release_session(chat_id, gw_a)
    assert routing.owner_of(chat_id) is None
    assert routing.claim_session(chat_id, gw_b, ttl_s=60) is True


def test_release_by_non_holder_is_a_noop():
    chat_id = "chat-6"
    gw_a, gw_b = "host-a:100", "host-b:200"
    routing.claim_session(chat_id, gw_a, ttl_s=60)
    routing.release_session(chat_id, gw_b)  # wrong holder — no-op
    assert routing.owner_of(chat_id) == gw_a


def test_coordination_unavailable_on_claim_logs_and_returns_false(monkeypatch):
    """A backend outage must not raise out of claim_session — treated the
    same as a lost/contended claim (log-and-continue posture)."""
    import app.chat.routing as routing_mod
    from app.coordination.base import CoordinationUnavailable

    class _BrokenBackend:
        def lease_acquire(self, *a, **k):
            raise CoordinationUnavailable("boom")

        def lease_renew(self, *a, **k):
            raise CoordinationUnavailable("boom")

        def lease_release(self, *a, **k):
            raise CoordinationUnavailable("boom")

        def lease_owner(self, *a, **k):
            raise CoordinationUnavailable("boom")

    monkeypatch.setattr(routing_mod, "coordination", lambda: _BrokenBackend())

    assert routing.claim_session("chat-x", "gw-a") is False
    assert routing.renew_session("chat-x", "gw-a") is False
    assert routing.owner_of("chat-x") is None
    routing.release_session("chat-x", "gw-a")  # must not raise


def test_shared_backend_singleton_reflects_across_calls():
    """Sanity check for the "two simulated managers share one backend"
    premise this whole test module relies on: coordination() returns the
    same singleton within a process."""
    assert coordination() is coordination()
