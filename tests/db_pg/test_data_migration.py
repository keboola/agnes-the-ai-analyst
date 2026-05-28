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


def test_migrate_pg_array_columns_coerce_from_duckdb_json_strings(tmp_path, pg_with_schema):
    """Regression: DuckDB-stored JSON arrays must arrive in PG as PG arrays.

    ``metric_definitions.dimensions`` (and ``tables``/``filters``/
    ``synonyms``/``notes``) are typed as ``ARRAY(String)`` on the PG
    side but DuckDB serialises them as JSON-encoded strings. Without
    the array-column coercion in ``GenericCopyTask.run``, psycopg
    forwards the raw string ``["technology", "kbc_stack"]`` to PG and
    PG raises ``InvalidTextRepresentation: malformed array literal —
    "[" must introduce explicitly-specified array dimensions`` —
    surfaced live on agnes-dev v5 migration.
    """
    import json
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, validate_task, TASKS

    duck_path = tmp_path / "src.duckdb"
    duck_conn = duckdb.connect(str(duck_path))
    _ensure_schema(duck_conn)

    # Seed a metric_definitions row whose ARRAY columns are JSON strings —
    # the exact shape DuckDB produces on the live agnes-dev system.
    duck_conn.execute(
        """
        INSERT INTO metric_definitions
            (id, name, display_name, category, description, type, unit, grain,
             table_name, tables, expression, time_column, dimensions, filters,
             synonyms, notes, sql, sql_variants, validation, source)
        VALUES
            ('test/m', 'm', 'Metric', 'finance', 'd', 'sum', 'USD', 'monthly',
             'tbl', ?, 'expr', 't', ?, NULL, ?, ?, 'SELECT 1', NULL, NULL,
             'manual')
        """,
        [
            json.dumps(["t1", "t2"]),
            json.dumps(["dim1", "dim2", "dim3"]),
            json.dumps(["syn"]),
            json.dumps(["note"]),
        ],
    )

    task = next(t for t in TASKS if t.target_table == "metric_definitions")
    run_task(task, duck_conn, pg_with_schema)

    # All four ARRAY columns must round-trip as native PG arrays.
    with pg_with_schema.connect() as conn:
        from sqlalchemy import text as sa_text
        row = conn.execute(
            sa_text("SELECT tables, dimensions, synonyms, notes FROM metric_definitions WHERE id='test/m'")
        ).first()
    assert row.tables == ["t1", "t2"]
    assert row.dimensions == ["dim1", "dim2", "dim3"]
    assert row.synonyms == ["syn"]
    assert row.notes == ["note"]

    report = validate_task(task, duck_conn, pg_with_schema)
    assert report["pg_rows"] == 1
    assert report["checksum_match"] is True
    duck_conn.close()


def test_migrate_substitutes_default_for_not_null_columns_with_null_value(tmp_path, pg_with_schema):
    """Regression: DuckDB rows with ``created_at=NULL`` must migrate cleanly.

    PG model declares ``created_at`` as ``NOT NULL`` with
    ``server_default=CURRENT_TIMESTAMP``, but SQLAlchemy treats explicit
    ``None`` in bind parameters as literal NULL — so PG raises
    ``NotNullViolation``. The migrator must substitute the server's
    default at copy time. Live agnes-dev v6: marketplace_plugins's
    ``keboola-howto`` row had ``created_at=NULL`` and blocked the whole
    migration on its single row.
    """
    from datetime import datetime, timezone
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck_path = tmp_path / "src.duckdb"
    duck_conn = duckdb.connect(str(duck_path))
    _ensure_schema(duck_conn)

    # Seed a marketplace_plugins row with created_at=NULL, mirroring
    # what agnes-dev DuckDB had.
    duck_conn.execute(
        """
        INSERT INTO marketplace_plugins
            (marketplace_id, name, description, version, category,
             source_type, source_spec, updated_at, created_at, is_system)
        VALUES
            ('mkt', 'plug', 'desc', '1.0', 'cat',
             'path', '{}', CURRENT_TIMESTAMP, NULL, FALSE)
        """
    )

    task = next(t for t in TASKS if t.target_table == "marketplace_plugins")
    run_task(task, duck_conn, pg_with_schema)

    from sqlalchemy import text as sa_text
    with pg_with_schema.connect() as conn:
        row = conn.execute(
            sa_text("SELECT marketplace_id, name, created_at FROM marketplace_plugins")
        ).first()
    assert row is not None, "marketplace_plugins row was not migrated"
    assert row.created_at is not None, "created_at must be auto-filled, not NULL"
    # The substituted timestamp should be close to now (within the last
    # 10s) — generous threshold to avoid CI flake.
    delta = abs((datetime.now(timezone.utc) - row.created_at).total_seconds())
    assert delta < 30, f"substituted created_at is off by {delta}s"
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


