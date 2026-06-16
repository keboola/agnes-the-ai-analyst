"""Tests for Collections schema (v77): file_corpora, corpus_files, corpus_chunks.

Written TDD-first: this test must FAIL before the migration is implemented,
then PASS after _v76_to_v77 and SCHEMA_VERSION = 77 land in src/db.py.
"""

from __future__ import annotations

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


COLLECTIONS_TABLES = ("file_corpora", "corpus_files", "corpus_chunks")


def _table_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }


def test_schema_version_is_77():
    """SCHEMA_VERSION constant must be 77 after the collections migration lands."""
    assert SCHEMA_VERSION >= 77


def test_collections_tables_exist_on_fresh_db(tmp_path):
    """Fresh install creates all three collections tables and reaches v77."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    tables = _table_names(conn)
    for t in COLLECTIONS_TABLES:
        assert t in tables, f"Expected table '{t}' missing from fresh DB; found: {tables}"

    conn.close()


def test_file_corpora_columns(tmp_path):
    """file_corpora has the expected column set on fresh install."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'file_corpora'"
        ).fetchall()
    }
    expected = {"id", "slug", "name", "description", "created_by", "created_at", "updated_at", "deleted_at"}
    assert expected <= cols, f"Missing columns in file_corpora: {expected - cols}"
    conn.close()


def test_corpus_files_columns(tmp_path):
    """corpus_files has the expected column set on fresh install."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'corpus_files'"
        ).fetchall()
    }
    expected = {
        "id",
        "corpus_id",
        "filename",
        "sha256",
        "file_type",
        "size_bytes",
        "storage_path",
        "processing_status",
        "processing_detail",
        "created_at",
        "updated_at",
    }
    assert expected <= cols, f"Missing columns in corpus_files: {expected - cols}"
    conn.close()


def test_corpus_chunks_columns(tmp_path):
    """corpus_chunks has the expected column set including embedding."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'corpus_chunks'"
        ).fetchall()
    }
    expected = {
        "id",
        "corpus_id",
        "file_id",
        "ordinal",
        "text",
        "embedding",
        "section_path",
        "page",
        "bbox",
        "metadata",
        "created_at",
    }
    assert expected <= cols, f"Missing columns in corpus_chunks: {expected - cols}"
    conn.close()


def test_v79_to_v80_upgrade(tmp_path):
    """A DB at v79 upgrades cleanly to v80 (adds the three collections tables)."""
    from src.db import _v79_to_v80

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Minimal v79 shape: just schema_version
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (79)")

    _v79_to_v80(conn)

    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 80

    tables = _table_names(conn)
    for t in COLLECTIONS_TABLES:
        assert t in tables, f"'{t}' missing after _v79_to_v80; tables: {tables}"

    conn.close()
