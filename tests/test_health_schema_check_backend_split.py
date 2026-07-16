"""``_check_db_schema()`` must not force-open the process-singleton DuckDB
connection on a Postgres-backed instance — see the module docstring in
``tests/test_backend_split_guard.py`` (bug classes #513/#518) and the
production incident that motivated this test: a request-serving process
held a persistent exclusive OS-level lock on ``system.duckdb`` even though
the instance was configured with ``database.backend: cloud`` (Postgres),
traced to this health-check hitting ``get_system_db()`` unconditionally on
every liveness probe (~every few seconds).
"""

from __future__ import annotations

import app.api.health as health_mod


def test_check_db_schema_on_pg_never_opens_duckdb(monkeypatch):
    """When ``use_pg()`` is True, ``_check_db_schema`` must consult the
    Alembic revision via ``src.db_pg._pg_revisions`` and must NOT call
    ``get_system_db()`` at all."""
    monkeypatch.setattr("src.repositories.use_pg", lambda: True)
    monkeypatch.setattr("src.db_pg._pg_revisions", lambda: ("head_rev", "head_rev", False))

    def _boom():
        raise AssertionError("get_system_db() must not be called on the Postgres backend")

    monkeypatch.setattr(health_mod, "get_system_db", _boom)

    result = health_mod._check_db_schema()
    assert result == {"db_schema": "ok", "current": "head_rev", "expected": "head_rev"}


def test_check_db_schema_on_pg_reports_mismatch(monkeypatch):
    monkeypatch.setattr("src.repositories.use_pg", lambda: True)
    monkeypatch.setattr("src.db_pg._pg_revisions", lambda: ("old_rev", "head_rev", False))
    monkeypatch.setattr(
        health_mod,
        "get_system_db",
        lambda: (_ for _ in ()).throw(AssertionError("must not open DuckDB on PG")),
    )

    result = health_mod._check_db_schema()
    assert result["db_schema"] == "mismatch"
    assert result["current"] == "old_rev"
    assert result["expected"] == "head_rev"


def test_check_db_schema_on_pg_db_ahead(monkeypatch):
    """DB ahead of the image's migration scripts (app rollback) — surfaced
    as a mismatch with a distinguishing detail, matching assert_pg_at_head's
    framing of the same condition."""
    monkeypatch.setattr("src.repositories.use_pg", lambda: True)
    monkeypatch.setattr("src.db_pg._pg_revisions", lambda: ("future_rev", "head_rev", True))
    monkeypatch.setattr(
        health_mod,
        "get_system_db",
        lambda: (_ for _ in ()).throw(AssertionError("must not open DuckDB on PG")),
    )

    result = health_mod._check_db_schema()
    assert result["db_schema"] == "mismatch"
    assert "ahead" in result.get("detail", "").lower()


def test_check_db_schema_on_duckdb_unchanged(seeded_app, monkeypatch):
    """The DuckDB (not use_pg()) branch keeps reading schema_version via
    get_system_db() exactly as before."""
    monkeypatch.setattr("src.repositories.use_pg", lambda: False)
    result = health_mod._check_db_schema()
    assert result["db_schema"] == "ok"
