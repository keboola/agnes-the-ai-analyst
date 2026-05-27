"""PG-backed tests for db_state_migrator (alembic upgrade step).

JobWriter unit tests live in ``tests/test_db_state_migrator.py`` because
they don't need a Postgres instance. This module covers steps that
require a real PG target — currently just ``alembic_upgrade_head``.
"""
from __future__ import annotations


def test_alembic_upgrade_head_runs(tmp_path, pg_engine):
    """alembic_upgrade_head brings target to current head revision."""
    from scripts.db_state_migrator import alembic_upgrade_head

    alembic_upgrade_head(str(pg_engine.url))

    # Verify alembic_version row exists with head revision
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    assert row is not None
    assert len(row[0]) > 0


def test_copy_duckdb_to_pg_full_cycle(tmp_path, pg_engine):
    """Seed DuckDB → copy to PG → verify rows present."""
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES ('u1', 'alice@example.com', 'Alice')"
    )
    conn.close()

    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg
    alembic_upgrade_head(str(pg_engine.url))

    summary = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert summary["rows_total"] >= 1

    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row[0] == "alice@example.com"
