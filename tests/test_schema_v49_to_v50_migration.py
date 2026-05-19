"""v49 → v50 migration: UNIQUE INDEX on ``store_entities.synthetic_name``.

v49 introduced ``synthetic_name`` as ``NOT NULL`` but without uniqueness.
With the column now the canonical attribution key (rollup keyspace, JSONL
prefix, marketplace bundle naming), v50 enforces uniqueness at the DB
level via ``CREATE UNIQUE INDEX`` (DuckDB has no
``ALTER TABLE ADD CONSTRAINT UNIQUE`` for existing tables).

Migration must:
- Pre-flight scan for existing duplicates and raise ``RuntimeError`` with
  a structured diagnostic listing them (instead of letting the index
  create fail mid-way with a raw DuckDB error).
- Create the UNIQUE index idempotently (re-runs are a no-op).
- Cover both fresh installs (index present in ``_SYSTEM_SCHEMA``) and
  upgrades from v49 (migration creates the index).
"""

import duckdb
import pytest

from src.db import (
    SCHEMA_VERSION,
    _ensure_schema,
    _v49_to_v50_migrate,
    get_schema_version,
)


def test_fresh_install_has_unique_index_on_synthetic_name(tmp_path):
    """Fresh install reaches v50 with the UNIQUE index on synthetic_name."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    # DuckDB exposes indexes via duckdb_indexes(); is_unique flag distinguishes
    # UNIQUE indexes from non-unique ones.
    rows = conn.execute(
        "SELECT index_name, is_unique FROM duckdb_indexes() "
        "WHERE table_name = 'store_entities'"
    ).fetchall()
    index_names = {r[0]: r[1] for r in rows}
    assert "idx_store_entities_synthetic_name" in index_names, (
        f"synthetic_name UNIQUE index missing on fresh install: {index_names}"
    )
    assert index_names["idx_store_entities_synthetic_name"] is True, (
        "idx_store_entities_synthetic_name exists but is not UNIQUE"
    )
    conn.close()


def test_fresh_install_rejects_duplicate_synthetic_name(tmp_path):
    """After fresh install, inserting two rows with the same synthetic_name
    raises a DuckDB constraint error — the index is actually enforcing."""
    db_path = tmp_path / "enforced.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    conn.execute(
        "INSERT INTO store_entities "
        "(id, owner_user_id, owner_username, type, name, version, "
        " visibility_status, title, synthetic_name) "
        "VALUES ('e1', 'u1', 'alice', 'skill', 'a', '1', 'approved', 'A', 'shared-slug')"
    )
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO store_entities "
            "(id, owner_user_id, owner_username, type, name, version, "
            " visibility_status, title, synthetic_name) "
            "VALUES ('e2', 'u2', 'bob', 'skill', 'b', '1', 'approved', 'B', 'shared-slug')"
        )
    conn.close()


def test_v49_db_migrates_when_no_duplicates(tmp_path):
    """A v49-shaped DB with clean (non-duplicate) data climbs to v50, and the
    UNIQUE index is in place and enforcing."""
    db_path = tmp_path / "v49.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up a minimal v49-shape store_entities. No need to populate
    # every column — just enough that the duplicate scan and index
    # creation see realistic data.
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (49)")
    conn.execute(
        """CREATE TABLE store_entities (
            id              VARCHAR PRIMARY KEY,
            owner_user_id   VARCHAR NOT NULL,
            owner_username  VARCHAR NOT NULL,
            type            VARCHAR NOT NULL,
            name            VARCHAR NOT NULL,
            version         VARCHAR NOT NULL,
            visibility_status VARCHAR NOT NULL DEFAULT 'pending',
            title           VARCHAR NOT NULL,
            tagline         VARCHAR,
            synthetic_name  VARCHAR NOT NULL,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )"""
    )
    conn.execute(
        "INSERT INTO store_entities "
        "(id, owner_user_id, owner_username, type, name, version, "
        " visibility_status, title, synthetic_name) "
        "VALUES ('e1', 'u1', 'alice', 'skill', 'a', '1', 'approved', 'A', 'a-by-alice')"
    )
    conn.execute(
        "INSERT INTO store_entities "
        "(id, owner_user_id, owner_username, type, name, version, "
        " visibility_status, title, synthetic_name) "
        "VALUES ('e2', 'u2', 'bob', 'skill', 'a', '1', 'approved', 'A', 'a-by-bob')"
    )

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    # Index in place — duplicate insert is now rejected.
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO store_entities "
            "(id, owner_user_id, owner_username, type, name, version, "
            " visibility_status, title, synthetic_name) "
            "VALUES ('e3', 'u3', 'carol', 'skill', 'a', '1', 'approved', 'A', 'a-by-alice')"
        )
    conn.close()


def test_v49_to_v50_blocks_on_duplicates(tmp_path):
    """If a v49 DB has duplicate synthetic_name rows (admin hand-fix gone
    wrong, etc.), the migration raises ``RuntimeError`` listing the
    conflicting slugs instead of letting the index create error out
    mid-way with a less informative DuckDB message."""
    db_path = tmp_path / "v49dupes.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (49)")
    conn.execute(
        """CREATE TABLE store_entities (
            id              VARCHAR PRIMARY KEY,
            owner_user_id   VARCHAR NOT NULL,
            owner_username  VARCHAR NOT NULL,
            type            VARCHAR NOT NULL,
            name            VARCHAR NOT NULL,
            version         VARCHAR NOT NULL,
            visibility_status VARCHAR NOT NULL DEFAULT 'pending',
            title           VARCHAR NOT NULL,
            tagline         VARCHAR,
            synthetic_name  VARCHAR NOT NULL,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )"""
    )
    # Two rows colliding on `dup-slug` — wedge condition for v50.
    for eid, owner in [("e1", "alice"), ("e2", "bob")]:
        conn.execute(
            "INSERT INTO store_entities "
            "(id, owner_user_id, owner_username, type, name, version, "
            " visibility_status, title, synthetic_name) "
            "VALUES (?, ?, ?, 'skill', 'x', '1', 'approved', 'X', 'dup-slug')",
            [eid, owner, owner],
        )

    with pytest.raises(RuntimeError) as excinfo:
        _v49_to_v50_migrate(conn)

    msg = str(excinfo.value)
    assert "dup-slug" in msg, msg
    assert "store_entities" in msg, msg

    # Migration did not create the index when it bailed — verify the table
    # still allows what would be a duplicate (no enforcement yet).
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() "
        "WHERE table_name = 'store_entities' "
        "AND index_name = 'idx_store_entities_synthetic_name'"
    ).fetchall()
    assert rows == [], "index should NOT be created when duplicates present"
    conn.close()


def test_v49_to_v50_function_is_idempotent(tmp_path):
    """Re-running the migration on an already-v50 DB is a no-op.

    ``CREATE UNIQUE INDEX IF NOT EXISTS`` short-circuits; duplicate
    pre-check still passes (no dupes possible with the index already
    enforcing)."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _v49_to_v50_migrate(conn)
    _v49_to_v50_migrate(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()
