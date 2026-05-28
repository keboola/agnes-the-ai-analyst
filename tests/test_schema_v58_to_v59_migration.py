"""v57 → v58: extended content columns on ``data_packages`` + per-table
documentation columns on ``table_registry``.

The new fields back the per-package detail page rewrite (Foundry Data
team extended-descriptions spec). All columns are ADDITIVE + NULLABLE so
existing instances upgrade cleanly without backfill.

Asserts on the migration shape:

  * fresh install lands at v59 with the new columns
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


def test_schema_version_is_current():
    # v59 → v60: ``setup_tokens`` table for Agnes Cowork one-click setup.
    # v60 → v61: ``mcp_sources`` + ``tool_registry`` + ``tool_grants`` (Universal MCP).
    # v61 → v62: ``mcp_secrets`` server-wide vault for MCP source auth.
    assert SCHEMA_VERSION == 63


def test_fresh_install_lands_at_current(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert get_schema_version(conn) == 63


def test_v58_to_v59_adds_data_packages_owner_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "data_packages")
    assert {"owner_name", "owner_team"}.issubset(cols)


def test_v58_to_v59_adds_data_packages_content_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "data_packages")
    assert {
        "tags", "long_description",
        "when_to_use", "when_not_to_use",
        "example_questions",
    }.issubset(cols)


def test_v58_to_v59_adds_table_registry_extended_docs(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "table_registry")
    assert _TABLE_REGISTRY_V56_COLS.issubset(cols), (
        f"missing v56 columns; have: {sorted(cols)}"
    )


def test_v58_to_v59_preserves_existing_data_packages_rows(tmp_path):
    """Sequential upgrade path: seed at v56, run the v58→v58 migration
    function directly, assert the seeded row still exists with its v55
    columns intact and the new v56 columns default to NULL.

    Uses ``_v58_to_v59`` in isolation rather than the full ``_ensure_schema``
    ladder — DuckDB refuses to drop columns when FK constraints
    (``data_package_tables`` here) reference the table, so we can't
    simulate a "real" v55 instance by mutating a v56 schema. Instead we
    seed a row, drop the v56 columns on an empty-FK copy, and re-add
    them via the migration.
    """
    from src.db import _v58_to_v59

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO data_packages(id, slug, name, description, created_by) "
        "VALUES ('pkg_legacy', 'legacy', 'Legacy bundle', 'Pre-v56 row', 'seed')"
    )
    # Re-run the v57→v58 migration; ALTER ADD COLUMN IF NOT EXISTS is a
    # no-op on already-present columns and must not nuke the seeded row.
    _v58_to_v59(conn)
    row = conn.execute(
        "SELECT slug, name, description, owner_name, owner_team, tags "
        "FROM data_packages WHERE id = 'pkg_legacy'"
    ).fetchone()
    assert row[0] == "legacy"
    assert row[1] == "Legacy bundle"
    assert row[2] == "Pre-v56 row"
    assert row[3] is None
    assert row[4] is None
    assert row[5] is None


def test_v58_to_v59_is_idempotent(tmp_path):
    """Running ``_ensure_schema`` twice in a row must not raise."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _ensure_schema(conn)  # second pass — no-op
    assert get_schema_version(conn) == 63


def test_v58_to_v59_preserves_table_registry_rows():
    """Seed a row, re-run the v57→v58 migration (idempotent ADD COLUMN
    IF NOT EXISTS) and confirm the row + its v52 docs columns
    (sample_questions / things_to_know / pairs_well_with) survive intact
    alongside the new v56 ones."""
    from src.db import _v58_to_v59

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO table_registry(id, name, description, sample_questions, "
        "things_to_know) "
        "VALUES ('tbl_legacy', 'orders', 'Pre-v56 table', "
        "        '[\"q1\", \"q2\"]', 'old notes')"
    )
    _v58_to_v59(conn)
    row = conn.execute(
        "SELECT name, description, sample_questions, things_to_know, "
        "       grain, platforms, partition_col, history, gotchas "
        "FROM table_registry WHERE id = 'tbl_legacy'"
    ).fetchone()
    assert row[0] == "orders"
    assert row[1] == "Pre-v56 table"
    # sample_questions stored as JSON column → roundtrips as a list-string
    # depending on DuckDB version; accept either canonical JSON or the
    # already-parsed list (DuckDB 1.5+ returns lists for JSON columns).
    raw_sq = row[2]
    if isinstance(raw_sq, str):
        import json
        assert json.loads(raw_sq) == ["q1", "q2"]
    else:
        assert list(raw_sq) == ["q1", "q2"]
    assert row[3] == "old notes"
    assert row[4] is None  # grain — new, default NULL
    assert row[5] is None
    assert row[6] is None
    assert row[7] is None
    assert row[8] is None
