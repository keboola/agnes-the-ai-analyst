"""Tests for the tmp-dir handle guard in ``tests/conftest.py``.

``tmp_path_retention_policy = failed`` (pytest.ini) deletes each passing
test's tmp_path in its teardown — and pytest reuses the freed numbered dir
name for the next same-named test. Process-global handles keyed by path
(src/db.py's DuckDB singletons, src/ducklake_session's sessions, root-logger
FileHandlers) survive that deletion and desync from disk: the historical
``Duplicate key "id: admin1"`` seeded_app failures that forced the policy
back to ``all``, and the shard-5 ``FileNotFoundError`` out of
``logger.info()`` from a leaked collector log handler. The autouse fixture
``_close_tmp_handles`` releases them at teardown; ``close_tmp_handles`` is
its factored-out body and ``handle_outlives_tmp`` the pure decision
function, both tested here.
"""

from __future__ import annotations

import logging
import os
import shutil

import duckdb
import pytest

from tests.conftest import close_tmp_handles, handle_outlives_tmp


class TestPredicate:
    def test_none_path_never_closes(self):
        assert handle_outlives_tmp(None, "/base") is False

    def test_path_under_basetemp_closes(self, tmp_path):
        db = tmp_path / "state" / "system.duckdb"
        assert handle_outlives_tmp(str(db), str(tmp_path)) is True

    def test_trailing_separator_on_basetemp_is_normalized(self, tmp_path):
        db = tmp_path / "state" / "system.duckdb"
        assert handle_outlives_tmp(str(db), str(tmp_path) + os.sep) is True

    def test_sibling_dir_sharing_the_name_prefix_does_not_match(self, tmp_path):
        # <base>/pytest-1-evil is NOT inside <base>/pytest-1 — a bare
        # startswith() without the separator would say it is.
        base = tmp_path / "pytest-1"
        sibling_state = tmp_path / "pytest-1-evil" / "state"
        sibling_state.mkdir(parents=True)
        assert handle_outlives_tmp(str(sibling_state / "system.duckdb"), str(base)) is False

    def test_shared_data_dir_with_live_parent_stays_open(self, tmp_path):
        # The default DATA_DIR case: outside basetemp, dir still on disk —
        # that handle legitimately spans tests and must NOT be closed.
        state = tmp_path / "shared" / "state"
        state.mkdir(parents=True)
        basetemp = str(tmp_path / "unrelated-basetemp")
        assert handle_outlives_tmp(str(state / "system.duckdb"), basetemp) is False

    def test_vanished_parent_dir_closes_even_outside_basetemp(self, tmp_path):
        # Any other tmp scheme (tempfile.mkdtemp + rmtree) hits the same
        # desync; a gone parent dir means the handle must not survive.
        gone = tmp_path / "shared" / "state" / "system.duckdb"
        basetemp = str(tmp_path / "unrelated-basetemp")
        assert handle_outlives_tmp(str(gone), basetemp) is True


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
        assert handle_outlives_tmp(db._system_db_path, basetemp) is True


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
        proves what ``_close_tmp_handles`` does at the test boundary heals
        it: close → reopen at the same path → fresh DB, seed succeeds.
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

        # What the autouse fixture does at teardown.
        close_tmp_handles(str(tmp_path))

        # The close must also clear the path globals, or the stale path
        # would re-trigger this close on every later test (Devin review).
        assert db._system_db_path is None
        assert db._operational_db_path is None
        assert db._analytics_db_path is None

        # The "next test" now reopens fresh at the very same path.
        conn = db.get_system_db()
        UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
        conn.close()

    def test_shared_dir_singleton_survives_a_stale_tmp_path(self, tmp_path, monkeypatch):
        """Devin-review regression: a stale tmp path left behind by an
        earlier close must not keep bouncing a singleton that lives on the
        shared (non-basetemp) DATA_DIR. With the path globals reset on
        close, the second sweep is a no-op and the shared handle stays."""
        import src.db as db

        basetemp = tmp_path / "bt"
        shared = tmp_path / "shared"
        (shared / "state").mkdir(parents=True)
        monkeypatch.setenv("DATA_DIR", str(shared))
        monkeypatch.delenv("STATE_DIR", raising=False)

        db.get_system_db().close()
        assert db._system_db_conn is not None

        # Simulate the pre-fix stale state: an analytics path pointing into
        # basetemp with its connection already gone.
        monkeypatch.setattr(db, "_analytics_db_conn", None)
        monkeypatch.setattr(db, "_analytics_db_path", str(basetemp / "x" / "analytics.duckdb"))

        close_tmp_handles(str(basetemp))  # fires once, clears the stale path
        assert db._analytics_db_path is None

        conn = db.get_system_db()  # shared-dir singleton reopens
        conn.close()
        close_tmp_handles(str(basetemp))  # second sweep must be a no-op
        assert db._system_db_conn is not None

    def test_leaked_root_logger_file_handler_is_swept(self, tmp_path):
        """Shard-5 regression: a root-logger FileHandler under a doomed tmp
        dir (the corporate-memory collector's ``main()`` adds one under
        DATA_DIR) must be removed at teardown, or the next ``logger.info()``
        anywhere in the process raises FileNotFoundError once retention
        deletes the dir. Handlers outside basetemp stay untouched."""
        doomed = tmp_path / "bt" / "test_x0" / "cm"
        doomed.mkdir(parents=True)
        keep_dir = tmp_path / "shared"
        keep_dir.mkdir()

        root = logging.getLogger()
        leaked = logging.FileHandler(doomed / "log")
        kept = logging.FileHandler(keep_dir / "log", delay=True)
        root.addHandler(leaked)
        root.addHandler(kept)
        try:
            shutil.rmtree(doomed)  # retention deletes the passing test's dir

            close_tmp_handles(str(tmp_path / "bt"))

            assert leaked not in root.handlers
            assert kept in root.handlers
            logging.getLogger("tmp-handle-guard-test").info("must not raise")
        finally:
            for handler in (leaked, kept):
                root.removeHandler(handler)
                handler.close()
