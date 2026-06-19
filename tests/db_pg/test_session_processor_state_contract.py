"""Cross-engine contract test for the session_processor_state repository.

Pins the per-(processor, session) bookkeeping read/write helpers that back
the session-pipeline health check (app/api/health.py), the pipeline-status
enrichment in the per-user stats tab (app/api/me_stats.py), and the
usage-reprocess admin action (app/api/admin_usage.py reprocess_usage).

Parametrising over both backends through the repo classes makes a parity
regression at the routing layer impossible: each new method must behave
identically on DuckDB and Postgres.
"""

from __future__ import annotations

import pytest


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (not bare `duckdb.connect`) so the session
    # timezone is pinned to UTC — matches the production helper and the
    # `tests/test_duckdb_session_tz.py` regression guard.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.session_processor_state import (
        SessionProcessorStateRepository,
    )

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "repo": SessionProcessorStateRepository(conn),
        "conn": conn,
        "backend": "duckdb",
    }


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.session_processor_state_pg import (
        SessionProcessorStatePgRepository,
    )

    eng = db_pg.get_engine()
    return {
        "repo": SessionProcessorStatePgRepository(eng),
        "engine": eng,
        "backend": "pg",
    }


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        bundle = _make_duckdb_repo(tmp_path)
        yield bundle
        bundle["conn"].close()
    else:
        bundle = _make_pg_repo(pg_engine, monkeypatch)
        yield bundle


def _seed(repos, processor, session_file, items=1):
    """Seed one state row via the production mark_processed UPSERT."""
    repos["repo"].mark_processed(
        processor_name=processor,
        session_file=session_file,
        username="alice",
        items_count=items,
        file_hash=f"hash-{processor}-{session_file}",
    )


# ---------------------------------------------------------------------------
# delete_for_processors
# ---------------------------------------------------------------------------


class TestDeleteForProcessors:
    def test_empty_input_returns_zero(self, repos):
        assert repos["repo"].delete_for_processors([]) == 0

    def test_deletes_only_named_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        _seed(repos, "usage", "alice/b.jsonl")
        _seed(repos, "verification", "alice/a.jsonl")

        deleted = repos["repo"].delete_for_processors(["usage"])
        assert deleted == 2

        # The OTHER processor's rows are untouched.
        assert repos["repo"].processed_session_files("usage") == set()
        assert repos["repo"].processed_session_files("verification") == {
            "alice/a.jsonl"
        }

    def test_deletes_multiple_processors(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        _seed(repos, "marketplace_rollup_30d", "alice/a.jsonl")
        _seed(repos, "verification", "alice/a.jsonl")

        deleted = repos["repo"].delete_for_processors(
            ["usage", "marketplace_rollup_30d"]
        )
        assert deleted == 2
        assert repos["repo"].processed_session_files("verification") == {
            "alice/a.jsonl"
        }

    def test_unknown_processor_deletes_nothing(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        assert repos["repo"].delete_for_processors(["nope"]) == 0
        assert repos["repo"].processed_session_files("usage") == {"alice/a.jsonl"}


# ---------------------------------------------------------------------------
# max_processed_at
# ---------------------------------------------------------------------------


class TestMaxProcessedAt:
    def test_none_when_no_rows(self, repos):
        assert repos["repo"].max_processed_at("verification") is None

    def test_returns_latest(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        result = repos["repo"].max_processed_at("verification")
        assert result is not None

    def test_isolated_per_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        # No 'verification' rows even though 'usage' has one.
        assert repos["repo"].max_processed_at("verification") is None


# ---------------------------------------------------------------------------
# processed_session_files
# ---------------------------------------------------------------------------


class TestProcessedSessionFiles:
    def test_empty_when_no_rows(self, repos):
        assert repos["repo"].processed_session_files("verification") == set()

    def test_returns_set_for_processor(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        _seed(repos, "verification", "alice/b.jsonl")
        _seed(repos, "usage", "alice/c.jsonl")
        assert repos["repo"].processed_session_files("verification") == {
            "alice/a.jsonl",
            "alice/b.jsonl",
        }


# ---------------------------------------------------------------------------
# get_states_for_session_files
# ---------------------------------------------------------------------------


class TestGetStatesForSessionFiles:
    def test_empty_input_returns_empty(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        assert repos["repo"].get_states_for_session_files("verification", []) == {}

    def test_returns_states_for_matching_files(self, repos):
        _seed(repos, "verification", "alice/a.jsonl", items=3)
        _seed(repos, "verification", "alice/b.jsonl", items=0)

        states = repos["repo"].get_states_for_session_files(
            "verification", ["alice/a.jsonl", "alice/b.jsonl"]
        )
        assert set(states.keys()) == {"alice/a.jsonl", "alice/b.jsonl"}
        assert states["alice/a.jsonl"]["items_extracted"] == 3
        assert states["alice/b.jsonl"]["items_extracted"] == 0
        assert states["alice/a.jsonl"]["processed_at"] is not None

    def test_only_returns_requested_files(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        _seed(repos, "verification", "alice/b.jsonl")
        states = repos["repo"].get_states_for_session_files(
            "verification", ["alice/a.jsonl"]
        )
        assert set(states.keys()) == {"alice/a.jsonl"}

    def test_scoped_to_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        # File exists under 'usage' but we ask 'verification' — no match.
        states = repos["repo"].get_states_for_session_files(
            "verification", ["alice/a.jsonl"]
        )
        assert states == {}

    def test_missing_file_absent_from_result(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        states = repos["repo"].get_states_for_session_files(
            "verification", ["alice/a.jsonl", "alice/missing.jsonl"]
        )
        assert set(states.keys()) == {"alice/a.jsonl"}
