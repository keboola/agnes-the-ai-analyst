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
