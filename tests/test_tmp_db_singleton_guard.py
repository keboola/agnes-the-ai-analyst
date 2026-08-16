"""Tests for the tmp-dir DuckDB-singleton guard in ``tests/conftest.py``.

``tmp_path_retention_policy = failed`` (pytest.ini) deletes each passing
test's tmp_path in its teardown — and pytest reuses the freed numbered dir
name for the next same-named test. src/db.py's connection singletons are
keyed by PATH, so a handle that survives the deletion string-matches the
next test's path and keeps serving the unlinked file (the historical
``Duplicate key "id: admin1"`` seeded_app failures that forced the policy
back to ``all``). The autouse fixture ``_close_tmp_db_singletons`` closes
any singleton pointing into basetemp at teardown; ``singleton_outlives_tmp``
is its pure decision function, tested here without the real singletons.
"""

from __future__ import annotations

import os
import shutil

import duckdb
import pytest

from tests.conftest import singleton_outlives_tmp


class TestPredicate:
    def test_none_path_never_closes(self):
        assert singleton_outlives_tmp(None, "/base") is False

    def test_path_under_basetemp_closes(self, tmp_path):
        db = tmp_path / "state" / "system.duckdb"
        assert singleton_outlives_tmp(str(db), str(tmp_path)) is True

    def test_trailing_separator_on_basetemp_is_normalized(self, tmp_path):
        db = tmp_path / "state" / "system.duckdb"
        assert singleton_outlives_tmp(str(db), str(tmp_path) + os.sep) is True

    def test_sibling_dir_sharing_the_name_prefix_does_not_match(self, tmp_path):
        # <base>/pytest-1-evil is NOT inside <base>/pytest-1 — a bare
        # startswith() without the separator would say it is.
        base = tmp_path / "pytest-1"
        sibling_state = tmp_path / "pytest-1-evil" / "state"
        sibling_state.mkdir(parents=True)
        assert singleton_outlives_tmp(str(sibling_state / "system.duckdb"), str(base)) is False

    def test_shared_data_dir_with_live_parent_stays_open(self, tmp_path):
        # The default DATA_DIR case: outside basetemp, dir still on disk —
        # that singleton legitimately spans tests and must NOT be closed.
        state = tmp_path / "shared" / "state"
        state.mkdir(parents=True)
        basetemp = str(tmp_path / "unrelated-basetemp")
        assert singleton_outlives_tmp(str(state / "system.duckdb"), basetemp) is False

    def test_vanished_parent_dir_closes_even_outside_basetemp(self, tmp_path):
        # Any other tmp scheme (tempfile.mkdtemp + rmtree) hits the same
        # desync; a gone parent dir means the handle must not survive.
        gone = tmp_path / "shared" / "state" / "system.duckdb"
        basetemp = str(tmp_path / "unrelated-basetemp")
        assert singleton_outlives_tmp(str(gone), basetemp) is True


class TestWiring:
    """Guard the fixture's wiring assumption: a tmp DATA_DIR really lands
    ``_system_db_path`` under basetemp, so the predicate flags it. Catches
    path-shape drift in ``_get_state_dir()`` that would silently disarm the
    autouse close and resurrect the duplicate-key errors."""

    def test_tmp_data_dir_singleton_is_flagged_for_close(self, tmp_path, tmp_path_factory, monkeypatch):
        import src.db as db

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("STATE_DIR", raising=False)
        (tmp_path / "state").mkdir()

        db.get_system_db().close()  # closes the cursor, not the singleton
        assert db._system_db_conn is not None
        assert db._system_db_path is not None
        basetemp = str(tmp_path_factory.getbasetemp())
        assert singleton_outlives_tmp(db._system_db_path, basetemp) is True


class TestEndToEnd:
    def test_close_heals_the_deleted_dir_reuse_desync(self, tmp_path, monkeypatch):
        """The full retention-policy sequence in one deterministic test:
        singleton opened under a tmp DATA_DIR → dir deleted (what `failed`
        does to a passing test) → the SAME path handed out again (pytest
        reuses the freed numbered name for the next same-named test).

        First half proves the desync is real — without a close, the reused
        path string-matches ``_system_db_path`` and the stale handle keeps
        serving the unlinked file, so re-seeding admin1 raises the historical
        duplicate-key error. (If DuckDB or pytest semantics ever change so
        this half fails, the autouse fixture may be retirable — that's the
        signal to re-read pytest.ini's retention comments.) Second half
        proves what ``_close_tmp_db_singletons`` does at the test boundary
        heals it: close → reopen at the same path → fresh DB, seed succeeds.
        """
        import src.db as db
        from src.repositories.users import UserRepository

        data_dir = tmp_path / "reused-name"
        (data_dir / "state").mkdir(parents=True)
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        monkeypatch.delenv("STATE_DIR", raising=False)

        conn = db.get_system_db()
        UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
        conn.close()

        shutil.rmtree(data_dir)  # retention deletes the passing test's dir
        (data_dir / "state").mkdir(parents=True)  # next test reuses the path

        # Pre-fix world: the stale handle still serves the unlinked file.
        conn = db.get_system_db()
        with pytest.raises(duckdb.ConstraintException, match="admin1"):
            UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
        conn.close()

        # What the autouse fixture does at teardown — decision, then close.
        assert singleton_outlives_tmp(db._system_db_path, str(tmp_path)) is True
        db.close_singleton_connections()

        # The "next test" now reopens fresh at the very same path.
        conn = db.get_system_db()
        UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
        conn.close()
