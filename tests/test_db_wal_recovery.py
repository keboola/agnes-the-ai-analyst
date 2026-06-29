"""WAL-replay auto-recovery for ``system.duckdb``.

Reproduces the production failure observed during PR #217's v27
rollout: a container kill mid-migration leaves an unflushed
``ALTER TABLE … ADD COLUMN`` op in ``system.duckdb.wal``. On the next
start, DuckDB's ``ReplayAlter`` path raises
``INTERNAL Error: Calling DatabaseManager::GetDefaultDatabase with no
default database set`` and the system database becomes unrecoverable
from the running binary — the operator has to restore from the
pre-migrate snapshot by hand.

The recovery is layered:
  1. ``_ensure_schema`` runs ``CHECKPOINT`` immediately after the
     migration ladder so a fresh ALTER doesn't sit in the WAL beyond
     the migration window. Tested implicitly by every migration test
     that survives a process restart between fixture runs (covered by
     the existing v25→v26→v27 tests).
  2. STEP A — ``_try_open_system_db`` catches the WAL-replay error class
     and first tries to SALVAGE the live file: discard only the
     unreplayable WAL (``_salvage_discard_wal``) and reopen at the last
     checkpoint. This preserves everything up to the checkpoint — far
     more than a snapshot rollback — and is the common path.
  3. STEP B — only if the live file itself won't reopen does it fall
     back to ``system.duckdb.pre-migrate`` (with the #379 version guard).

Issue #379 coverage: schema-version-aware refusal — the Step B fallback
proceeds when the pre-migrate snapshot is at HEAD, but raises
RuntimeError when the snapshot is stale (older than SCHEMA_VERSION) or
unreadable. The Step B tests force Step A to fail (``fail_times=2``) so
the snapshot path is reached.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """Point DATA_DIR at a fresh tmp dir so each test gets its own
    state/system.duckdb without bleed."""
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data))
    # Clear any cached connection from earlier tests in the same process.
    import src.db as db_mod
    db_mod._system_db_conn = None
    db_mod._system_db_path = None
    yield data / "state"
    db_mod._system_db_conn = None
    db_mod._system_db_path = None


@pytest.fixture
def make_wal_error():
    """Return a context-manager factory that patches ``src.db.duckdb.connect``
    so the first call to ``db_path`` raises a WAL-replay error (the class
    ``_try_open_system_db`` specifically handles) and subsequent calls
    delegate to the real ``duckdb.connect``.

    ``fail_times`` controls how many opens of ``db_path`` raise the
    WAL-replay error before delegating to the real connect. Use the
    default (1) to exercise the Step A salvage (initial open fails, the
    post-WAL-discard reopen succeeds). Use 2 to force Step A to fail too
    (initial open + salvage reopen both fail) so the test reaches the
    Step B pre-migrate fallback.

    Usage inside a test::

        def test_foo(tmp_path, make_wal_error):
            db_path = tmp_path / "system.duckdb"
            ...
            with make_wal_error(db_path, fail_times=2):
                conn = db_module._try_open_system_db(str(db_path))
    """
    real_connect = duckdb.connect

    def _factory(db_path: Path, fail_times: int = 1):
        call_count = {"n": 0}
        fake_error = duckdb.Error(
            "INTERNAL Error: Failure while replaying WAL file: "
            "Calling DatabaseManager::GetDefaultDatabase with no default "
            "database set"
        )

        def flaky_connect(path, *args, **kwargs):
            if str(path) == str(db_path) and call_count["n"] < fail_times:
                call_count["n"] += 1
                raise fake_error
            return real_connect(path, *args, **kwargs)

        return patch("src.db.duckdb.connect", side_effect=flaky_connect)

    return _factory


# ---------------------------------------------------------------------------
# Shared test helpers (plain functions reused across tests).
# ---------------------------------------------------------------------------

def _make_db_with_schema_version(path: Path, version: int) -> None:
    """Create a fresh DuckDB file containing a ``schema_version`` table
    with the given version row.  Mirrors the shape ``_peek_schema_version``
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
    """Create a DuckDB file with some other table but no ``schema_version``
    — simulates a pre-v1 / structurally-foreign snapshot."""
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE other (id INTEGER)")
    finally:
        conn.close()


