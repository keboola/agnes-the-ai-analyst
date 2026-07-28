# `_try_open_system_db` — refuse stale pre-migrate auto-recovery

Date: 2026-05-26
Issue: [#379](https://github.com/keboola/agnes-the-ai-analyst/issues/379)
Related (out of scope for this PR): #380 (rolling pre-migrate refresh), #381 (WAL salvage), #383 (operator runbook).
Related (confirmed fixed during audit): #382 — `stop_grace_period: 60s` is in `docker-compose.yml:50,93` and `close_system_db()` (`src/db.py:4538-4565`) CHECKPOINTs on shutdown, which uvicorn invokes via the FastAPI lifespan teardown on SIGTERM.

## Problem

`src/db.py::_try_open_system_db()` (lines 1103-1154) auto-restores from `system.duckdb.pre-migrate` whenever DuckDB raises a WAL-replay error class. The doc comment claims "The migration ladder is idempotent, so the second start re-runs it and ends up at the same SCHEMA_VERSION cleanly."

That is true for **schema**, false for **data**. `system.duckdb.pre-migrate` is captured once per migration transition (`src/db.py:4216`, inside the migration ladder) and never refreshed. After v(N-1)→v(N) ran, the snapshot is frozen at v(N-1). Any row data added between that moment and the WAL-recovery event is silently dropped when the recovery code copies the snapshot over the broken DB and re-runs the ladder.

A deployer hit this on 2026-05-21: 12 + 29 user-created rows lost when a fresh container start tripped WAL replay against a 2-days-old snapshot. The `schema_version.applied_at` rewrite also masked the destructive moment in forensics.

## Approach

Before unconditionally copying the snapshot over the broken DB, **inspect the snapshot's `schema_version` and refuse auto-recovery if it is older than the current `SCHEMA_VERSION` constant**. Move the broken DB + WAL aside as today (so the operator has the forensic artifact), but raise a `RuntimeError` instead of producing a fresh empty DB.

This converts "recover at any cost, lose data silently" into "recover when safe; otherwise fail loudly and let an operator decide." It is the single defensive change `#379` calls for. The rolling-refresh (#380), WAL salvage (#381), and operator runbook (#383) are out of scope — separate PRs each.

## Components

### 1. `_peek_schema_version(snapshot_path: Path) -> int`

New helper, defined in `src/db.py` near `_try_open_system_db`. Opens the snapshot **read-only** (`duckdb.connect(str(path), read_only=True)`) so DuckDB's WAL replay path is bypassed entirely — even if the snapshot itself has a stale WAL, the read-only handle ignores it. Selects `MAX(version) FROM schema_version`, returns the integer.

Conservative on error: any `duckdb.Error` (table missing, file corrupt, permission denied) returns `0`. `0` < `SCHEMA_VERSION` always, so an unreadable snapshot is treated as stale and refuses recovery. The operator gets the same loud failure either way.

The handle is closed in a `try/finally` so a partially-opened connection doesn't leak.

### 2. Inline check in `_try_open_system_db`

After the existing `snapshot.exists()` guard and before the existing `shutil.move(...) / shutil.copy2(...)` block, insert:

```python
snapshot_version = _peek_schema_version(snapshot)
if snapshot_version < SCHEMA_VERSION:
    # Preserve the broken DB + WAL the same way the happy path does,
    # so the operator gets a forensic artifact even when we refuse to
    # restore. The snapshot file is left untouched at its original path.
    broken = Path(db_path + f".broken.{int(time.time())}")
    shutil.move(db_path, str(broken))
    if wal_path.exists():
        shutil.move(str(wal_path), str(broken) + ".wal")
    logger.critical(
        "REFUSING auto-recovery: pre-migrate snapshot is at schema v%d, "
        "target is v%d. Auto-recovery would re-run the migration ladder "
        "and silently drop all rows added since v%d. Broken DB preserved "
        "at %s; broken WAL at %s.wal if it existed. Manual intervention "
        "required.",
        snapshot_version, SCHEMA_VERSION, snapshot_version,
        broken, broken,
    )
    raise RuntimeError(
        f"pre-migrate snapshot stale "
        f"(v{snapshot_version} < target v{SCHEMA_VERSION}); "
        f"auto-recovery refused. Broken DB at {broken}."
    )
```

Then the existing happy-path code runs (snapshot is at HEAD; safe to restore).

### Why `RuntimeError`, not `SystemExit(1)`

The existing function already raises `duckdb.Error` on the "no snapshot file" branch (line 1135). Using `RuntimeError` keeps the function's contract uniform: "I either return a connection or raise." The decision of whether to exit the process or render a graceful error page belongs to the caller, not the recovery routine.

### Logging level

`logger.critical(...)` — the highest level — because this is a data-loss-avoidance event. Operators relying on log-level alerting should see this immediately. The structured message includes both versions and the preserved broken-file paths so the operator can act without re-reading the source.

## Tests — `tests/test_db_wal_recovery.py`

Three tests, all using a `tmp_path` to fabricate a controlled (db_path + snapshot) pair. None of them need a real running app.

### Test 1: snapshot at HEAD → recovery proceeds (regression guard)

Set up: copy a synthetic DB (created by writing `schema_version` table with `SCHEMA_VERSION`) to both `db_path` and `db_path + ".pre-migrate"`, then corrupt the main DB's `.wal` with garbage to trigger the WAL-replay branch. Call `_try_open_system_db(db_path)`. Assert: returns a connection (no exception), broken DB is preserved at `.broken.<ts>`, main `db_path` is now a copy of the snapshot.

### Test 2: snapshot at v(N-1) → refuse with RuntimeError

Same fabrication, but write `SCHEMA_VERSION - 1` into the snapshot's `schema_version` table. Call `_try_open_system_db`. Assert: raises `RuntimeError` whose message mentions both version numbers; broken DB is preserved at `.broken.<ts>`; main `db_path` no longer exists (it was moved); snapshot file is untouched at its original location.

### Test 3: snapshot has no `schema_version` table → treat as stale

Same fabrication, but the snapshot is a DuckDB file with no `schema_version` table at all (simulates a pre-v1 / corrupt snapshot). `_peek_schema_version` returns 0 conservatively → 0 < SCHEMA_VERSION → same refusal path as Test 2.

### Fixture sketch

```python
def _make_db_with_schema_version(path: Path, version: int) -> None:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP)")
    conn.execute("INSERT INTO schema_version VALUES (?, current_timestamp)", [version])
    conn.close()


def _corrupt_wal_so_replay_fails(db_path: Path) -> None:
    """Write garbage to .wal that DuckDB will reject on replay."""
    (db_path.with_suffix(db_path.suffix + ".wal")).write_bytes(b"\x00" * 64)
```

Tests instantiate the fixtures, invoke the function, assert on the post-state. No mocks for DuckDB — uses real files end-to-end, which exercises the actual `read_only=True` peek path.

**On the "no mocks for DuckDB" intent.** The pre-existing tests in this file
already use `monkeypatch.setattr(duckdb, "connect", ...)` to inject the
WAL-replay error class — DuckDB 1.5 is resilient to short garbage WAL
content, so a file-level corruption alone doesn't reliably trigger the
recovery branch across DuckDB versions. Following that established pattern,
the new tests use a `make_wal_error` context-manager fixture that patches
only the first `duckdb.connect` call against `db_path`; subsequent calls
(including the read-only peek of the snapshot path the new code exercises)
hit real DuckDB. The "real files end-to-end" goal is preserved for the
peek path — only the corruption-trigger is mocked.

## Failure modes covered by this change

| Today | After this change |
|---|---|
| Stale snapshot + WAL replay fail → ladder re-runs against snapshot → all post-snapshot rows lost, `schema_version.applied_at` rewritten | RuntimeError raised, broken DB preserved, ops alerted |
| HEAD snapshot + WAL replay fail → ladder re-runs, no data loss (no-op replay) | Unchanged — happy path preserved |
| No snapshot file at all → `logger.error` + re-raise | Unchanged |
| Snapshot file exists but is corrupt / unreadable | NEW: `_peek_schema_version` returns 0 → refused as stale |

## Non-goals

- **Rolling pre-migrate refresh** — covered by #380; orthogonal fix that tightens the recovery RPO. Out of scope here.
- **WAL salvage before fallback** — covered by #381; gives operators per-table parquet to reconcile. Out of scope here.
- **Operator runbook** — covered by #383; the error message in this PR points the operator at the preserved file paths but doesn't reference a non-existent runbook.
- **Replacing the auto-recovery mechanism entirely** — keeping it as a safety net for the genuine in-migration-crash case it was built for.

## CHANGELOG + release-cut

- `### Fixed` bullet under `[Unreleased]`.
- Patch bump `0.55.10 → 0.55.11` in the final commit of the PR per CLAUDE.md.

## Acceptance

1. With a HEAD-version snapshot, the recovery path still produces a working DB (Test 1).
2. With a v(N-1) snapshot, the recovery refuses, preserves both broken files, and surfaces both version numbers in the log (Test 2).
3. With an unreadable snapshot, behavior collapses to the refusal path (Test 3).
4. Full pytest suite green on this branch.

---

## Amendment 2026-05-26 — review pass: symmetric refusal + mode 0o600 + manual-recovery hint

Three review findings addressed during the release-cut pass for `0.55.12`:

1. **Symmetric refusal (architecture review).** Original check was
   `peek_version < SCHEMA_VERSION`. The mirror case — operator rolls
   the binary back from v60 to v59, snapshot was captured at the
   v59→v60 transition (so snapshot.peek = v60) — would pass the
   one-sided check and let `shutil.copy2(snapshot, db_path)` land a
   v60-schema DB under a v59 binary. The next start's `_ensure_schema`
   would then hit the split-brain `current > target` branch (which is
   itself a no-op per `src/db.py:4251`, leaving the DB in an unsupported
   state). Both directions mean corruption; the check is now `peek_version
   != SCHEMA_VERSION` with a `direction` ("stale" / "future") tag in
   both the log line and the `RuntimeError` message so operators can
   tell which incident class they hit.

2. **`os.chmod(0o600)` on `.broken.<ts>` artifacts (RBAC review).**
   `system.duckdb` holds argon2 password hashes (`users.password_hash`),
   personal-access-token rows (`personal_access_tokens`), and the audit
   log. `shutil.move` inherits the source mode (typically `0o644` under
   default umask), so the preserved broken file was world-readable on
   its way out. The new `_move_to_broken(db_path, wal_path)` helper
   (extracted from the inline duplication of the same move pattern in
   the refusal path and the happy auto-recovery path) chmods both the
   `.broken.<ts>` and `.broken.<ts>.wal` to `0o600` via best-effort
   `try/except OSError: pass`. The containing `state/` directory is
   typically `0o700` already, but defense in depth matters because
   backups, container volumes, and tab-completion mistakes can all
   surface the file. The helper is exercised by a new test
   `test_recovery_broken_files_are_chmod_0600`.

3. **Manual-recovery escape hatch (architecture review).** Original
   `RuntimeError` named the broken-DB path but not the recovery
   action; an operator had to read the source to figure out the manual
   `cp` step. Both the `logger.critical` message and the
   `RuntimeError` text now include `cp {snapshot} {db_path}` verbatim
   so the operator's runbook is in the failure itself.

The new `test_recovery_refuses_when_snapshot_is_from_future_version`
test locks in the symmetric-refusal contract; the existing
`test_recovery_refuses_when_snapshot_has_no_schema_version_table` test
assertion was tightened from `"v0 <"` (no longer matches the new
direction-tagged format) to `"v0" + "stale"` checks on the message.

A separate follow-up is worth filing for `.broken.<ts>` retention
policy: argon2 hashes don't expire, so indefinite retention of broken
artifacts becomes a long-lived credential archive. Currently undocumented;
likely future issue.
