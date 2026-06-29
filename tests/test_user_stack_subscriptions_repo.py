"""Repository tests for ``user_stack_subscriptions`` (v49).

Generic per-user opt-in for ``requirement='available'`` grants. Currently
scoped to ``data_package`` + ``memory_domain`` resource types — Marketplace
plugins stay on their own ``user_plugin_optouts`` table per D1.
"""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.user_stack_subscriptions import (
    UserStackSubscriptionsRepository,
)


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return UserStackSubscriptionsRepository(conn)


class TestSubscribe:
    def test_subscribe_returns_true_on_first_call(self, repo):
        assert repo.subscribe("u1", "data_package", "pkg_sales") is True

    def test_subscribe_idempotent(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        # Second call returns False — row already exists.
        assert repo.subscribe("u1", "data_package", "pkg_sales") is False


class TestUnsubscribe:
    def test_unsubscribe_returns_true_when_row_existed(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        assert repo.unsubscribe("u1", "data_package", "pkg_sales") is True

    def test_unsubscribe_returns_false_when_missing(self, repo):
        assert repo.unsubscribe("u1", "data_package", "pkg_sales") is False

    def test_unsubscribe_idempotent_after_first_delete(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        repo.unsubscribe("u1", "data_package", "pkg_sales")
        # A second unsubscribe returns False but doesn't raise.
        assert repo.unsubscribe("u1", "data_package", "pkg_sales") is False


class TestIsSubscribed:
    def test_is_subscribed_after_subscribe(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        assert repo.is_subscribed("u1", "data_package", "pkg_sales") is True

    def test_not_subscribed_for_other_user(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        assert repo.is_subscribed("u2", "data_package", "pkg_sales") is False

    def test_not_subscribed_for_different_resource_type(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        assert repo.is_subscribed("u1", "memory_domain", "pkg_sales") is False


class TestListForUser:
    def test_returns_ids_filtered_by_type(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        repo.subscribe("u1", "data_package", "pkg_finance")
        repo.subscribe("u1", "memory_domain", "md_finance")
        pkgs = repo.list_for_user("u1", "data_package")
        assert set(pkgs) == {"pkg_sales", "pkg_finance"}
        doms = repo.list_for_user("u1", "memory_domain")
        assert doms == ["md_finance"]

    def test_returns_empty_for_user_with_no_subs(self, repo):
        assert repo.list_for_user("nobody", "data_package") == []


class TestListUsersSubscribedTo:
    def test_returns_distinct_user_ids(self, repo):
        repo.subscribe("u1", "data_package", "pkg_sales")
        repo.subscribe("u2", "data_package", "pkg_sales")
        repo.subscribe("u3", "data_package", "pkg_other")
        users = repo.list_users_subscribed_to("data_package", "pkg_sales")
        assert set(users) == {"u1", "u2"}

    def test_returns_empty_when_no_subscribers(self, repo):
        assert repo.list_users_subscribed_to("data_package", "pkg_sales") == []
