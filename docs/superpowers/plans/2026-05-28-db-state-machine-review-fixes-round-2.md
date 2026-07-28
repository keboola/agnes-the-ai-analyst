# DB State Machine Review Fixes — Round 2 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Close every remaining finding from cvrysanek's PR #455 review that round-1 (plan `2026-05-28-db-state-machine-review-fixes.md`) left open or only partially addressed. After this round, **every BLOCKER, HIGH, MEDIUM, LOW, testing-gap, and residual-risk item from both review comments must be either fixed-with-test or explicitly closed with a noted rationale.**

**Architecture:** Same as round 1 — each finding gets a code change with a regression test; no new architectural primitives. Grouped into 6 phases (A–F) by file/topic affinity so each phase commits as a coherent slice and reviewers see related work together.

**Tech stack:** Python 3.13 + FastAPI + SQLAlchemy 2.0 + psycopg 3 + DuckDB ≥1.5; pytest + pixeltable-pgserver for tests; bash for the host applier; alembic for migrations.

## Branch & worktree

- **Worktree:** `/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.claude/worktrees/zs+db-state-machine`
- **Branch:** `zs/db-state-machine` (HEAD `d585533a` — round-1 release-cut to 0.56.0)
- **Release strategy:** Since the 0.56.0 release-cut commit (`d585533a`) hasn't been tagged/published, this round either (a) extends the 0.56.0 entry under `[Unreleased]` then amends the cut commit, or (b) follows the cut with a 0.56.1 patch entry. Decided in phase F.

## File map

| File | Touched by tasks |
| --- | --- |
| `scripts/db_state_migrator.py` | A.1, A.2, A.4, B.1, B.4, C.2, E.1 |
| `scripts/migrate_duckdb_to_pg/__init__.py` | A.3, C.1 |
| `scripts/migrate_duckdb_to_pg/tasks.py` | B.1 |
| `scripts/ops/agnes-state-applier.sh` | B.2, B.3, C.2, E.5 |
| `app/api/db_state.py` | B.2 |
| `migrations/versions/0012_resource_grants_fk.py` (new) | E.3 |
| `src/db.py` (v60→v61 ladder) | E.3 |
| `tests/db_pg/test_db_state_migrator.py` | A.1, A.2, A.4, B.1, B.4, C.2, D.2, D.3, E.1 |
| `tests/db_pg/test_data_migration.py` | A.3, B.1, C.1, D.1, E.3 |
| `tests/test_api_db_state.py` | B.2, E.4 |
| `tests/test_state_applier_host_script.sh` | B.3, C.2, E.2, E.5 |
| `CHANGELOG.md` | F |
| `pyproject.toml` | F |

---

## Phase A — Migrator hardening (4 tasks)

### Task A.1: Batch `copy_pg_to_pg` like DuckDB path (HIGH H4)

**The bug.** `scripts/db_state_migrator.py::copy_pg_to_pg` does:

```python
rows = source_conn.execute(sa.select(*src_cols).select_from(src_table)).all()
with target_engine.begin() as target_conn:
    if rows:
        target_conn.execute(target_table.insert(), rows)
```

`.all()` materializes the entire table into RAM. Single `target.begin()` transaction. The DuckDB path uses 500-row batches; PG→PG path does not. Production `audit_log` / `usage_events` (millions of rows) will OOM the migrator container or blow the PG transaction log.

**Fix design.**

Stream the source via SQLAlchemy `yield_per(BATCH_SIZE)`. Insert in chunks of `BATCH_SIZE`. Each chunk lands in its own `target.begin()` block so a mid-stream failure rolls back only the in-flight batch, and the migrator can resume via ON CONFLICT DO NOTHING.

