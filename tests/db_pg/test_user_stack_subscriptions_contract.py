"""Cross-engine contract tests for the user_stack_subscriptions repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_recipes_contract.py`` (Task 1D.4).
No FK-target seeding needed — ``user_stack_subscriptions`` is a pure
composite-PK association table that references ``resource_type`` /
``resource_id`` as opaque strings (no FK to ``user_groups`` /
``resource_grants`` / etc.). No JSONB, no soft-delete.

Closes Phase 1D of the PG follow-up plan — five contract suites covering
five repository clusters with one shared parametrisation pattern.
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
    from src.repositories.user_stack_subscriptions import (
        UserStackSubscriptionsRepository,
    )

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UserStackSubscriptionsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
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

    from src.repositories.user_stack_subscriptions_pg import (
        UserStackSubscriptionsPgRepository,
    )
    return UserStackSubscriptionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a user_stack_subscriptions repo bound to either DuckDB or PG."""
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
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------

def test_subscribe_then_is_subscribed(repo):
    assert repo.subscribe("user_a", "data_package", "pkg_1") is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_other") is False
    assert repo.is_subscribed("user_b", "data_package", "pkg_1") is False


def test_subscribe_idempotent(repo):
    first = repo.subscribe("u", "data_package", "pkg_1")
    second = repo.subscribe("u", "data_package", "pkg_1")  # no exception
    assert first is True
    assert second is False
    assert repo.is_subscribed("u", "data_package", "pkg_1") is True


def test_unsubscribe(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    assert repo.unsubscribe("user_a", "data_package", "pkg_1") is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is False
    # Idempotent: second unsubscribe returns False.
    assert repo.unsubscribe("user_a", "data_package", "pkg_1") is False


def test_list_for_user_filtered_by_type(repo):
    repo.subscribe("u", "data_package", "pkg_1")
    repo.subscribe("u", "data_package", "pkg_2")
    repo.subscribe("u", "memory_domain", "dom_1")
    # Other-user noise that must not bleed through.
    repo.subscribe("other", "data_package", "pkg_3")

    packages = repo.list_for_user("u", "data_package")
    assert sorted(packages) == ["pkg_1", "pkg_2"]

    domains = repo.list_for_user("u", "memory_domain")
    assert domains == ["dom_1"]

    assert repo.list_for_user("u", "unknown_type") == []
    assert repo.list_for_user("nobody", "data_package") == []


def test_list_users_subscribed_to(repo):
    repo.subscribe("alice", "data_package", "pkg_1")
    repo.subscribe("bob", "data_package", "pkg_1")
    repo.subscribe("alice", "data_package", "pkg_2")
    # Cross-type noise that must not bleed through.
    repo.subscribe("carol", "memory_domain", "pkg_1")

    users = repo.list_users_subscribed_to("data_package", "pkg_1")
    assert sorted(users) == ["alice", "bob"]

    assert repo.list_users_subscribed_to("data_package", "pkg_2") == ["alice"]
    assert repo.list_users_subscribed_to("data_package", "missing") == []
