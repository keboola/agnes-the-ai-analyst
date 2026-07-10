"""Cross-engine contract tests for the user_curated_subscriptions repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back.

``user_plugin_optouts`` is the backing table (row presence = subscribed,
v28 semantic). ``stack_counts`` returns
``{(marketplace_id, plugin_name): subscriber_count}`` for every plugin
with at least one subscriber.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UserCuratedSubscriptionsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.user_curated_subscriptions_pg import (
        UserCuratedSubscriptionsPgRepository,
    )

    return UserCuratedSubscriptionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def curated_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a user_curated_subscriptions repo for either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_stack_counts_empty(curated_repo):
    assert curated_repo.stack_counts() == {}


def test_stack_counts_single_subscriber(curated_repo):
    curated_repo.subscribe("alice", "mkt-a", "plugin-x")
    counts = curated_repo.stack_counts()
    assert counts == {("mkt-a", "plugin-x"): 1}


def test_stack_counts_multiple_subscribers_and_plugins(curated_repo):
    curated_repo.subscribe("alice", "mkt-a", "plugin-x")
    curated_repo.subscribe("bob", "mkt-a", "plugin-x")
    curated_repo.subscribe("carol", "mkt-a", "plugin-x")
    curated_repo.subscribe("alice", "mkt-a", "plugin-y")
    curated_repo.subscribe("alice", "mkt-b", "plugin-z")

    counts = curated_repo.stack_counts()
    assert counts[("mkt-a", "plugin-x")] == 3
    assert counts[("mkt-a", "plugin-y")] == 1
    assert counts[("mkt-b", "plugin-z")] == 1
    assert len(counts) == 3


def test_stack_counts_idempotent_subscribe_not_double_counted(curated_repo):
    curated_repo.subscribe("alice", "mkt-a", "plugin-x")
    curated_repo.subscribe("alice", "mkt-a", "plugin-x")  # duplicate — no-op
    counts = curated_repo.stack_counts()
    assert counts == {("mkt-a", "plugin-x"): 1}


def test_stack_counts_drops_to_zero_after_unsubscribe(curated_repo):
    curated_repo.subscribe("alice", "mkt-a", "plugin-x")
    curated_repo.unsubscribe("alice", "mkt-a", "plugin-x")
    assert curated_repo.stack_counts() == {}
