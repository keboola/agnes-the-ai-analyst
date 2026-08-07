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


def test_list_for_user_with_dates(repo):
    repo.subscribe("u", "data_package", "pkg_1")
    repo.subscribe("u", "memory_domain", "dom_1")
    # Other-user noise that must not bleed through.
    repo.subscribe("other", "data_package", "pkg_2")

    rows = repo.list_for_user_with_dates("u")
    assert {(r["resource_type"], r["resource_id"]) for r in rows} == {
        ("data_package", "pkg_1"),
        ("memory_domain", "dom_1"),
    }
    # Every row carries a real subscribed_at timestamp.
    assert all(r["subscribed_at"] is not None for r in rows)

    assert repo.list_for_user_with_dates("nobody") == []


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


# ---------------------------------------------------------------------------
# subscribe_group_members — soft-downgrade fan-out parity (#518)
# ---------------------------------------------------------------------------


def _seed_group_with_members(repo, group_id, member_ids):
    """Seed a user_groups row (FK target) + its members on whichever backend
    the repo is bound to."""
    if hasattr(repo, "conn"):  # DuckDB
        repo.conn.execute("INSERT INTO user_groups(id, name) VALUES (?, ?)", [group_id, group_id])
        for uid in member_ids:
            repo.conn.execute(
                "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'manual')",
                [uid, group_id],
            )
    else:  # PG
        import sqlalchemy as sa

        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO user_groups(id, name) VALUES (:g, :g)"),
                {"g": group_id},
            )
            for uid in member_ids:
                conn.execute(
                    sa.text("INSERT INTO user_group_members(user_id, group_id, source) VALUES (:u, :g, 'manual')"),
                    {"u": uid, "g": group_id},
                )


def test_subscribe_group_members_subscribes_all_and_is_idempotent(repo):
    _seed_group_with_members(repo, "grp_1", ["alice", "bob"])

    n = repo.subscribe_group_members("grp_1", "data_package", "pkg_1")
    assert n == 2
    assert repo.is_subscribed("alice", "data_package", "pkg_1") is True
    assert repo.is_subscribed("bob", "data_package", "pkg_1") is True

    # Idempotent: re-running creates no new rows.
    assert repo.subscribe_group_members("grp_1", "data_package", "pkg_1") == 0


def test_subscribe_group_members_empty_group_is_noop(repo):
    _seed_group_with_members(repo, "grp_empty", [])
    assert repo.subscribe_group_members("grp_empty", "data_package", "pkg_1") == 0
