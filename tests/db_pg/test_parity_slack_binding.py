"""Parity test for Slack identity binding across both backends.

The Slack bot binds a Slack ``user_id`` to an Agnes account via a /agnes
verification code: ``issue_verification_code`` → user pastes it →
``redeem_verification_code`` writes ``users.slack_user_id`` → later
``lookup_user_email`` maps the Slack id back to the account.

The binding persistence (``users.slack_user_id``) previously went through a raw
DuckDB connection, so on a Postgres instance the redeem wrote to a DuckDB
``users`` table the factory-backed reads never consult — binding silently
failed and ``/agnes`` looped on "bind your identity first". v71 formalizes the
column and routes the binding read/write through ``users_repo()``.

The transient verification-code tables stay DuckDB-only by design (ephemeral
operational state), so issue/redeem still take a DuckDB ``conn`` — while the
user + binding live in whichever state backend is active. On DuckDB that conn
is the system DB; on Postgres the system DuckDB must never be opened, so the
caller passes ``None`` (mirroring ``chat_repo._conn`` / ``_get_db`` on PG) and
``binding`` falls back to the dedicated ``operational.duckdb`` file. These
tests replicate that per-backend conn source.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def test_issue_redeem_lookup_round_trip_both_backends(_env):
    from types import SimpleNamespace

    from services.slack_bot.binding import (
        issue_verification_code,
        lookup_user_email,
        redeem_verification_code,
    )
    from src.db import get_system_db
    from src.repositories import users_repo

    # The analyst account lives in the active state backend (PG on [pg]).
    users_repo().create(id="u_slack", email="slack@example.com", name="Slacker")

    # Verification codes are DuckDB-only operational state. On DuckDB the conn
    # is the system DB; on Postgres it must be None (the system DuckDB is never
    # opened there — binding falls back to the dedicated slack_binding.duckdb).
    conn = None if _env == "pg" else get_system_db()
    code = issue_verification_code(conn, slack_user_id="U_PARITY")

    # Before redeem, the Slack id resolves to nobody.
    assert lookup_user_email(SimpleNamespace(_conn=conn), "U_PARITY") is None

    ok = redeem_verification_code(conn, user_email="slack@example.com", code=code)
    assert ok is True, f"[{_env}] redeem failed"

    # The binding must persist in the active backend and resolve back.
    bound = users_repo().get_by_slack_user_id("U_PARITY")
    assert bound is not None, (
        f"[{_env}] slack_user_id binding did not persist in the active backend "
        f"— redeem wrote to the wrong store."
    )
    assert bound["email"] == "slack@example.com"
    assert lookup_user_email(SimpleNamespace(_conn=conn), "U_PARITY") == "slack@example.com"


def test_redeem_wrong_code_does_not_bind_both_backends(_env):
    from services.slack_bot.binding import redeem_verification_code
    from src.db import get_system_db
    from src.repositories import users_repo

    users_repo().create(id="u_nb", email="nb@example.com", name="NB")
    conn = None if _env == "pg" else get_system_db()

    ok = redeem_verification_code(conn, user_email="nb@example.com", code="000000")
    assert ok is False
    assert users_repo().get_by_id("u_nb")["slack_user_id"] is None