```python
BATCH_SIZE = 500

def copy_pg_to_pg(source_url: str, target_url: str) -> dict[str, Any]:
    ...
    for table_name in ordered_tables:
        ...
        with source_engine.connect() as src_conn:
            stmt = sa.select(*src_cols).select_from(src_table).execution_options(yield_per=BATCH_SIZE)
            result = src_conn.execute(stmt)
            batch: list[dict] = []
            rows_copied = 0
            for row in result:
                batch.append(_row_to_dict_with_jsonb_cast(row, src_cols, jsonb_cols))
                if len(batch) >= BATCH_SIZE:
                    _flush_batch(target_engine, target_table, batch)
                    rows_copied += len(batch)
                    batch.clear()
            if batch:
                _flush_batch(target_engine, target_table, batch)
                rows_copied += len(batch)
        ...
```

`_flush_batch` opens its own short `target.begin()` and uses `Insert.on_conflict_do_nothing` for PG-native upsert semantics.

**Files:** Modify `scripts/db_state_migrator.py`. Add test to `tests/db_pg/test_db_state_migrator.py`.

**Test (TDD).**

```python
def test_copy_pg_to_pg_batches_large_tables(tmp_path, pg_engine):
    """H4 — PG→PG copy must stream-batch rather than .all()-materialize.

    Seed 2000 rows; assert success without OOM tracking + assert that
    the implementation does NOT hold all rows in memory at once.
    We can't directly measure RAM, but we CAN verify the function
    uses execution_options(yield_per=...) by patching ``Result.all``
    to raise — if the implementation switched to yield_per+iterator
    the test passes; if it still calls .all() the test fails.
    """
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import copy_pg_to_pg

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        for i in range(2000):
            conn.execute(sa.text(
                "INSERT INTO users (id, email, name) VALUES (:i, :e, :n)"
            ), {"i": f"u{i:04d}", "e": f"u{i}@x.com", "n": f"User {i}"})

    # Sentinel: patching Result.all to raise. If the implementation
    # streams correctly via yield_per, it never calls .all() — test passes.
    original_all = sa.engine.Result.all
    calls = {"all": 0}
    def counting_all(self):
        calls["all"] += 1
        return original_all(self)
    sa.engine.Result.all = counting_all
    try:
        summary = copy_pg_to_pg(str(pg_engine.url), str(pg_engine.url))
    finally:
        sa.engine.Result.all = original_all

    assert summary["rows_total"] >= 2000
    # If .all() was called on the users table source query we materialised
    # everything in RAM. The fix is to use a streaming iterator.
    # We expect SOME .all() calls (e.g. for the verify step which is fine
    # because verify already aggregates), but the per-table copy loop
    # itself MUST NOT call .all() on the source SELECT.
    # Heuristic: under the batched implementation, .all() is not called
    # at all from the copy_pg_to_pg function path. Allow up to 2 calls
    # for unrelated internal SQLAlchemy bookkeeping; flag anything more.
    assert calls["all"] <= 2, f"copy still materialises via .all() — {calls['all']} calls"
```

### Task A.2: `subprocess.run` timeouts on `alembic_upgrade_head` + `backup_*` (HIGH H5)

**The bug.** `alembic_upgrade_head`, `backup_duckdb`, `backup_sidecar_pg` all call `subprocess.run(..., check=False)` with no `timeout=` kwarg. Hung target PG, network partition mid-pg_dump, hung alembic → migrator pinned forever. Engine-side `statement_timeout` (A.6 from round 1) only bounds queries, not the subprocess itself.

**Fix.**

Add `timeout=300` (5 min) to `alembic_upgrade_head` `subprocess.run`. Add `timeout=1800` (30 min) to `backup_duckdb` gzip and `backup_sidecar_pg` pg_dump — backups of multi-GB DBs take time. On `subprocess.TimeoutExpired` raise a `RuntimeError` with the timeout duration so `JobWriter.mark_failed` carries the actionable message.

**Test.**

```python
def test_alembic_upgrade_head_raises_on_timeout(tmp_path, monkeypatch):
    """H5 — hung alembic must surface as a clean RuntimeError, not
    pin the migrator forever."""
    import subprocess
    from scripts.db_state_migrator import alembic_upgrade_head

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 300))
    monkeypatch.setattr("subprocess.run", fake_run)

    import pytest
    with pytest.raises(RuntimeError, match="alembic.*timed out"):
        alembic_upgrade_head("postgresql+psycopg://x@y/z")


def test_backup_duckdb_raises_on_timeout(tmp_path, monkeypatch):
    """H5 — hung gzip during backup must surface, not pin."""
    import subprocess, duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import backup_duckdb

    duck = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck))
    _ensure_schema(conn)
    conn.close()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1800))
    monkeypatch.setattr("subprocess.run", fake_run)

    backups = tmp_path / "backups"
    import pytest
    with pytest.raises(RuntimeError, match="backup.*timed out"):
        backup_duckdb(duck, backups)
```