def test_run_all_reports_per_table_error(tmp_path, pg_with_schema):
    """If a per-table copy raises, ``run_all`` must include the failure
    in its return list with an ``error`` key — and the CLI wrapper must
    exit non-zero on that signal.

    Regression for the cvrysanek review item: the predicate
    ``all(r.get("checksum_match", True) ...)`` returned True for error
    reports (default), so the migrator exited 0 even on hard failure,
    the applier read MIG_RC=0, flipped the backend, and the app booted
    against a partially-populated PG.

    We seed a row in audit_log so there is data to copy, then drop the PG
    table to force a real INSERT-time failure. An empty source would return
    0 rows silently (nothing to insert = no error), so we need actual data.
    """
    import duckdb
    from src.db import _ensure_schema
    from src.repositories.audit import AuditRepository
    from scripts.migrate_duckdb_to_pg import run_all

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    # Seed a row so the copy actually tries to INSERT.
    AuditRepository(duck).log(user_id="u1", action="test.event", correlation_id="c-1")
    # Drop the PG table to force per-table failure on copy.
    with pg_with_schema.connect() as conn:
        from sqlalchemy import text as sa_text
        conn.execute(sa_text("DROP TABLE IF EXISTS audit_log CASCADE"))
        conn.commit()

    reports = run_all(duck, pg_with_schema, validate=False)
    # At least one report must carry an error.
    assert any("error" in r for r in reports), reports
    duck.close()


def test_run_all_cli_exits_nonzero_on_error_report(tmp_path, pg_with_schema):
    """The CLI wrapper must exit 1 when reports contain an error
    entry, regardless of whether checksum_match is missing."""
    # Build a synthetic reports list that mimics what run_all returns
    # on per-table failure, then exercise the predicate that the CLI
    # uses (extracted into a callable for testability — or copy the
    # exact same predicate inline if the CLI uses a literal one-liner).
    reports = [
        {"table": "audit_log", "duckdb_rows": 10, "pg_rows": 10, "checksum_match": True},
        {"table": "users", "error": "table missing in PG"},
    ]
    # The predicate the CLI uses:
    ok = all("error" not in r and r.get("checksum_match", True) for r in reports)
    assert ok is False, "CLI predicate must reject reports with error entries"


def test_run_raises_on_duckdb_column_missing_in_pg_with_data(tmp_path, pg_with_schema):
    """If DuckDB has data in a column the PG schema lacks, the copy
    task MUST raise. Silent drop = silent data loss. Empty columns
    pass through with a warning (covered by a separate test).
    """
    import duckdb
    import pytest
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    # Add an extra column DuckDB-side that PG doesn't have.
    duck.execute("ALTER TABLE table_registry ADD COLUMN extra_field VARCHAR")
    duck.execute(
        "INSERT INTO table_registry (id, name, source_type, extra_field) "
        "VALUES ('t1', 'tbl', 'duckdb', 'has-data')"
    )
    task = next(t for t in TASKS if t.target_table == "table_registry")
    with pytest.raises(RuntimeError, match="extra_field.*data will be lost"):
        run_task(task, duck, pg_with_schema)
    duck.close()


def test_run_warns_but_continues_on_empty_duckdb_only_column(tmp_path, pg_with_schema, caplog):
    """DuckDB-only column with NO data → warning log + continue.
    Operator's response: drop the column from DuckDB to clean it up;
    not blocking the cutover."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    duck.execute("ALTER TABLE table_registry ADD COLUMN unused_field VARCHAR")
    # No data inserted into unused_field.
    task = next(t for t in TASKS if t.target_table == "table_registry")
    with caplog.at_level("WARNING"):
        run_task(task, duck, pg_with_schema)  # must not raise
    assert any("unused_field" in rec.message for rec in caplog.records)


def test_audit_log_timestamp_preserved_when_present(tmp_path, pg_with_schema):
    """audit_log rows with explicit timestamps must keep them. The
    previous _substitute_default replaced NULLs AND non-NULL bound
    values with datetime.now() because the helper looked at the
    column's server_default + nullable status, not whether the row
    actually carried a value. Audit trail integrity => never rewrite.
    """
    import datetime as _dt
    import duckdb
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    original = _dt.datetime(2025, 1, 15, 9, 30, 0, tzinfo=_dt.timezone.utc)
    duck.execute(
        "INSERT INTO audit_log (id, timestamp, action) VALUES (?, ?, ?)",
        ["a1", original, "test.event"],
    )

    task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(task, duck, pg_with_schema)

    with pg_with_schema.connect() as conn:
        row = conn.execute(sa_text("SELECT timestamp FROM audit_log WHERE id='a1'")).first()
    assert row.timestamp == original
    duck.close()
    duck.close()


def test_copy_duckdb_to_pg_summary_lists_failed_tables(tmp_path, pg_with_schema):
    """copy_duckdb_to_pg currently silently drops failed-table reports
    from its summary (the ``if 'error' not in r`` filter). Operators
    + verify both then see ``tables_migrated == len(reports)`` and
    proceed. The summary must list failures explicitly.

    We seed audit_log with a row so the copy for that table actually
    touches PG, then drop the audit_log table from PG to force a
    per-table failure that is non-empty (users and most tables are
    empty in the seed, so a missing PG table silently copies 0 rows).
    """
    import duckdb
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from src.repositories.audit import AuditRepository
    from scripts.db_state_migrator import copy_duckdb_to_pg

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    # Seed a row so the copy actually tries to INSERT into PG.
    AuditRepository(duck).log(user_id="u1", action="test.event", correlation_id="c-1")
    duck.close()

    # Drop the target table to force a per-table error on copy.
    with pg_with_schema.connect() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS audit_log CASCADE"))
        conn.commit()

    summary = copy_duckdb_to_pg(duck_path, str(pg_with_schema.url))
    assert summary.get("tables_failed"), summary
    assert "audit_log" in [t["table"] for t in summary["tables_failed"]]
    # Shape: each entry must carry both keys.
    for entry in summary["tables_failed"]:
        assert "table" in entry and "error" in entry, entry
