"""Tests for the ``auth_method='oauth'`` branch of ``connectors/mcp/client.py``
(2026-07-30 outbound MCP OAuth sources spec §4).

Covers:
* Fail-closed token lookup — no shared fallback, caller-less path gets
  nothing.
* Refresh-when-near-expiry, persisted atomically (rotated refresh token
  included).
* ``invalid_grant`` deletes the row (forces re-connect).
* The dedicated two-process refresh-race: two coroutines with DISTINCT
  coordination-lease holder ids (simulating two separate OS processes,
  which never share an in-process ``asyncio.Lock``) race a refresh of the
  same ``(source, user)`` pair — exactly one token-endpoint call happens
  and no rotation is orphaned.

DB access is a plain in-memory DuckDB connection with just the two tables
this module touches (mirrors the pattern in ``tests/test_mcp_user_secrets.py``)
— ``src.repositories.get_system_db`` is monkeypatched to hand out that
connection, so the real repo classes run unmodified.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.coordination.factory import reset_coordination_for_tests
from app.secrets_vault import _reset_ephemeral_key_for_tests
from connectors.mcp import client as mcp_client
from connectors.mcp import oauth_client as mcp_oauth_client
from src.duckdb_conn import _open_duckdb


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


@pytest.fixture(autouse=True)
def _isolated_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture(autouse=True)
def _isolated_locks():
    mcp_client.reset_oauth_refresh_locks_for_tests()
    yield
    mcp_client.reset_oauth_refresh_locks_for_tests()


def _oauth_conn() -> duckdb.DuckDBPyConnection:
    # ``_open_duckdb`` (not a bare ``duckdb.connect``) — pins session
    # TimeZone='UTC' the same way the real system-db connection does.
    # Without this pin, a tz-aware ``datetime.now(timezone.utc)`` write
    # gets silently shifted into the host's local zone before the tzinfo
    # is stripped, which broke ``_needs_refresh`` on any non-UTC dev box.
    conn = _open_duckdb(":memory:")
    conn.execute(
        """CREATE TABLE mcp_user_oauth_tokens (
              source_id         VARCHAR NOT NULL,
              user_id           VARCHAR NOT NULL,
              access_token_enc  BLOB NOT NULL,
              refresh_token_enc BLOB,
              expires_at        TIMESTAMP,
              scopes            VARCHAR,
              created_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
              PRIMARY KEY (source_id, user_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE mcp_source_oauth_clients (
              source_id                     VARCHAR PRIMARY KEY,
              issuer                        VARCHAR NOT NULL,
              client_id                     VARCHAR NOT NULL,
              client_secret_enc             BLOB,
              registration_access_token_enc BLOB,
              authorization_endpoint        VARCHAR NOT NULL,
              token_endpoint                VARCHAR NOT NULL,
              scopes                        VARCHAR,
              created_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp
           )"""
    )
    return conn


@pytest.fixture
def oauth_db(monkeypatch):
    conn = _oauth_conn()
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    return conn


def _seed_client_row(conn, source_id="src_oauth1"):
    from src.repositories.mcp_source_oauth_clients import MCPSourceOAuthClientRepository

    MCPSourceOAuthClientRepository(conn).upsert(
        source_id,
        issuer="https://as.example.com",
        client_id="agnes-client",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        client_secret="agnes-secret",
    )


def _seed_token_row(conn, *, source_id="src_oauth1", user_id="user1", expires_in_seconds, refresh_token="rt-1"):
    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    expires_at = None
    if expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    MCPUserOAuthTokenRepository(conn).upsert(
        source_id,
        user_id,
        "at-1",
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


_SOURCE = {
    "id": "src_oauth1",
    "auth_method": "oauth",
    "scope": "per_user",
    "transport": "http",
    "url": "https://mcp.example.com/mcp",
}


# ---------------------------------------------------------------------------
# Fail-closed lookup, no shared fallback
# ---------------------------------------------------------------------------


def test_no_token_row_returns_none(oauth_db):
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result is None


def test_caller_less_path_gets_no_token_even_with_a_row(oauth_db):
    _seed_token_row(oauth_db, expires_in_seconds=3600)
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, None))
    assert result is None


def test_fresh_token_returned_without_refresh_call(oauth_db, monkeypatch):
    _seed_token_row(oauth_db, expires_in_seconds=3600)

    async def _boom(**kwargs):
        raise AssertionError("must not refresh a fresh token")

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _boom)
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result == "at-1"


def test_headers_async_builds_bearer_for_oauth(oauth_db):
    _seed_token_row(oauth_db, expires_in_seconds=3600)
    headers = asyncio.run(mcp_client._resolve_http_headers_async(_SOURCE, caller_user_id="user1"))
    assert headers == {"Authorization": "Bearer at-1"}


def test_headers_async_empty_when_no_row(oauth_db):
    headers = asyncio.run(mcp_client._resolve_http_headers_async(_SOURCE, caller_user_id="user1"))
    assert headers == {}


# ---------------------------------------------------------------------------
# Refresh-when-near-expiry
# ---------------------------------------------------------------------------


def test_refresh_when_within_skew(oauth_db, monkeypatch):
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=30, refresh_token="old-rt")

    calls = []

    async def _fake_refresh(*, token_endpoint, client_id, client_secret, refresh_token, client):
        calls.append((token_endpoint, client_id, client_secret, refresh_token))
        return mcp_oauth_client.TokenSet(access_token="new-at", refresh_token="new-rt", expires_in=3600, scopes=None)

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)

    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result == "new-at"
    assert calls == [("https://as.example.com/token", "agnes-client", "agnes-secret", "old-rt")]

    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    row = MCPUserOAuthTokenRepository(oauth_db).get("src_oauth1", "user1")
    assert row["access_token"] == "new-at"
    assert row["refresh_token"] == "new-rt"


def test_refresh_reuses_old_refresh_token_when_as_omits_a_new_one(oauth_db, monkeypatch):
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=10, refresh_token="stable-rt")

    async def _fake_refresh(**kwargs):
        return mcp_oauth_client.TokenSet(access_token="new-at", refresh_token=None, expires_in=3600, scopes=None)

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)
    asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))

    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    row = MCPUserOAuthTokenRepository(oauth_db).get("src_oauth1", "user1")
    assert row["refresh_token"] == "stable-rt"


def test_invalid_grant_deletes_row(oauth_db, monkeypatch):
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=10, refresh_token="dead-rt")

    async def _fake_refresh(**kwargs):
        raise mcp_oauth_client.OAuthTokenError("token refresh failed (HTTP 400): invalid_grant: token revoked")

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result is None

    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    assert MCPUserOAuthTokenRepository(oauth_db).get("src_oauth1", "user1") is None


def test_other_as_error_keeps_row_and_returns_stale_token(oauth_db, monkeypatch):
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=10, refresh_token="rt-1")

    async def _fake_refresh(**kwargs):
        raise mcp_oauth_client.OAuthTokenError("token refresh failed (HTTP 503): server_error: try again")

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result == "at-1"  # stale, but still on file

    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    assert MCPUserOAuthTokenRepository(oauth_db).get("src_oauth1", "user1") is not None


def test_no_refresh_token_returns_stale_access_token_without_calling_as(oauth_db, monkeypatch):
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=10, refresh_token=None)

    async def _boom(**kwargs):
        raise AssertionError("must not call the AS with no refresh token")

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _boom)
    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result == "at-1"


# ---------------------------------------------------------------------------
# Two-process refresh race (spec §4: "PR 1 ships a dedicated two-process
# refresh-race test, not just contract tests")
# ---------------------------------------------------------------------------


def test_two_process_refresh_race_exactly_one_token_call(oauth_db, monkeypatch):
    """Two coroutines, each acting as a DIFFERENT process (distinct
    coordination-lease holder ids — real separate OS processes never share
    an ``asyncio.Lock``), race a refresh of the same ``(source, user)``
    pair via ``_refresh_oauth_token_with_lease`` directly (bypassing the
    in-process lock entirely). The coordination lease alone must cap the
    number of token-endpoint calls at exactly one, and neither loser must
    orphan the winner's rotated refresh token."""
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=5, refresh_token="race-rt")

    call_count = 0

    async def _fake_refresh(*, token_endpoint, client_id, client_secret, refresh_token, client):
        nonlocal call_count
        call_count += 1
        # Yield control so the second coroutine gets a chance to run before
        # this one persists — proves the LEASE (not scheduling luck) is
        # what prevents the double call.
        await asyncio.sleep(0.05)
        return mcp_oauth_client.TokenSet(
            access_token="winner-at", refresh_token="winner-rt", expires_in=3600, scopes=None
        )

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)

    async def _drive():
        return await asyncio.gather(
            mcp_client._refresh_oauth_token_with_lease("src_oauth1", "user1", holder_id="process-a"),
            mcp_client._refresh_oauth_token_with_lease("src_oauth1", "user1", holder_id="process-b"),
        )

    results = asyncio.run(_drive())

    assert call_count == 1, "exactly one token-endpoint call across both racing processes"
    # The loser must not orphan the winner's rotation: both callers observe
    # a token that is either the fresh winner's or (if it raced ahead of
    # persistence) the pre-refresh stale one — never None, never a
    # half-written row.
    assert all(r in ("winner-at", "at-1") for r in results)

    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    row = MCPUserOAuthTokenRepository(oauth_db).get("src_oauth1", "user1")
    assert row["access_token"] == "winner-at"
    assert row["refresh_token"] == "winner-rt"


