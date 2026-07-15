"""End-to-end guard: a Postgres instance never opens the system DuckDB.

Boots the FULL app lifespan (``create_app()`` under a ``TestClient`` context
manager) on each backend and asserts the invariant behaviourally:

  * Postgres — no ``{DATA_DIR}/state/system.duckdb`` file is ever created, and
    auth still works end-to-end (an authenticated request + the admin gate).
  * DuckDB — the system DuckDB IS created (the store) and auth works.

This is the behavioural complement to ``tests/test_backend_split_guard.py``
(static) and ``test_parity_seed_admin_groups.py`` (unit): it exercises the real
startup path + a couple of request handlers so a future ungated
``get_system_db()`` on the request/boot path fails here.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _seed_admin() -> str:
    """Create an admin user through the active-backend factory, return a token."""
    import sqlalchemy as sa

    from app.auth.jwt import create_access_token
    from src.repositories import use_pg, user_group_members_repo, users_repo

    users_repo().create(id="boot_admin", email="boot-admin@example.com", name="Boot Admin")

    if use_pg():
        from src.db_pg import get_engine

        with get_engine().connect() as c:
            admin_gid = c.execute(
                sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")
            ).scalar()
    else:
        from src.db import get_system_db

        admin_gid = get_system_db().execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()[0]
    user_group_members_repo().add_member("boot_admin", admin_gid, source="system_seed")
    return create_access_token("boot_admin", "boot-admin@example.com")


def test_full_boot_and_auth_respects_system_duckdb_invariant(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    # Reset the system-DB singleton so DATA_DIR takes effect for this test.
    from src.db import close_system_db

    close_system_db()

    token = _seed_admin()

    from fastapi.testclient import TestClient

    from app.main import create_app

    system_duckdb = Path(tmp_path) / "state" / "system.duckdb"

    # The context manager runs startup + shutdown lifespan events.
    with TestClient(create_app()) as client:
        # Liveness — no auth. Backend-aware schema check must report healthy.
        r = client.get("/api/health")
        assert r.status_code == 200, r.text
        assert r.json()["db_schema"] == "ok", r.json()

        auth = {"Authorization": f"Bearer {token}"}

        # Authenticated request — exercises the (now plain-def) auth dependency.
        r = client.get("/api/me/home-stats", headers=auth)
        assert r.status_code == 200, f"[{state_backend}] authed request failed: {r.text}"

        # Admin gate — require_admin resolves the Admin membership on the
        # active backend.
        r = client.get("/api/admin/server-config", headers=auth)
        assert r.status_code == 200, f"[{state_backend}] admin gate failed: {r.text}"

    if state_backend == "pg":
        assert not system_duckdb.exists(), (
            "system.duckdb was created on a Postgres instance during full boot "
            f"+ auth at {system_duckdb}"
        )
    else:
        assert system_duckdb.exists(), (
            f"[duck] system.duckdb should be the store but was not created at {system_duckdb}"
        )
