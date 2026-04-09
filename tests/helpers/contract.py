"""Shared validator for the extract.duckdb contract."""
import duckdb
from pathlib import Path

def validate_extract_contract(db_path: str) -> None:
    """Verify an extract.duckdb conforms to the contract. Raises AssertionError if not."""
    path = Path(db_path)
    assert path.exists(), f"extract.duckdb not found at {db_path}"

    conn = duckdb.connect(str(path), read_only=True)
    try:
        # _meta table must exist with correct schema
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='_meta' ORDER BY ordinal_position"
        ).fetchall()
        col_names = [c[0] for c in cols]
        assert col_names == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"], \
            f"_meta schema mismatch: {col_names}"

        # Every local table in _meta must have a view/table
        local_tables = conn.execute("SELECT table_name FROM _meta WHERE query_mode = 'local'").fetchall()
        for (name,) in local_tables:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?", [name]
            ).fetchall()
            assert len(tables) > 0, f"Local table '{name}' in _meta but no view/table exists"

        # If _remote_attach exists, validate schema
        ra_exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='_remote_attach'"
        ).fetchone()[0]
        if ra_exists:
            ra_cols = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='_remote_attach' ORDER BY ordinal_position"
            ).fetchall()
            ra_col_names = [c[0] for c in ra_cols]
            assert ra_col_names == ["alias", "extension", "url", "token_env"], \
                f"_remote_attach schema mismatch: {ra_col_names}"
    finally:
        conn.close()
