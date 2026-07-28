# system.duckdb resilience — design

**Date:** 2026-06-01
**Status:** approved (design), pending implementation plan
**Area:** `src/db.py`, `src/fts.py`

## Problem

On a memory-constrained deployment (e.g. a 4 GiB container), the FastAPI/uvicorn
process is OOM-killed by the kernel cgroup, restarts, and on restart the
`system.duckdb` open can fail during WAL replay — which today triggers a
*destructive* recovery that rolls the database back to a stale snapshot,
silently losing admin state (data packages, RBAC grants, group members)
written since that snapshot. Two independent defects combine into a
data-loss incident:

1. **Unbounded memory → OOM kill (the trigger).** DuckDB sizes
   `memory_limit` per-connection, not per-process. The analytics
   connection and the per-request read-only connections are capped (2 GiB
   each, added in PR #434), but the **`system.duckdb` singleton connection
   is never capped** — it runs at DuckDB's default (~80% of the cgroup
   limit). The per-connection budgets do not sum to fit the container:
   `system (~default) + analytics (2 GiB) + N×readonly (2 GiB each)` can
   exceed the cgroup cap. A heavy aggregation over the append-only
   telemetry / audit / usage tables on the uncapped system connection,
   concurrent with the analytics working set, pushes process RSS past the
   cap → kernel OOM-kill. (DuckDB 1.5.x *is* cgroup-aware — a fresh
   connection in a 4 GiB container defaults to ~3.1 GiB — so the fix is
   explicit conservative per-connection caps that sum under the cap, not
   reading the cgroup ourselves.)

2. **WAL replay fails on FTS DDL (the corruption path).** The
   knowledge-item BM25 search rebuilds its FTS index on every `search()`
   call with `create_fts_index(..., overwrite=1)`, which DROPs+CREATEs the
   `fts_main_knowledge_items` schema on the **long-lived system
   connection**. Those DDL operations land in `system.duckdb.wal`. When
   the process is killed (OOM, or a deploy with a short stop grace) before
   the next checkpoint, the WAL persists. On restart, DuckDB's WAL replay
   raises `Failure while replaying WAL … Cannot drop entry
   "fts_main_knowledge_items" because there are entries that depend on it`
   — a drop-ordering failure in FTS schema replay.

3. **Recovery is destructive (the amplifier).** `_try_open_system_db`
   catches the generic WAL-replay error class and restores the
   `system.duckdb.pre-migrate` snapshot, moving the live file aside to
   `system.duckdb.broken.<ts>`. The pre-migrate snapshot is only refreshed
   at migration time, so between migrations it can be days old. The live
   file's last checkpoint is almost always newer — so the rollback throws
   away far more data than necessary.

## Goals

- The instance must **survive an unclean kill in a 4 GiB container without
  rolling back to a stale snapshot** — at most lose transactions written
  since the last checkpoint, never days of admin state.
- The instance must **stop OOM-killing itself** under normal dashboard /
  telemetry / query load in a 4 GiB container.
- No schema migration. Mirror the existing capped-connection pattern.
- Vendor-agnostic: no deployment-specific values in code or docs.

## Non-goals

- Reworking the FTS index lifecycle (moving it off `system.duckdb`, or
  rebuilding only on corpus change) — larger change, deferred.
- A process-global DuckDB memory budget across connections (DuckDB has no
  such primitive) — we approximate it with summed per-connection caps +
  disk spill.
- Raising the container memory limit — an operator stopgap, not part of
  this code change. The fix must work within 4 GiB.

## Design

Three focused changes, all in the `system.duckdb` open/use path.

### Change 1 — non-destructive WAL recovery (`src/db.py`, `_try_open_system_db`)

Insert a salvage step before the existing pre-migrate rollback. New
decision tree on a WAL-replay error:

```
duckdb.connect(db) raises WAL-replay error class?
 ├─ no / other error → raise (unchanged)
 └─ yes:
     STEP A [new] — discard ONLY the WAL, keep the live file:
        move <db>.wal → <db>.wal.discarded.<ts> (chmod 600, preserved for forensics)
        retry duckdb.connect(db)            # opens at the live file's last checkpoint
        ├─ success → log warning (lost only post-checkpoint txns), return conn
        └─ failure → the main file itself is unreadable:
     STEP B [existing] — pre-migrate fallback:
        _move_to_broken(db, wal)            # wal already moved; move_to_broken skips if absent
        version-guard (#379) + copy pre-migrate → db + reopen
```

**Correctness:** the live file's last checkpoint timestamp is always ≥ the
pre-migrate snapshot timestamp (checkpoints run continuously; pre-migrate
is captured only at migrations), so discard-WAL loses ≤ the data the
current rollback loses. It also handles the original mid-migration case:
if the WAL held an uncommitted migration `ALTER`, discarding it leaves the
file at the pre-migration schema version and the idempotent migration
ladder re-runs forward on the same start. The `#379` version-guard stays,
relevant only on the Step B fallback. Empirically validated: the
incident's `.broken.<ts>` file opened cleanly read-only *without* its WAL
and contained the full pre-crash state.