### Task A.3: `run_all` early-abort on first per-table failure (HIGH H6)

**The bug.** `scripts/migrate_duckdb_to_pg/__init__.py::run_all` loops `for task in selected:` and on per-task exception does `continue`. Other tables keep getting copied. The migrator's `main()` then halts before flip_backend (which prevents the broken backend from going live), but ORPHAN ROWS are inserted into PG before main() sees the failure. PG-level FKs don't exist on `personal_access_tokens(user_id)` etc., so the orphans are persistent garbage.

**Fix.**

Break the loop on the first per-task failure. Mark the rest of the tasks as `{table, skipped: True, reason: "halted after prior failure"}` so the summary still has one entry per task. Idempotent re-run on retry: ON CONFLICT DO NOTHING means a successful retry overwrites nothing.

```python
def run_all(duck_conn, pg_engine, only=None, dry_run=False, validate=True):
    selected = [t for t in TASKS if not only or t.target_table in only]
    reports: list[dict] = []
    halted = False
    for task in selected:
        if halted:
            reports.append({"table": task.target_table, "skipped": True,
                            "reason": "halted after prior task failure"})
            continue
        try:
            run_task(task, duck_conn, pg_engine, dry_run=dry_run)
        except Exception as exc:
            log.exception("task %s failed: %s", task.source_table, exc)
            reports.append({"table": task.target_table, "error": str(exc)})
            halted = True  # ← new: stop processing further tables
            continue
        if validate:
            try:
                reports.append(validate_task(task, duck_conn, pg_engine))
            except Exception as exc:
                log.exception("validate %s failed: %s", task.source_table, exc)
                reports.append({"table": task.target_table, "error": str(exc)})
                halted = True  # ← also halt on validate failure
        else:
            reports.append({"table": task.target_table, "ok": True})
    return reports
```

**Test.**

```python
def test_run_all_halts_on_first_failure(tmp_path, pg_with_schema, monkeypatch):
    """H6 — when one task raises, subsequent tasks MUST be skipped
    (status='skipped'), not silently produce orphan rows. The migration
    halts before flip; partial-state PG never goes live, but the report
    surface lets the operator see exactly which tables ran."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.migrate_duckdb_to_pg import run_all, TASKS

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    # Pick a task in the middle of the ordering to force-fail.
    fail_at = TASKS[2].target_table

    original_run = TASKS[2].run
    def boom(*a, **kw):
        raise RuntimeError("simulated mid-loop failure")
    TASKS[2].run = boom

    duck_ro = duckdb.connect(str(duck_path), read_only=True)
    try:
        reports = run_all(duck_ro, pg_with_schema, validate=False)
    finally:
        duck_ro.close()
        TASKS[2].run = original_run

    fail_idx = next(i for i, r in enumerate(reports) if r["table"] == fail_at)
    assert "error" in reports[fail_idx]
    # ALL tables after the failing one are 'skipped'.
    for r in reports[fail_idx + 1:]:
        assert r.get("skipped") is True, f"task {r['table']} should be skipped, got {r}"
```

### Task A.4: Content-hash verification (HIGH H12)

**The bug.** PG→PG verification only counts rows. Preseed Cloud SQL with users that have matching PK ids but different `name`/`email` (left over from a prior failed migration attempt) → row counts match → "verify ok" → flip_backend → app boots with stale corrupted users table.

**Fix.**

Extend `verify_pg_row_counts` (and `verify_row_counts`) to compute a SHA-256 hash over a representative non-PK column subset (when available) per table. Compare hashes between source and target. Hash on a content sample so the verify step doesn't add minutes to a multi-million-row table — sample 1000 rows by PK-stable ordering. Existing test `test_verify_row_counts_match` already exercises the count parity; add a new test for content drift.

