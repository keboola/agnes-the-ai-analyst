"""ART-index corruption self-heal for ``system.duckdb``.

Production failure class (multiple occurrences across deployments): DuckDB
backs PRIMARY KEY / UNIQUE constraints with
an on-disk ART (Adaptive Radix Tree) index. An abrupt process/VM
termination that bypasses the graceful ``CHECKPOINT``-and-close path
(OOM SIGKILL, VM ``-replace`` destroy, host crash) can leave the index's
on-disk pages torn — the base table is intact but the index diverges. On
the next start the file OPENS fine, then the first write that touches the
bad index raises

    Invalid Input Error: Failed to delete all rows from index. Only
    deleted 0 out of 1 rows.

which invalidates the whole connection (``database has been invalidated
because of a previous fatal error``). A plain restart does NOT heal it —
the corruption is on disk. The only fix is an EXPORT/IMPORT rebuild, which
reconstructs every index from the (readable) base-table data.

This differs from the WAL-replay recovery (``test_db_wal_recovery.py``):
that fires when the file won't OPEN; this fires when the file opens but a
specific index operation fails at runtime. The two are layered
independently in ``_try_open_system_db``.

Coverage:
  1. ``_probe_art_integrity`` — the delete-in-tx-rollback canary reports
     healthy on a good DB and leaves data untouched.
  2. ``_rebuild_system_db`` — export/import rebuild preserves all data,
     re-enforces constraints, and quarantines the original as ``.broken.*``.
  3. ``_try_open_system_db`` — a probe that reports corruption triggers an
     automatic rebuild + reopen; a healthy probe leaves the file untouched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

import src.db as db_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_db(path: str) -> None:
    """A minimal system-like DB: one indexed table with rows + one without."""
    c = duckdb.connect(path)
    c.execute("CREATE TABLE jobs(id VARCHAR PRIMARY KEY, status VARCHAR)")
    c.execute("INSERT INTO jobs VALUES ('a', 'running'), ('b', 'done')")
    c.execute("CREATE TABLE notes(txt VARCHAR)")  # no index
    c.execute("INSERT INTO notes VALUES ('x')")
    c.execute("CHECKPOINT")
    c.close()


@pytest.fixture
def db_path(tmp_path) -> str:
    p = str(tmp_path / "system.duckdb")
    _seed_db(p)
    return p


# ---------------------------------------------------------------------------
# 1. Canary probe
# ---------------------------------------------------------------------------


def test_probe_reports_healthy_and_leaves_data_untouched(db_path):
    conn = duckdb.connect(db_path)
    healthy, detail = db_mod._probe_art_integrity(conn)
    assert healthy is True
    assert detail is None
    # canary uses BEGIN/DELETE/ROLLBACK — nothing must actually be deleted
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    conn.close()


class _FaultyConn:
    """Duck-typed wrapper: delegates to a real connection but raises a
    chosen error on DELETE. DuckDB's C ``execute`` can't be monkeypatched
    on the instance, so the probe is fed this stand-in instead."""

    def __init__(self, real, on_delete_exc):
        self._real = real
        self._exc = on_delete_exc

    def execute(self, sql, *a, **k):
        if sql.strip().upper().startswith("DELETE"):
            raise self._exc
        return self._real.execute(sql, *a, **k)


def test_probe_flags_the_art_corruption_signature(db_path):
    """A DELETE that raises the ART signature is reported as corruption
    (not re-raised), so the caller can rebuild."""
    conn = duckdb.connect(db_path)
    faulty = _FaultyConn(
        conn,
        duckdb.InvalidInputException("Failed to delete all rows from index. Only deleted 0 out of 1 rows."),
    )
    healthy, detail = db_mod._probe_art_integrity(faulty)
    assert healthy is False
    assert "Failed to delete all rows from index" in detail
    conn.close()


def test_probe_treats_non_corruption_errors_as_healthy(db_path):
    """A non-corruption error on a table (e.g. an FK constraint blocking the
    probe delete, or an engine quirk) must NOT be reported as corruption and
    must NOT break the probe — the table is skipped. The probe's only job is
    spotting the ART-corruption signature; it must never trigger a needless
    rebuild or crash the DB open."""
    conn = duckdb.connect(db_path)
    faulty = _FaultyConn(conn, duckdb.ConstraintException("FK violation"))
    healthy, detail = db_mod._probe_art_integrity(faulty)
    assert healthy is True
    assert detail is None
    conn.close()


# ---------------------------------------------------------------------------
# 2. Rebuild
# ---------------------------------------------------------------------------


def test_rebuild_preserves_data_and_quarantines_original(db_path):
    db_mod._rebuild_system_db(db_path)

    # original quarantined as .broken.<ts>
    broken = list(Path(db_path).parent.glob("system.duckdb.broken.*"))
    assert broken, "original corrupt DB must be preserved as .broken.*"
    assert oct(broken[0].stat().st_mode)[-3:] == "600"

    # rebuilt file opens and has all data
    conn = duckdb.connect(db_path)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    # index rebuilt → PK re-enforced
    with pytest.raises(duckdb.ConstraintException):
        conn.execute("INSERT INTO jobs VALUES ('a', 'dup')")
    conn.close()


def test_rebuild_broken_copy_is_a_readable_snapshot(db_path):
    """The quarantined .broken.<ts> is a COPY (atomic-swap design): db_path
    is never momentarily absent, so a concurrent opener can't slip a fresh
    empty DB into the gap. Both the snapshot and the live rebuilt file are
    intact afterwards."""
    broken = db_mod._rebuild_system_db(db_path)
    assert broken.exists()
    snap = duckdb.connect(str(broken), read_only=True)
    assert snap.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    snap.close()
    live = duckdb.connect(db_path, read_only=True)
    assert live.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    live.close()


def test_rebuild_leaves_a_usable_db_even_if_original_had_no_wal(db_path):
    # no .wal beside the file; rebuild must still work
    assert not Path(db_path + ".wal").exists()
    db_mod._rebuild_system_db(db_path)
    conn = duckdb.connect(db_path)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    conn.close()


def test_move_to_broken_does_not_clobber_an_existing_quarantine(db_path, tmp_path):
    """Devin Review, PR #948: two quarantines of the same db_path within the
    same wall-clock second (e.g. STEP B's own quarantine, immediately
    followed by _rebuild_system_db's quarantine of the restored-but-still-
    corrupt snapshot) must NOT resolve to the same .broken.<ts> path —
    shutil.move silently overwrites an existing destination, which would
    destroy the first quarantined file's forensics value."""
    wal_path = Path(db_path + ".wal")

    # First quarantine: db_path -> some .broken.<ts>.
    first_broken = db_mod._move_to_broken(db_path, wal_path)
    assert first_broken.exists()
    first_content = first_broken.read_bytes()

    # A second file lands at db_path (mirrors the pre-migrate snapshot copy
    # in STEP B) and must be quarantined too, without erasing the first.
    Path(db_path).write_bytes(b"second-generation-content")
    second_broken = db_mod._move_to_broken(db_path, wal_path)

    assert second_broken != first_broken, "second quarantine reused the first quarantine's path"
    assert first_broken.exists(), "first quarantine was destroyed by the second"
    assert first_broken.read_bytes() == first_content, "first quarantine's content was overwritten"
    assert second_broken.exists()
    assert second_broken.read_bytes() == b"second-generation-content"


