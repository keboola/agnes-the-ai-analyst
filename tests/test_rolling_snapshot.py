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
        def execute(self, sql, *a, **k):
            if "EXPORT DATABASE" in sql:
                raise RuntimeError("disk full")
            return conn.execute(sql, *a, **k)

    monkeypatch.setattr(db_mod, "_system_db_conn", _FailingExportConn())
    assert db_mod.refresh_rolling_snapshot(force=True) is False

    after = sorted(p.name for p in snap_dir.iterdir())
    assert before == after, "a failed export must never touch the previous snapshot"
    assert not _tmp_scratch_dir(tmp_path).exists(), "failed tmp export dir must be cleaned up"


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