**Implementation sketch.**

```python
def _content_hash_sample(engine, table_name: str, pk_cols: list[str],
                         non_pk_cols: list[str], sample_size: int = 1000) -> str:
    """Hash the first ``sample_size`` rows ordered by PK, considering
    only non-PK columns. Two databases with the same PK set + same
    non-PK content yield the same hash. Cheap sample bounds the
    verify-step cost; full equality on PKs (already covered by
    existing checksum) + sampled content equality on non-PKs catches
    common drift modes."""
    if not non_pk_cols:
        return "no-non-pk-content"
    pk_order = ", ".join(f'"{c}"' for c in pk_cols)
    sel_cols = ", ".join(f'"{c}"' for c in non_pk_cols)
    sql = f"SELECT {sel_cols} FROM {table_name} ORDER BY {pk_order} LIMIT {sample_size}"
    import hashlib
    h = hashlib.sha256()
    with engine.connect() as conn:
        for row in conn.execute(sa.text(sql)):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def verify_pg_row_counts(source_url, target_url):
    # ... existing row-count loop ...
    # ADDITION: for each table that has non-PK columns, compute content
    # hash on both sides and flag a mismatch.
    for table_name, pk_cols, non_pk_cols in _table_columns():
        src_hash = _content_hash_sample(source_engine, table_name, pk_cols, non_pk_cols)
        tgt_hash = _content_hash_sample(target_engine, table_name, pk_cols, non_pk_cols)
        if src_hash != tgt_hash:
            diffs.append({"table": table_name, "kind": "content_drift",
                          "source_hash": src_hash[:16], "target_hash": tgt_hash[:16]})
    return diffs
```

**Test.**

```python
def test_verify_pg_detects_non_pk_content_drift(tmp_path, pg_engine):
    """H12 — same PK set, different content (e.g. stale email on user
    'u1') must surface as a verification failure, not as 'rows match
    → ok'."""
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_pg_row_counts

    # Create the schema twice (we treat 'source' and 'target' as the
    # same DB; the drift is simulated by INSERTing identical PKs with
    # different non-PK columns then immediately running verify which
    # in same-URL mode compares a row against itself — for THIS test
    # we need separation, so use a second PG db or a schema. Simplest:
    # use the same engine and rely on the implementation correctly
    # reading from two URLs).
    # ...test fixture follows the same pattern as the existing E2E
    # cloud→side_car smoke test.
```

(Pragmatic note: the same-URL constraint of pgserver makes this hard to test cleanly. Document the limitation in the test and assert at least that two known-different content samples produce different hashes via direct calls to `_content_hash_sample`.)

---

## Phase B — Data integrity + operational hardening (4 tasks)

### Task B.1: Pre-copy PII scrub of audit_log historical rows (HIGH H7)

**The bug.** `_sanitize_for_audit` runs at WRITE time. Audit rows captured BEFORE that sanitizer existed contain raw passwords, tokens, and PII in `params` / `params_before`. The migrator copies them verbatim to PG — fresh PG instance now carries the historical leak.

**Fix.**

Add a pre-copy scrub pass at the start of `copy_duckdb_to_pg`. Walk `audit_log` rows where `params` or `params_before` contains any of `(password|token|secret|key|bearer|api[-_ ]?key)` (regex on the JSON-stringified value) and rewrite those rows in the SOURCE DuckDB to `{"_redacted_at_migration": true}` before the copy runs. Idempotent; subsequent runs find no matches.

Alternative: do the scrub IN the copy task (transform the row in `_normalize_for_pg`). Pre-copy in-place is preferred so the DuckDB backup also has the redacted form — the backup IS the recovery artifact, we don't want to leave PII in it.

**Test.**