### Change 2 — checkpoint after FTS (re)create (`src/fts.py`, `ensure_knowledge_fts_index`)

After a successful `PRAGMA create_fts_index(...)`, issue a best-effort
`CHECKPOINT` so the FTS DDL is flushed into the main file and never lingers
in the WAL to be replayed after an unclean kill. Best-effort: wrap in
`try/except duckdb.Error`, log at debug, still return `True` (a checkpoint
failure — e.g. a concurrent write txn — must not break search). Search is
a low-frequency admin/analyst path over a small corpus, so the checkpoint
cost is negligible. This removes the unreplayable WAL content *at the
source*; Change 1 is the safety net if it still occurs.

### Change 3 — cap the system connection + global budget + spill (`src/db.py`)

In `get_system_db()`, after `_try_open_system_db()` and before
`_ensure_schema()`, apply the same defensive caps the analytics path uses,
wrapped in `try/except` with a warning on failure:

```python
SET memory_limit='1GB'              # system DB = metadata + telemetry; 1 GiB is generous
SET threads=2
SET preserve_insertion_order=false
```

Budget so the realistic concurrent sum fits under a 4 GiB cgroup with
headroom for the host process:

| Connection | cap | rationale |
|---|---|---|
| system (singleton) | **1 GiB** | metadata + telemetry aggregations |
| analytics (singleton) | **1.5 GiB** | lowered from 2 GiB |
| analytics readonly (per request) | **1 GiB** | lowered from 2 GiB |

Plus a **disk-spill safety net** on each connection so a query exceeding
its budget spills to disk instead of growing RSS (or surfaces a clean
DuckDB error) rather than OOM-killing the process:

```python
SET temp_directory='<state-dir>/duckdb-tmp'
SET max_temp_directory_size='<bounded, e.g. 10GB>'
```

The exact numbers are tunable; the invariant is `system + analytics +
one readonly + host headroom < cgroup cap`, with spill as backstop for
overshoot. Centralize the cap PRAGMAs for the three `src/db.py`
connections (system, analytics, readonly) into one helper so the budget
lives in a single place. The connector/profiler extract-time connections
(`connectors/keboola/extractor.py`, `connectors/bigquery/access.py`,
`src/profiler.py`) also hardcode `'2GB'` but are short-lived and
out-of-process for the app's RSS — unifying them is an optional follow-up,
out of scope here to keep the diff focused.

## Components & boundaries

- `_try_open_system_db(db_path)` — owns open + recovery. New private
  helper `_salvage_discard_wal(db_path, wal_path) -> conn | None` keeps the
  salvage step testable in isolation.
- `_move_to_broken` — unchanged (already tolerates an absent WAL).
- `ensure_knowledge_fts_index(conn)` — adds the post-create checkpoint;
  signature/return contract unchanged.
- A new `_apply_connection_caps(conn, *, memory, threads=2)` helper (or
  module constants) centralizes the cap PRAGMAs used by system, analytics,
  readonly, and the connector/profiler paths.

## Error handling

- Change 1: each branch logged; discarded WAL preserved chmod 600;
  broken files chmod 600 (unchanged); if Step B also fails, propagate
  (unchanged — auto-recovery exhausted).
- Change 2: checkpoint failure → debug log, proceed.
- Change 3: cap PRAGMA failure → warning log, proceed with default
  (mirrors the existing analytics path's `except`).

## Testing (TDD)

`_try_open_system_db` (fake `duckdb.connect` via monkeypatch):
- WAL-replay error → Step A discard-WAL succeeds → returns the live-file
  connection; pre-migrate snapshot is **not** touched; live file is **not**
  moved to `.broken`; WAL preserved as `.wal.discarded.<ts>`.
- WAL-replay error → Step A reopen also fails → falls through to Step B
  pre-migrate restore with the version-guard (existing behavior preserved).
- Non-WAL error → raises (unchanged).
- Real reproduction if it can be made deterministic: build an FTS index,
  snapshot db+wal mid-flight to simulate a kill, assert the new path opens
  without data loss.

`ensure_knowledge_fts_index`:
- After a successful create, a `CHECKPOINT` is issued (spy / assert WAL
  flushed).
- A `CHECKPOINT` that raises does not break the function (still `True`).

`get_system_db` caps:
- After open, `current_setting('memory_limit')` on the system connection
  reflects the configured cap (not the DuckDB default).
- Cap-PRAGMA failure is swallowed (connection still returned).

Run the full suite before pushing: `.venv/bin/pytest tests/ --tb=short -n auto -q`.

## Files touched

- `src/db.py` — recovery salvage step; system-connection caps; centralized
  cap helper.
- `src/fts.py` — post-create checkpoint.
- `tests/` — new tests for the above.
- `CHANGELOG.md` — `Fixed` bullet under `[Unreleased]`.

No schema migration. No config-file changes (caps are code defaults).
