"""DuckDB connection helper pins session timezone to UTC.

See `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.
"""

from datetime import datetime, timezone

from src.db import _open_duckdb


def test_open_duckdb_pins_session_to_utc():
    conn = _open_duckdb(":memory:")
    tz = conn.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert tz == "UTC"


def test_open_duckdb_aware_utc_roundtrip_no_shift():
    conn = _open_duckdb(":memory:")
    conn.execute("CREATE TABLE t (ts TIMESTAMP)")
    aware = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    conn.execute("INSERT INTO t VALUES (?)", [aware])
    (got,) = conn.execute("SELECT ts FROM t").fetchone()
    assert got.tzinfo is None
    assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 5, 26, 12, 0)


def test_open_duckdb_read_only_still_utc(tmp_path):
    db = tmp_path / "x.duckdb"
    rw = _open_duckdb(str(db))
    rw.execute("CREATE TABLE t (ts TIMESTAMP)")
    rw.close()
    ro = _open_duckdb(str(db), read_only=True)
    assert ro.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"
