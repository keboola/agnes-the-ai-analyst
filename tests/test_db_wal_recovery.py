"""WAL-replay auto-recovery for ``system.duckdb``.

Reproduces the production failure observed during PR #217's v27
rollout: a container kill mid-migration leaves an unflushed
``ALTER TABLE … ADD COLUMN`` op in ``system.duckdb.wal``. On the next
start, DuckDB's ``ReplayAlter`` path raises
``INTERNAL Error: Calling DatabaseManager::GetDefaultDatabase with no
default database set`` and the system database becomes unrecoverable
from the running binary — the operator has to restore from the
pre-migrate snapshot by hand.

The fix is two-pronged:
  1. ``_ensure_schema`` runs ``CHECKPOINT`` immediately after the
     migration ladder so a fresh ALTER doesn't sit in the WAL beyond
     the migration window. Tested implicitly by every migration test
     that survives a process restart between fixture runs (covered by
     the existing v25→v26→v27 tests).
  2. ``_try_open_system_db`` catches the WAL-replay error class and
     falls back to ``system.duckdb.pre-migrate``. That's the path
     this file exercises.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest


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

    def flaky_connect(path, *args, **kwargs):
        if str(path) == str(db_path) and call_count["n"] == 0:
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

    # 5. Broken DB and broken WAL were moved aside (kept for forensics).
    all_broken = sorted(state_dir.glob("system.duckdb.broken.*"))
    broken_dbs = [p for p in all_broken if not p.name.endswith(".wal")]
    broken_wals = [p for p in all_broken if p.name.endswith(".wal")]
    assert len(broken_dbs) == 1, all_broken
    assert len(broken_wals) == 1, all_broken

    # 6. The current main DB exists and the (broken) WAL beside it does
    #    NOT — the recovery path must drop the unflushed WAL or the
    #    next start would replay the same broken op.
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
    edited it / disk error). The first recovery ``duckdb.connect``
    succeeds (via the mock) so the function returns, but the second
    call would still fail. We assert the re-attempted connect's error
    propagates rather than being swallowed."""
    from src.db import _try_open_system_db

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
        with pytest.raises(duckdb.Error, match="malformed"):
            _try_open_system_db(str(db_path))
