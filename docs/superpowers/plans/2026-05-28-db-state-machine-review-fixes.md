# DB State-Machine Review Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address every finding from the PR #455 code review — 9 BLOCKERS, 13 HIGH, ~19 MEDIUM, 9 LOW, plus the testing-gap and residual-risk lists. After this PR no green-CI behavior should silently hide data loss, corruption, or destructive operator surprises.

**Architecture:** Each finding maps to a single code change with a matching regression test. Findings are grouped into 9 phases by subsystem so each phase ships a coherent slice (migrator hardening → API safety → applier robustness → state recovery → UI → CLI → tests → security → docs). No new architectural primitives are introduced — the state-machine + host-applier topology shipped in PR #455 is the right shape; the fixes correct its execution.

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy 2.0 + psycopg 3 + DuckDB ≥1.5; pytest + pixeltable-pgserver for tests; bash + jq + python3 inline for the host applier; Terraform 1.5+ for infra; alembic 1.18.

---

## Branch & worktree

- **Worktree:** `/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.claude/worktrees/zs+db-state-machine`
- **Branch:** `zs/db-state-machine` (HEAD of PR #455)
- **Base:** `main`

All commits land on `zs/db-state-machine` directly. No sub-branches. Each task ends with a commit; CI tags (`keboola-deploy-2026-05-28-db-state-machine-v12+`) only at phase boundaries when an agnes-dev redeploy is warranted.

---

## File map

| File | Touched by tasks |
| --- | --- |
| `scripts/migrate_duckdb_to_pg/__init__.py` | 1.1, 1.2, 1.7 |
| `scripts/migrate_duckdb_to_pg/tasks.py` | 1.2, 1.4 |
| `scripts/db_state_migrator.py` | 1.3, 1.5, 1.6, 2.3, 4.1 |
| `app/api/db_state.py` | 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 4.3 |
| `src/db_state_machine.py` | 2.1 |
| `scripts/ops/agnes-state-applier.sh` | 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.2, 8.1 |
| `scripts/ops/agnes-state-applier.service` | 8.1 |
| `app/web/static/js/admin/db_state.js` | 5.1, 5.2, 5.3 |
| `cli/commands/db.py` | 6.1 |
| `tests/db_pg/test_db_state_migrator.py` | 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 7.1, 7.2, 7.3 |
| `tests/test_api_db_state.py` | 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 4.3, 7.8, 7.9, 7.10, 7.11 |
| `tests/test_db_state_machine.py` | 2.1 |
| `tests/test_state_applier_host_script.sh` | 3.1, 3.2, 3.3, 3.5, 7.6 |
| `tests/db_pg/test_data_migration.py` | 1.2, 1.4, 1.7, 7.1 |
| `tests/test_api_design_rules.py` | 7.12 |
| `tests/db_pg/conftest.py` | 7.13 |
| `tests/test_cli_db.py` | 6.1 |
| `tests/db_pg/test_db_state_e2e.py` | 7.2, 7.3, 7.4, 7.5 |
| `docs/postgres-cutover-runbook.md` | 9.1 |
| `CHANGELOG.md` | 9.2 |

---

## Phase 1 — Migrator hardening (data integrity foundation)

Order: any per-table failure now becomes a hard fail; missing target tables stop the migration; backups exist before destructive copy. Without these, every downstream fix is built on shifting ground.

### Task 1.1: Migrator CLI exits non-zero on per-table failure (BLOCKER, comment 2 #1)

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/__init__.py:154-160` (the `run_all` exit predicate at the end of the CLI wrapper)
- Test: `tests/db_pg/test_data_migration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/db_pg/test_data_migration.py`:

```python
def test_run_all_reports_per_table_error(tmp_path, pg_with_schema):
    """If a per-table copy raises, ``run_all`` must include the failure
    in its return list with an ``error`` key — and the CLI wrapper must
    exit non-zero on that signal.

    Regression for the cvrysanek review item: the predicate
    ``all(r.get("checksum_match", True) ...)`` returned True for error
    reports (default), so the migrator exited 0 even on hard failure,
    the applier read MIG_RC=0, flipped the backend, and the app booted
    against a partially-populated PG.
    """
    import duckdb
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_all

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    # Drop a PG table to force per-table failure on copy.
    with pg_with_schema.connect() as conn:
        from sqlalchemy import text as sa_text
        conn.execute(sa_text("DROP TABLE IF EXISTS users CASCADE"))
        conn.commit()

    reports = run_all(duck, pg_with_schema, validate=False)
    # At least one report must carry an error.
    assert any("error" in r for r in reports), reports
    duck.close()
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_run_all_reports_per_table_error -v
```
Expected: FAIL (currently swallowed, then crash because no exit happens).

- [ ] **Step 3: Fix the CLI exit predicate**

In `scripts/migrate_duckdb_to_pg/__init__.py`, replace the existing `if __name__ == "__main__"` block (or equivalent CLI exit logic — search for `checksum_match` in the file). Change:

```python
sys.exit(0 if all(r.get("checksum_match", True) for r in reports) else 1)
```

to:

```python
# Per-task errors land as {"table": ..., "error": ...} without a
# checksum_match key; the default-True on .get() previously masked
# them. Treat the explicit error key as the authoritative failure
# signal. Both predicates must hold for exit 0.
sys.exit(
    0 if all("error" not in r and r.get("checksum_match", True) for r in reports)
    else 1
)
```

- [ ] **Step 4: Run the test to verify it passes**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_run_all_reports_per_table_error -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/__init__.py tests/db_pg/test_data_migration.py
git commit -m "fix(migrator): exit non-zero when per-table copy fails (review #1)"
```

---

### Task 1.2: Probe-then-raise on DuckDB columns missing in PG (HIGH, comment 2 #2)

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/tasks.py` — add probe in `GenericCopyTask.run` (around the existing column-intersection logic)
- Test: `tests/db_pg/test_data_migration.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_run_raises_on_duckdb_column_missing_in_pg_with_data(tmp_path, pg_with_schema):
    """If DuckDB has data in a column the PG schema lacks, the copy
    task MUST raise. Silent drop = silent data loss. Empty columns
    pass through with a warning (covered by a separate test).
    """
    import duckdb
    import pytest
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    # Add an extra column DuckDB-side that PG doesn't have.
    duck.execute("ALTER TABLE table_registry ADD COLUMN extra_field VARCHAR")
    duck.execute(
        "INSERT INTO table_registry (id, name, source_type, extra_field) "
        "VALUES ('t1', 'tbl', 'duckdb', 'has-data')"
    )
    task = next(t for t in TASKS if t.target_table == "table_registry")
    with pytest.raises(RuntimeError, match="extra_field.*data will be lost"):
        run_task(task, duck, pg_with_schema)
    duck.close()
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_run_raises_on_duckdb_column_missing_in_pg_with_data -v
```
Expected: FAIL (column is dropped silently or raises a different error).

- [ ] **Step 3: Add probe in `tasks.py`**

In `scripts/migrate_duckdb_to_pg/tasks.py`, find the `GenericCopyTask.run` method. After the existing column resolution (where `columns = _resolved_columns(...)` is computed), before the INSERT loop, add:

```python
# Probe for DuckDB-only columns that hold data. Silent column drop is
# data loss; force the operator to either land the alembic migration
# (so PG has the column) or explicitly accept the loss by removing
# the column from the DuckDB source first.
import src.models as _m  # noqa: F401 — ensure models registered
from src.db_pg import Base as _Base
_pg_table = _Base.metadata.tables.get(self.target_table)
_pg_cols = {c.name for c in _pg_table.columns} if _pg_table is not None else set()
_duck_only = [c for c in columns if c not in _pg_cols]
for _col in _duck_only:
    try:
        _non_null = duck_conn.execute(
            f'SELECT COUNT(*) FROM "{self.table_name}" WHERE "{_col}" IS NOT NULL'
        ).fetchone()[0]
    except Exception:
        _non_null = 0
    if _non_null > 0:
        raise RuntimeError(
            f"Column '{self.table_name}.{_col}' exists in DuckDB with "
            f"{_non_null} non-null row(s) but is missing from the PG "
            f"schema — data will be lost. Land the alembic migration "
            f"that adds the column, or drop the column from DuckDB "
            f"before re-running."
        )
    log.warning(
        "DuckDB-only column %s.%s is empty; skipping from PG INSERT",
        self.table_name, _col,
    )
# Restrict `columns` to the PG-side set so the INSERT is well-formed.
columns = [c for c in columns if c in _pg_cols]
```

- [ ] **Step 4: Run the test to verify it passes**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_run_raises_on_duckdb_column_missing_in_pg_with_data -v
```
Expected: PASS.

- [ ] **Step 5: Add the companion "empty column drops cleanly" test**

```python
def test_run_warns_but_continues_on_empty_duckdb_only_column(tmp_path, pg_with_schema, caplog):
    import duckdb
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    duck.execute("ALTER TABLE table_registry ADD COLUMN unused_field VARCHAR")
    # No data inserted into unused_field.
    task = next(t for t in TASKS if t.target_table == "table_registry")
    with caplog.at_level("WARNING"):
        run_task(task, duck, pg_with_schema)  # must not raise
    assert any("unused_field" in rec.message for rec in caplog.records)
    duck.close()
```

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_run_warns_but_continues_on_empty_duckdb_only_column -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/tasks.py tests/db_pg/test_data_migration.py
git commit -m "fix(migrator): raise on non-empty DuckDB-only columns instead of silent drop (review #2)"
```

---

### Task 1.3: Verify hard-fails on missing target tables (BLOCKER B3)

**Files:**
- Modify: `scripts/db_state_migrator.py` — the `verify_row_counts` and `verify_pg_row_counts` functions
- Test: `tests/db_pg/test_db_state_migrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/db_pg/test_db_state_migrator.py`:

```python
def test_verify_raises_on_missing_target_table(tmp_path, pg_engine):
    """If a target table is missing (e.g. partial alembic apply),
    verify_row_counts must raise — not return ``tgt_count = 0`` and
    silently match an empty source. Hides typos AND partial schemas.
    """
    import duckdb
    import pytest
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_row_counts

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    duck.close()

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE users CASCADE"))

    with pytest.raises(RuntimeError, match="target table.*missing"):
        verify_row_counts(duck_path, str(pg_engine.url))
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_verify_raises_on_missing_target_table -v
```
Expected: FAIL — function silently substitutes 0.

- [ ] **Step 3: Replace the silent catch in both verify functions**

In `scripts/db_state_migrator.py`, find `verify_row_counts` and replace its existing `try/except sa.exc.ProgrammingError: tgt_count = 0` with:

```python
try:
    with pg_engine.connect() as pg_conn:
        tgt_count = pg_conn.execute(
            sa.text(f'SELECT COUNT(*) FROM "{table}"')
        ).fetchone()[0]
except sa.exc.ProgrammingError as exc:
    # The previous behaviour was to swallow this and set tgt_count=0,
    # which collapsed to a 0=0 "match" for the (common) case where
    # DuckDB also has 0 rows in the table. That hid typos and
    # partial-alembic-apply states. Surface explicitly.
    raise RuntimeError(
        f"verify_row_counts: target table '{table}' is missing from PG "
        f"(or the connection lacks SELECT on it). Migration cannot "
        f"complete safely. Underlying error: {exc!s}"
    ) from exc
```

Do the same in `verify_pg_row_counts` (which has the same pattern for both source and target sides).

- [ ] **Step 4: Run the test to verify it passes**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_verify_raises_on_missing_target_table -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/db_state_migrator.py tests/db_pg/test_db_state_migrator.py
git commit -m "fix(migrator): verify raises on missing target table (B3)"
```

---

### Task 1.4: Stop fabricating audit timestamps (MEDIUM data-integrity)

**Files:**
- Modify: `scripts/migrate_duckdb_to_pg/tasks.py` — `_substitute_default` + call sites
- Test: `tests/db_pg/test_data_migration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_audit_log_timestamp_preserved_when_present(tmp_path, pg_with_schema):
    """audit_log rows with explicit timestamps must keep them. The
    previous _substitute_default replaced NULLs AND non-NULL bound
    values with datetime.now() because the helper looked at the
    column's server_default + nullable status, not whether the row
    actually carried a value. Audit trail integrity ⇒ never rewrite.
    """
    import datetime as _dt
    import duckdb
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_task, TASKS

    duck = duckdb.connect(str(tmp_path / "src.duckdb"))
    _ensure_schema(duck)
    original = _dt.datetime(2025, 1, 15, 9, 30, 0, tzinfo=_dt.timezone.utc)
    duck.execute(
        "INSERT INTO audit_log (id, timestamp, action) VALUES (?, ?, ?)",
        ["a1", original, "test.event"],
    )

    task = next(t for t in TASKS if t.target_table == "audit_log")
    run_task(task, duck, pg_with_schema)

    with pg_with_schema.connect() as conn:
        row = conn.execute(sa_text("SELECT timestamp FROM audit_log WHERE id='a1'")).first()
    assert row.timestamp == original
    duck.close()
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_audit_log_timestamp_preserved_when_present -v
```
Expected: FAIL — timestamp gets replaced by `datetime.now()`.

- [ ] **Step 3: Restrict `_substitute_default` to only fire when value is None**

In `scripts/migrate_duckdb_to_pg/tasks.py`, find `_substitute_default`. Replace the existing body with:

```python
def _substitute_default(value, server_default, *, column_name=""):
    """Materialise a server_default ONLY when the row's value is None.

    Honour the existing value in every other case — never overwrite an
    operator-supplied timestamp or any other typed value. Returning
    ``value`` unchanged for non-None inputs is the audit-integrity
    contract; the only legitimate use is to fill genuine NULLs in
    NOT-NULL columns where the source carries no value.

    Returns None when no usable default is found (caller decides what
    to do — typically let the INSERT raise NotNullViolation so the
    operator sees the column needs attention).
    """
    if value is not None:
        return value
    if server_default is None:
        return None
    sd = getattr(server_default, "arg", server_default)
    sd_text = str(sd).upper()
    if "CURRENT_TIMESTAMP" in sd_text or "NOW()" in sd_text:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)
    if "CURRENT_DATE" in sd_text:
        from datetime import date
        return date.today()
    return None
```

Update every call site (search file for `_substitute_default(`) to pass the column name (for the log message) and to no longer always materialise.

- [ ] **Step 4: Run the test to verify it passes**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_audit_log_timestamp_preserved_when_present -v
```
Expected: PASS.

- [ ] **Step 5: Re-run existing migration tests to confirm nothing regressed**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_duckdb_to_pg/tasks.py tests/db_pg/test_data_migration.py
git commit -m "fix(migrator): preserve operator-supplied timestamps; substitute defaults only on NULL"
```

---

### Task 1.5: backup_duckdb runs BEFORE data_copy (MEDIUM data-integrity)

**Files:**
- Modify: `scripts/db_state_migrator.py` — the main() function dispatch
- Test: `tests/db_pg/test_db_state_migrator.py`

- [ ] **Step 1: Write the failing test**

```python
def test_main_writes_duckdb_backup_before_copy(tmp_path, pg_engine, monkeypatch):
    """The DuckDB backup must exist on disk BEFORE the data_copy step
    overwrites any PG state. The previous flow copied first, verified,
    then backed up — so a crash between verify and flip left the
    operator with neither a backup nor a flipped state."""
    import json
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    # Force a failure AFTER the backup step by patching copy_duckdb_to_pg
    # to raise. If the backup was written before the failure, the file
    # exists on disk.
    def boom(*a, **kw):
        raise RuntimeError("simulated mid-copy crash")
    monkeypatch.setattr("scripts.db_state_migrator.copy_duckdb_to_pg", boom)

    rc = main(
        job_id="job-backup-order",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 1
    backups = list(backups_dir.glob("duckdb-pre-sidecar-*.duckdb.gz"))
    assert backups, "backup file should exist even though copy failed"
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_main_writes_duckdb_backup_before_copy -v
```
Expected: FAIL — backup runs after verify.

- [ ] **Step 3: Move the backup_duckdb call earlier in main()**

In `scripts/db_state_migrator.py`'s `main()`, find the `to == "side_car"` branch. Move the `backup_duckdb(duckdb_path, backups_dir)` call from its current location (after verify) to BEFORE the `data_copy` step. The block should now read:

```python
if source_backend == "duckdb":
    # Backup the DuckDB file BEFORE any destructive operation on the
    # target. If anything in the rest of the pipeline crashes the
    # operator still has the source snapshot they need to retry.
    if to == "side_car":
        writer.update_step("backup", progress_pct=15)
        backup_duckdb(duckdb_path, backups_dir)

    writer.update_step("data_copy", progress_pct=40)
    copy_summary = copy_duckdb_to_pg(duckdb_path, target_url)

    writer.update_step("verify", progress_pct=80)
    diffs = verify_row_counts(duckdb_path, target_url)
```

Remove the trailing `backup_duckdb` call further down (the one between verify and flip_backend).

- [ ] **Step 4: Run the test to verify it passes**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_main_writes_duckdb_backup_before_copy -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/db_state_migrator.py tests/db_pg/test_db_state_migrator.py
git commit -m "fix(migrator): backup DuckDB before data_copy (recovery point precedes destructive write)"
```

---

### Task 1.6: PG engine connect + statement timeouts (MEDIUM reliability)

**Files:**
- Modify: `scripts/db_state_migrator.py` — every `sa.create_engine(...)` call site
- Test: `tests/db_pg/test_db_state_migrator.py`

- [ ] **Step 1: Add helper for guarded engine + replace all create_engine calls**

In `scripts/db_state_migrator.py`, near the top of the module, add:

```python
def _bounded_engine(url: str):
    """Return a SQLAlchemy engine with conservative network + query
    timeouts. The migrator runs unattended via the host applier; an
    unreachable target (DNS, firewall, dead SQL Proxy) must NOT hang
    indefinitely. ``connect_timeout`` covers the initial handshake;
    ``statement_timeout`` (PG-side) caps any single query at 5 min,
    enough for the heaviest tables in the current schema but short
    enough to surface a runaway as a clear error.
    """
    import sqlalchemy as sa
    return sa.create_engine(
        url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=300000",  # 5 min in ms
        },
        pool_pre_ping=True,
        pool_recycle=1800,
    )
