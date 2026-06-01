"""Integration tests for UserStackSubscriptionsPgRepository.

PG-side smoke. Cross-engine parity covered in Task 1D.5 contract test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.user_stack_subscriptions_pg import (
        UserStackSubscriptionsPgRepository,
    )

    return UserStackSubscriptionsPgRepository(pg_engine)


def test_subscribe_then_is_subscribed(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_other") is False


def test_subscribe_is_idempotent(repo):
    first = repo.subscribe("user_a", "data_package", "pkg_1")
    second = repo.subscribe("user_a", "data_package", "pkg_1")  # no exception
    assert first is True
    assert second is False  # DuckDB sibling returns False on duplicate
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is True


def test_unsubscribe(repo):
    repo.subscribe("user_a", "data_package", "pkg_1")
    deleted = repo.unsubscribe("user_a", "data_package", "pkg_1")
    assert deleted is True
    assert repo.is_subscribed("user_a", "data_package", "pkg_1") is False
    # Idempotent: second unsubscribe returns False.
    assert repo.unsubscribe("user_a", "data_package", "pkg_1") is False


def test_list_for_user_filters_by_type(repo):
    repo.subscribe("u", "data_package", "pkg_1")
    repo.subscribe("u", "data_package", "pkg_2")
    repo.subscribe("u", "recipe", "rec_1")
    result = repo.list_for_user("u", "data_package")
    assert sorted(result) == ["pkg_1", "pkg_2"]


def test_list_users_subscribed_to(repo):
    repo.subscribe("alice", "data_package", "pkg_1")
    repo.subscribe("bob", "data_package", "pkg_1")
    repo.subscribe("alice", "data_package", "pkg_2")
    users = repo.list_users_subscribed_to("data_package", "pkg_1")
    assert sorted(users) == ["alice", "bob"]
