"""Reusable assertion helpers for the test suite."""

from pathlib import Path

import duckdb


def assert_api_error(response, expected_status: int, detail_contains: str = "") -> None:
    """Assert that an API response is an error with the expected status code.

    Args:
        response: httpx / TestClient response object.
        expected_status: Expected HTTP status code (e.g. 400, 404, 422).
        detail_contains: If non-empty, assert the response JSON 'detail'
            field contains this substring (case-sensitive).
    """
    assert response.status_code == expected_status, (
        f"Expected status {expected_status}, got {response.status_code}. "
        f"Response body: {response.text}"
    )
    if detail_contains:
        try:
            body = response.json()
        except Exception:
            body = {}
        detail = body.get("detail", "")
        if isinstance(detail, list):
            # FastAPI validation errors return a list of error dicts
            detail_str = str(detail)
        else:
            detail_str = str(detail)
        assert detail_contains in detail_str, (
            f"Expected detail to contain {detail_contains!r}, got: {detail_str!r}"
        )


def assert_parquet_readable(path: str | Path, min_rows: int = 0) -> None:
    """Assert that a parquet file is readable and contains at least min_rows rows.

    Args:
        path: Filesystem path to the parquet file.
        min_rows: Minimum number of rows expected (default 0 = non-empty optional).
    """
    path = str(path)
    conn = duckdb.connect()
    try:
        result = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()
        assert result is not None, f"Could not read parquet file: {path}"
        row_count = result[0]
        assert row_count >= min_rows, (
            f"Parquet file {path!r} has {row_count} rows, expected >= {min_rows}"
        )
    finally:
        conn.close()


def assert_duckdb_table_exists(db_path: str | Path, table_name: str) -> None:
    """Assert that a table (or view) with the given name exists in a DuckDB file.

    Args:
        db_path: Filesystem path to the DuckDB database file.
        table_name: Name of the table or view to check.
    """
    db_path = str(db_path)
    conn = duckdb.connect(db_path, read_only=True)
    try:
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()
        assert result is not None and result[0] > 0, (
            f"Table or view {table_name!r} does not exist in DuckDB database {db_path!r}"
        )
    finally:
        conn.close()