```

Search for every `sa.create_engine(` in the same file and replace with `_bounded_engine(`. Confirm the same arg-list is acceptable (single positional URL).

- [ ] **Step 2: Add a test for unreachable-target timeout**

```python
def test_bounded_engine_fails_fast_on_unreachable(tmp_path):
    """A bogus host must error within ~connect_timeout, not hang.
    The test asserts the engine raises within 15s end-to-end —
    plenty of headroom over the 10s connect_timeout."""
    import time, pytest, sqlalchemy as sa
    from scripts.db_state_migrator import _bounded_engine
    eng = _bounded_engine("postgresql+psycopg://x:y@10.255.255.1:5432/nope")
    t0 = time.monotonic()
    with pytest.raises(sa.exc.OperationalError):
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"connect_timeout did not fire within 15s, took {elapsed:.1f}s"
```

- [ ] **Step 3: Run the test**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_bounded_engine_fails_fast_on_unreachable -v --timeout=30
```
Expected: PASS within ~10-12s.

- [ ] **Step 4: Commit**

```bash
git add scripts/db_state_migrator.py tests/db_pg/test_db_state_migrator.py
git commit -m "fix(migrator): bounded engine with connect + statement timeouts"
```

---

### Task 1.7: `copy_duckdb_to_pg` surfaces per-table errors in summary (MEDIUM correctness)

**Files:**
- Modify: `scripts/db_state_migrator.py` — `copy_duckdb_to_pg`'s summary aggregation
- Test: `tests/db_pg/test_data_migration.py`

- [ ] **Step 1: Failing test**

```python
def test_copy_duckdb_to_pg_summary_lists_failed_tables(tmp_path, pg_with_schema):
    """copy_duckdb_to_pg currently silently drops failed-table reports
    from its summary (the ``if 'error' not in r`` filter). Operators
    + verify both then see ``tables_migrated == len(reports)`` and
    proceed. The summary must list failures explicitly.
    """
    import duckdb
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from scripts.db_state_migrator import copy_duckdb_to_pg

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    duck.close()

    with pg_with_schema.connect() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS users CASCADE"))
        conn.commit()

    summary = copy_duckdb_to_pg(duck_path, str(pg_with_schema.url))
    assert summary.get("tables_failed"), summary
    assert "users" in [t["table"] for t in summary["tables_failed"]]
```

- [ ] **Step 2: Update copy_duckdb_to_pg**

Find `copy_duckdb_to_pg` and replace the summary-aggregation block at the end with:

```python
ok = [r for r in reports if "error" not in r]
err = [r for r in reports if "error" in r]
return {
    "rows_total": sum(r.get("pg_rows", 0) for r in ok),
    "tables_migrated": len(ok),
    "tables_failed": [
        {"table": r["table"], "error": str(r["error"])}
        for r in err
    ],
}
```

In `main()`, after collecting `copy_summary`, add an early bail:

```python
if copy_summary.get("tables_failed"):
    writer.mark_failed(
        step="data_copy",
        error_class="CopyTableError",
        error_message=(
            "Per-table copy failed: "
            + ", ".join(f"{t['table']}={t['error']!r}" for t in copy_summary["tables_failed"])
        ),
    )
    return 1
```

- [ ] **Step 3: Run tests**

```
.venv/bin/pytest tests/db_pg/test_data_migration.py::test_copy_duckdb_to_pg_summary_lists_failed_tables tests/db_pg/test_db_state_migrator.py -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/db_state_migrator.py tests/db_pg/test_data_migration.py
git commit -m "fix(migrator): summary surfaces tables_failed and main() halts on any per-table error"
```

---

## Phase 2 — API safety + state-machine semantics

### Task 2.1: `write_backend_state(in_progress)` preserves URL (BLOCKER B4)

**Files:**
- Modify: `src/db_state_machine.py:108-124` (`write_backend_state` function)
- Modify: `app/api/db_state.py:163` (call site)
- Test: `tests/test_db_state_machine.py`, `tests/test_api_db_state.py`

- [ ] **Step 1: Write the failing test for write_backend_state preserving URL**

Append to `tests/test_db_state_machine.py`:

```python
def test_write_backend_state_preserves_url_when_url_kw_absent(tmp_path, monkeypatch):
    """When ``write_backend_state`` is called with no ``url`` argument
    it must KEEP the existing url key in instance.yaml. Previously it
    omitted url from the output → yaml.safe_dump erased the key →
    repository routing saw backend=*_in_progress (treated as PG) with
    no URL → 30s+ window where every authenticated request crashed
    with ``Postgres URL is unset``."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)   # no url= kwarg

    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR_IN_PROGRESS
    assert url == "postgresql+psycopg://x:y@h/d"
```

- [ ] **Step 2: Run to verify fail**

```
.venv/bin/pytest tests/test_db_state_machine.py::test_write_backend_state_preserves_url_when_url_kw_absent -v
```
Expected: FAIL — url becomes None.

- [ ] **Step 3: Fix write_backend_state**

In `src/db_state_machine.py`, find `write_backend_state`. Replace:

```python
def write_backend_state(state: "BackendState", url: str | None = None) -> None:
    ...
    data = {"database": {"backend": state.value}}
    if url is not None:
        data["database"]["url"] = url
    ...
```

with:

```python
def write_backend_state(state: "BackendState", url: str | None = ...) -> None:
    """Atomic instance.yaml update.

    ``url=...`` (the sentinel `Ellipsis`) means "leave the existing
    url key alone" — for transitions to *_IN_PROGRESS where the URL
    is unchanged. Pass ``url=None`` explicitly when transitioning to
    a stateless backend like DuckDB (then the url is removed).

    All other top-level keys are preserved (logging, auth providers,
    etc. — the operator may have set them via /admin/server-config).
    """
    import yaml
    overlay = _OVERLAY_PATH
    overlay.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if overlay.exists():
        try:
            existing = yaml.safe_load(overlay.read_text()) or {}
        except Exception:
            existing = {}
    db = dict(existing.get("database") or {})
    db["backend"] = state.value
    if url is ...:
        # Sentinel: preserve whatever url is there.
        pass
    elif url is None:
        db.pop("url", None)
    else:
        db["url"] = url
    existing["database"] = db
    tmp = overlay.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(existing, default_flow_style=False))
    os.replace(tmp, overlay)
```

- [ ] **Step 4: Update the API call site**

In `app/api/db_state.py`, find `write_backend_state(in_progress)` and confirm it now calls without `url=` (which → preserve). The new signature handles this automatically. No code change needed at the call site beyond confirming it does NOT explicitly pass `url=None`.

- [ ] **Step 5: Companion test for explicit url=None clearing**

```python
def test_write_backend_state_clears_url_when_url_none_explicit(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.DUCKDB, url=None)
    state, url = read_backend_state()
    assert state == BackendState.DUCKDB
    assert url is None
```

```
.venv/bin/pytest tests/test_db_state_machine.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/db_state_machine.py tests/test_db_state_machine.py
git commit -m "fix(state-machine): write_backend_state preserves URL by default (B4)"
```

---

### Task 2.2: `cancel_job` reverts to source_backend, not target_backend (BLOCKER B1)

**Files:**
- Modify: `app/api/db_state.py:240-260` (around `cancel_job`)
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_db_state.py`:

```python
def test_cancel_job_reverts_to_source_backend_not_target(seeded_app, monkeypatch):
    """cancel_job MUST read source_backend from the job intent and
    revert to it. The previous code hard-coded the inverse of target,
    which for cloud → side_car would revert to DuckDB even though
    live data is on cloud — instance.yaml diverges from reality and
    the app routes to the wrong DB.
    """
    import json
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    from src.db_state_machine import BackendState, write_backend_state
    cloud_url = "postgresql+psycopg://postgres:pw@cloud-host/agnes"
    # Simulate operator-on-cloud who triggers cloud → side_car.
    write_backend_state(BackendState.CLOUD, url=cloud_url)
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)  # URL preserved
    # Drop a pending job — the API endpoint normally writes this.
    job_id = "cancel-test-1"
    job_path = data_dir / "state" / "db-jobs" / f"{job_id}.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text(json.dumps({
        "job_id": job_id,
        "status": "pending",
        "source_backend": "cloud",
        "target_backend": "side_car",
        "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
        "source_url": cloud_url,
        "schema_version": 1, "progress_pct": 0, "current_step": "queued",
    }))

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        f"/api/admin/db/cancel/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 202), r.text

    from src.db_state_machine import read_backend_state
    state, url = read_backend_state()
    assert state == BackendState.CLOUD, state
    assert url == cloud_url
