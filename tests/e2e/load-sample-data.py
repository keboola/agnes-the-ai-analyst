"""Seed the E2E analytics DuckDB with SQL fixtures.

The container's start.sh runs this before uvicorn so the chat tests
have something to inspect (`agnes catalog`, `agnes schema`, `agnes
query`). The orchestrator's normal `extract.duckdb` ATTACH flow does
*not* fire here — we write directly into the master analytics DB so
the fixtures are unconditionally visible regardless of which connector
the test instance is configured for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

DATA_DIR = Path("/data")
ANALYTICS_DB = DATA_DIR / "analytics" / "server.duckdb"
SAMPLE_DIR = Path("/app/tests/e2e/sample-data")


def main() -> int:
    ANALYTICS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(ANALYTICS_DB))
    try:
        sql_files = sorted(SAMPLE_DIR.glob("*.sql"))
        if not sql_files:
            print(f"[load-sample-data] no .sql files under {SAMPLE_DIR}", file=sys.stderr)
            return 1
        for sql_path in sql_files:
            print(f"[load-sample-data] applying {sql_path.name}")
            conn.execute(sql_path.read_text())
        tables = conn.execute("SHOW TABLES").fetchall()
        print(f"[load-sample-data] tables now present: {[t[0] for t in tables]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
