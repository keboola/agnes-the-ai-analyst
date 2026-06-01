"""F.10 — Slack DM roundtrip (verification → bind → bound DM reply).

Unlike F.1–F.9 this test does NOT need the docker-compose stack —
the Slack bridge lives entirely in-process. We build a FastAPI app
with the real Slack router + chat router mounted, mock
``services.slack_bot.sender.send_thread_reply`` to capture outgoing
Slack messages, and drive the full flow over a TestClient.

The three legs:

  1. **Unbound DM**: an unknown ``slack_user_id`` DMs the bot. The
     ``_handle_dm`` path issues a 6-digit verification code and
     replies in-thread with the bind instructions.
  2. **Bind**: the user (logged in via the test auth override) POSTs
     the code to ``/api/slack/bind``. Success returns 200; the
     ``users.slack_user_id`` column gets the Slack ID stamped.
  3. **Bound DM**: a second DM from the same Slack user hits the
     bound branch — creates a chat session, attaches the sink bridge,
     forwards the user text to the manager. With the fake-agent
     runner that the in-process ``attach`` simulates, the
     ``assistant_message`` round-trips back to a mocked
     ``send_thread_reply`` call.

No real Slack signing secret is involved — we either bypass the sig
check by overriding the dependency or compute a valid HMAC against a
test secret. Computing the HMAC keeps the production code path
exercised (sigverify) without coupling to Anthropic.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.api.slack import router as slack_router
from app.api.chat import router as chat_router
from app.auth.dependencies import get_current_user
from app.chat.persistence import ChatRepository
from app.chat.types import ChatSession, Surface


_TEST_SIGNING_SECRET = "f10-signing-secret-aaa"
_TEST_USER_EMAIL = "f10@agnes.local"
_TEST_USER_ID = "f10-user"
_TEST_SLACK_USER_ID = "U_F10"
_TEST_CHANNEL = "D_F10"


# ---------------------------------------------------------------------------
# Fixture: in-process app with the slack + chat routers
# ---------------------------------------------------------------------------


def _build_app(
    *, captured_slack_calls: list[tuple[str, str, str]],
) -> tuple[FastAPI, duckdb.DuckDBPyConnection, SimpleNamespace]:
    """Build a minimal FastAPI app with the real Slack + Chat routers.

    Returns the app, the DuckDB conn (so the test can inspect
    ``users.slack_user_id`` after binding), and a SimpleNamespace
    that holds the chat-manager state we need to assert on
    (created sessions, attached sinks, sent user messages).
    """
    app = FastAPI()
    app.include_router(slack_router)
    app.include_router(chat_router)

    # Bog-standard DuckDB with the schema migration applied + one user row.
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users(id, email, name) VALUES (?, ?, ?)",
        [_TEST_USER_ID, _TEST_USER_EMAIL, "F10 User"],
    )

    repo = ChatRepository(conn)
    # The binding tables (and the users.slack_user_id column) are
    # created lazily by `issue_verification_code` on first call, but
    # `lookup_user_email` runs *first* in the events dispatcher. Force
    # the table init now so the very first DM doesn't crash on a
    # missing column.
    from services.slack_bot.binding import _ensure_table as _ensure_binding_table
    _ensure_binding_table(conn)

    app.state.chat_repo = repo
    app.state.public_url = "https://agnes.example.com"

    # The mock chat manager records what _handle_dm does so the test
    # can grep over its history. attach() simulates one assistant
    # round-trip through the sink so the sender mock captures it.
    state = SimpleNamespace(
        created=[],
        attached=[],
        sent=[],
        live=[],
    )

    async def create_session(*, user_email, surface, slack_channel_id=None,
                             slack_thread_ts=None, title=None):
        s = ChatSession(
            id="sess-f10",
            user_email=user_email,
            surface=surface,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            title=title,
            started_at=datetime.now(timezone.utc),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        state.created.append(s)
        return s

    async def attach(chat_id, sink):
        state.attached.append((chat_id, sink))
        # Drive one round-trip through the bridge: ready → assistant_message.
        # SlackSinkBridge translates the latter into send_thread_reply.
        await sink.send_json({"type": "ready"})
        await sink.send_json({
            "type": "assistant_message",
            "content": "echo: hello agnes",
        })

    async def send_user_message(chat_id, text):
        state.sent.append((chat_id, text))

    mgr = SimpleNamespace(
        list_live=lambda: list(state.live),
        create_session=create_session,
        attach=attach,
        send_user_message=send_user_message,
        _state=state,
        # Chat REST endpoints check `_config.enabled`; the slack flow
        # doesn't touch it but we set the attr anyway.
        _config=SimpleNamespace(enabled=True),
    )
    app.state.chat_manager = mgr

    # Auth override for /api/slack/bind — that endpoint uses
    # get_current_user which would otherwise demand a JWT.
    app.dependency_overrides[get_current_user] = lambda: {
        "id": _TEST_USER_ID,
        "email": _TEST_USER_EMAIL,
        "is_admin": False,
    }

    return app, conn, state


def _slack_headers(body_bytes: bytes, secret: str) -> dict[str, str]:
    """Build the X-Slack-Request-Timestamp + X-Slack-Signature pair.

    Re-implements what Slack does at request time so we exercise the
    real ``verify_slack_signature`` path. Keeping the sigverify in
    play catches regressions where the signature contract drifts
    (e.g. the ``v0:`` prefix or the hash algo).
    """
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode() + body_bytes
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/json",
    }


@pytest.fixture
def slack_app(monkeypatch):
    """Yield (TestClient, conn, state, captured_slack_calls).

    Stubs ``SLACK_SIGNING_SECRET`` env, patches the sender at every
    import site (events + sink modules both call it), and provides
    the captured outbound calls list for assertions.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _TEST_SIGNING_SECRET)
    captured: list[tuple[str, str, str]] = []

    async def fake_send(channel: str, thread_ts: str, text: str) -> None:
        captured.append((channel, thread_ts, text))

    # Patch BOTH import sites — `services.slack_bot.events` imports
    # it for the unbound-user reply, and `services.slack_bot.sink`
    # imports it for the bound assistant_message bridge.
    from services.slack_bot import events as events_mod
    from services.slack_bot import sink as sink_mod
    monkeypatch.setattr(events_mod, "send_thread_reply", fake_send)
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)
    # Chat is a default-deny RBAC resource; the bound-DM branch checks the
    # user's grant before spawning. This roundtrip exercises the bind →
    # bound-reply plumbing, not the gate, so grant access. (Default-deny is
    # covered by test_chat_api::test_chat_requires_rbac_grant.)
    import app.auth.access as _access
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, conn, state = _build_app(captured_slack_calls=captured)
    client = TestClient(app)
    yield client, conn, state, captured
    client.close()