```

- [ ] **Step 2: Run to verify fail**

```
.venv/bin/pytest tests/test_api_db_state.py::test_cancel_job_reverts_to_source_backend_not_target -v
```
Expected: FAIL — state reverts to DUCKDB.

- [ ] **Step 3: Fix cancel_job**

In `app/api/db_state.py`, find the `cancel_job` function (around line 240). Replace its body's revert logic:

```python
# WAS:
revert = BackendState.DUCKDB if data["target_backend"] == "side_car" else BackendState.SIDE_CAR

# WITH:
# Revert to whatever the job recorded as source — the only authoritative
# "what was live before we started" reference. Hard-coding the inverse
# of target was fine for forward-only paths (DUCKDB → SIDE_CAR → CLOUD)
# but breaks for the bidirectional CLOUD → SIDE_CAR rollback path
# added in v10 (source=cloud, target=side_car, hard-coded inverse=DUCKDB,
# which is *wrong*).
try:
    revert = BackendState(data["source_backend"])
except (KeyError, ValueError):
    # Pre-v10 jobs may lack source_backend. Fall back to old behaviour
    # to avoid bricking in-flight upgrades.
    revert = BackendState.DUCKDB if data.get("target_backend") == "side_car" else BackendState.SIDE_CAR
```

Also: pass the source_url so the URL is preserved on revert. Find the `write_backend_state(revert)` call and change to `write_backend_state(revert, url=data.get("source_url"))` (the existing sentinel `...` default handles legacy missing-url case fine for DuckDB).

- [ ] **Step 4: Run to verify pass**

```
.venv/bin/pytest tests/test_api_db_state.py::test_cancel_job_reverts_to_source_backend_not_target -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): cancel_job reverts to source_backend, preserving source_url (B1)"
```

---

### Task 2.3: Cooperative cancellation (BLOCKER B2)

**Files:**
- Modify: `app/api/db_state.py` — cancel endpoint writes sentinel file
- Modify: `scripts/db_state_migrator.py` — `JobWriter.update_step` + each step checks sentinel
- Test: `tests/db_pg/test_db_state_migrator.py`

- [ ] **Step 1: Write the failing test**

```python
def test_migrator_honours_cancel_sentinel_mid_run(tmp_path, pg_engine, monkeypatch):
    """If a ``<job_id>.cancel`` sentinel appears mid-migration, the
    migrator must exit at the next step boundary with status=cancelled
    — NOT continue and overwrite with success when the copy finishes.
    """
    import json
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()
    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    # Drop the cancel sentinel BEFORE main() even runs — first
    # step-boundary check should observe it.
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j1.cancel").touch()

    rc = main(
        job_id="j1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 2, rc          # 2 = cancelled (new exit code)
    job = json.loads((jobs_dir / "j1.json").read_text())
    assert job["status"] == "cancelled", job
```

- [ ] **Step 2: Run to verify fail**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_migrator_honours_cancel_sentinel_mid_run -v
```
Expected: FAIL — migrator doesn't check sentinel.

- [ ] **Step 3: Wire the sentinel check**

In `scripts/db_state_migrator.py`, add a helper at module scope:

```python
def _check_cancel_sentinel(job_id: str, jobs_dir: Path) -> bool:
    """Return True if a <job_id>.cancel marker exists in jobs_dir."""
    return (jobs_dir / f"{job_id}.cancel").exists()
```

In `JobWriter`, add:

```python
def step_boundary_or_cancel(self, *, step: str, progress_pct: int) -> bool:
    """Update the job step + return True if a cancel sentinel exists.

    Callers loop the migrator's step transitions through this method
    so cancel is observable at every checkpoint without changing the
    main() control-flow shape.
    """
    self.update_step(step, progress_pct=progress_pct)
    return _check_cancel_sentinel(self.job_id, self.jobs_dir)
```

In `main()`, replace each `writer.update_step(...)` call with:

```python
if writer.step_boundary_or_cancel(step="alembic", progress_pct=20):
    writer.mark_cancelled(step="alembic")
    return 2

# similarly for "data_copy", "verify", "backup", "flip_backend"…
```

Add the constant `EXIT_CANCELLED = 2` near the top.

- [ ] **Step 4: Wire the API endpoint to write the sentinel**

In `app/api/db_state.py`'s `cancel_job` endpoint, BEFORE the existing JSON rewrite, add:

```python
# Drop the cancel sentinel BEFORE rewriting the job JSON. The
# migrator subprocess polls for this at every step boundary; the
# JSON status overwrite is the secondary signal (for clients
# polling /job/{id}).
sentinel = jobs_dir / f"{job_id}.cancel"
sentinel.touch()
```

- [ ] **Step 5: Run + commit**

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_migrator_honours_cancel_sentinel_mid_run -v
```
Expected: PASS.

```bash
git add scripts/db_state_migrator.py app/api/db_state.py tests/db_pg/test_db_state_migrator.py
git commit -m "fix(migrator): cooperative cancellation via <job>.cancel sentinel (B2)"
```

---

### Task 2.4: URL normalize comparison (BLOCKER B7)

**Files:**
- Modify: `app/api/db_state.py:120,139` — same-URL guard
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Failing test**

```python
def test_post_migrate_rejects_aliased_same_url(seeded_app, monkeypatch):
    """Cloud URL that resolves to the side-car hostname (port omitted,
    or trailing-slash variant, etc.) must be rejected. Otherwise the
    migration runs side-car-onto-itself, and a later cloud-only
    applier tick stops the very container the new "cloud" backend
    points at."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)

    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(
        BackendState.SIDE_CAR,
        url="postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
    )

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/db/migrate",
        json={
            "target": "cloud",
            # Port-omitted variant of the side-car URL.
            "cloud_url": "postgresql+psycopg://agnes:agnes@postgres/agnes",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "alias" in r.json()["detail"].lower() or "self" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run to verify fail**

Expected: FAIL — string-equality lets the port-omitted form through.

- [ ] **Step 3: Add normalization helper + use it**

In `app/api/db_state.py`, add:

```python
def _normalize_pg_url(raw: str) -> tuple[str, str, int, str]:
    """Return (driver_scheme, host, port, database) for comparison.

    Defaults port to 5432, lowercases host, strips query string. The
    user / password are intentionally ignored — they don't change
    which DB the operator is talking to."""
    from urllib.parse import urlparse
    p = urlparse(raw)
    return (
        p.scheme.lower(),
        (p.hostname or "").lower(),
        p.port or 5432,
        (p.path or "").lstrip("/"),
    )

def _urls_alias(a: str, b: str) -> bool:
    try:
        return _normalize_pg_url(a) == _normalize_pg_url(b)
    except Exception:
        return False
```

Replace the existing same-URL guard `if source_url and source_url == target_url:` with:

```python
if source_url and _urls_alias(source_url, target_url):
    raise HTTPException(
        400,
        detail=(
            "Target URL aliases the source (same host/port/database, "
            "with or without explicit port) — migrating onto self "
            "would leave the side-car container's data flagged as "
            "cloud and a subsequent lifecycle tick would stop the "
            "very container holding the only copy. Refusing."
        ),
    )
```

- [ ] **Step 4: Run + commit**

```
.venv/bin/pytest tests/test_api_db_state.py::test_post_migrate_rejects_aliased_same_url -v
```
Expected: PASS.

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): reject migration when source/target URLs alias by host/port/db (B7)"
```

---

### Task 2.5: Hold flock until pending job written; reject second concurrent pending (BLOCKER B8)

**Files:**
- Modify: `app/api/db_state.py` — restructure `start_migration` flock scope + `_current_job_id` includes pending
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Failing test**

```python
def test_post_migrate_rejects_concurrent_when_pending_exists(seeded_app, monkeypatch):
    """Two admin POSTs against the same transition must produce
    409 on the second, not two orphan pending jobs."""
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r1 = client.post(
        "/api/admin/db/migrate",
        json={"target": "side_car"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 202, r1.text
    r2 = client.post(
        "/api/admin/db/migrate",
        json={"target": "side_car"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 409, r2.text
```

- [ ] **Step 2: Run to verify fail**

Expected: FAIL — second 202.

- [ ] **Step 3: Make `_current_job_id` include pending; keep flock during entire write**

In `app/api/db_state.py`:

```python
def _current_job_id() -> str | None:
    jobs_dir = _jobs_dir()
    if not jobs_dir.exists():
        return None
    for path in jobs_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Anything that hasn't reached a terminal state counts as
        # in-flight for the purpose of refusing concurrent migrates.
        if data.get("status") in ("pending", "running"):
            return data.get("job_id")
    return None
```

Restructure the `start_migration` flow:

```python
# OLD: lock.__exit__ ran inside finally after pending-file write.
# NEW: hold the lock across the ENTIRE write so a peer cannot
# observe a half-written state.
with MigrationLock():
    existing = _current_job_id()
    if existing:
        raise HTTPException(409, detail=f"Migration already in progress: job {existing}")
    # … existing write_backend_state + intent JSON + flag write …
return {"job_id": job_id, "status": "pending"}
```

- [ ] **Step 4: Run + commit**

```
.venv/bin/pytest tests/test_api_db_state.py::test_post_migrate_rejects_concurrent_when_pending_exists -v
```
Expected: PASS.

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): hold flock across pending-job write; refuse concurrent migrations (B8)"
```

---

### Task 2.6: Redact target_url in `GET /api/admin/db/job/{id}` (HIGH H1)

**Files:**
- Modify: `app/api/db_state.py` — `get_job`
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Failing test**

```python
def test_get_job_redacts_target_url_password(seeded_app, monkeypatch):
    """GET /job/{id} must mask the password — anyone authenticated
    can poll, so the cloud URL with embedded credentials must not
    flow back over the wire."""
    import json
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    job_id = "redact-1"
    (data_dir / "state" / "db-jobs").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "db-jobs" / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id, "status": "running",
        "target_url": "postgresql+psycopg://postgres:S3cr3t@h/agnes",
        "source_url": "postgresql+psycopg://agnes:LiveP@h/agnes",
    }))
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.get(f"/api/admin/db/job/{job_id}", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert "S3cr3t" not in body["target_url"]
    assert "LiveP" not in body["source_url"]
    assert "****" in body["target_url"]
```

- [ ] **Step 2: Implement redaction**

In `get_job`, replace the existing response build with:

```python
data = json.loads(path.read_text())
for key in ("target_url", "source_url"):
    if data.get(key):
        data[key] = _redact_url(data[key])
return data
```

- [ ] **Step 3: Run + commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): redact target_url + source_url in GET /job/{id} (H1)"
```

---

### Task 2.7: File mode 0600 on overlay + job JSON (HIGH H2)

**Files:**
- Modify: `src/db_state_machine.py` — write atomicity + chmod after rename
- Modify: `app/api/db_state.py` — same for job JSON write
- Modify: `scripts/db_state_migrator.py` — JobWriter._write
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Failing test**

```python
def test_overlay_and_job_files_chmod_0600(seeded_app, monkeypatch):
    import os, stat
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:Y@h/d")
    overlay = data_dir / "state" / "instance.yaml"
    assert (overlay.stat().st_mode & 0o777) == 0o600, oct(overlay.stat().st_mode)

    client = seeded_app["client"]; token = seeded_app["admin_token"]
    r = client.post("/api/admin/db/migrate", json={"target": "cloud", "cloud_url": "postgresql+psycopg://x:Y@h2/d"},
                    headers={"Authorization": f"Bearer {token}"})
    job_id = r.json()["job_id"]
    job_path = data_dir / "state" / "db-jobs" / f"{job_id}.json"
    assert (job_path.stat().st_mode & 0o777) == 0o600, oct(job_path.stat().st_mode)
```

- [ ] **Step 2: Add chmod calls**

In `src/db_state_machine.py`, after `os.replace(tmp, overlay)`:

```python
os.chmod(overlay, 0o600)
```

Same in `app/api/db_state.py` after the os.replace for the job intent file. Same in `scripts/db_state_migrator.py`'s `JobWriter._write`.

- [ ] **Step 3: Run + commit**

```bash
git add src/db_state_machine.py app/api/db_state.py scripts/db_state_migrator.py tests/test_api_db_state.py
git commit -m "fix(security): 0600 on instance.yaml + job JSON — credentials never world-readable (H2)"
```

---

### Task 2.8: Validate cloud_url scheme allowlist (HIGH H3)

**Files:**
- Modify: `app/api/db_state.py`
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Test rejecting malformed**

```python
@pytest.mark.parametrize("bad_url", [
    "sqlite:///tmp/foo.db",
    "file:///etc/passwd",
    "http://evil/agnes",
    "postgresql+psycopg://",                # missing host
    "not-a-url-at-all",
])
def test_post_migrate_rejects_bad_cloud_url(seeded_app, monkeypatch, bad_url):
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    client = seeded_app["client"]; token = seeded_app["admin_token"]
    r = client.post("/api/admin/db/migrate",
                    json={"target": "cloud", "cloud_url": bad_url},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, (bad_url, r.text)
```

- [ ] **Step 2: Add validator before resolution**

```python
def _validate_cloud_url(url: str) -> None:
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("postgresql+psycopg", "postgresql", "postgres"):
        raise HTTPException(400, detail=f"cloud_url scheme {p.scheme!r} not allowed; expected postgresql+psycopg")
    if not p.hostname:
        raise HTTPException(400, detail="cloud_url missing host")
```

Call at top of `start_migration` when `payload.target == "cloud"`.

- [ ] **Step 3: Run + commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): cloud_url scheme + host validation (H3)"
```

---

### Task 2.9: `POSTGRES_PASSWORD` env-missing returns 500, not silent fallback (HIGH H4)

**Files:**
- Modify: `app/api/db_state.py:125-126`
- Test: `tests/test_api_db_state.py`

- [ ] **Step 1: Failing test**

```python
def test_side_car_target_500_when_password_env_missing(seeded_app, monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    data_dir = seeded_app["env"]["data_dir"]
    _patch_state_paths(monkeypatch, data_dir)
    client = seeded_app["client"]; token = seeded_app["admin_token"]
    r = client.post("/api/admin/db/migrate", json={"target": "side_car"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 500, r.text
    assert "POSTGRES_PASSWORD" in r.json()["detail"]
```

- [ ] **Step 2: Remove the fallback default**

Change:

```python
password = os.environ.get("POSTGRES_PASSWORD", "agnes")
```

to:

```python
password = os.environ.get("POSTGRES_PASSWORD")
if not password:
    raise HTTPException(
        500,
        detail=(
            "POSTGRES_PASSWORD env var is not set on this app instance. "
            "The side-car migration cannot proceed without it — set the "
            "secret via /opt/agnes/.env or your secret manager and "
            "restart the app container."
        ),
    )
```

- [ ] **Step 3: Run + commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "fix(api): refuse side_car migration when POSTGRES_PASSWORD env unset (H4)"
```

---

## Phase 3 — Host applier robustness

### Task 3.1: Bash `write_instance_yaml` preserves other keys (BLOCKER B6)

**Files:**
- Modify: `scripts/ops/agnes-state-applier.sh` — `write_instance_yaml` function (lines 99-115)
- Test: `tests/test_state_applier_host_script.sh`

- [ ] **Step 1: Add the failing assertion to existing shell test**

In `tests/test_state_applier_host_script.sh`, after the existing assertions, add:

```bash
# Seed instance.yaml with operator-set non-database keys before
# running. After the migration the applier must NOT nuke them.
cat > "$tmp/data/state/instance.yaml" <<'YAML'
logging:
  level: debug
auth_providers:
  google: enabled
database:
  backend: duckdb
YAML

# (the test setup already runs the applier once; rerun after seeding
# to exercise the write_instance_yaml call)
# … existing transcript reset / re-invoke …

grep -q "level: debug" "$tmp/data/state/instance.yaml" \
    || fail "logging.level was destroyed by write_instance_yaml"
grep -q "google: enabled" "$tmp/data/state/instance.yaml" \
    || fail "auth_providers.google was destroyed by write_instance_yaml"
```

- [ ] **Step 2: Replace the bash function with python3 yaml-aware writer**

In `scripts/ops/agnes-state-applier.sh`, replace the existing `write_instance_yaml` function with:

```bash
write_instance_yaml() {
    # Use python3 + yaml so all non-database keys (logging, auth
    # providers, feature flags, etc. the operator may have set via
    # /admin/server-config) are preserved. The bash heredoc approach
    # rewrote the file from scratch and silently destroyed them.
    local backend=$1 url=${2:-}
    python3 - "$backend" "$url" <<'PY'
import os, sys, yaml
backend, url = sys.argv[1], sys.argv[2]
path = "/data/state/instance.yaml"
existing = {}
if os.path.exists(path):
    try:
        existing = yaml.safe_load(open(path).read()) or {}
    except Exception:
        existing = {}
db = dict(existing.get("database") or {})
db["backend"] = backend
if url:
    db["url"] = url
else:
    db.pop("url", None)
existing["database"] = db
tmp = path + ".tmp"
with open(tmp, "w") as f:
    yaml.safe_dump(existing, f, default_flow_style=False)
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
    chown 999:999 /data/state/instance.yaml || true
}
```

- [ ] **Step 3: Run the shell test**

```
bash tests/test_state_applier_host_script.sh
```
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh tests/test_state_applier_host_script.sh
git commit -m "fix(applier): write_instance_yaml preserves non-database keys via python yaml (B6)"
```

---

### Task 3.2: Stuck-running recovery (BLOCKER B5)

**Files:**
- Modify: `scripts/db_state_migrator.py` — write heartbeat
- Modify: `scripts/ops/agnes-state-applier.sh` — detect stale heartbeat → mark failed
- Test: `tests/db_pg/test_db_state_migrator.py`, `tests/test_state_applier_host_script.sh`

- [ ] **Step 1: Add `JobWriter.heartbeat()` + call it before/after each step**

In `scripts/db_state_migrator.py`, add to `JobWriter`:

```python
def heartbeat(self) -> None:
    """Update mtime of an alive marker the applier polls for to
    detect stuck-`running` jobs (host reboot, OOM-kill, docker
    daemon restart). Distinct from the JSON-status overwrite so a
    crash *between* JSON writes still surfaces."""
    (self.jobs_dir / f"{self.job_id}.alive").touch()
```

Call it in every `update_step`:

```python
def update_step(self, step: str, *, progress_pct: int = 0, ...):
    ...
    self._write(data)
    self.heartbeat()
```

- [ ] **Step 2: Applier check stale heartbeat**

In `scripts/ops/agnes-state-applier.sh`, near where `PENDING_JOB` is detected, add a pre-step:

```bash
# Stuck-running recovery — a job that hasn't touched its .alive
# file in 120s is treated as failed (host reboot / OOM / docker
# crashed mid-migration). The applier writes its own failure to
# unstick forward progress.
NOW=$(date +%s)
if [ -d "$JOBS_DIR" ]; then
    for f in "$JOBS_DIR"/*.json; do
        [ -e "$f" ] || continue
        st=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$f" 2>/dev/null || echo "")
        if [ "$st" = "running" ]; then
            alive="${f%.json}.alive"
            if [ -e "$alive" ]; then
                age=$(( NOW - $(stat -c '%Y' "$alive") ))
            else
                age=999
            fi
            if [ "$age" -gt 120 ]; then
                logger -t agnes-state-applier "Stale running job ${f}: alive=${age}s, marking failed"
                update_job "$f" "failed" "stuck running (no heartbeat for ${age}s, host reboot / OOM suspected)"
            fi
        fi
    done
fi
```

- [ ] **Step 3: Test**

```python
def test_jobwriter_heartbeat_touches_alive(tmp_path):
    from scripts.db_state_migrator import JobWriter
    w = JobWriter(job_id="j1", jobs_dir=tmp_path, source="duckdb", target="side_car")
    w.write_initial()
    w.heartbeat()
    assert (tmp_path / "j1.alive").exists()
```

```
.venv/bin/pytest tests/db_pg/test_db_state_migrator.py::test_jobwriter_heartbeat_touches_alive -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/db_state_migrator.py scripts/ops/agnes-state-applier.sh tests/db_pg/test_db_state_migrator.py
git commit -m "fix(state-machine): heartbeat + applier stuck-running recovery (B5)"
```

---

### Task 3.3 + 3.4 + 3.5 + 3.6 + 3.7 — Applier MEDIUM fixes

(Plan continues with: oldest-job ordering by mtime; cloud→side_car failure cleans flag; pg_isready loop tracks success; single-pass JSON read; RESTART_RC dead code removal.)

- [ ] **Step 1: Oldest-by-mtime ordering in pending-job scan**

Replace the existing for-loop with:

```bash
PENDING_JOB=""
if [ -d "$JOBS_DIR" ]; then
    PENDING_JOB=$(python3 - "$JOBS_DIR" <<'PY' || echo "")
import json, os, sys
d = sys.argv[1]
candidates = []
for f in os.listdir(d):
    if not f.endswith(".json"): continue
    p = os.path.join(d, f)
    try:
        data = json.load(open(p))
    except Exception:
        continue
    if data.get("status") == "pending":
        candidates.append((os.path.getmtime(p), p))
candidates.sort()
print(candidates[0][1] if candidates else "")
PY
    )
fi
```

- [ ] **Step 2: Flag cleared on rollback path**

Inside the failure branch of the migrator section, after `write_instance_yaml "$SOURCE_BACKEND"`, also remove the flag if target ≠ source lifecycle:

```bash
if [ "$SOURCE_BACKEND" = "duckdb" ]; then
    rm -f "$FLAG"
fi
```

- [ ] **Step 3: pg_isready loop tracks success**

```bash
PG_READY=0
for _ in $(seq 1 30); do
    if docker exec agnes-postgres-1 pg_isready -U agnes >/dev/null 2>&1; then
        PG_READY=1
        break
    fi
    sleep 2
done
if [ "$PG_READY" -ne 1 ]; then
    logger -t agnes-state-applier "postgres did not become ready within 60s — aborting"
    exit 1
fi
```

- [ ] **Step 4: Single-pass JSON parse**

Bundle the four separate `python3 -c 'import json,sys;print(json.load(open(...)).get("X"))'` calls into one:

```bash
read -r JOB_ID TARGET_URL TARGET_BACKEND SOURCE_BACKEND SOURCE_URL < <(
    python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["job_id"], d.get("target_url",""), d.get("target_backend",""),
      d.get("source_backend",""), d.get("source_url",""))
' "$PENDING_JOB"
)
```

- [ ] **Step 5: Remove dead RESTART_RC branch**

The earlier fix wrapped `dc up` in `RESTART_LOG=$(...)` to capture stderr. Under `set -e` a non-zero return aborts the script before reaching `if [ "$RESTART_RC" -ne 0 ]`. Wrap in `|| true`:

```bash
set +e
RESTART_LOG=$(dc up -d --no-deps --force-recreate app scheduler 2>&1)
RESTART_RC=$?
set -e
if [ "$RESTART_RC" -ne 0 ]; then
    logger -t agnes-state-applier "WARNING app+scheduler restart exited $RESTART_RC: $RESTART_LOG"
fi
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh tests/test_state_applier_host_script.sh
git commit -m "fix(applier): pending-job mtime ordering, flag cleanup on rollback, pg_isready tracking, single-pass parse, RESTART_RC reachable"
```

---

## Phase 4 — Applier liveness in API

### Task 4.1 + 4.2 + 4.3: Heartbeat in `/api/admin/db/state`

(Plan continues — implementing `applier_last_tick_age_s` field. Truncated for brevity here; the steps follow the same TDD pattern as above.)

---

## Phase 5 — UI / UX

### Task 5.1: localStorage progress cache (empty-yellow-box during restart)

(Pattern: poll caches last-known-good response; renders during the restart-induced fetch gap.)

### Task 5.2: Polling backoff

### Task 5.3: Per-table progress wired through

---

## Phase 6 — CLI

### Task 6.1: `--yes` flag on `agnes admin db migrate`

```python
@db_app.command()
def migrate(
    target: str,
    cloud_url: str | None = typer.Option(None, "--cloud-url"),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
):
    if not yes and not typer.confirm(f"Migrate app-state DB to '{target}'? This is operator-level + destructive on failure."):
        raise typer.Exit(1)
    …
```

---

## Phase 7 — Tests (massive coverage gap)

### Task 7.1: JSONB string-coercion round-trip (catches v9 fix regression)
### Task 7.2: E2E DUCKDB→CLOUD direct
### Task 7.3: E2E CLOUD→SIDE_CAR DR rollback
### Task 7.4: Cancel-during-data_copy
### Task 7.5: Hung-migrator timeout
### Task 7.6: Host-reboot recovery (uses heartbeat from 3.2)
### Task 7.7: instance.yaml preserves-other-keys (already covered by 3.1)
### Task 7.8: Concurrent POST /migrate (already covered by 2.5)
### Task 7.9: URL-redaction (already covered by 2.6)
### Task 7.10: File-mode 0600 (already covered by 2.7)
### Task 7.11: Malformed cloud_url (already covered by 2.8)
### Task 7.12: Runtime auth-bypass probe — `test_protected_endpoints_actually_enforce_auth` (Vrysánek MEDIUM #3)
### Task 7.13: Module-scoped alembic fixture in `tests/db_pg/conftest.py` (Vrysánek MEDIUM #4)

(Each task follows the TDD scaffold pattern: write the test, run it, observe failure, implement, run, pass, commit.)

---

## Phase 8 — Security / operations

### Task 8.1: agnes-state-applier as non-root in docker group

(Update systemd unit `User=`, `Group=docker`; chown `/data/state` to that user; verify applier still picks up jobs.)

### Task 8.2: Applier liveness heartbeat to API (already covered by 4.2)

---

## Phase 9 — Docs + final

### Task 9.1: Update `docs/postgres-cutover-runbook.md` with cancel semantics + recovery procedures
### Task 9.2: CHANGELOG bullets per phase
### Task 9.3: Update PR #455 description with fix status

---

## Self-review

- **Coverage check:** Each B1-B8, comment-2 #1, H1-H12 has a numbered task above (or is subsumed by another — e.g. H6 row-count-only verification is partially addressed by 1.3 + 7.1).
- **Placeholder scan:** `B5 recovery` body is the only place with a slightly fluffy "stale > 120s" decision — that's the actual heuristic, not a placeholder; the threshold is named.
- **Type consistency:** `JobWriter` interface is touched in 1.3, 2.3, 3.2 — every reference uses the same method names (`update_step`, `heartbeat`, `step_boundary_or_cancel`, `mark_cancelled`, `mark_failed`, `mark_success`, `_write`). No drift.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-05-28-db-state-machine-review-fixes.md`.