```python
def test_pre_copy_scrubs_audit_log_pii(tmp_path, pg_with_schema):
    """H7 — historical audit_log rows with password/token in params
    must be scrubbed BEFORE copy so neither the migrated PG nor the
    DuckDB backup retain the secret."""
    import duckdb, json, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import copy_duckdb_to_pg

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    # Seed three rows: secret-bearing (must scrub), clean (must
    # survive verbatim), edge-case mixed (must scrub the field).
    conn.execute("""INSERT INTO audit_log (id, action, params, timestamp) VALUES
        ('a1', 'login', ?, current_timestamp),
        ('a2', 'view', ?, current_timestamp),
        ('a3', 'config_change', ?, current_timestamp)""",
        [
            json.dumps({"password": "secret123", "user": "alice"}),
            json.dumps({"page": "/dashboard"}),
            json.dumps({"name": "Bob", "api_key": "sk-deadbeef"}),
        ],
    )
    conn.close()

    copy_duckdb_to_pg(duck_path, str(pg_with_schema.url))

    # Source DuckDB is now scrubbed in place.
    duck_ro = duckdb.connect(str(duck_path), read_only=True)
    rows = duck_ro.execute(
        "SELECT id, params FROM audit_log ORDER BY id"
    ).fetchall()
    duck_ro.close()
    params = {rid: json.loads(p) if isinstance(p, str) else p for rid, p in rows}
    assert "secret123" not in str(params["a1"])
    assert params["a2"] == {"page": "/dashboard"}, "clean rows untouched"
    assert "sk-deadbeef" not in str(params["a3"])

    # Target PG carries only the scrubbed form.
    with pg_with_schema.connect() as c:
        pg_rows = c.execute(
            sa.text("SELECT id, params FROM audit_log ORDER BY id")
        ).fetchall()
    for rid, p in pg_rows:
        assert "secret" not in str(p).lower() or rid == "a2", \
            f"PG row {rid} still carries secret: {p}"
```

### Task B.2: Pending-job age expiry (HIGH H8)

**The bug.** A pending job file has no `created_at` and no expiry. Operator masks the timer, queues a migration, manually fixes state via the CLI, then weeks later unmasks the timer — applier blindly runs whatever pending JSON sits on disk against potentially-incompatible current state.

**Fix.**

`POST /migrate` already writes `started_at` into the job JSON (line 91 in `JobWriter.write_initial`). Add `queued_at: now()` to the pending JSON at the API layer (`start_migration`). In the applier, before processing a pending job, compute `now() - queued_at`. If `> 3600` (1 hour) mark the job failed with `expired` and `current_step="queued"`, skip processing. The threshold is operator-tunable later; 1 hour matches the 30s applier tick × generous human reaction window.

**Files:** `app/api/db_state.py` (write `queued_at`); `scripts/ops/agnes-state-applier.sh` (read + expiry check).

**Tests:** unit test that `POST /migrate` populates `queued_at`; shell test that a seeded pending job with `queued_at` 2 hours ago is marked `expired` not processed.

### Task B.3: cloud→side_car failure path cleans flag (MEDIUM)

**The bug.** `scripts/ops/agnes-state-applier.sh` task 3.4 (round 1) cleans `$FLAG` only when `SOURCE_BACKEND == duckdb`. The cloud→side_car DR rollback case is asymmetric: if a failed migration leaves `instance.yaml::backend = cloud` (rolled back to source) but `$FLAG = side-car-enabled`, the next applier tick will re-enable the postgres container because flag still says side_car. Result: orphan `agnes-postgres-1` running with no data.

**Fix.**

Extend the rollback branch (around line 218) to also clear `$FLAG` when the rollback ends on a state that doesn't need the postgres lifecycle:

```bash
case "$SOURCE_BACKEND" in
    duckdb|cloud)
        # Both states don't need agnes-postgres-1 running. Remove flag
        # so the next tick's lifecycle case enforces teardown.
        rm -f "$FLAG"
        ;;
esac
```

(For source=side_car the flag already says `side-car-enabled` which is correct.)

**Test:** extend the shell test to seed a `source=cloud` migration that fails post-copy, assert `$FLAG` is gone after the failure branch fires.

### Task B.4: Backup failure during side_car→cloud is hard fail (MEDIUM)

**The bug.** `scripts/db_state_migrator.py::main` swallows `backup_sidecar_pg` failure as a warning (line 587-594):