# ---------------------------------------------------------------------------
# F.10 — the roundtrip
# ---------------------------------------------------------------------------


def test_f10_slack_dm_verification_bind_then_bound_reply(slack_app):
    """End-to-end Slack flow: unbound → code → bind → bound + reply."""
    client, conn, state, captured = slack_app

    # --- Leg 1: unbound DM → verification code ----------------------------
    event_unbound = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": _TEST_CHANNEL,
            "user": _TEST_SLACK_USER_ID,
            "ts": "1.1",
            "text": "hello",
        },
    }
    body = json.dumps(event_unbound).encode("utf-8")
    headers = _slack_headers(body, _TEST_SIGNING_SECRET)
    r = client.post("/api/slack/events", data=body, headers=headers)
    assert r.status_code == 200, r.text

    # Should have captured exactly one outbound reply with a 6-digit code.
    assert len(captured) == 1, captured
    ch, _ts, text = captured[0]
    assert ch == _TEST_CHANNEL
    code_match = re.search(r"\*(\d{6})\*", text)
    assert code_match, f"expected *NNNNNN* in reply; got: {text!r}"
    code = code_match.group(1)

    # Slack manager hasn't created/attached anything yet — that's the
    # bound branch's job.
    assert state.created == []
    assert state.attached == []

    # --- Leg 2: user redeems the code via /api/slack/bind -----------------
    r = client.post("/api/slack/bind", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    bound_id = conn.execute(
        "SELECT slack_user_id FROM users WHERE email = ?", [_TEST_USER_EMAIL]
    ).fetchone()[0]
    assert bound_id == _TEST_SLACK_USER_ID

    # --- Leg 3: second DM lands on the bound branch ------------------------
    captured.clear()
    event_bound = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": _TEST_CHANNEL,
            "user": _TEST_SLACK_USER_ID,
            "ts": "1.2",
            "text": "hello agnes",
        },
    }
    body = json.dumps(event_bound).encode("utf-8")
    headers = _slack_headers(body, _TEST_SIGNING_SECRET)
    r = client.post("/api/slack/events", data=body, headers=headers)
    assert r.status_code == 200, r.text

    # The bound branch waits 100 ms after scheduling attach() to let
    # the pump set up before forwarding the user message. TestClient
    # is synchronous and the slack handler awaits its own asyncio
    # sleep, so by the time we get here both attach() and
    # send_user_message() should have fired.
    assert len(state.created) == 1
    assert state.created[0].user_email == _TEST_USER_EMAIL
    assert state.created[0].surface == Surface.SLACK_DM
    assert len(state.attached) == 1
    assert state.attached[0][0] == "sess-f10"
    assert state.sent == [("sess-f10", "hello agnes")]

    # SlackSinkBridge translated the assistant_message into a
    # send_thread_reply with the echoed text. The bridge is
    # asynchronous (it pumps via an asyncio.Queue) so the post-handler
    # state may not include the reply yet — drive the loop briefly.
    for _ in range(20):
        if any(text == "echo: hello agnes" for _ch, _ts, text in captured):
            break
        # Yield to any background tasks scheduled by attach().
        # We cannot run a fresh event loop here because dispatch_event
        # already returned, but the bridge's asyncio.Queue lives in
        # the same loop the TestClient was using. A tiny sleep on the
        # current thread lets pytest's loop drain.
        time.sleep(0.05)

    # We're being lenient here — if the bridge hasn't drained yet,
    # accept either the assistant echo or at least the captured-list
    # being non-empty (the `ready` frame is suppressed by the bridge
    # so the assistant_message is the only candidate).
    assert captured, (
        "expected an outbound Slack reply from the assistant message; "
        f"state.sent={state.sent}, state.attached={state.attached}"
    )
    assert any(
        text == "echo: hello agnes" for _ch, _ts, text in captured
    ), f"expected 'echo: hello agnes' reply; got: {captured!r}"


def test_f10_invalid_signature_is_rejected(slack_app):
    """Defensive: a wrong HMAC must 401 even if the body is well-formed.

    Belt-and-suspenders for the sigverify wiring — if a Slack-spec
    change is ever botched (timestamp window, hash algo), this test
    catches the regression before a fuzzed prod request does.
    """
    client, _conn, _state, _captured = slack_app
    body = b'{"type":"event_callback","event":{"type":"message"}}'
    headers = _slack_headers(body, "wrong-secret")
    r = client.post("/api/slack/events", data=body, headers=headers)
    assert r.status_code == 401
