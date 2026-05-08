"""Repository tests for store_entities, user_store_installs, user_plugin_optouts."""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository
    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _create_entity(conn, *, owner_id: str, owner_username: str, name: str,
                   type_: str = "skill") -> str:
    from src.repositories.store_entities import StoreEntitiesRepository
    repo = StoreEntitiesRepository(conn)
    eid = uuid.uuid4().hex
    repo.create(
        id=eid, owner_user_id=owner_id, owner_username=owner_username,
        type=type_, name=name, description="desc", category=None,
        version="abcd1234abcd1234", file_size=100,
    )
    return eid


class TestStoreEntities:
    def test_create_and_get(self, db_conn):
        from src.repositories.store_entities import StoreEntitiesRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        eid = _create_entity(db_conn, owner_id="u1", owner_username="u1", name="my-skill")
        e = StoreEntitiesRepository(db_conn).get(eid)
        assert e is not None
        assert e["name"] == "my-skill"
        assert e["owner_username"] == "u1"
        assert e["install_count"] == 0
        assert e["doc_paths"] == []

    def test_unique_owner_name(self, db_conn):
        _make_user(db_conn, user_id="u1", email="u1@x")
        _create_entity(db_conn, owner_id="u1", owner_username="u1", name="dup")
        with pytest.raises(Exception):
            _create_entity(db_conn, owner_id="u1", owner_username="u1", name="dup")

    def test_different_owners_same_name_ok(self, db_conn):
        _make_user(db_conn, user_id="u1", email="u1@x")
        _make_user(db_conn, user_id="u2", email="u2@x")
        _create_entity(db_conn, owner_id="u1", owner_username="u1", name="shared")
        _create_entity(db_conn, owner_id="u2", owner_username="u2", name="shared")

    def test_list_with_filters(self, db_conn):
        from src.repositories.store_entities import StoreEntitiesRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        _create_entity(db_conn, owner_id="u1", owner_username="u1", name="alpha", type_="skill")
        _create_entity(db_conn, owner_id="u1", owner_username="u1", name="beta", type_="agent")
        _create_entity(db_conn, owner_id="u1", owner_username="u1", name="gamma", type_="plugin")

        repo = StoreEntitiesRepository(db_conn)
        items, total = repo.list(skip=0, limit=10)
        assert total == 3
        assert len(items) == 3

        items, total = repo.list(skip=0, limit=10, type="skill")
        assert total == 1 and items[0]["name"] == "alpha"

        items, total = repo.list(skip=0, limit=10, search="bet")
        assert total == 1 and items[0]["name"] == "beta"

    def test_bump_install_count(self, db_conn):
        from src.repositories.store_entities import StoreEntitiesRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        eid = _create_entity(db_conn, owner_id="u1", owner_username="u1", name="x")
        repo = StoreEntitiesRepository(db_conn)
        repo.bump_install_count(eid, 1)
        repo.bump_install_count(eid, 1)
        assert repo.get(eid)["install_count"] == 2
        repo.bump_install_count(eid, -1)
        assert repo.get(eid)["install_count"] == 1
        # Floor at zero
        repo.bump_install_count(eid, -10)
        assert repo.get(eid)["install_count"] == 0


class TestUserStoreInstalls:
    def test_install_idempotent(self, db_conn):
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        _make_user(db_conn, user_id="u2", email="u2@x")
        eid = _create_entity(db_conn, owner_id="u1", owner_username="u1", name="x")
        repo = UserStoreInstallsRepository(db_conn)
        assert repo.install("u2", eid) is True
        assert repo.install("u2", eid) is False
        assert repo.is_installed("u2", eid) is True
        assert repo.installer_count(eid) == 1

    def test_uninstall(self, db_conn):
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        _make_user(db_conn, user_id="u2", email="u2@x")
        eid = _create_entity(db_conn, owner_id="u1", owner_username="u1", name="x")
        repo = UserStoreInstallsRepository(db_conn)
        repo.install("u2", eid)
        assert repo.uninstall("u2", eid) is True
        assert repo.uninstall("u2", eid) is False  # already gone

    def test_list_for_user_joins_entity(self, db_conn):
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        _make_user(db_conn, user_id="u1", email="u1@x")
        _make_user(db_conn, user_id="u2", email="u2@x")
        eid = _create_entity(db_conn, owner_id="u1", owner_username="u1", name="zzz")
        repo = UserStoreInstallsRepository(db_conn)
        repo.install("u2", eid)
        rows = repo.list_for_user("u2")
        assert len(rows) == 1
        assert rows[0]["name"] == "zzz"
        assert rows[0]["owner_username"] == "u1"


class TestUserCuratedSubscriptions:
    """Same physical table (user_plugin_optouts) as the legacy opt-out repo,
    but with v27+ Model B semantics: presence = subscribed.
    """

    def test_subscribe_unsubscribe(self, db_conn):
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        _make_user(db_conn, user_id="u1", email="u1@x")
        repo = UserCuratedSubscriptionsRepository(db_conn)
        assert repo.subscribe("u1", "mkt", "p1") is True
        assert repo.is_subscribed("u1", "mkt", "p1") is True
        assert ("mkt", "p1") in repo.subscribed_set("u1")
        assert repo.unsubscribe("u1", "mkt", "p1") is True
        assert repo.is_subscribed("u1", "mkt", "p1") is False
        assert repo.subscribed_set("u1") == set()

    def test_subscribe_idempotent(self, db_conn):
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        _make_user(db_conn, user_id="u1", email="u1@x")
        repo = UserCuratedSubscriptionsRepository(db_conn)
        assert repo.subscribe("u1", "mkt", "p1") is True
        assert repo.subscribe("u1", "mkt", "p1") is False  # second call: no-op
        assert len(repo.list_for_user("u1")) == 1

    def test_delete_for_plugin_drops_all_users(self, db_conn):
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        _make_user(db_conn, user_id="u1", email="u1@x")
        _make_user(db_conn, user_id="u2", email="u2@x")
        repo = UserCuratedSubscriptionsRepository(db_conn)
        repo.subscribe("u1", "mkt", "p1")
        repo.subscribe("u2", "mkt", "p1")
        repo.subscribe("u1", "mkt", "p2")  # different plugin — survives
        dropped = repo.delete_for_plugin("mkt", "p1")
        assert dropped == 2
        assert repo.subscribed_set("u1") == {("mkt", "p2")}
        assert repo.subscribed_set("u2") == set()

    def test_delete_for_marketplace(self, db_conn):
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        _make_user(db_conn, user_id="u1", email="u1@x")
        repo = UserCuratedSubscriptionsRepository(db_conn)
        repo.subscribe("u1", "mkt-a", "p1")
        repo.subscribe("u1", "mkt-a", "p2")
        repo.subscribe("u1", "mkt-b", "p1")
        dropped = repo.delete_for_marketplace("mkt-a")
        assert dropped == 2
        assert repo.subscribed_set("u1") == {("mkt-b", "p1")}
