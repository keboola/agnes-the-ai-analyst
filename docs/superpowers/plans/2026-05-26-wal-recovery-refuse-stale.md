# WAL recovery — refuse stale snapshot (#379) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop silent data loss in `src/db.py::_try_open_system_db` by refusing auto-recovery when the `system.duckdb.pre-migrate` snapshot is older than the current `SCHEMA_VERSION`.

**Architecture:** Add a `_peek_schema_version()` helper that opens the snapshot read-only (bypassing WAL replay), reads its schema version, and returns it. Inline a check in `_try_open_system_db` before the snapshot-copy step: if the snapshot is older than `SCHEMA_VERSION`, preserve both broken files (DB + WAL) and raise `RuntimeError` with both version numbers in the message. Happy path (HEAD-version snapshot) and the "no snapshot" path are unchanged.

**Tech Stack:** Python 3.13 / DuckDB / pytest (existing repo deps).

**Spec:** `docs/superpowers/specs/2026-05-26-wal-recovery-refuse-stale-design.md`

---

## File map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `src/db.py` (≈10 lines for helper near `_try_open_system_db`, ≈18 lines inline in the function) | Add `_peek_schema_version()` helper; gate snapshot restore by version check |
| Create | `tests/test_db_wal_recovery.py` (≈100 lines, 3 tests + fixtures) | Regression guard for happy path + verify refusal-with-preserve behavior on stale or unreadable snapshots |
| Modify | `CHANGELOG.md` (1 bullet, then promote `[Unreleased]` → `[0.55.11]` at the end) | Per CLAUDE.md release-cut rule |
| Modify | `pyproject.toml` (1 line) | `version = "0.55.10"` → `"0.55.11"` |

---

## Conventions used across multiple tasks

