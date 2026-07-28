"""Tests for wave-2F task 6: ws_gateway absorbed into coordination pub/sub.

Covers both halves of the new path:

- ``app.notifications.publish_notification`` — the producer side (replaces
  the old Unix-socket ``dispatch_to_ws_gateway``).
- ``app.api.notifications_ws`` — the GATEWAY-role WS consumer side (replaces
  the standalone ``services/ws_gateway`` aiohttp process), including the
  ported JWT auth check (was ``services/ws_gateway/auth.py``, covered by the
  now-deleted ``tests/test_ws_gateway.py``).
"""

from __future__ import annotations

import time
from typing import Optional

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.notifications_ws as ws_mod
from app.coordination.factory import reset_coordination_for_tests
from app.notifications import publish_notification
from app.roles import reset_roles_cache

SECRET = "test-secret-notifications-ws"


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition not met within timeout"


def _make_token(payload: dict, secret: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def _valid_payload(user: str = "alice") -> dict:
    return {"sub": user, "exp": int(time.time()) + 3600}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("DESKTOP_JWT_SECRET", SECRET)
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    reset_coordination_for_tests()
    ws_mod._connections.clear()
    yield
    ws_mod._connections.clear()
    reset_roles_cache()
    reset_coordination_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(ws_mod.router)
    return TestClient(app)


def _connect_and_auth(client: TestClient, user: str = "alice", token: Optional[str] = None):
    """Open the WS and complete the auth handshake; return the open ws."""
    ws = client.websocket_connect("/api/notifications/ws").__enter__()
    ws.send_json({"type": "auth", "token": token or _make_token(_valid_payload(user))})
    frame = ws.receive_json()
    assert frame == {"type": "auth_ok", "username": user}
    return ws


# ---------------------------------------------------------------------------
# publish_notification -> connected WS receives it (memory backend, same
# process)
# ---------------------------------------------------------------------------


def test_publish_delivers_to_connected_ws(client: TestClient):
    ws = _connect_and_auth(client, user="alice")
    try:
        publish_notification("alice", {"title": "Hello", "message": "world"})
        frame = ws.receive_json()
        assert frame == {"type": "notification", "title": "Hello", "message": "world"}
    finally:
        ws.__exit__(None, None, None)


def test_publish_only_reaches_the_matching_user(client: TestClient):
    """A notification for 'bob' must never land on 'alice's socket."""
    alice_ws = _connect_and_auth(client, user="alice")
    try:
        publish_notification("bob", {"title": "not for alice"})
        publish_notification("alice", {"title": "for alice"})
        frame = alice_ws.receive_json()
        assert frame == {"type": "notification", "title": "for alice"}
    finally:
        alice_ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Auth rejected without a valid JWT
# ---------------------------------------------------------------------------


def test_auth_rejected_with_invalid_token(client: TestClient):
    with client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": "not-a-real-token"})
        frame = ws.receive_json()
        assert frame == {"type": "auth_error", "message": "Invalid token"}


def test_auth_rejected_for_token_signed_with_wrong_secret(client: TestClient):
    bad_token = _make_token(_valid_payload("alice"), secret="wrong-secret")
    with client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": bad_token})
        frame = ws.receive_json()
        assert frame == {"type": "auth_error", "message": "Invalid token"}


def test_auth_rejected_without_desktop_jwt_secret_configured(client: TestClient, monkeypatch):
    """No DESKTOP_JWT_SECRET configured -> every token fails closed."""
    monkeypatch.delenv("DESKTOP_JWT_SECRET", raising=False)
    token = _make_token(_valid_payload("alice"))
    with client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        frame = ws.receive_json()
        assert frame == {"type": "auth_error", "message": "Invalid token"}


def test_non_gateway_process_closes_before_auth(client: TestClient, monkeypatch):
    """A process without the GATEWAY role never even reaches the auth
    handshake — matches the chat WS routes' role-gating story."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/notifications/ws") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4503


# ---------------------------------------------------------------------------
# Max connections per user enforced
# ---------------------------------------------------------------------------


def test_max_connections_per_user_enforced(client: TestClient, monkeypatch):
    monkeypatch.setattr(ws_mod, "MAX_CONNECTIONS_PER_USER", 1)
    first = _connect_and_auth(client, user="alice")
    try:
        with client.websocket_connect("/api/notifications/ws") as second:
            second.send_json({"type": "auth", "token": _make_token(_valid_payload("alice"))})
            frame = second.receive_json()
            assert frame == {"type": "auth_error", "message": "Too many connections"}
    finally:
        first.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Disconnect unsubscribes
# ---------------------------------------------------------------------------


def test_disconnect_unsubscribes_and_clears_registry(client: TestClient):
    with client.websocket_connect("/api/notifications/ws") as ws:
        ws.send_json({"type": "auth", "token": _make_token(_valid_payload("alice"))})
        ws.receive_json()
        assert len(ws_mod._connections.get("alice", [])) == 1

    _wait_for(lambda: not ws_mod._connections.get("alice"))

    # A notification published after full disconnect has nobody to reach —
    # must not raise, and the module's local registry stays empty.
    publish_notification("alice", {"title": "too late"})
    assert not ws_mod._connections.get("alice")


def test_second_connection_unaffected_by_first_disconnecting(client: TestClient, monkeypatch):
    """Two sockets for the same user each hold their OWN subscription;
    disconnecting one must not affect the other's delivery."""
    monkeypatch.setattr(ws_mod, "MAX_CONNECTIONS_PER_USER", 5)
    first = _connect_and_auth(client, user="alice")
    second = _connect_and_auth(client, user="alice")
    try:
        assert len(ws_mod._connections["alice"]) == 2
        first.__exit__(None, None, None)
        _wait_for(lambda: len(ws_mod._connections.get("alice", [])) == 1)

        publish_notification("alice", {"title": "still listening"})
        frame = second.receive_json()
        assert frame == {"type": "notification", "title": "still listening"}
    finally:
        second.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# publish with no listeners = no-op
# ---------------------------------------------------------------------------


def test_publish_with_no_listeners_is_noop():
    # Nobody is connected for "ghost" — must not raise.
    publish_notification("ghost-user", {"title": "nobody home"})
    assert "ghost-user" not in ws_mod._connections


# ---------------------------------------------------------------------------
# CoordinationUnavailable on publish -> log-and-continue
# ---------------------------------------------------------------------------


def test_publish_notification_logs_and_continues_on_coordination_unavailable(monkeypatch, caplog):
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    backend = coordination()

    def _raise(channel: str, message: str) -> None:
        raise CoordinationUnavailable("redis blip")

    monkeypatch.setattr(backend, "publish", _raise)

    with caplog.at_level("WARNING"):
        publish_notification("alice", {"title": "dropped"})  # must not raise

    assert any("coordination backend unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# validate_desktop_token — ported from the deleted tests/test_ws_gateway.py
# ---------------------------------------------------------------------------


class TestValidateDesktopToken:
    def test_valid_token_returns_payload(self):
        payload = {"sub": "alice", "exp": int(time.time()) + 3600}
        token = _make_token(payload)
        result = ws_mod.validate_desktop_token(token)
        assert result is not None
        assert result["sub"] == "alice"

    def test_expired_token_returns_none(self):
        payload = {"sub": "bob", "exp": int(time.time()) - 10}
        token = _make_token(payload)
        assert ws_mod.validate_desktop_token(token) is None

    def test_invalid_signature_returns_none(self):
        payload = {"sub": "charlie", "exp": int(time.time()) + 3600}
        token = _make_token(payload, secret="wrong-secret")
        assert ws_mod.validate_desktop_token(token) is None

    def test_token_missing_sub_returns_none(self):
        payload = {"exp": int(time.time()) + 3600, "role": "admin"}
        token = _make_token(payload)
        assert ws_mod.validate_desktop_token(token) is None

    def test_garbage_string_returns_none(self):
        assert ws_mod.validate_desktop_token("not.a.token") is None

    def test_valid_token_includes_all_claims(self):
        payload = {"sub": "dave", "exp": int(time.time()) + 3600, "role": "analyst"}
        token = _make_token(payload)
        result = ws_mod.validate_desktop_token(token)
        assert result is not None
        assert result["role"] == "analyst"

    def test_no_secret_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("DESKTOP_JWT_SECRET", raising=False)
        token = _make_token({"sub": "erin", "exp": int(time.time()) + 3600})
        assert ws_mod.validate_desktop_token(token) is None


# ---------------------------------------------------------------------------
# Bonus: the same delivery path over a fakeredis-backed coordination
# backend — exercises the cross-thread `run_coroutine_threadsafe` bridge
# (Redis's pub/sub listener thread, not the event loop thread) that
# cross-replica delivery depends on.
# ---------------------------------------------------------------------------


def test_delivery_works_over_fakeredis_backend(client: TestClient, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")

    from app.coordination import factory as coordination_factory
    from app.coordination.redis_backend import RedisCoordinationBackend

    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    reset_coordination_for_tests()
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = RedisCoordinationBackend(redis_client)
    monkeypatch.setattr(coordination_factory, "_instance", backend)

    ws = _connect_and_auth(client, user="alice")
    try:
        publish_notification("alice", {"title": "via redis"})
        frame = ws.receive_json()
        assert frame == {"type": "notification", "title": "via redis"}
    finally:
        ws.__exit__(None, None, None)
        backend.close()
