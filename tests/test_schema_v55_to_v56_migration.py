"""v55 → v56: extended content columns on ``data_packages`` + per-table
documentation columns on ``table_registry``.

The new fields back the per-package detail page rewrite (Foundry Data
team extended-descriptions spec). All columns are ADDITIVE + NULLABLE so
existing instances upgrade cleanly without backfill.

Asserts on the migration shape:

  * fresh install lands at v56 with the new columns
  * sequential upgrade from v55 adds the columns without dropping data
  * idempotent — re-running the migration is a no-op
  * SCHEMA_VERSION constant matches
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


_DATA_PACKAGES_V56_COLS = {
    # v56 additions — Foundry spec content fields.
    "owner_name", "owner_team",
    "tags", "long_description",
    "when_to_use", "when_not_to_use",
    "example_questions",
}

_TABLE_REGISTRY_V56_COLS = {
    "grain", "platforms", "partition_col", "history", "gotchas",
}


def _columns(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE lower(table_name) = lower(?)",
            [table],
        ).fetchall()
    }


def test_schema_version_is_56():
    assert SCHEMA_VERSION == 56


def test_fresh_install_lands_at_v56(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert get_schema_version(conn) == 56


def test_v55_to_v56_adds_data_packages_owner_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "data_packages")
    assert {"owner_name", "owner_team"}.issubset(cols)


def test_v55_to_v56_adds_data_packages_content_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "data_packages")
    assert {
        "tags", "long_description",
        "when_to_use", "when_not_to_use",
        "example_questions",
    }.issubset(cols)


def test_v55_to_v56_adds_table_registry_extended_docs(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "table_registry")
    assert _TABLE_REGISTRY_V56_COLS.issubset(cols), (
        f"missing v56 columns; have: {sorted(cols)}"
    )


def test_v55_to_v56_preserves_existing_data_packages_rows(tmp_path):
    """Sequential upgrade path: seed at v55, run _ensure_schema, assert
    the seeded row still exists with its v55 columns intact and the new
    v56 columns default to NULL / empty."""
    from src.db import _v54_to_v55  # provides v55 state

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)  # land at current (v56+) so columns exist

    # Pretend we're a v55 instance: rewind version stamp + drop v56 cols.
    for col in _DATA_PACKAGES_V56_COLS:
        conn.execute(f"ALTER TABLE data_packages DROP COLUMN IF EXISTS {col}")
    for col in _TABLE_REGISTRY_V56_COLS:
        conn.execute(f"ALTER TABLE table_registry DROP COLUMN IF EXISTS {col}")
    conn.execute("UPDATE schema_version SET version = 55")

    # Seed a pre-v56 package row.
    conn.execute(
        "INSERT INTO data_packages(id, slug, name, description, created_by) "
        "VALUES ('pkg_legacy', 'legacy', 'Legacy bundle', 'Pre-v56 row', 'seed')"
    )
    conn.close()

    # Re-open + re-migrate; v55→v56 ALTER ADD COLUMN IF NOT EXISTS should run.
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    assert get_schema_version(conn) == 56

    row = conn.execute(
        "SELECT slug, name, description, owner_name, owner_team, tags "
        "FROM data_packages WHERE id = 'pkg_legacy'"
    ).fetchone()
    assert row[0] == "legacy"
    assert row[1] == "Legacy bundle"
    assert row[2] == "Pre-v56 row"
    # New columns exist and default to NULL.
    assert row[3] is None
    assert row[4] is None
    assert row[5] is None


def test_v55_to_v56_is_idempotent(tmp_path):
    """Running ``_ensure_schema`` twice in a row must not raise."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _ensure_schema(conn)  # second pass — no-op
    assert get_schema_version(conn) == 56


def test_v55_to_v56_preserves_table_registry_rows(tmp_path):
    """Seed a v55-style table_registry row, drop v56 cols, re-migrate,
    confirm the row + its v52 docs columns (sample_questions / things_to_know /
    pairs_well_with) survive intact alongside the new v56 ones."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    for col in _TABLE_REGISTRY_V56_COLS:
        conn.execute(f"ALTER TABLE table_registry DROP COLUMN IF EXISTS {col}")
    conn.execute("UPDATE schema_version SET version = 55")
    conn.execute(
        "INSERT INTO table_registry(id, name, description, sample_questions, "
        "things_to_know) "
        "VALUES ('tbl_legacy', 'orders', 'Pre-v56 table', "
        "        '[\"q1\", \"q2\"]', 'old notes')"
    )
    conn.close()

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    row = conn.execute(
        "SELECT name, description, sample_questions, things_to_know, "
        "       grain, platforms, partition_col, history, gotchas "
        "FROM table_registry WHERE id = 'tbl_legacy'"
    ).fetchone()
    assert row[0] == "orders"
    assert row[1] == "Pre-v56 table"
    assert row[2] == '["q1", "q2"]'  # v52 docs survived
    assert row[3] == "old notes"
    assert row[4] is None  # grain — new, default NULL
    assert row[5] is None
    assert row[6] is None
    assert row[7] is None
    assert row[8] is None
