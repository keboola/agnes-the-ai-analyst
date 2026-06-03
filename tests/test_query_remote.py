"""Unit tests for per-session BQ scan budget (Task 12.3).

These tests exercise the in-memory ``accumulate_session_bq_bytes`` helper
directly — no HTTP layer needed. The integration with the JWT / request.state
path is deferred to Task 13.1.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api.query as query_module
from app.api.query import accumulate_session_bq_bytes, _per_session_bq_bytes


@pytest.fixture(autouse=True)
def _reset_session_bytes():
    """Clear the in-memory counter before and after each test."""
    _per_session_bq_bytes.clear()
    yield
    _per_session_bq_bytes.clear()


def test_accumulate_within_budget_does_not_raise():
    """Scans below the limit accumulate without raising."""
    limit = 10 * 1024**3  # 10 GiB
    accumulate_session_bq_bytes("sess1", 1 * 1024**3, limit_bytes=limit)
    accumulate_session_bq_bytes("sess1", 2 * 1024**3, limit_bytes=limit)
    assert _per_session_bq_bytes["sess1"] == 3 * 1024**3


def test_accumulate_exceeds_budget_raises_bq_budget_exhausted():
    """Once cumulative bytes exceed the limit, HTTPException 400 bq_budget_exhausted."""
    limit = 5 * 1024**3  # 5 GiB
    accumulate_session_bq_bytes("sess2", 3 * 1024**3, limit_bytes=limit)
    with pytest.raises(HTTPException) as exc_info:
        accumulate_session_bq_bytes("sess2", 3 * 1024**3, limit_bytes=limit)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["reason"] == "bq_budget_exhausted"
    assert detail["session_id"] == "sess2"
    assert detail["scan_bytes_cumulative"] == 6 * 1024**3
    assert detail["limit_bytes"] == limit


def test_accumulate_exact_limit_does_not_raise():
    """Exactly hitting the limit is allowed (rejection is strictly over)."""
    limit = 4 * 1024**3
    accumulate_session_bq_bytes("sess3", 4 * 1024**3, limit_bytes=limit)
    # No exception raised — equal is still allowed


def test_separate_sessions_tracked_independently():
    """Different session IDs have independent byte counters."""
    limit = 5 * 1024**3
    accumulate_session_bq_bytes("sessA", 4 * 1024**3, limit_bytes=limit)
    # sessB starts fresh — should not inherit sessA's bytes
    accumulate_session_bq_bytes("sessB", 4 * 1024**3, limit_bytes=limit)
    assert _per_session_bq_bytes["sessA"] == 4 * 1024**3
    assert _per_session_bq_bytes["sessB"] == 4 * 1024**3


def test_zero_limit_disables_cap():
    """Setting limit_bytes=0 disables the cap (no exception on any scan size)."""
    accumulate_session_bq_bytes("sess4", 100 * 1024**3, limit_bytes=0)
    # No exception raised


# ---------------------------------------------------------------------------
# Task B.1: end-to-end wiring — chat-JWT request → request.state stash →
# execute_query budget accumulator
# ---------------------------------------------------------------------------


def test_chat_session_jwt_stashes_chat_session_id_on_request_state(monkeypatch):
    """The auth helper ``_stash_chat_session_id_from_token`` must decode a
    chat-scope JWT and pin ``chat_session_id`` on ``request.state``.

    Without this stash, ``accumulate_session_bq_bytes`` would never be called
    from the query handler — the per-session BQ scan budget would be a
    dead control.
    """
    import jwt as pyjwt
    import time as _time
    from types import SimpleNamespace

    from app.auth.dependencies import _stash_chat_session_id_from_token

    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!"
    )

    now = int(_time.time())
    token = pyjwt.encode(
        {
            "sub": "user_chat_test",
            "iat": now,
            "exp": now + 3600,
            "scope": "chat",
            "chat_session_id": "chat_test_session_42",
            "email": "chat-user@x",
        },
        "test-jwt-secret-key-minimum-32-chars!!",
        algorithm="HS256",
    )

    req = SimpleNamespace(state=SimpleNamespace())
    _stash_chat_session_id_from_token(req, token)
    assert req.state.chat_session_id == "chat_test_session_42"


def test_non_chat_jwt_does_not_set_chat_session_id(monkeypatch):
    """A regular session JWT (no scope=chat) must NOT add chat_session_id to
    request.state — regular /api/query callers don't charge a session bucket.
    """
    import jwt as pyjwt
    import time as _time
    from types import SimpleNamespace

    from app.auth.dependencies import _stash_chat_session_id_from_token

    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!"
    )

    now = int(_time.time())
    token = pyjwt.encode(
        {
            "sub": "user_x", "iat": now, "exp": now + 3600,
            "typ": "session", "email": "x@y",
        },
        "test-jwt-secret-key-minimum-32-chars!!",
        algorithm="HS256",
    )

    req = SimpleNamespace(state=SimpleNamespace())
    _stash_chat_session_id_from_token(req, token)
    assert not hasattr(req.state, "chat_session_id")


def test_execute_query_calls_accumulate_session_bq_bytes_for_chat_session(monkeypatch):
    """When the handler runs under a chat session JWT (request.state.chat_session_id
    set), the BQ scan accumulator must be called with the configured per-session
    limit pulled from ``app.state.chat_config``.

    Patches the accumulator directly and exercises the helper that the
    execute_query handler will call inside the BQ scan-guard block.
    """
    import app.api.query as query_module

    seen: list[tuple[str, int, int]] = []

    def fake_accum(session_id, scan_bytes, *, limit_bytes):
        seen.append((session_id, scan_bytes, limit_bytes))

    monkeypatch.setattr(
        query_module, "accumulate_session_bq_bytes", fake_accum,
    )

    # Simulate what execute_query does after BQ scan completes.
    from app.chat.config import ChatConfig
    cfg = ChatConfig(per_session_bq_scan_bytes=15 * 1024**3)

    class _State:
        chat_session_id = "chat_xyz"

    class _Req:
        state = _State()

        class app:
            class state:
                chat_config = cfg

    query_module._maybe_charge_chat_session_bq_budget(_Req(), 1024**3)
    assert seen == [("chat_xyz", 1024**3, 15 * 1024**3)]


def test_maybe_charge_skips_when_no_chat_session(monkeypatch):
    """No chat_session_id on request.state → accumulator must NOT be called.

    Regular (non-chat) /api/query callers should never charge the per-session
    budget — that bucket belongs to chat sessions only.
    """
    import app.api.query as query_module

    called = []

    def fake_accum(*a, **k):
        called.append(1)

    monkeypatch.setattr(
        query_module, "accumulate_session_bq_bytes", fake_accum,
    )

    from app.chat.config import ChatConfig

    class _State:
        pass  # no chat_session_id

    class _Req:
        state = _State()

        class app:
            class state:
                chat_config = ChatConfig()

    query_module._maybe_charge_chat_session_bq_budget(_Req(), 5_000_000)
    assert called == []