# ---------------------------------------------------------------------------
# 3. Wiring into _try_open_system_db
# ---------------------------------------------------------------------------


def test_open_auto_heals_when_probe_reports_corruption(db_path):
    calls = {"n": 0}
    real = db_mod._probe_art_integrity

    def probe(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            return (False, "Failed to delete all rows from index.")
        return real(conn)  # after rebuild → healthy

    with patch.object(db_mod, "_probe_art_integrity", side_effect=probe):
        conn = db_mod._try_open_system_db(db_path)

    assert list(Path(db_path).parent.glob("system.duckdb.broken.*")), "rebuild should have quarantined the original"
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    conn.close()


def test_open_does_not_touch_a_healthy_db(db_path):
    with patch.object(db_mod, "_rebuild_system_db") as rebuild:
        conn = db_mod._try_open_system_db(db_path)
    rebuild.assert_not_called()
    assert not list(Path(db_path).parent.glob("system.duckdb.broken.*"))
    conn.close()


def test_self_heal_can_be_disabled_by_env(db_path, monkeypatch):
    monkeypatch.setenv("AGNES_DB_SELF_HEAL", "0")
    with patch.object(db_mod, "_probe_art_integrity") as probe:
        conn = db_mod._try_open_system_db(db_path)
    probe.assert_not_called()
    conn.close()


def test_open_probes_and_heals_after_wal_salvage_recovery(db_path, monkeypatch):
    """Devin Review, PR #948: the same abrupt termination that dirties the
    WAL can also tear the on-disk ART index — a DB that needed WAL
    recovery (STEP A salvage) is not evidence the index survived. The
    probe must run on the salvage-recovered connection too, not only on a
    cleanly-opened one."""
    real_connect = duckdb.connect
    real_probe = db_mod._probe_art_integrity

    # Force the FIRST open of db_path to raise the WAL-replay error class
    # so _try_open_system_db takes the STEP A salvage branch.
    fake_wal_error = duckdb.Error(
        "INTERNAL Error: Failure while replaying WAL file: "
        "Calling DatabaseManager::GetDefaultDatabase with no default database set"
    )
    call_count = {"n": 0}

    def flaky_connect(path, *args, **kwargs):
        if str(path) == db_path and call_count["n"] == 0:
            call_count["n"] += 1
            raise fake_wal_error
        return real_connect(path, *args, **kwargs)

    # A .wal file must exist for _salvage_discard_wal to have something to
    # discard aside (mirrors _corrupt_wal_so_replay_fails in
    # test_db_wal_recovery.py).
    Path(db_path + ".wal").write_bytes(b"\x00" * 64)

    probe_calls = {"n": 0}

    def probe(conn):
        probe_calls["n"] += 1
        if probe_calls["n"] == 1:
            return (False, "Failed to delete all rows from index.")
        return real_probe(conn)  # after rebuild → healthy

    with (
        patch("src.db.duckdb.connect", side_effect=flaky_connect),
        patch.object(db_mod, "_probe_art_integrity", side_effect=probe),
    ):
        conn = db_mod._try_open_system_db(db_path)

    assert probe_calls["n"] >= 1, "the salvage-recovered connection must still be probed"
    assert list(Path(db_path).parent.glob("system.duckdb.broken.*")), (
        "corruption surfaced after WAL salvage should still trigger the EXPORT/IMPORT rebuild"
    )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    conn.close()


# ---------------------------------------------------------------------------
# 4. CLI: agnes admin db repair
# ---------------------------------------------------------------------------


def test_repair_cli_no_op_on_postgres(monkeypatch):
    from typer.testing import CliRunner

    import src.repositories as repos
    from cli.commands.db import db_app

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    called = {}
    monkeypatch.setattr(db_mod, "_rebuild_system_db", lambda p: called.setdefault("path", p))

    result = CliRunner().invoke(db_app, ["repair", "--yes"])
    assert result.exit_code == 0, result.output
    assert "does not apply" in result.output
    assert "path" not in called  # never touched the DuckDB rebuild


def test_repair_cli_rebuilds_duckdb(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    import src.repositories as repos
    from cli.commands.db import db_app

    monkeypatch.setattr(repos, "use_pg", lambda: False)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("STATE_DIR", str(state))
    _seed_db(str(state / "system.duckdb"))

    result = CliRunner().invoke(db_app, ["repair", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Rebuilt" in result.output
    assert list(state.glob("system.duckdb.broken.*"))
    conn = duckdb.connect(str(state / "system.duckdb"))
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    conn.close()


def test_repair_cli_refuses_without_yes_in_noninteractive_shell(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    import src.repositories as repos
    from cli.commands.db import db_app

    monkeypatch.setattr(repos, "use_pg", lambda: False)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("STATE_DIR", str(state))
    _seed_db(str(state / "system.duckdb"))

    result = CliRunner().invoke(db_app, ["repair"])  # no --yes; runner stdin isn't a tty
    assert result.exit_code == 1
    assert "without --yes" in result.output
    assert not list(state.glob("system.duckdb.broken.*"))
