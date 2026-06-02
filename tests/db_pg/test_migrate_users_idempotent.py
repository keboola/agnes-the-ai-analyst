"""NEW-X — DuckDB→PG users copy is idempotent on every UNIQUE
constraint, not just the PK.

Live-discovered 2026-06-01: with 6 source users (all distinct emails)
and ON CONFLICT (id) DO NOTHING, a psycopg executemany leaves 2 of 6
committed and fails the rest with users.email UNIQUE violation.
Replacing the target with bare ON CONFLICT DO NOTHING makes the copy
idempotent regardless of which UNIQUE constraint triggers.

Placed in tests/db_pg/ so the pg_engine fixture from conftest.py is
inherited natively (no fragile re-wrapping). The local pg_with_schema
fixture follows the same pattern as test_data_migration.py:pg_with_schema.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_with_schema(pg_engine, monkeypatch):
    """Run alembic upgrade head on the per-test PG engine."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


def test_users_copy_idempotent_when_pg_has_partial_state(
    pg_with_schema, tmp_path: Path
) -> None:
    """Pre-state PG with 1 conflicting row; migrator must complete
    without UniqueViolation.

    Pre-seed PG with a row whose ID differs from source rows but whose
    email matches 'alice@example.com'. ON CONFLICT (id) alone does NOT
    catch this — the insert collides on the email UNIQUE constraint and
    raises UniqueViolation. The fix (ON CONFLICT DO NOTHING) matches any
    UNIQUE constraint and silently skips the conflicting row.

    Expected final state:
      - preseed-1 / alice@example.com  (pre-seeded, unchanged)
      - u2 / bob@example.com           (copied from DuckDB)
      - u3 / carol@example.com         (copied from DuckDB)

    u1/alice is skipped because its email already exists; total considered
    remains 3 (the migrator sees 3 source rows regardless).
    """
    # Build a DuckDB source with 3 users.
    src = tmp_path / "system.duckdb"
    c = duckdb.connect(str(src))
    c.execute(
        """CREATE TABLE users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            name VARCHAR,
            password_hash VARCHAR,
            setup_token VARCHAR,
            setup_token_created TIMESTAMP,
            reset_token VARCHAR,
            reset_token_created TIMESTAMP,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            deactivated_at TIMESTAMP,
            deactivated_by VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP,
            onboarded BOOLEAN DEFAULT FALSE,
            last_pull_at TIMESTAMP
        )"""
    )
    rows = [
        ("u1", "alice@example.com", "Alice"),
        ("u2", "bob@example.com", "Bob"),
        ("u3", "carol@example.com", "Carol"),
    ]
    for r in rows:
        c.execute(
            "INSERT INTO users (id, email, name) VALUES (?, ?, ?)", list(r)
        )
    c.close()

    # Pre-seed PG with one row whose ID differs but whose email matches
    # ``alice@example.com``. ON CONFLICT (id) alone would NOT catch this;
    # NEW-X expects ON CONFLICT DO NOTHING (any UNIQUE) → skipped.
    with pg_with_schema.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, email, name, active, created_at) "
                "VALUES (:id, :email, :name, TRUE, NOW())"
            ),
            {"id": "preseed-1", "email": "alice@example.com", "name": "Preseed"},
        )

    # Now run the migrator task.
    from scripts.migrate_duckdb_to_pg.tasks import GenericCopyTask

    task = GenericCopyTask(table_name="users", pk_columns=["id"])
    duck_conn = duckdb.connect(str(src), read_only=True)
    try:
        considered = task.run(duck_conn, pg_with_schema)
    finally:
        duck_conn.close()

    # All 3 rows considered, none crashed.
    assert considered == 3

    # PG ends up with preseed row + the 2 non-conflicting source rows.
    # Alice (u1) was a no-op due to email conflict; bob + carol landed.
    with pg_with_schema.connect() as conn:
        emails = sorted(
            r[0] for r in conn.execute(sa.text("SELECT email FROM users")).all()
        )
    assert emails == ["alice@example.com", "bob@example.com", "carol@example.com"]