def _corrupt_wal_so_replay_fails(db_path: Path) -> None:
    """Write a sentinel .wal file next to *db_path*.

    DuckDB 1.5 is resilient enough to ignore a short garbage WAL, so
    this helper writes the file as a forensic marker for the recovery
    path (``_try_open_system_db`` moves it aside when it exists) but
    does NOT rely on actually triggering a WAL-replay exception from
    DuckDB itself.  The actual IOError/InternalError that drives
    recovery is injected via ``patch("src.db.duckdb.connect", ...)``
    in each test that needs it — keeping the exception injection
    co-located with the test expectation while still exercising the
    real ``shutil.move`` / ``shutil.copy2`` file-system operations.
    """
    wal = Path(str(db_path) + ".wal")
    wal.write_bytes(b"\x00" * 64)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_recovery_restores_pre_migrate_snapshot_on_wal_replay_error(
    state_dir,
):
    """Simulate a corrupted DB + valid pre-migrate snapshot. Open path
    must restore from the snapshot, leave the broken DB aside (with
    `.broken.<ts>` suffix for post-mortem), drop the broken WAL, and
    return a working connection. The migration ladder runs once on the
    restored DB to land back at the current SCHEMA_VERSION."""
    from src.db import _try_open_system_db, _ensure_schema, SCHEMA_VERSION

    db_path = state_dir / "system.duckdb"
    snapshot_path = state_dir / "system.duckdb.pre-migrate"

    # 1. Create a clean valid DB at snapshot_path. This stands in for
    #    the snapshot taken at the start of the most recent migration.
    seed_conn = duckdb.connect(str(snapshot_path))
    _ensure_schema(seed_conn)
    seed_conn.close()
    assert snapshot_path.exists(), "fixture setup: snapshot must exist"

    # 2. Plant a fake "broken" DB at db_path: simply copy the snapshot
    #    and add a .wal sentinel so the restore path moves both aside.
    shutil.copy2(str(snapshot_path), str(db_path))
    wal_path = Path(str(db_path) + ".wal")
    wal_path.write_bytes(b"FAKE_WAL_CONTENT")

    # 3. Make `duckdb.connect(db_path)` raise the WAL-replay error on
    #    the FIRST call only. The auto-recovery's second call (after
    #    snapshot restore) must succeed.
    real_connect = duckdb.connect
    call_count = {"n": 0}
    fake_error = duckdb.Error(
        "INTERNAL Error: Failure while replaying WAL file: "
        "Calling DatabaseManager::GetDefaultDatabase with no default "
        "database set"
    )

    # Fail the initial open AND the Step A salvage reopen, so recovery
    # falls through to the Step B pre-migrate restore (the path this test
    # asserts). The third connect — after the snapshot is copied in —
    # succeeds.
    def flaky_connect(path, *args, **kwargs):
        if str(path) == str(db_path) and call_count["n"] < 2:
            call_count["n"] += 1
            raise fake_error
        return real_connect(path, *args, **kwargs)

    with patch("src.db.duckdb.connect", side_effect=flaky_connect):
        conn = _try_open_system_db(str(db_path))

    # 4. The returned connection is usable.
    ver = conn.execute(
        "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    assert ver is not None
    assert ver[0] == SCHEMA_VERSION, (
        f"recovered DB should be at the current schema version "
        f"({SCHEMA_VERSION}); got {ver[0]}"
    )

    # 5. The unreadable DB was moved aside to .broken.<ts> (Step B), and
    #    the unreplayable WAL was preserved aside to .wal.discarded.<ts>
    #    (Step A) — both kept for forensics.
    broken_dbs = sorted(state_dir.glob("system.duckdb.broken.*"))
    discarded_wals = sorted(state_dir.glob("system.duckdb.wal.discarded.*"))
    assert len(broken_dbs) == 1, broken_dbs
    assert len(discarded_wals) == 1, discarded_wals

    # 6. The current main DB exists (restored from snapshot) and the
    #    broken WAL beside it does NOT — recovery must drop the unflushed
    #    WAL or the next start would replay the same broken op.
    assert db_path.exists()
    assert not (state_dir / "system.duckdb.wal").exists()


def test_recovery_does_not_fire_on_unrelated_error(state_dir):
    """Recovery must be narrow — only the WAL-replay error class. A
    generic ``IO Error: file is locked`` (a real corruption / permission
    case) must propagate so an operator notices instead of silently
    losing whatever's in the WAL by overwriting from snapshot."""
    from src.db import _try_open_system_db

    db_path = state_dir / "system.duckdb"
    db_path.write_bytes(b"corrupted")

    real_connect = duckdb.connect
    unrelated_error = duckdb.Error(
        "IO Error: file is locked by another process"
    )

    def always_unrelated(path, *args, **kwargs):
        if str(path) == str(db_path):
            raise unrelated_error
        return real_connect(path, *args, **kwargs)

    with patch("src.db.duckdb.connect", side_effect=always_unrelated):
        with pytest.raises(duckdb.Error, match="file is locked"):
            _try_open_system_db(str(db_path))


def test_recovery_propagates_when_no_snapshot_exists(state_dir):
    """If the WAL-replay error fires but ``system.duckdb.pre-migrate``
    is missing, recovery has nowhere to fall back. Re-raise the
    original error so the operator sees what's actually wrong."""
    from src.db import _try_open_system_db

    db_path = state_dir / "system.duckdb"
    db_path.write_bytes(b"corrupted")
    # No snapshot at state_dir / "system.duckdb.pre-migrate"

    real_connect = duckdb.connect
    wal_error = duckdb.Error(
        "INTERNAL Error: Failure while replaying WAL file: "
        "Calling DatabaseManager::GetDefaultDatabase"
    )

    def fake(path, *args, **kwargs):
        if str(path) == str(db_path):
            raise wal_error
        return real_connect(path, *args, **kwargs)

    with patch("src.db.duckdb.connect", side_effect=fake):
        with pytest.raises(duckdb.Error, match="GetDefaultDatabase"):
            _try_open_system_db(str(db_path))


def test_recovery_re_raises_if_snapshot_also_broken(state_dir):
    """Edge case: snapshot exists but is itself corrupted (operator
    edited it / disk error). ``_peek_schema_version`` catches the
    ``duckdb.Error`` from the corrupt snapshot and returns 0, which
    is below ``SCHEMA_VERSION``. The stale-snapshot refusal path fires,
    preserving the broken DB and raising ``RuntimeError`` so the
    operator is forced to intervene rather than silently recovering
    against a snapshot of unknown version."""
    from src.db import _try_open_system_db, SCHEMA_VERSION

    db_path = state_dir / "system.duckdb"
    snapshot_path = state_dir / "system.duckdb.pre-migrate"
    db_path.write_bytes(b"corrupted")
    snapshot_path.write_bytes(b"also-corrupted")

    wal_error = duckdb.Error(
        "INTERNAL Error: ReplayAlter failed"
    )
    snapshot_error = duckdb.Error("IO Error: malformed database file")

    call_count = {"n": 0}

    def fake(path, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise wal_error
        raise snapshot_error

    with patch("src.db.duckdb.connect", side_effect=fake):
        with pytest.raises(RuntimeError) as excinfo:
            _try_open_system_db(str(db_path))

    # Error message must mention the detected version (0), the target,
    # and the direction (peek=0 < target → stale).
    msg = str(excinfo.value)
    assert "v0" in msg
    assert "stale" in msg
    assert str(SCHEMA_VERSION) in msg


def test_recovery_proceeds_when_snapshot_is_at_head(tmp_path, make_wal_error):
    """Regression guard: with a HEAD-version snapshot, recovery returns
    a working connection, preserves the broken DB at .broken.<ts>, and
    replaces the main DB with the snapshot."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION)
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=2):
        conn = db_module._try_open_system_db(str(db_path))
    try:
        # The recovered DB carries the snapshot's schema_version row.
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == db_module.SCHEMA_VERSION
    finally:
        conn.close()

    # Both the broken DB and the broken WAL must be preserved.
    broken_dbs = sorted(tmp_path.glob("system.duckdb.broken.*"))
    discarded_wals = sorted(tmp_path.glob("system.duckdb.wal.discarded.*"))
    assert len(broken_dbs) == 1, broken_dbs
    assert len(discarded_wals) == 1, discarded_wals
    # Snapshot was copied into the main DB path.
    assert db_path.exists()
    # Snapshot file itself was left in place (not consumed).
    assert snapshot_path.exists()


def test_recovery_refuses_when_snapshot_is_stale(tmp_path, make_wal_error):
    """Snapshot at SCHEMA_VERSION - 1 → recovery raises RuntimeError,
    preserves both broken files, leaves snapshot untouched, does NOT
    create a fresh DB at db_path."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION - 1)
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=2):
        with pytest.raises(RuntimeError) as excinfo:
            db_module._try_open_system_db(str(db_path))

    # The error message identifies both versions so the operator can act.
    msg = str(excinfo.value)
    assert str(db_module.SCHEMA_VERSION - 1) in msg
    assert str(db_module.SCHEMA_VERSION) in msg

    # Both broken files preserved (DB + WAL split — same pattern as
    # the pre-existing test_recovery_restores_... test).
    broken_dbs = sorted(tmp_path.glob("system.duckdb.broken.*"))
    discarded_wals = sorted(tmp_path.glob("system.duckdb.wal.discarded.*"))
    assert len(broken_dbs) == 1, broken_dbs
    assert len(discarded_wals) == 1, discarded_wals

    # Snapshot was not consumed.
    assert snapshot_path.exists()

    # Main DB path no longer exists — moved aside, NOT overwritten.
    assert not db_path.exists()


def test_recovery_refuses_when_snapshot_has_no_schema_version_table(
    tmp_path, make_wal_error
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

    with make_wal_error(db_path, fail_times=2):
        with pytest.raises(RuntimeError) as excinfo:
            db_module._try_open_system_db(str(db_path))

    # v0 surfaces in the message (the conservative fallback value);
    # treated as stale since 0 < SCHEMA_VERSION.
    msg = str(excinfo.value)
    assert "v0" in msg
    assert "stale" in msg
    assert str(db_module.SCHEMA_VERSION) in msg

    # Same preservation contract as the stale case.
    assert not db_path.exists()
    assert snapshot_path.exists()
    assert any(tmp_path.glob("system.duckdb.broken.*"))


def test_recovery_refuses_when_snapshot_is_from_future_version(
    tmp_path, make_wal_error
):
    """Snapshot at SCHEMA_VERSION + 1 → recovery raises with 'future'
    direction. Mirror of the stale case: operator rolled the binary
    back, so auto-recovery would land the DB in the split-brain
    'current > target' branch. Must refuse symmetrically."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION + 1)
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=2):
        with pytest.raises(RuntimeError) as excinfo:
            db_module._try_open_system_db(str(db_path))

    msg = str(excinfo.value)
    assert "future" in msg
    assert str(db_module.SCHEMA_VERSION + 1) in msg
    assert str(db_module.SCHEMA_VERSION) in msg

    # Same preservation contract — broken DB moved aside, snapshot kept.
    assert not db_path.exists()
    assert snapshot_path.exists()
    assert any(tmp_path.glob("system.duckdb.broken.*"))


def test_recovery_broken_files_are_chmod_0600(tmp_path, make_wal_error):
    """``system.duckdb`` holds argon2 password hashes + PAT secrets;
    the broken-aside files must be chmod 0o600 on the refusal path so
    they don't outlive the incident with default-umask ``0o644``."""
    import stat

    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"

    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION - 1)
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=2):
        with pytest.raises(RuntimeError):
            db_module._try_open_system_db(str(db_path))

    broken = list(tmp_path.glob("system.duckdb.broken.*")) + list(
        tmp_path.glob("system.duckdb.wal.discarded.*")
    )
    assert broken, "no broken-aside files were created"
    # Both the broken DB (Step B) and the discarded WAL (Step A) hold
    # potentially-sensitive bytes and must be owner-only.
    assert any(".wal.discarded." in p.name for p in broken), broken
    for path in broken:
        mode = stat.S_IMODE(path.stat().st_mode)
        # 0o600 owner-only RW. We accept any subset that's ≤ 0o600 in
        # practice (some filesystems mask group/other bits even on
        # 0o644 source files), but ANY group/other bit set is a fail.
        assert mode & 0o077 == 0, (
            f"{path.name}: mode {oct(mode)} has group/other bits set"
        )