def test_two_refreshes_across_separate_event_loops_both_work(oauth_db, monkeypatch):
    """The sync wrappers spin a fresh loop per asyncio.run(); a cached
    asyncio.Lock from the first loop must not poison the second renewal in
    the same process ("attached to a different loop" — Devin Review on
    #1124)."""
    _seed_client_row(oauth_db)

    async def _fake_refresh(*, token_endpoint, client_id, client_secret, refresh_token, client):
        return mcp_oauth_client.TokenSet(
            access_token=f"at-after-{refresh_token}", refresh_token="rt-next", expires_in=1, scopes=None
        )

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)

    _seed_token_row(oauth_db, expires_in_seconds=30, refresh_token="rt-1")
    first = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert first == "at-after-rt-1"

    # Second renewal for the SAME (source, user) under a brand-new loop.
    _seed_token_row(oauth_db, expires_in_seconds=30, refresh_token="rt-2")
    second = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert second == "at-after-rt-2"


def test_lease_winner_rereads_row_and_skips_replayed_refresh(oauth_db, monkeypatch):
    """TOCTOU guard: the refresh token is re-read AFTER the lease is won.
    If another process already rotated it in the window, we must NOT replay
    the superseded refresh token (AS reuse detection can revoke the whole
    grant) — and when the other process fully refreshed, no refresh call
    happens at all (Devin Review on #1124)."""
    _seed_client_row(oauth_db)
    _seed_token_row(oauth_db, expires_in_seconds=30, refresh_token="rt-stale")

    from app.coordination.factory import coordination

    real_backend = coordination()

    class _RotatingLease:
        """Simulates a concurrent process completing a full refresh between
        our initial row read and winning the lease."""

        def lease_acquire(self, name, holder, *, ttl_s):
            from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

            MCPUserOAuthTokenRepository(oauth_db).upsert(
                "src_oauth1",
                "user1",
                "at-fresh-from-other-process",
                refresh_token="rt-fresh",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            return real_backend.lease_acquire(name, holder, ttl_s=ttl_s)

        def lease_release(self, name, holder):
            return real_backend.lease_release(name, holder)

    monkeypatch.setattr("app.coordination.factory.coordination", lambda: _RotatingLease())

    refresh_calls = []

    async def _fake_refresh(*, token_endpoint, client_id, client_secret, refresh_token, client):
        refresh_calls.append(refresh_token)
        return mcp_oauth_client.TokenSet(access_token="never", refresh_token=None, expires_in=3600, scopes=None)

    monkeypatch.setattr(mcp_oauth_client, "refresh_access_token", _fake_refresh)

    result = asyncio.run(mcp_client._resolve_oauth_access_token(_SOURCE, "user1"))
    assert result == "at-fresh-from-other-process"
    assert refresh_calls == []  # the superseded rt-stale was never replayed
