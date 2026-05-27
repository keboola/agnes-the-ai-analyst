"""Tests for the DuckDB → Postgres data migration framework.

The framework lives at ``scripts/migrate_duckdb_to_pg/`` and is invoked
either as a one-shot CLI (``python -m scripts.migrate_duckdb_to_pg``) or
piecewise from Python code during the dual-write window.

Contract:
  - A ``MigrationTask`` describes how to copy one table.
  - ``run_task(task, duckdb_conn, pg_engine, dry_run=False)`` performs
    the copy. Idempotent (re-runs are safe; ON CONFLICT DO NOTHING on
    the PK).
  - ``validate_task(task, duckdb_conn, pg_engine)`` returns a dict with
    ``duckdb_rows``, ``pg_rows``, and ``checksum_match: bool``.
  - Dry-run mode logs intent but does not write to PG.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def duckdb_with_audit_rows(tmp_path):
    """Seeded DuckDB with the audit_log table + a few rows."""
    from src.db import _ensure_schema

    db_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    from src.repositories.audit import AuditRepository
    repo = AuditRepository(conn)
    repo.log(user_id="u1", action="auth.login", correlation_id="c-1")
    repo.log(user_id="u1", action="sync.trigger", correlation_id="c-2")
    repo.log(user_id="u2", action="auth.logout", correlation_id="c-3")
    yield conn
    conn.close()


@pytest.fixture
def pg_with_schema(pg_engine, monkeypatch):
    """Run alembic upgrade head on the per-test PG."""
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


def test_module_imports():
    """The migration framework module exists."""
    import scripts.migrate_duckdb_to_pg as m
    assert hasattr(m, "MigrationTask")
    assert hasattr(m, "run_task")
    assert hasattr(m, "validate_task")
    assert hasattr(m, "TASKS")


def test_migrate_audit_log_round_trip(duckdb_with_audit_rows, pg_with_schema):
    """DuckDB → PG copy preserves rows and validates clean."""
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS

    audit_task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    report = validate_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    assert report["duckdb_rows"] == 3
    assert report["pg_rows"] == 3
    assert report["checksum_match"] is True


def test_migrate_audit_log_is_idempotent(duckdb_with_audit_rows, pg_with_schema):
    """Running the same task twice does not duplicate rows."""
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS

    audit_task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    run_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    report = validate_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    assert report["pg_rows"] == 3, "re-running must not duplicate"


def test_migrate_dry_run_does_not_write(duckdb_with_audit_rows, pg_with_schema):
    """dry_run=True logs but performs no writes."""
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS

    audit_task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(audit_task, duckdb_with_audit_rows, pg_with_schema, dry_run=True)
    report = validate_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    assert report["pg_rows"] == 0, "dry-run wrote rows"


def test_validation_detects_data_drift(duckdb_with_audit_rows, pg_with_schema):
    """If a row exists in DuckDB but not PG, validation reports mismatch."""
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS
    from src.repositories.audit import AuditRepository

    audit_task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(audit_task, duckdb_with_audit_rows, pg_with_schema)

    # Add a row to DuckDB only — PG is now behind
    AuditRepository(duckdb_with_audit_rows).log(action="late.event")
    report = validate_task(audit_task, duckdb_with_audit_rows, pg_with_schema)
    assert report["duckdb_rows"] == 4
    assert report["pg_rows"] == 3
    assert report["checksum_match"] is False


def test_migrate_users_round_trip(tmp_path, pg_with_schema):
    """Users migration mirrors rows to PG."""
    from src.db import _ensure_schema
    from src.repositories.users import UserRepository
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS

    duck_path = tmp_path / "src.duckdb"
    duck_conn = duckdb.connect(str(duck_path))
    _ensure_schema(duck_conn)
    users = UserRepository(duck_conn)
    users.create(id="u1", email="alice@example.com", name="Alice")
    users.create(id="u2", email="bob@example.com", name="Bob")

    task = next(t for t in TASKS if t.target_table == "users")
    run_task(task, duck_conn, pg_with_schema)
    report = validate_task(task, duck_conn, pg_with_schema)
    assert report["pg_rows"] == 2
    assert report["checksum_match"] is True
    duck_conn.close()


def test_non_id_pk_tables_are_in_pk_columns_map():
    """Tables whose primary key isn't a single column named 'id' must be
    registered in _PK_COLUMNS so the generic copy loop knows what to
    ON CONFLICT on. Catches the regression where a new model with a
    composite or renamed PK is added without updating _PK_COLUMNS."""
    from src import models  # noqa: F401 — ensure all models register
    from src.db_pg import Base
    from scripts.migrate_duckdb_to_pg import _PK_COLUMNS

    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        pk_cols = [c.name for c in table.primary_key.columns]
        if pk_cols != ["id"] and table.name not in _PK_COLUMNS:
            missing.append(f"{table.name} (PK={pk_cols})")
    assert not missing, (
        "Tables with non-id primary keys must be registered in _PK_COLUMNS:\n  - "
        + "\n  - ".join(missing)
        + "\nAdd them to scripts/migrate_duckdb_to_pg/__init__.py._PK_COLUMNS."
    )
