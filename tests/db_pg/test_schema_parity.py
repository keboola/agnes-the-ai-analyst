"""DuckDB → Postgres schema parity contract test.

Catches the agnes-dev deploy blocker class — DuckDB schema gaining a column
that PG SQLAlchemy models don't have. The migrator copies row-by-row using
the DuckDB column list as the INSERT column list; any missing column in
the PG schema raises ``psycopg.errors.UndefinedColumn`` mid-migration,
silently rolling back the chunk and leaving the target table empty.

This test creates a fresh DuckDB system database via ``_ensure_schema``
(the same path the running app uses), then diffs its column set against
each SQLAlchemy model's column set. Any drift fails the test with an
actionable message.

Tolerated drift — these intentional asymmetries are not failures:
  - ``schema_version``: DuckDB-only, replaced by alembic's
    ``alembic_version`` table on the PG side.
  - PG-only columns that are nullable AND don't appear in the DuckDB
    INSERT path (the migrator never tries to write them, so PG defaults
    them to NULL — harmless).
"""
from __future__ import annotations

import duckdb
import pytest


# DuckDB-only tables that should NEVER have a PG counterpart.
DUCKDB_ONLY_TABLES: set[str] = {
    "schema_version",  # alembic_version supersedes it in the PG world
    # cli_auth_codes are short-lived single-use exchange codes for the
    # browser-loopback ``agnes auth login`` flow (~2-min TTL). They are
    # session-local — never need cross-backend portability; the state
    # machine migrator skips them deliberately.
    "cli_auth_codes",
    # Cloud-chat tables are DuckDB-only for now. Chat runs single-worker
    # (the WS ticket store + ChatManager are in-process; HA/multi-replica
    # is explicitly out of scope today), so its transcript + per-user
    # workdir state never needs to migrate to the Postgres state backend.
    # When chat goes multi-worker, add PG models + an alembic revision and
    # drop these from the allowlist.
    "chat_sessions",
    "chat_messages",
    "user_workdirs",
}


def _duckdb_schema_snapshot(tmp_path) -> dict[str, set[str]]:
    """Return ``{tablename: {col1, col2, ...}}`` from a fresh system DuckDB."""
    import src.db as db_module

    db_path = str(tmp_path / "state" / "system.duckdb")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(db_path)
    try:
        db_module._ensure_schema(conn)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name='main'"
            ).fetchall()
        ]
        out: dict[str, set[str]] = {}
        for t in tables:
            cols = conn.execute(f"DESCRIBE main.{t}").fetchall()
            out[t] = {c[0] for c in cols}
        return out
    finally:
        conn.close()


def _pg_schema_snapshot() -> dict[str, set[str]]:
    """Return ``{tablename: {col1, col2, ...}}`` from SQLAlchemy models."""
    import src.models  # noqa: F401 — registers every model
    from src.db_pg import Base

    out: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        out[table.name] = {c.name for c in table.columns}
    return out


def test_no_duckdb_columns_missing_from_pg_models(tmp_path):
    """Every DuckDB column must have a PG model counterpart.

    Drift here is what blocked the agnes-dev deploy:
    ``UndefinedColumn: column "requirement" of relation "resource_grants"``.
    """
    duck = _duckdb_schema_snapshot(tmp_path)
    pg = _pg_schema_snapshot()

    errors: list[str] = []
    for tbl, duck_cols in duck.items():
        if tbl in DUCKDB_ONLY_TABLES:
            continue
        if tbl not in pg:
            errors.append(f"  Table '{tbl}' exists in DuckDB but has NO PG model")
            continue
        missing = duck_cols - pg[tbl]
        if missing:
            errors.append(
                f"  Table '{tbl}': PG model is missing columns {sorted(missing)}"
            )

    if errors:
        pytest.fail(
            "DuckDB → PG schema drift (would break the state-machine migrator):\n"
            + "\n".join(errors)
            + "\n\nFix: add the missing column to the corresponding "
            "src/models/*.py and create an alembic revision."
        )


def test_no_pg_only_required_columns(tmp_path):
    """PG-only columns are tolerated only if nullable.

    A PG ``NOT NULL`` column without a DuckDB counterpart would block
    the migrator's INSERT (no value supplied, no default → fail).
    """
    import src.models  # noqa: F401
    from src.db_pg import Base

    duck = _duckdb_schema_snapshot(tmp_path)
    errors: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name in DUCKDB_ONLY_TABLES:
            continue
        duck_cols = duck.get(table.name, set())
        if not duck_cols:
            # New PG table with no DuckDB rows to migrate — fine.
            continue
        for col in table.columns:
            if col.name in duck_cols:
                continue
            # PG-only column. NULLability check.
            if not col.nullable and col.server_default is None:
                errors.append(
                    f"  Table '{table.name}': PG-only NOT NULL column "
                    f"'{col.name}' without server_default — migration would fail"
                )

    if errors:
        pytest.fail(
            "PG schema has NOT NULL columns absent from DuckDB:\n" + "\n".join(errors)
        )
