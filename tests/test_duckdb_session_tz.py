"""DuckDB connection helper pins session timezone to UTC.

See `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

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


def test_open_duckdb_cursor_inherits_utc():
    """Cursors created via conn.cursor() must also report UTC.

    DuckDB session-level `SET TimeZone` does NOT propagate to cursors —
    that's the trap. The helper uses `SET GLOBAL` so every repository
    that calls `get_system_db().cursor()` gets the pin too.
    """
    conn = _open_duckdb(":memory:")
    cur = conn.cursor()
    assert cur.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"


def test_open_duckdb_cursor_no_shift_on_aware_utc_write():
    conn = _open_duckdb(":memory:")
    conn.execute("CREATE TABLE t (ts TIMESTAMP)")
    cur = conn.cursor()
    aware = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    cur.execute("INSERT INTO t VALUES (?)", [aware])
    (got,) = cur.execute("SELECT ts FROM t").fetchone()
    assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 5, 26, 12, 0)


def test_open_duckdb_read_only_still_utc(tmp_path):
    db = tmp_path / "x.duckdb"
    rw = _open_duckdb(str(db))
    rw.execute("CREATE TABLE t (ts TIMESTAMP)")
    rw.close()
    ro = _open_duckdb(str(db), read_only=True)
    assert ro.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"


def test_no_bare_duckdb_connect_in_production_code():
    """Regression guard: every duckdb.connect call site funnels through
    `_open_duckdb` (or runs an inline `SET GLOBAL TimeZone='UTC'`).

    DuckDB's TIMESTAMP type shifts tz-aware writes into the session zone
    before stripping tzinfo. A single bypass on a non-UTC host stores
    local-clock-naive values, which the FastAPI serializer then labels
    as UTC — so frontend timestamps render with the host's UTC offset.
    See `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.
    """
    repo_root = Path(__file__).resolve().parents[1]
    # Scan production trees + the PG-contract test directory. tests/db_pg/
    # is high-risk: it builds DuckDB fixtures, runs production migrators
    # against them, then asserts on rows written/read across both sides.
    # A bare duckdb.connect() on the test side stores host-local-naive
    # timestamps; the production-side _open_duckdb read sees them as
    # UTC-naive; on a non-UTC dev host (CET = UTC+1/+2) timestamps drift
    # by the host's offset and tests get flaky. Wider tests/ tree is NOT
    # included — most non-db_pg fixtures use :memory: and don't write
    # tz-sensitive values; pinning them globally would cost ~20+ allow-
    # list entries with little payoff. See the devil's-advocate review
    # of the merge resolution for the cost analysis.
    trees = ["src", "app", "connectors", "cli", "scripts", "services", "tests/db_pg"]

    # Allow-list: substring matches anywhere on the offending line that
    # mean "this is OK / already routed through the helper / standalone
    # script that inlines a SET on the next line".
    allow_substrings = (
        "_open_duckdb",
        # Docstring / comment lines that mention the API:
        "All `duckdb.connect(...)`",
        "`get_analytics_db()` opened a fresh `duckdb.connect()`",
        "``duckdb.connect(path, read_only=True)`` from a fresh handle is",
        "a separate ``duckdb.connect()`` to the same path in the same",
        # Standalone scripts that inline the SET right after open:
        "scripts/generate_sample_data.py",
        "scripts/build_demo_extract.py",
        "connectors/jira/scripts/sync_jira.sh",
        "connectors/jira/scripts/consistency_check.py",
        "scripts/smoke-test-materialized-bq.sh",
        # BigQuery extractor opens connections via duckdb.connect directly
        # and inlines `_pin_session_utc(...)` on each one. Routing through
        # `_open_duckdb` would bypass the heavy test-suite patching of
        # `connectors.bigquery.extractor.duckdb`. See the inline pin
        # helper in connectors/bigquery/extractor.py.
        "connectors/bigquery/extractor.py",
        # Developer POC script — standalone, not part of the production
        # app path, not imported by any FastAPI handler or CLI command.
        "scripts/dev/poc_mcp_e2e.py",
        # tests/db_pg/ existing fixtures predate the _open_duckdb rollout
        # and intentionally use bare connect for fixture isolation. Each
        # is listed by file path so any NEW bare connect in tests/db_pg/
        # gets caught.
        "tests/db_pg/test_audit_contract.py",
        "tests/db_pg/test_data_migration.py",
        "tests/db_pg/test_data_packages_contract.py",
        "tests/db_pg/test_db_state_e2e.py",
        "tests/db_pg/test_db_state_migrator.py",
        "tests/db_pg/test_memory_domain_suggestions_contract.py",
        "tests/db_pg/test_memory_domains_contract.py",
        "tests/db_pg/test_migrate_users_idempotent.py",
        "tests/db_pg/test_ported_methods_contract.py",
        "tests/db_pg/test_rbac_contract.py",
        "tests/db_pg/test_recipes_contract.py",
        "tests/db_pg/test_schema_parity.py",
        "tests/db_pg/test_store_contract.py",
        "tests/db_pg/test_user_stack_subscriptions_contract.py",
        "tests/db_pg/test_users_contract.py",
    )

    pat = re.compile(r"duckdb\.connect\(")
    offenders: list[str] = []
    for tree in trees:
        root = repo_root / tree
        if not root.exists():
            continue
        # Only audit Python sources. Markdown skill docs and shell
        # heredocs aren't production code paths and would also be
        # impossible to grep-guard reliably.
        result = subprocess.run(
            [
                "grep", "-rn",
                "--include=*.py",
                "duckdb.connect(", str(root),
            ],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if not pat.search(line):
                continue
            # Skip if any allow-list substring appears anywhere in the
            # line (path + content).
            if any(s in line for s in allow_substrings):
                continue
            # Skip lines that are obviously inside a string literal /
            # docstring / comment.
            content = line.split(":", 2)[-1] if ":" in line else line
            if content.lstrip().startswith(("#", '"', "'", "*")):
                continue
            # Skip the helper module itself (its single direct call).
            if "src/duckdb_conn.py" in line:
                continue
            offenders.append(line)

    assert not offenders, (
        "Found `duckdb.connect(...)` call sites that bypass `_open_duckdb`. "
        "Route them through `src.duckdb_conn._open_duckdb` so the session "
        "timezone is pinned to UTC, OR add an inline `SET GLOBAL "
        "TimeZone='UTC'` immediately after open and add the file to the "
        "allow_substrings in this test. Offenders:\n  "
        + "\n  ".join(offenders)
    )