- pytest invocation: `/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest` (the worktree has no `.venv` of its own; use the parent checkout's).
- The test file uses `tmp_path` (built-in pytest fixture) for DB-on-disk fixtures. No mocks for DuckDB — real files end-to-end so the read-only peek path is actually exercised.
- The `_try_open_system_db` function lives at `src/db.py:1103-1154` today; current `SCHEMA_VERSION` is `59` (`src/db.py:43`).

---

## Task 1 — Test fixtures + first failing test (happy path regression guard)

**Files:**
- Create: `tests/test_db_wal_recovery.py`

We start with the happy-path test even though it should already pass against current code, because:
1. It's a regression guard — Task 2's stale-snapshot change must not break it.
2. The fixtures it introduces (`_make_db_with_schema_version`, `_corrupt_wal_so_replay_fails`) are reused by the next two tests.

- [ ] **Step 1.1: Create `tests/test_db_wal_recovery.py` with fixtures + happy-path test**

```python
"""Contract tests for src/db.py::_try_open_system_db.

Covers the schema-version-aware refusal added for issue #379:
auto-recovery proceeds when the pre-migrate snapshot is at HEAD, but
raises RuntimeError when the snapshot is stale (older than
SCHEMA_VERSION) or unreadable.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


def _make_db_with_schema_version(path: Path, version: int) -> None:
    """Create a fresh DuckDB file containing a `schema_version` table
    with the given version row. Mirrors the shape `_peek_schema_version`
    expects."""
    conn = duckdb.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (?, current_timestamp)", [version]
        )
    finally:
        conn.close()


def _make_db_no_schema_version_table(path: Path) -> None:
    """Create a DuckDB file with some other table but no `schema_version`
    — simulates a pre-v1 / structurally-foreign snapshot."""
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE other (id INTEGER)")
    finally:
        conn.close()


def _corrupt_wal_so_replay_fails(db_path: Path) -> None:
    """Write garbage to <db_path>.wal so DuckDB's WAL replay aborts on
    next open with one of the error strings `_try_open_system_db`
    recognises ('Failure while replaying WAL', 'ReplayAlter', or
    'GetDefaultDatabase with no default database set')."""
    wal = Path(str(db_path) + ".wal")
    wal.write_bytes(b"\x00" * 64)


def test_recovery_proceeds_when_snapshot_is_at_head(tmp_path, monkeypatch):
    """Regression guard: with a HEAD-version snapshot, recovery returns
    a working connection, preserves the broken DB at .broken.<ts>, and
    replaces the main DB with the snapshot."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION)
    _corrupt_wal_so_replay_fails(db_path)

    conn = db_module._try_open_system_db(str(db_path))
    try:
        # The recovered DB carries the snapshot's schema_version row.
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == db_module.SCHEMA_VERSION
    finally:
        conn.close()

    # Broken DB was preserved alongside.
    broken_files = list(tmp_path.glob("system.duckdb.broken.*"))
    assert len(broken_files) == 1, broken_files
    # Snapshot file is left in place (not consumed).
    assert snapshot_path.exists()
```

- [ ] **Step 1.2: Run test and confirm it passes against current code**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/test_db_wal_recovery.py::test_recovery_proceeds_when_snapshot_is_at_head -v
```

Expected: PASS. If it fails, that's a real defect in current behavior — surface it before continuing.

- [ ] **Step 1.3: Commit fixtures + regression guard**

```bash
git add tests/test_db_wal_recovery.py
git commit -m "test(db): regression guard for WAL recovery happy path (#379)"
```

---

## Task 2 — Stale-snapshot test + implementation (TDD)

**Files:**
- Modify: `tests/test_db_wal_recovery.py` (append second test)
- Modify: `src/db.py` (add helper + inline check)

- [ ] **Step 2.1: Append the failing test for stale snapshot**

Add to `tests/test_db_wal_recovery.py`:

```python
def test_recovery_refuses_when_snapshot_is_stale(tmp_path, monkeypatch):
    """Snapshot at SCHEMA_VERSION - 1 → recovery raises RuntimeError,
    preserves both broken files, leaves snapshot untouched, does NOT
    create a fresh DB at db_path."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION - 1)
    _corrupt_wal_so_replay_fails(db_path)

    with pytest.raises(RuntimeError) as excinfo:
        db_module._try_open_system_db(str(db_path))

    # The error message identifies both versions so the operator can act.
    msg = str(excinfo.value)
    assert str(db_module.SCHEMA_VERSION - 1) in msg
    assert str(db_module.SCHEMA_VERSION) in msg

    # Broken DB preserved.
    broken_dbs = list(tmp_path.glob("system.duckdb.broken.*"))
    broken_dbs = [p for p in broken_dbs if not p.name.endswith(".wal")]
    assert len(broken_dbs) == 1, broken_dbs

    # Broken WAL preserved alongside.
    broken_wals = list(tmp_path.glob("system.duckdb.broken.*.wal"))
    assert len(broken_wals) == 1, broken_wals

    # Snapshot was not consumed.
    assert snapshot_path.exists()

    # Main DB path no longer exists (it was moved aside, NOT overwritten
    # by the snapshot).
    assert not db_path.exists()
```

- [ ] **Step 2.2: Run the test and confirm it fails**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/test_db_wal_recovery.py::test_recovery_refuses_when_snapshot_is_stale -v
```

Expected: FAIL. Current code copies the snapshot over `db_path` and returns a connection — no RuntimeError. The assertion either `pytest.raises(RuntimeError)` fails or `assert not db_path.exists()` fails depending on which check fires first.

- [ ] **Step 2.3: Add `_peek_schema_version` helper to `src/db.py`**

Locate `_try_open_system_db` at `src/db.py:1103` (search for `def _try_open_system_db`). Insert this helper immediately ABOVE it (so the function order reads helper → user):

```python
def _peek_schema_version(snapshot_path: Path) -> int:
    """Open a DuckDB snapshot read-only and return its
    ``MAX(schema_version.version)``.

    Read-only mode bypasses WAL replay entirely — even if the snapshot
    has its own stale WAL, the read-only handle ignores it. Any
    ``duckdb.Error`` (table missing, file corrupt, permission denied)
    is treated conservatively as version 0, so an unreadable snapshot
    fails the freshness check in :func:`_try_open_system_db` and ends
    in the refusal path. Defensive: never returns -1 / None / raises.
    """
    try:
        conn = duckdb.connect(str(snapshot_path), read_only=True)
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except duckdb.Error:
        return 0
```

- [ ] **Step 2.4: Add the inline freshness check inside `_try_open_system_db`**

Find this block at `src/db.py:1136-1151`:

```python
        wal_path = Path(db_path + ".wal")
        logger.warning(
            "WAL replay failed (%s) — auto-restoring from pre-migrate "
            "snapshot %s. The migration ladder will re-run on this start.",
            msg.split("\n", 1)[0][:200],
            snapshot,
        )
        # Move (not copy) the broken DB aside so an operator can post-
        # mortem if needed. The pre-migrate snapshot becomes the new
        # main DB; the WAL is dropped (its content is what failed to
        # replay).
        broken = Path(db_path + f".broken.{int(time.time())}")
        shutil.move(db_path, str(broken))
        if wal_path.exists():
            shutil.move(str(wal_path), str(broken) + ".wal")
        shutil.copy2(str(snapshot), db_path)
```

Insert the freshness gate immediately AFTER `wal_path = Path(db_path + ".wal")` and BEFORE the `logger.warning(...)` happy-path log. The full replacement block for the section becomes:

```python
        wal_path = Path(db_path + ".wal")

        # #379: refuse auto-recovery if the snapshot is older than the
        # current SCHEMA_VERSION. The migration ladder is idempotent
        # for schema but not for data; re-running it against a stale
        # snapshot silently drops every row added since the snapshot
        # was captured. Better to fail loudly and let an operator
        # decide. The broken DB + WAL are preserved either way.
        snapshot_version = _peek_schema_version(snapshot)
        if snapshot_version < SCHEMA_VERSION:
            broken = Path(db_path + f".broken.{int(time.time())}")
            shutil.move(db_path, str(broken))
            if wal_path.exists():
                shutil.move(str(wal_path), str(broken) + ".wal")
            logger.critical(
                "REFUSING auto-recovery: pre-migrate snapshot is at "
                "schema v%d, target is v%d. Auto-recovery would re-run "
                "the migration ladder and silently drop all rows added "
                "since v%d. Broken DB preserved at %s; broken WAL at "
                "%s.wal if it existed. Manual intervention required.",
                snapshot_version, SCHEMA_VERSION, snapshot_version,
                broken, broken,
            )
            raise RuntimeError(
                f"pre-migrate snapshot stale "
                f"(v{snapshot_version} < target v{SCHEMA_VERSION}); "
                f"auto-recovery refused. Broken DB at {broken}."
            )

        logger.warning(
            "WAL replay failed (%s) — auto-restoring from pre-migrate "
            "snapshot %s. The migration ladder will re-run on this start.",
            msg.split("\n", 1)[0][:200],
            snapshot,
        )
        # Move (not copy) the broken DB aside so an operator can post-
        # mortem if needed. The pre-migrate snapshot becomes the new
        # main DB; the WAL is dropped (its content is what failed to
        # replay).
        broken = Path(db_path + f".broken.{int(time.time())}")
        shutil.move(db_path, str(broken))
        if wal_path.exists():
            shutil.move(str(wal_path), str(broken) + ".wal")
        shutil.copy2(str(snapshot), db_path)
```

- [ ] **Step 2.5: Run both tests and confirm both pass**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/test_db_wal_recovery.py -v
```

Expected: 2 passed.

- [ ] **Step 2.6: Commit**

```bash
git add src/db.py tests/test_db_wal_recovery.py
git commit -m "fix(db): refuse WAL auto-recovery when pre-migrate snapshot is stale (#379)"
```

---

## Task 3 — Unreadable snapshot test (TDD; behavior already covered)

**Files:**
- Modify: `tests/test_db_wal_recovery.py` (append third test)

The implementation in Task 2 handles this case (because `_peek_schema_version` returns `0` on any `duckdb.Error`, and `0 < SCHEMA_VERSION` always). We add the explicit test to lock that contract.

- [ ] **Step 3.1: Append the test**

```python
def test_recovery_refuses_when_snapshot_has_no_schema_version_table(
    tmp_path, monkeypatch
):
    """If the snapshot is a DuckDB file with no `schema_version` table
    at all (pre-v1 / unrelated DB), _peek_schema_version returns 0;
    recovery refuses via the same code path as test_..._is_stale."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_no_schema_version_table(snapshot_path)
    _corrupt_wal_so_replay_fails(db_path)

    with pytest.raises(RuntimeError) as excinfo:
        db_module._try_open_system_db(str(db_path))

    # v0 surfaces in the message (the conservative fallback value).
    msg = str(excinfo.value)
    assert "v0" in msg
    assert str(db_module.SCHEMA_VERSION) in msg

    # Same preservation contract as the stale case.
    assert not db_path.exists()
    assert snapshot_path.exists()
    assert any(tmp_path.glob("system.duckdb.broken.*"))
```

- [ ] **Step 3.2: Run all three tests**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/test_db_wal_recovery.py -v
```

Expected: 3 passed.

- [ ] **Step 3.3: Commit**

```bash
git add tests/test_db_wal_recovery.py
git commit -m "test(db): lock contract — corrupt snapshot treated as stale (#379)"
```

---

## Task 4 — Full test suite + CHANGELOG + release-cut

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`

- [ ] **Step 4.1: Run the full pytest suite**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: full suite green. Should be 5252 baseline + 3 new tests = 5255 passed (give or take if other changes have landed in main since). Failures in code you touched: fix before moving on. Failures unrelated to your diff: confirm with `git stash` they reproduce on a clean branch.

- [ ] **Step 4.2: Edit `CHANGELOG.md`**

Find the `[Unreleased]` section and the `[0.55.10] — 2026-05-25` heading below it. Replace this block:

```markdown
## [Unreleased]

## [0.55.10] — 2026-05-25
```

with:

```markdown
## [Unreleased]

## [0.55.11] — 2026-05-26

### Fixed
- **`src/db.py::_try_open_system_db` no longer silently drops post-migration data on WAL-replay recovery (#379).** The auto-recovery path used to copy `system.duckdb.pre-migrate` over the broken DB and re-run the migration ladder unconditionally — but the snapshot is captured once per migration transition and never refreshed, so any rows added since that transition vanished without warning. The function now opens the snapshot read-only to peek its `schema_version`; if it is older than the current `SCHEMA_VERSION`, the broken DB + WAL are preserved at `.broken.<ts>` (forensic artifact) and a `RuntimeError` is raised so operators see the failure instead of an empty DB. The happy-path (HEAD-version snapshot) and the "no snapshot file" path are unchanged.

## [0.55.10] — 2026-05-25
```

- [ ] **Step 4.3: Edit `pyproject.toml`**

Change line 3 from:

```toml
version = "0.55.10"
```

to:

```toml
version = "0.55.11"
```

- [ ] **Step 4.4: Re-run the full suite as a final sanity check**

```bash
/Users/zdeneksrotyr/Sources/VsCode/component_factory/tmp_oss/.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: still green.

- [ ] **Step 4.5: Commit the release-cut**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "release: 0.55.11 — refuse stale WAL-recovery snapshot"
```

---

## Task 5 — Push + PR

**Files:** none modified.

- [ ] **Step 5.1: Push the branch under a clean name**

```bash
git push -u origin worktree-zs+wal-recovery-fix:zs/wal-recovery-fix
```

- [ ] **Step 5.2: Open the PR**

```bash
gh pr create --base main --head zs/wal-recovery-fix \
  --title "fix(db): refuse stale pre-migrate auto-recovery in _try_open_system_db (#379) + release 0.55.11" \
  --body "Closes #379.

## Why

\`src/db.py::_try_open_system_db\` auto-restores from \`system.duckdb.pre-migrate\` whenever WAL replay fails on DB open. The snapshot is captured **once** per migration transition and never refreshed, so any row data added between that moment and the WAL-recovery event is silently dropped when the recovery path copies the snapshot over the broken DB and re-runs the migration ladder.

A deployer hit this on 2026-05-21: 12 + 29 rows lost, \`schema_version.applied_at\` rewrite masking the destructive moment in forensics.

## Fix

Inspect the snapshot's \`schema_version\` before copying. If it is older than the current \`SCHEMA_VERSION\`, preserve both broken files (DB + WAL at \`.broken.<ts>\`) and raise \`RuntimeError\` with both version numbers in the message. Happy path (HEAD-version snapshot) and the \"no snapshot\" path are unchanged.

- New \`_peek_schema_version()\` helper opens the snapshot read-only (bypasses WAL replay), reads \`MAX(version)\`, returns 0 conservatively on any \`duckdb.Error\` (so corrupt / pre-v1 snapshots also fail the freshness check).
- Inline check in \`_try_open_system_db\` between the existing \`snapshot.exists()\` guard and the existing \`shutil.move\` / \`shutil.copy2\` block.
- Three unit tests in \`tests/test_db_wal_recovery.py\`: happy-path regression guard, stale-snapshot refusal, corrupt-snapshot refusal.

## Out of scope (separate issues)

- #380 — rolling pre-migrate refresh (tightens the recovery RPO).
- #381 — WAL salvage before fallback (per-table parquet for manual reconciliation).
- #383 — operator runbook at \`docs/runbooks/wal-recovery.md\`.
- #382 — \`stop_grace_period: 60s\` + CHECKPOINT-on-SIGTERM: closed during audit; both are already in place (\`docker-compose.yml:50,93\` + \`close_system_db\` CHECKPOINT in \`src/db.py:4538-4565\` runs from the FastAPI lifespan teardown that uvicorn invokes on SIGTERM).

## Spec + plan

- Spec: \`docs/superpowers/specs/2026-05-26-wal-recovery-refuse-stale-design.md\`
- Plan: \`docs/superpowers/plans/2026-05-26-wal-recovery-refuse-stale.md\`

## Release-cut

Patch \`0.55.10 → 0.55.11\` in the same PR per the CLAUDE.md release-cut rule.

## Verification

- Full pytest suite green locally (3 new tests added).
- The refusal path was exercised end-to-end via the stale-snapshot test (real DuckDB files on \`tmp_path\`, real read-only peek, real \`shutil.move\`)."
```

- [ ] **Step 5.3: Confirm PR opened cleanly**

```bash
gh pr view --json url --jq .url
gh pr checks
```

If \`release.yml\` reports the cancelled-by-concurrency artefact from the create+push double-trigger (same pattern as #389 and #420 had), re-run the cancelled run:

```bash
RUN_ID=$(gh run list --workflow=release.yml --branch=zs/wal-recovery-fix --limit 5 --json databaseId,event,conclusion --jq '[.[] | select(.event == "push" and .conclusion == "cancelled")][0].databaseId')
if [[ -n "$RUN_ID" ]]; then gh run rerun "$RUN_ID"; fi
```

---

## Self-review

**Spec coverage:**
- ✅ `_peek_schema_version` helper (spec §1) → Task 2 Step 2.3.
- ✅ Inline freshness check (spec §2) → Task 2 Step 2.4.
- ✅ `RuntimeError`, not `SystemExit(1)` (spec §"Why `RuntimeError`") → Task 2 Step 2.4 (raise RuntimeError) + Task 2 Step 2.1 (test asserts `pytest.raises(RuntimeError)`).
- ✅ `logger.critical(...)` with both versions + broken-file path (spec §"Logging level") → Task 2 Step 2.4.
- ✅ Test 1 (happy path) → Task 1.
- ✅ Test 2 (stale snapshot) → Task 2 Step 2.1.
- ✅ Test 3 (unreadable / missing schema_version table) → Task 3.
- ✅ Fixture sketch (`_make_db_with_schema_version`, `_corrupt_wal_so_replay_fails`) → Task 1 Step 1.1.
- ✅ Failure-modes table (spec §"Failure modes") → exercised by Tests 1-3.
- ✅ Non-goals (no #380/#381/#383) → out-of-scope in PR body and noted in commit messages.
- ✅ CHANGELOG `### Fixed` bullet + `0.55.10 → 0.55.11` (spec §"CHANGELOG + release-cut") → Task 4.

**Placeholder scan:** None. Every code block is complete and copy-pasteable. Commit messages are concrete.

**Type / name consistency:**
- `_peek_schema_version(snapshot_path: Path) -> int` — same name and signature in Task 2 Step 2.3 (helper) and Task 2 Step 2.4 (call site).
- `snapshot_version` — same variable name in implementation (Task 2 Step 2.4), the spec, and the test assertion (Task 2 Step 2.1 asserts on `str(db_module.SCHEMA_VERSION - 1)` in the error message, which the implementation produces).
- `SCHEMA_VERSION` — imported once at module level in `src/db.py:43`; tests reference `db_module.SCHEMA_VERSION` consistently.
- `system.duckdb.pre-migrate`, `.broken.<ts>`, `.broken.<ts>.wal` — filename forms identical across implementation, tests, CHANGELOG bullet, PR body.

**File paths:** all absolute or repo-relative-from-root, no `../` traversal.