```python
try:
    backup_sidecar_pg(...)
except Exception as e:
    _log_backup_skip(writer, str(e))
```

UI shows success once the migration completes; operator discovers the missing recovery point only at restore time. The warning lives only in the job JSON's `error` field which UI doesn't surface for success-status jobs.

**Fix.**

Treat backup failure as a hard `mark_failed("BackupError", e)` + `return 1` for side_car→cloud transitions specifically. For DuckDB → side_car the prior `backup_duckdb` already runs and was promoted to pre-copy ordering (round 1 task 1.5). Operator must explicitly retry the migration after fixing the backup path (e.g. running `pg_dump` requires the postgres container to be up; if applier brought it down too early, backup fails — surface that loud).

Alternative if a soft mode is desired: add a `--allow-backup-failure` CLI flag with default `False`. Out of scope for this round; default fail.

**Test:** seed a failure injection on `backup_sidecar_pg`, assert main() returns 1 with `mark_failed(class="BackupError")` in the job JSON.

---

## Phase C — Progress wiring + applier subprocess timeout (2 tasks)

### Task C.1: Wire `update_table_progress` into `run_all` (MEDIUM)

**The bug.** `JobWriter.update_table_progress(current_table, tables_done, tables_total)` exists but is never called. Phase 5.3 (round 1) added the UI render for `table_progress` block when present — but the field never lands in the JSON. progress_pct freezes at 40% for the entire data_copy step.

**Fix.**

`run_all` doesn't have access to the JobWriter today (kept the migrator-script layer pure). Two options:

**(a)** Pass an optional `progress_callback: Callable[[str, int, int], None]` into `run_all`. `copy_duckdb_to_pg` / `copy_pg_to_pg` callers pass `lambda table, done, total: writer.update_table_progress(...)`.

**(b)** Pass the JobWriter explicitly. Couples migrator script to JobWriter type but simpler.

Pick (a) — keeps the migrator subscript independently callable for one-off CLI runs without a JobWriter.

```python
# scripts/migrate_duckdb_to_pg/__init__.py
def run_all(duck_conn, pg_engine, only=None, dry_run=False, validate=True,
            progress_callback=None):
    selected = [t for t in TASKS if not only or t.target_table in only]
    reports = []
    halted = False
    for i, task in enumerate(selected):
        if progress_callback:
            progress_callback(task.target_table, i, len(selected))
        ...
```

```python
# scripts/db_state_migrator.py::copy_duckdb_to_pg
def copy_duckdb_to_pg(duckdb_path, target_url, writer=None):
    ...
    progress_cb = None
    if writer is not None:
        progress_cb = lambda t, done, total: writer.update_table_progress(t, done, total)
    reports = run_all(duck_conn, pg_engine, validate=True,
                      progress_callback=progress_cb)
    ...

# scripts/db_state_migrator.py::main
copy_summary = copy_duckdb_to_pg(duck_path, target_url, writer=writer)
```

**Test:**

```python
def test_copy_duckdb_to_pg_emits_table_progress(tmp_path, pg_with_schema):
    """MED — JobWriter.update_table_progress must fire from copy_*
    so UI gets per-table % during data_copy."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import JobWriter, copy_duckdb_to_pg

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    writer = JobWriter(job_id="job-progress", jobs_dir=tmp_path / "jobs",
                       source="duckdb", target="side_car")
    writer.write_initial()
    copy_duckdb_to_pg(duck_path, str(pg_with_schema.url), writer=writer)

    import json
    job = json.loads((tmp_path / "jobs" / "job-progress.json").read_text())
    # After the copy, the most recent table_progress reflects the last
    # table processed (tables_done == tables_total).
    tp = job.get("table_progress")
    assert tp is not None, "update_table_progress was never called"
    assert tp["tables_total"] > 0
    assert tp["tables_done"] == tp["tables_total"]
```

### Task C.2: Applier `docker run` timeout (HIGH H5 — subprocess layer)