# ---------------------------------------------------------------------------
# Step A — salvage the live file by discarding the unreplayable WAL.
# ---------------------------------------------------------------------------

def test_salvage_reopens_live_file_without_snapshot_restore(
    tmp_path, make_wal_error
):
    """The common case: WAL replay fails but the live file opens cleanly
    once the WAL is discarded. Recovery must return the LIVE-file
    connection, never touch the pre-migrate snapshot, keep the DB in
    place (not moved to .broken), and preserve the discarded WAL."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    snapshot_path = tmp_path / "system.duckdb.pre-migrate"
    _make_db_with_schema_version(db_path, db_module.SCHEMA_VERSION)
    # A DELIBERATELY different snapshot version: if recovery wrongly fell
    # back to the snapshot, the returned version would change (and the
    # #379 guard would fire). Step A must make the snapshot irrelevant.
    _make_db_with_schema_version(snapshot_path, db_module.SCHEMA_VERSION - 5)
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=1):
        conn = db_module._try_open_system_db(str(db_path))
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == db_module.SCHEMA_VERSION, "returned the live file, not the snapshot"
    finally:
        conn.close()

    # Live DB kept in place — NOT moved to .broken.
    assert db_path.exists()
    assert not list(tmp_path.glob("system.duckdb.broken.*"))
    # Snapshot untouched (still the stale version, not consumed).
    assert snapshot_path.exists()
    assert db_module._peek_schema_version(snapshot_path) == db_module.SCHEMA_VERSION - 5
    # Unreplayable WAL discarded aside, not left in place to replay again.
    assert not (tmp_path / "system.duckdb.wal").exists()
    assert len(list(tmp_path.glob("system.duckdb.wal.discarded.*"))) == 1


def test_salvage_preserves_checkpointed_rows(tmp_path, make_wal_error):
    """Salvage keeps every row up to the last checkpoint — the whole
    point of preferring the live file over a stale snapshot. No
    pre-migrate snapshot exists here, so only Step A can recover."""
    from src import db as db_module

    db_path = tmp_path / "system.duckdb"
    seed = duckdb.connect(str(db_path))
    seed.execute("CREATE TABLE keep (id INTEGER)")
    seed.execute("INSERT INTO keep VALUES (1), (2), (3)")
    seed.execute("CHECKPOINT")
    seed.close()
    _corrupt_wal_so_replay_fails(db_path)

    with make_wal_error(db_path, fail_times=1):
        conn = db_module._try_open_system_db(str(db_path))
    try:
        assert conn.execute("SELECT count(*) FROM keep").fetchone()[0] == 3
    finally:
        conn.close()

    # No snapshot was needed or created; data recovered from the file itself.
    assert not (tmp_path / "system.duckdb.pre-migrate").exists()
    assert not list(tmp_path.glob("system.duckdb.broken.*"))
