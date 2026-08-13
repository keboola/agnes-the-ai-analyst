"""Rolling refresh of system.duckdb.rolling-snapshot (#380).

``system.duckdb.pre-migrate`` is captured once per migration transition and
never refreshed — as a recovery snapshot it goes stale within hours (see
#379). This module adds a SEPARATE, rolling-refreshed artifact
(``system.duckdb.rolling-snapshot/``) built via DuckDB ``EXPORT DATABASE``
(Pattern A from #380): ``CHECKPOINT`` + logical export into a tmp dir, then
an atomic swap into place. The export runs over the app's own long-lived
``system.duckdb`` singleton connection (the same locking discipline as
``checkpoint_system_db`` / #710) — it never opens a second connection to the
file.

``system.duckdb.pre-migrate`` itself is deliberately left untouched: it is a
plain DuckDB file, opened directly by ``_peek_schema_version`` and copied
wholesale by ``_try_open_system_db`` during WAL-recovery — a parquet-export
*directory* cannot serve that contract without also rewriting both call
sites and the WAL-recovery runbook. See ``refresh_rolling_snapshot``'s
docstring in ``src/db.py`` for the full rationale.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR; closed after the test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    yield conn, tmp_path
    close_system_db()


def _snapshot_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "system.duckdb.rolling-snapshot"


def _tmp_scratch_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "system.duckdb.rolling-snapshot.tmp"


def _prev_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "system.duckdb.rolling-snapshot.prev"


def _seed(conn, row_id: str, action: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (id, timestamp, user_id, action) VALUES (?, now(), 'test', ?)",
        [row_id, action],
    )


def _assert_loadable(snap_dir: Path, row_id: str, action: str) -> None:
    fresh = duckdb.connect(":memory:")
    try:
        fresh.execute(f"IMPORT DATABASE '{snap_dir}'")
        row = fresh.execute("SELECT action FROM audit_log WHERE id = ?", [row_id]).fetchone()
        assert row == (action,)
    finally:
        fresh.close()


def test_refresh_produces_loadable_snapshot(system_db):
    conn, tmp_path = system_db
    from src.db import refresh_rolling_snapshot

    conn.execute(
        "INSERT INTO audit_log (id, timestamp, user_id, action) VALUES "
        "('snap-1', now(), 'test', 'rolling-snapshot-seed')"
    )

    assert refresh_rolling_snapshot(force=True) is True
    snap_dir = _snapshot_dir(tmp_path)
    assert snap_dir.is_dir()
    assert any(snap_dir.glob("*.parquet"))

    # A completely fresh DuckDB connection can IMPORT DATABASE from the
    # export — proving the artifact is a real, loadable logical snapshot.
    fresh = duckdb.connect(":memory:")
    try:
        fresh.execute(f"IMPORT DATABASE '{snap_dir}'")
        row = fresh.execute("SELECT action FROM audit_log WHERE id = 'snap-1'").fetchone()
        assert row == ("rolling-snapshot-seed",)
    finally:
        fresh.close()


def test_failed_export_leaves_previous_snapshot_untouched(system_db, monkeypatch):
    conn, tmp_path = system_db
    import src.db as db_mod

    assert db_mod.refresh_rolling_snapshot(force=True) is True
    snap_dir = _snapshot_dir(tmp_path)
    before = sorted(p.name for p in snap_dir.iterdir())

    class _FailingExportConn:
        def cursor(self):
            # The export runs on a dedicated cursor outside the lock
            # (#1294 review); the failure seam moves with it.
            return self

        def close(self):
            pass

        def execute(self, sql, *a, **k):
            if "EXPORT DATABASE" in sql:
                raise RuntimeError("disk full")
            return conn.execute(sql, *a, **k)

    monkeypatch.setattr(db_mod, "_system_db_conn", _FailingExportConn())
    assert db_mod.refresh_rolling_snapshot(force=True) is False

    after = sorted(p.name for p in snap_dir.iterdir())
    assert before == after, "a failed export must never touch the previous snapshot"
    assert not _tmp_scratch_dir(tmp_path).exists(), "failed tmp export dir must be cleaned up"


def test_failed_restore_must_not_destroy_the_last_snapshot(system_db, monkeypatch):
    """Devin on #1294 — the swap moves the live snapshot aside to ``.prev``
    and renames the fresh export into its place. If the second rename fails
    AND the restore that puts the old one back fails too, the copy sitting at
    ``.prev`` is the only recovery artifact left on disk. Deleting it — or
    letting the restore's ``OSError`` escape past the documented ``False``
    return — leaves the instance with no recovery snapshot at all, which is
    the single outcome this mechanism exists to prevent.
    """
    conn, tmp_path = system_db
    import src.db as db_mod

    _seed(conn, "snap-keep", "must-survive")
    assert db_mod.refresh_rolling_snapshot(force=True) is True
    final_dir = _snapshot_dir(tmp_path)
    _assert_loadable(final_dir, "snap-keep", "must-survive")

    real_rename = os.rename

    def _rename(src, dst, *a, **kw):
        # Fail every move INTO the final name: first the swap (tmp -> final),
        # then the restore of the previous snapshot (prev -> final). Moving
        # the live snapshot aside (final -> prev) still succeeds, so the last
        # good copy really is stranded under `.prev`.
        if str(dst) == str(final_dir):
            raise OSError(5, "Input/output error")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", _rename)

    # Documented contract: report failure, never raise.
    assert db_mod.refresh_rolling_snapshot(force=True) is False

    survivors = [d for d in (final_dir, _prev_dir(tmp_path)) if d.is_dir() and any(d.glob("*.parquet"))]
    assert survivors, "a double failure during the swap destroyed the last good recovery snapshot"
    _assert_loadable(survivors[0], "snap-keep", "must-survive")


def test_stranded_previous_snapshot_is_reclaimed(system_db, monkeypatch):
    """Aftermath of the double failure above: the only snapshot lives under
    ``.prev``, a name neither the runbook nor an operator knows. The next run
    must put it back under the documented name — including when that run's own
    export fails, since otherwise the artifact stays invisible indefinitely
    and the following swap would delete it as swap scratch.
    """
    conn, tmp_path = system_db
    import src.db as db_mod

    _seed(conn, "snap-stranded", "stranded-but-good")
    assert db_mod.refresh_rolling_snapshot(force=True) is True

    final_dir = _snapshot_dir(tmp_path)
    prev_dir = _prev_dir(tmp_path)
    os.rename(final_dir, prev_dir)  # the state a failed restore leaves behind

    class _FailingExportConn:
        def cursor(self):
            return self

        def close(self):
            pass

        def execute(self, sql, *a, **k):
            if "EXPORT DATABASE" in sql:
                raise RuntimeError("disk full")
            return conn.execute(sql, *a, **k)

    monkeypatch.setattr(db_mod, "_system_db_conn", _FailingExportConn())
    assert db_mod.refresh_rolling_snapshot(force=True) is False

    assert final_dir.is_dir(), "the stranded snapshot was not reclaimed under the documented name"
    assert not prev_dir.exists()
    _assert_loadable(final_dir, "snap-stranded", "stranded-but-good")


def test_snapshot_is_not_world_readable(system_db):
    """``system.duckdb`` holds argon2 password hashes, PAT rows, the audit log
    and vault rows, and ``EXPORT DATABASE`` writes all of it out as parquet
    under the process umask (dir ``0o755`` / files ``0o644``). Every other
    derivative of that file in ``src/db.py`` is explicitly tightened to
    ``0o600`` (``_move_to_broken``, the discarded WAL); this one is no
    different.
    """
    conn, tmp_path = system_db
    from src.db import refresh_rolling_snapshot

    assert refresh_rolling_snapshot(force=True) is True
    snap_dir = _snapshot_dir(tmp_path)

    assert snap_dir.stat().st_mode & 0o777 == 0o700, "snapshot directory is group/world-accessible"
    contents = list(snap_dir.rglob("*"))
    assert contents, "export produced no files"
    for path in contents:
        mode = path.stat().st_mode & 0o777
        expected = 0o700 if path.is_dir() else 0o600
        assert mode == expected, f"{path.name} is {oct(mode)}, expected {oct(expected)}"


def test_export_scratch_dir_is_not_world_readable_during_export(system_db):
    """Tightening the modes only after the export leaves a window in which the
    freshly written parquet — full password hashes and PAT rows — is
    world-readable. The scratch directory must already be ``0o700`` when
    ``EXPORT DATABASE`` starts writing into it.
    """
    import src.db as db

    real = db._system_db_conn
    seen: dict[str, int] = {}

    class _Cursor:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **kw):
            if str(sql).lstrip().upper().startswith("EXPORT"):
                target = Path(str(sql).split("'")[1])
                if target.exists():
                    seen["mode"] = target.stat().st_mode & 0o777
                else:
                    seen["mode"] = -1  # created by DuckDB itself, under umask
            return self._c.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    class _Conn:
        def __init__(self, c):
            self._c = c

        def cursor(self):
            return _Cursor(self._c.cursor())

        def __getattr__(self, name):
            return getattr(self._c, name)

    db._system_db_conn = _Conn(real)
    try:
        assert db.refresh_rolling_snapshot(force=True) is True
    finally:
        db._system_db_conn = real

    assert seen.get("mode") == 0o700, (
        f"export scratch dir was {oct(seen.get('mode', 0))} when EXPORT DATABASE started "
        "— credential material is world-readable for the duration of the export"
    )


def test_cadence_gate_skips_when_fresh(system_db):
    conn, tmp_path = system_db
    from src.db import refresh_rolling_snapshot

    assert refresh_rolling_snapshot(force=True) is True
    snap_dir = _snapshot_dir(tmp_path)
    mtime_before = snap_dir.stat().st_mtime

    # Default cadence is hours; calling again immediately (no force) must
    # skip — the existing snapshot is still fresh.
    assert refresh_rolling_snapshot() is False
    assert snap_dir.stat().st_mtime == mtime_before


def test_cadence_gate_refreshes_when_stale(system_db, monkeypatch):
    conn, tmp_path = system_db
    import src.db as db_mod

    assert db_mod.refresh_rolling_snapshot(force=True) is True
    snap_dir = _snapshot_dir(tmp_path)
    mtime_before = snap_dir.stat().st_mtime

    # Shrink the configured interval to (effectively) zero so the very next
    # non-forced call sees the existing snapshot as stale.
    monkeypatch.setattr(db_mod, "_rolling_snapshot_interval_hours", lambda: 1e-9)
    time.sleep(0.01)
    assert db_mod.refresh_rolling_snapshot() is True
    assert snap_dir.stat().st_mtime >= mtime_before


def test_noop_on_postgres_backend(system_db, monkeypatch):
    conn, tmp_path = system_db
    import src.db as db_mod

    monkeypatch.setattr(db_mod, "_state_backend_is_pg", lambda: True)
    assert db_mod.refresh_rolling_snapshot(force=True) is False
    assert not _snapshot_dir(tmp_path).exists()


def test_noop_when_singleton_not_open(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.db import close_system_db, refresh_rolling_snapshot

    close_system_db()
    # Must never open a second connection to system.duckdb just to snapshot it.
    assert refresh_rolling_snapshot(force=True) is False
    assert not _snapshot_dir(tmp_path).exists()


def test_disabled_by_zero_interval(system_db, monkeypatch):
    conn, tmp_path = system_db
    import src.db as db_mod

    monkeypatch.setattr(db_mod, "_rolling_snapshot_interval_hours", lambda: 0.0)
    assert db_mod.refresh_rolling_snapshot() is False
    assert not _snapshot_dir(tmp_path).exists()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 6.0),  # unset -> default
        (2, 2.0),
        (0, 0.0),  # disabled
        ("not-a-number", 6.0),  # unparsable -> default
    ],
)
def test_interval_hours_reads_config(monkeypatch, configured, expected):
    import app.instance_config as cfg_mod
    import src.db as db_mod

    def _get_value(*_keys, default=None):
        return default if configured is None else configured

    monkeypatch.setattr(cfg_mod, "get_value", _get_value)
    assert db_mod._rolling_snapshot_interval_hours() == expected


def test_interval_hours_falls_back_on_config_error(monkeypatch):
    import app.instance_config as cfg_mod
    import src.db as db_mod

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cfg_mod, "get_value", _raise)
    assert db_mod._rolling_snapshot_interval_hours() == 6.0


def test_export_runs_outside_the_system_db_lock(system_db):
    """Devin on #1294 — `_system_db_lock` sits on `get_system_db()`'s hot
    path (every authed request grabs it briefly for a cursor). A full-DB
    `EXPORT DATABASE` reads and serializes the whole system DB, so holding
    the lock across it stalls every request for the export's duration. The
    lock guards the singleton's LIFECYCLE, not query execution — the export
    must run on its own cursor with the lock released."""
    import src.db as db

    real = db._system_db_conn
    seen: dict[str, bool] = {}

    class _Cursor:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **kw):
            if str(sql).lstrip().upper().startswith("EXPORT"):
                free = db._system_db_lock.acquire(blocking=False)
                seen["lock_free_during_export"] = free
                if free:
                    db._system_db_lock.release()
            return self._c.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    class _Conn:
        def __init__(self, c):
            self._c = c

        def cursor(self):
            return _Cursor(self._c.cursor())

        def __getattr__(self, name):
            return getattr(self._c, name)

    db._system_db_conn = _Conn(real)
    try:
        assert db.refresh_rolling_snapshot(force=True) is True
    finally:
        db._system_db_conn = real

    assert seen.get("lock_free_during_export") is True, (
        "EXPORT DATABASE executed while _system_db_lock was held — it must run on a dedicated cursor outside the lock"
    )
