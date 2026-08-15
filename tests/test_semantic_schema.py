"""v116: semantic_models, semantic_sources, data_package_semantic_models."""

from src.db import SCHEMA_VERSION, _ensure_schema
from src.duckdb_conn import _open_duckdb


def _columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}


def test_semantic_tables_exist_on_fresh_install(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)

    assert _columns(conn, "semantic_models") >= {
        "id",
        "slug",
        "name",
        "description",
        "document",
        "document_json",
        "spec_version",
        "content_hash",
        "source",
        "source_ref",
        "status",
        "validation_errors",
        "validated_at",
        "created_at",
        "updated_at",
    }
    assert _columns(conn, "semantic_sources") >= {
        "id",
        "kind",
        "name",
        "adapter",
        "config",
        "enabled",
        "last_sync_at",
        "last_sync_status",
        "last_sync_error",
    }
    assert _columns(conn, "data_package_semantic_models") == {"package_id", "model_id"}
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] >= 116
    assert SCHEMA_VERSION >= 116
    conn.close()