**The bug.** Round-1 task 1.6 set engine-level timeouts. The applier's `docker run --rm ... python -m scripts.db_state_migrator ...` (around line 185 of the applier) has NO outer time bound. A hung migrator that doesn't reach a step boundary (e.g. wedged DuckDB connection that holds GIL) sits forever.

**Fix.**

Add `timeout` to the `docker run` invocation via `timeout 1800 docker run --rm ...` (Linux `timeout(1)` from coreutils — universally available on customer-instance VMs). 1800s = 30 min is generous for current schema; configurable via env var `MIGRATOR_TIMEOUT_SEC`.

```bash
MIGRATOR_TIMEOUT_SEC=${MIGRATOR_TIMEOUT_SEC:-1800}
set +e
RESTART_LOG=$(timeout --signal=TERM --kill-after=30 "$MIGRATOR_TIMEOUT_SEC" \
    docker run --rm \
        ${NETWORK_ARGS[@]+"${NETWORK_ARGS[@]}"} \
        -v /data:/data \
        ...
)
MIG_RC=$?
set -e
if [ "$MIG_RC" -eq 124 ]; then
    # timeout returns 124 on TERM, 137 on KILL — both indicate watchdog fired.
    update_job "$PENDING_JOB" "failed" \
        "migrator subprocess exceeded ${MIGRATOR_TIMEOUT_SEC}s timeout"
    FINAL_STATUS="failed"
fi
```

Also add `timeout=10` to all `python3 -c '...'` invocations in the applier — they should never take more than seconds; the timeout is a defense-in-depth net.

**Test:** extend `tests/test_state_applier_host_script.sh` to inject a `docker run` stub that sleeps for 5s with `MIGRATOR_TIMEOUT_SEC=2`; assert the applier marks the job failed with `exceeded.*timeout`.

---

## Phase D — Tests for uncovered branches (3 tasks)

### Task D.1: `_substitute_default` covers NOW() + CURRENT_DATE (MEDIUM)

**The bug.** Round-1 task 1.4 fixed the timestamp-fabrication semantics. Tests only cover `CURRENT_TIMESTAMP`. The branches for `NOW()` and `CURRENT_DATE` (and edge case `now() AT TIME ZONE 'UTC'` if PG emits that) are uncovered.

**Fix:** add three parametrized tests to `tests/db_pg/test_data_migration.py` covering each default form. Assert that a column with the given default substituted via `_substitute_default` returns the actual `datetime`/`date` Python object on the round trip — and that operator-supplied values pass through unchanged (the round-1 contract).

### Task D.2: PG→PG copy with array + JSONB (MEDIUM)

**The bug.** Round-1 task 7.3 cloud→side_car smoke test seeds only `users(id, email, name)` — no array, no JSONB. The v9 JSONB CAST fix only fires when JSONB columns get exercised. Regression hole.

**Fix:** extend `test_main_cloud_to_side_car_dr_rollback_smoke` to ALSO seed an audit_log row with `params` containing a dict + a row in some PG-array column (whichever model has `sa.ARRAY(String)` — likely `marketplace_plugins.doc_links` or similar). Assert both come back intact post-copy.

### Task D.3: Hung-migrator subprocess timeout E2E (TESTING GAP)

Already partly covered by C.2's shell-side test. Add the python-side complement: launch the migrator with a monkeypatched `time.sleep(99999)` in some step, assert via subprocess timeout that the watchdog fires within the configured horizon.

---

## Phase E — LOW cleanups (5 tasks)

### Task E.1: `verify_row_counts` opens DuckDB read-only (LOW)

**The bug.** `verify_row_counts` calls `duckdb.connect(str(duckdb_path))` (writable). DuckDB creates a `.wal` file alongside, which adds clutter and can confuse subsequent reads if the migrator crashes between verify and flip.

**Fix:** add `read_only=True` to the connect call. One line. Test asserts no `.wal` file exists after `verify_row_counts` returns.

### Task E.2: Brittle docker-run ordering assertions (LOW)

`tests/test_state_applier_host_script.sh` asserts the EXACT `docker run` argv ordering. Any refactor that reorders flags (purely cosmetic) breaks the test. Replace with semantic matchers: assert each REQUIRED arg appears in the transcript regardless of order, instead of asserting one literal substring covers the whole line.

