"""Parity tests for the MCP secret vault at forward time, across both backends.

Two problems this pins:

1. **Shared vault on Postgres.** ``SharedSecretsRepository`` (the server-wide
   ``mcp_secrets`` vault) had no Postgres implementation, so shared MCP
   credentials lived only in the DuckDB system file even on a PG instance —
   inconsistent with the per-user secrets that moved to PG (#530) and lost on a
   DuckDB reset. A new ``SharedSecretsPgRepository`` + ``shared_secrets_repo()``
   factory fix that.

2. **Per-user secret invisible at forward time on Postgres (active bug).**
   ``connectors.mcp.client._lookup_secret_for_source`` read the per-user secret
   off a raw always-DuckDB connection. Per-user secrets are stored in Postgres,
   so on a PG instance the analyst's own credential was never found at call time
   and the call silently fell through to the shared/env path. Both reads now go
   through the factory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


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


def test_shared_secret_round_trip_both_backends(_env):
    from src.repositories import shared_secrets_repo

    repo = shared_secrets_repo()
    repo.upsert("src_shared", "sh-secret-value")
    assert repo.has("src_shared") is True
    assert repo.get("src_shared") == "sh-secret-value"

    repo.delete("src_shared")
    assert repo.has("src_shared") is False
    assert repo.get("src_shared") is None


def test_lookup_resolves_per_user_secret_both_backends(_env):
    """scope='per_user' with the caller's own stored secret → that secret,
    resolved through the factory on either backend (the active PG bug)."""
    from connectors.mcp.client import _lookup_secret_for_source
    from src.repositories import per_user_secrets_repo

    per_user_secrets_repo().upsert("src_pu", "user1", "alice-token")
    src = {"id": "src_pu", "scope": "per_user", "auth_secret_env": ""}

    value = _lookup_secret_for_source(src, caller_user_id="user1")
    assert value == "alice-token", (
        f"[{_env}] per-user secret not resolved at forward time — it is stored "
        f"in the active backend but was read off the always-DuckDB connection."
    )


def test_lookup_falls_back_to_shared_both_backends(_env):
    """scope='shared' → shared vault; per_user materialize (no caller) → shared;
    per_user with an identified caller and no row → fail closed (None, never
    the shared credential). Resolved through the factory on either backend."""
    from connectors.mcp.client import _lookup_secret_for_source
    from src.repositories import shared_secrets_repo

    shared_secrets_repo().upsert("src_sh", "shared-token")

    per_user = {"id": "src_sh", "scope": "per_user", "auth_secret_env": ""}
    # Identified caller, no per-user row → fail closed; shared NOT borrowed.
    assert _lookup_secret_for_source(per_user, caller_user_id="nobody") is None
    # Materialize path (no caller) → shared fallback preserved.
    assert _lookup_secret_for_source(per_user, caller_user_id=None) == "shared-token"

    # plain shared scope.
    shared = {"id": "src_sh", "scope": "shared", "auth_secret_env": ""}
    assert _lookup_secret_for_source(shared) == "shared-token"


def test_admin_delete_secret_clears_shared_vault_both_backends(seeded_app_both):
    """DELETE /api/admin/mcp-sources/{id}/secret drops the shared row on both
    backends (the endpoint routes through shared_secrets_repo())."""
    from src.repositories import mcp_sources_repo, shared_secrets_repo

    sid = "src_admin_del"
    mcp_sources_repo().upsert(
        id=sid,
        name="probe",
        transport="http",
        url="https://example.com/mcp",
        scope="shared",
    )
    shared_secrets_repo().upsert(sid, "to-clear")
    assert shared_secrets_repo().has(sid) is True

    r = seeded_app_both["client"].delete(
        f"/api/admin/mcp-sources/{sid}/secret",
        headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
    )
    assert r.status_code == 204, f"[{seeded_app_both['backend']}] {r.text}"
    assert shared_secrets_repo().has(sid) is False, (
        f"[{seeded_app_both['backend']}] shared secret survived admin delete"
    )


# ---------------------------------------------------------------------------
# decrypt failure (vault key rotated) — both vault scopes, both backends.
# Both repos read through app.secrets_vault.decrypt_optional; these pin the
# contract that a stale row degrades to None (never raises) and that the
# WARNING still names the column that failed.
# ---------------------------------------------------------------------------


def test_shared_secret_decrypt_failure_returns_none_both_backends(_env, monkeypatch, caplog):
    import logging

    from cryptography.fernet import Fernet

    from src.repositories import shared_secrets_repo

    repo = shared_secrets_repo()
    repo.upsert("src_rot", "encrypted-under-key-a")

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.secrets_vault"):
        assert repo.get("src_rot") is None, (
            f"[{_env}] undecryptable shared secret must read as absent so the caller can fall back to the env-var path"
        )
    # The row is still there — only unreadable. has() must not start lying.
    assert repo.has("src_rot") is True, f"[{_env}] has() should still see the stale row"
    assert "mcp_secrets.secret_value_enc[src_rot]" in caplog.text, (
        f"[{_env}] decrypt warning does not identify the column: {caplog.text!r}"
    )


def test_per_user_secret_decrypt_failure_returns_none_both_backends(_env, monkeypatch, caplog):
    import logging

    from cryptography.fernet import Fernet

    from src.repositories import per_user_secrets_repo

    repo = per_user_secrets_repo()
    repo.upsert("src_pu_rot", "user1", "encrypted-under-key-a")

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.secrets_vault"):
        assert repo.get("src_pu_rot", "user1") is None, f"[{_env}] undecryptable per-user secret must read as absent"
    assert repo.has("src_pu_rot", "user1") is True, f"[{_env}] has() should still see the stale row"
    assert "mcp_user_secrets.secret_value_enc[src_pu_rot/user1]" in caplog.text, (
        f"[{_env}] decrypt warning does not identify the column: {caplog.text!r}"
    )
