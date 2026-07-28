"""Parity test for co-session token resolution across both backends.

``app/auth/pat_resolver.resolve_token_to_user`` resolves a ``typ=co_session``
JWT into a ``SessionPrincipal`` by reading the live participant set
(``chat_session_participants``) and the ``is_co_session`` flag
(``chat_sessions``). It previously read those tables off the always-DuckDB
system connection, so on a Postgres instance the lookups came back empty and
EVERY co-session token failed closed (``invalid_token``). The fix routes both
reads through the repo factory (``chat_session_participants_repo()`` /
``chat_session_repo()``), and ``compute_grant_intersection`` resolves
participant identities through the factory too.

These tests seed a co-session via the factory and assert the resolver returns
the live principal on DuckDB AND Postgres (``state_backend`` runs each twice).
"""
from __future__ import annotations

import pytest

_SECRET = "test-secret-key-minimum-32-characters!!"


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    """DATA_DIR + JWT secret + (DuckDB) fresh system DB, for either backend."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", _SECRET)
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups
    return state_backend


def _seed_co_session() -> str:
    """Seed two users + a co-session (owner + invitee) through the factory.

    Returns the co-session id. Backend-agnostic: every write goes through a
    factory function, so it lands in DuckDB or Postgres per the active backend.
    """
    from app.chat.types import Surface
    from src.repositories import (
        chat_session_participants_repo,
        chat_session_repo,
        users_repo,
    )

    users_repo().create(id="ua", email="a@example.com", name="A")
    users_repo().create(id="ub", email="b@example.com", name="B")
    s0 = chat_session_repo().create_session(
        user_email="a@example.com", surface=Surface.WEB
    )
    s1 = chat_session_participants_repo().fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    return s1.id


def test_co_session_token_resolves_live_principal_both_backends(_env):
    """A co-session JWT resolves to a SessionPrincipal carrying the live
    participant set — on DuckDB and Postgres alike."""
    from app.auth.access import mint_co_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import SessionPrincipal

    co_id = _seed_co_session()
    subj, reason = resolve_token_to_user(None, mint_co_session_jwt(co_id))

    assert reason is None, f"unexpected reject on {_env}: {reason}"
    assert isinstance(subj, SessionPrincipal)
    assert set(subj.participant_emails) == {"a@example.com", "b@example.com"}
    assert set(subj.participant_user_ids) == {"ua", "ub"}


def test_single_user_token_against_co_session_fails_closed_both_backends(_env):
    """Defense-in-depth (SR-3): a plain single-user token that names a
    co-session must fail closed regardless of backend."""
    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user

    co_id = _seed_co_session()
    tok = create_access_token(
        "ua", "a@example.com", extra_claims={"chat_session_id": co_id}
    )
    subj, reason = resolve_token_to_user(None, tok)

    assert subj is None
    assert reason == "invalid_token"
