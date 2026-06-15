"""Schema gate for the v74 source-connections tables (spec 2026-06-12)."""

from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    return {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}


def test_v74_tables_exist(tmp_path):
    conn = _open_duckdb(str(tmp_path / "s.duckdb"))
    _ensure_schema(conn)
    assert _cols(conn, "source_connections") >= {
        "id",
        "name",
        "source_type",
        "config",
        "token_env",
        "is_default",
        "created_by",
        "created_at",
    }
    assert _cols(conn, "connection_secrets") >= {
        "connection_id",
        "ciphertext",
        "updated_at",
    }
    assert "connection_id" in _cols(conn, "table_registry")