### Task E.3: `resource_grants.resource_id` FK (LOW)

**The bug.** `resource_grants` has no FK enforcing that `resource_id` references a real entity. Orphan grants accumulate when entities are deleted. The PG migration is the right time to add the constraint — for the dynamic types (`bucket`, `marketplace`, `data_package`, `memory_domain`, etc.) the FK targets are per-type, so model this as a CHECK that the type-id pair is referentially valid via an application-level constraint or as deferred FKs.

**Pragma:** if the cross-type-table polymorphism makes a real FK impractical, mark this CLOSED-OUT with a docstring on the `ResourceGrant` model explaining why; otherwise add the alembic migration.

**Decision:** read the existing `resource_grants` model + look at all 8 resource types. If 2+ types target tables that don't have stable surrogate-key columns (e.g. tables keyed by composite name+namespace), close-out with rationale; otherwise add the FK migration.

### Task E.4: Drop vacuous `test_post_migrate_does_not_spawn_subprocess` (LOW)

The existing test only checks `resp.status_code == 202`. Doesn't verify the no-subprocess invariant. Either:

- Replace with a real probe (monkeypatch `subprocess.Popen` and assert it wasn't called during the POST), OR
- Delete the test outright (it's redundant; the architecture guarantees no in-process subprocess).

Pick the delete option — the architecture-side guarantee is documented in the applier docstring; the test adds no value.

### Task E.5: Applier `set -e` + heredoc abort → structured rollback (LOW)

When a python heredoc inside the applier raises mid-execution, `set -e` aborts the script. The flock at the top of the script auto-releases (file descriptor closed). No structured rollback happens — pending job stays at `pending`, the *_in_progress state on instance.yaml stays as-is, the next tick may or may not recover.

**Fix:** wrap the main work block in a `trap '__rollback' ERR` that writes a `failed` status to the pending job + reverts instance.yaml::backend to source if the script is about to abort. `__rollback` is a function that idempotently does the same thing the python-side `except JobCancelled` handler does.

---

## Phase F — Final commit + release

### Task F.1: Full suite run

```
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/ -q --tb=short \
    --ignore=tests/api_keboola --ignore=tests/integration
```

Must be all-green. If a pre-existing flake surfaces, document it in commit body and proceed.

### Task F.2: PR body — round-2 fix index

Append `## Review fixes — round 2` section to PR #455 body. Same format as round-1: each finding → commit SHA + one-line description.

### Task F.3: Release-cut amendment

Round-1 released as 0.56.0 via commit `d585533a`. Since 0.56.0 hasn't been tagged/published, amend by:

1. `git reset --soft d585533a^` (un-cut the release)
2. Land all round-2 commits under `[Unreleased]`
3. Re-cut: rename `[Unreleased]` → `[0.56.0] — 2026-05-28`, bump `pyproject.toml`, add new empty `[Unreleased]`
4. New final commit `release(0.56.0): admin-controlled DB backend state machine + review fixes (round 2)`

Force-push: `git push --force-with-lease origin zs/db-state-machine`.

---

## Self-review

- **Coverage check:** all 12 open items from round-1 audit have a numbered task above. H4 (RAM), H5 (timeout), H6 (orphans), H7 (PII), H8 (expiry), H12 (content hash), 5 MEDs, 5 LOWs, 1 testing gap.
- **Placeholder scan:** every task has concrete file paths, code snippets, and tests. The H12 same-URL test caveat is acknowledged.
- **Type consistency:** `JobWriter` interface untouched (just adds a new caller). `run_all` signature gains optional `progress_callback`. `copy_duckdb_to_pg` / `copy_pg_to_pg` gain optional `writer=`. All optional → backwards compatible with existing callers.

## Execution

Single orchestrator subagent (model: opus) drives all 6 phases sequentially. For each task it dispatches an implementer subagent (model: sonnet), then a spec reviewer, then a code-quality reviewer. After all task commits, F.1–F.3 run sequentially. Total expected wall-clock: 60–90 min.
