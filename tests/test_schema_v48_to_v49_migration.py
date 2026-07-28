"""v48 → v49 migration: phase-1 Flea refactor.

Adds three columns to ``store_entities``:

- ``title`` (VARCHAR NOT NULL) — humanized display name, backfilled via
  ``humanize_name(strip_archive_suffix(name))`` on existing rows.
- ``tagline`` (VARCHAR, nullable) — optional 200-char short description.
- ``synthetic_name`` (VARCHAR NOT NULL) — deterministic
  ``<name>-by-<owner_username>`` string, backfilled via the concat formula.

The migration is implemented as a Python function (``_v48_to_v49_migrate``)
because the humanize backfill has no clean SQL equivalent.
"""

import duckdb
import pytest

from src.db import (
    SCHEMA_VERSION,
    _ensure_schema,
    _v48_to_v49_migrate,
    get_schema_version,
)


def test_fresh_install_has_v49_columns(tmp_path):
    """Fresh install reaches v49 with the three new columns present."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    assert {"title", "tagline", "synthetic_name"} <= cols, (
        f"v49 columns missing on store_entities: {cols}"
    )
    conn.close()


def test_v48_db_migrates_and_backfills(tmp_path):
    """A pre-existing v48 DB with seeded store_entities rows climbs to v49
    with title + synthetic_name backfilled and tagline left NULL."""
    db_path = tmp_path / "v48.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up a minimal v48-shape store_entities (no title/tagline/synthetic).
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (48)")
    conn.execute(
        """CREATE TABLE store_entities (
            id              VARCHAR PRIMARY KEY,
            owner_user_id   VARCHAR NOT NULL,
            owner_username  VARCHAR NOT NULL,
            type            VARCHAR NOT NULL,
            name            VARCHAR NOT NULL,
            description     TEXT,
            category        VARCHAR,
            version         VARCHAR NOT NULL,
            photo_path      VARCHAR,
            video_url       VARCHAR,
            doc_paths       JSON,
            file_size       BIGINT,
            install_count   BIGINT NOT NULL DEFAULT 0,
            visibility_status VARCHAR NOT NULL DEFAULT 'pending',
            archived_at     TIMESTAMP,
            archived_by     VARCHAR,
            version_no      INTEGER NOT NULL DEFAULT 1,
            version_history JSON DEFAULT '[]',
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )"""
    )
    # Plain skill, archived skill, MCP acronym, multi-word with v-suffix.
    conn.execute(
        "INSERT INTO store_entities (id, owner_user_id, owner_username, type, name, version) "
        "VALUES ('e1', 'u1', 'alice', 'skill', 'code-review', 'v1')"
    )
    conn.execute(
        "INSERT INTO store_entities (id, owner_user_id, owner_username, type, name, version) "
        "VALUES ('e2', 'u2', 'bob', 'agent', 'mcp-builder', 'v1')"
    )
    conn.execute(
        "INSERT INTO store_entities (id, owner_user_id, owner_username, type, name, version, visibility_status) "
        "VALUES ('e3', 'u1', 'alice', 'skill', 'oldname__archived__1700000000', 'v1', 'archived')"
    )
    conn.execute(
        "INSERT INTO store_entities (id, owner_user_id, owner_username, type, name, version) "
        "VALUES ('e4', 'u3', 'c-bsolinovapauerova', 'skill', 'html-deck-creator', 'v1')"
    )

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    rows = {
        r[0]: r
        for r in conn.execute(
            "SELECT id, name, title, tagline, synthetic_name FROM store_entities"
        ).fetchall()
    }

    # title: humanize_name(strip_archive_suffix(name))
    assert rows["e1"][2] == "Code Review"
    assert rows["e2"][2] == "MCP Builder"
    # Archived row: strip __archived__<epoch> before humanizing.
    assert rows["e3"][2] == "Oldname"
    assert rows["e4"][2] == "HTML Deck Creator"

    # tagline stays NULL — no backfill source.
    for eid in ("e1", "e2", "e3", "e4"):
        assert rows[eid][3] is None, f"{eid} tagline should be NULL"

    # synthetic_name uses the actually-stored name (incl. archive suffix).
    assert rows["e1"][4] == "code-review-by-alice"
    assert rows["e2"][4] == "mcp-builder-by-bob"
    assert rows["e3"][4] == "oldname__archived__1700000000-by-alice"
    assert rows["e4"][4] == "html-deck-creator-by-c-bsolinovapauerova"

    conn.close()


def test_v48_to_v49_function_is_idempotent(tmp_path):
    """Calling ``_v48_to_v49_migrate`` twice is a no-op the second time."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    # Re-run directly on a clean v49 DB — ADD COLUMN IF NOT EXISTS +
    # SET NOT NULL on an already-NOT-NULL column are both idempotent.
    _v48_to_v49_migrate(conn)
    _v48_to_v49_migrate(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_not_null_constraint_after_migration(tmp_path):
    """title and synthetic_name must be NOT NULL after migration."""
    db_path = tmp_path / "constraints.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    # information_schema reports NOT NULL via is_nullable = 'NO'
    nullable = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'store_entities' "
            "AND column_name IN ('title', 'tagline', 'synthetic_name')"
        ).fetchall()
    }
    assert nullable.get("title") == "NO", f"title nullable: {nullable}"
    assert nullable.get("synthetic_name") == "NO", f"synthetic_name nullable: {nullable}"
    assert nullable.get("tagline") == "YES", f"tagline must be nullable: {nullable}"
    conn.close()
