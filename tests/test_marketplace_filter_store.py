"""Tests for the composition layer in src.marketplace_filter.

Covers ``resolve_user_marketplace`` — Model B (v27+) served plugin set built
from admin grants intersected with explicit user subscriptions, unioned with
store installs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


def _register_marketplace(conn, *, id, plugins):
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [id, id.upper(), f"https://example.test/{id}.git", datetime.now(timezone.utc)],
    )
    for p in plugins:
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
            [id, p["name"], p.get("version"), json.dumps(p), datetime.now(timezone.utc)],
        )


def _make_user(conn, *, user_id, email):
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _make_group(conn, *, name):
    from src.repositories.user_groups import UserGroupsRepository

    return UserGroupsRepository(conn).create(name=name)["id"]


def _add_member(conn, *, user_id, group_id):
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")


def _grant(conn, *, group_id, marketplace, plugin, requirement=None):
    from src.repositories.resource_grants import ResourceGrantsRepository

    ResourceGrantsRepository(conn).create(
        group_id=group_id,
        resource_type="marketplace_plugin",
        resource_id=f"{marketplace}/{plugin}",
        requirement=requirement,
    )


def _seed_user_with_grant(conn, *, marketplace, plugin, user_id="u1", requirement=None):
    _register_marketplace(conn, id=marketplace, plugins=[{"name": plugin, "version": "1.0"}])
    gid = _make_group(conn, name=f"G-{user_id}")
    _grant(conn, group_id=gid, marketplace=marketplace, plugin=plugin, requirement=requirement)
    _make_user(conn, user_id=user_id, email=f"{user_id}@x")
    _add_member(conn, user_id=user_id, group_id=gid)


def _create_store_entity(conn, *, owner_id, owner_username, name, type_="skill", visibility_status="approved"):
    """Default ``visibility_status='approved'`` so these tests exercise the
    marketplace filter, not the v29 guardrail flow. See
    docs/STORE_GUARDRAILS.md — the guardrail wiring lives at the API layer
    and gates uploads BEFORE rows reach this repo, so unit tests of the
    composition layer skip it intentionally."""
    from src.repositories.store_entities import StoreEntitiesRepository

    eid = uuid.uuid4().hex
    StoreEntitiesRepository(conn).create(
        id=eid,
        owner_user_id=owner_id,
        owner_username=owner_username,
        type=type_,
        name=name,
        description="d",
        category=None,
        version="abc1234567890def",
        file_size=10,
        visibility_status=visibility_status,
    )
    return eid


def _install_for(conn, *, user_id, entity_id):
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    UserStoreInstallsRepository(conn).install(user_id, entity_id)


def _subscribe(conn, *, user_id, marketplace, plugin):
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    UserCuratedSubscriptionsRepository(conn).subscribe(user_id, marketplace, plugin)


class TestResolveUserMarketplace:
    def test_admin_grant_without_subscription_returns_empty(self, db_conn):
        """Model B: RBAC grant alone is no longer enough — caller must
        explicitly subscribe before the plugin enters the served set."""
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert result == []

    def test_admin_grant_plus_subscribe_yields_entry(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        _subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 1
        assert result[0]["source"] == "marketplace"
        assert result[0]["prefixed_name"] == "mkt-p1"

    def test_unsubscribe_removes_from_view(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        repo = UserCuratedSubscriptionsRepository(db_conn)
        repo.subscribe("u1", "mkt", "p1")
        repo.unsubscribe("u1", "mkt", "p1")
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert result == []

    def test_required_grant_served_without_subscription(self, db_conn):
        """v49 ``requirement='required'`` is the always-in-stack tier —
        a required marketplace_plugin grant enters the served set with
        NO explicit subscription, mirroring the StackResolver's
        ``required ∪ subscribed`` union for data packages."""
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1", requirement="required")
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert [p["prefixed_name"] for p in result] == ["mkt-p1"]
        assert result[0]["source"] == "marketplace"

    def test_required_plus_subscribed_appears_once(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1", requirement="required")
        _subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert [p["prefixed_name"] for p in result] == ["mkt-p1"]

    def test_required_beats_available_across_groups(self, db_conn):
        """Section 4.3 OR rule: a required grant in ANY of the user's
        groups wins over an available grant on the same plugin."""
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        g2 = _make_group(db_conn, name="G2")
        _grant(
            db_conn,
            group_id=g2,
            marketplace="mkt",
            plugin="p1",
            requirement="required",
        )
        _add_member(db_conn, user_id="u1", group_id=g2)
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert [p["prefixed_name"] for p in result] == ["mkt-p1"]

    def test_malformed_required_resource_id_is_ignored(self, db_conn):
        """A required grant whose resource_id has no ``<slug>/<name>``
        separator must not crash the serve path — it is skipped."""
        from src.marketplace_filter import resolve_user_marketplace
        from src.repositories.resource_grants import ResourceGrantsRepository

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        gid = _make_group(db_conn, name="G-mal")
        ResourceGrantsRepository(db_conn).create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="no-slash-here",
            requirement="required",
        )
        _add_member(db_conn, user_id="u1", group_id=gid)
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert result == []  # available grant unsubscribed; malformed skipped

    def test_skill_install_yields_bundle_entry(self, db_conn):
        """A single skill install becomes a bundle entry, not a standalone
        store-<id> plugin. Skills/agents are merged into one synth plugin
        named ``flea`` regardless of how many are installed."""
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        _subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")
        _make_user(db_conn, user_id="owner", email="owner@x")
        eid = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-skill")
        _install_for(db_conn, user_id="u1", entity_id=eid)

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 2
        admin_e = next(p for p in result if p["source"] == "marketplace")
        bundle = next(p for p in result if p["source"] == "store-bundle")
        assert admin_e["prefixed_name"] == "mkt-p1"
        assert bundle["prefixed_name"] == "flea"
        assert bundle["manifest_name"] == "flea"
        assert bundle["marketplace_id"] == "store"
        assert bundle["plugin_dir"] is None
        assert eid in bundle["bundle_entity_ids"]

    def test_multiple_skills_share_one_bundle(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        e1 = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="alpha", type_="skill")
        e2 = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="beta", type_="skill")
        _install_for(db_conn, user_id="u1", entity_id=e1)
        _install_for(db_conn, user_id="u1", entity_id=e2)

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 1  # one bundle, no standalone entries
        bundle = result[0]
        assert bundle["source"] == "store-bundle"
        assert set(bundle["bundle_entity_ids"]) == {e1, e2}

    def test_skill_and_agent_share_bundle(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        s = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-skill", type_="skill")
        a = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-agent", type_="agent")
        _install_for(db_conn, user_id="u1", entity_id=s)
        _install_for(db_conn, user_id="u1", entity_id=a)

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 1
        assert result[0]["source"] == "store-bundle"
        assert set(result[0]["bundle_entity_ids"]) == {s, a}

    def test_plugin_entity_stays_standalone(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        plugin_eid = _create_store_entity(
            db_conn, owner_id="owner", owner_username="owner", name="my-plugin", type_="plugin"
        )
        _install_for(db_conn, user_id="u1", entity_id=plugin_eid)

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 1
        entry = result[0]
        assert entry["source"] == "store"
        assert entry["prefixed_name"] == f"store-{plugin_eid}"
        assert entry["manifest_name"] == "my-plugin-by-owner"

    def test_mixed_plugin_and_skill_two_entries(self, db_conn):
        """Plugin entity stays standalone, skill goes into bundle → 2 entries."""
        from src.marketplace_filter import resolve_user_marketplace

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        p_eid = _create_store_entity(
            db_conn, owner_id="owner", owner_username="owner", name="my-plugin", type_="plugin"
        )
        s_eid = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-skill", type_="skill")
        _install_for(db_conn, user_id="u1", entity_id=p_eid)
        _install_for(db_conn, user_id="u1", entity_id=s_eid)

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 2
        sources = [p["source"] for p in result]
        # Standalone plugin first (alphabetical-ish), bundle last.
        assert "store" in sources and "store-bundle" in sources
        bundle = next(p for p in result if p["source"] == "store-bundle")
        assert bundle["bundle_entity_ids"] == [s_eid]

    def test_store_install_independent_of_subscription(self, db_conn):
        """Curated subscription state only gates curated entries — store
        installs always pass through. Here u1 is granted a plugin but
        never subscribes; the store-installed skill still appears."""
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        _make_user(db_conn, user_id="owner", email="owner@x")
        eid = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-skill")
        _install_for(db_conn, user_id="u1", entity_id=eid)
        # No subscribe call — admin grant alone shouldn't surface the plugin.

        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert len(result) == 1
        assert result[0]["source"] == "store-bundle"

    def test_anonymous_user_returns_empty(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        assert resolve_user_marketplace(db_conn, {}) == []
        assert resolve_user_marketplace(db_conn, {"id": None}) == []

    def test_admin_first_then_bundle_order(self, db_conn):
        from src.marketplace_filter import resolve_user_marketplace

        _seed_user_with_grant(db_conn, marketplace="mkt", plugin="p1")
        _subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")
        _make_user(db_conn, user_id="owner", email="owner@x")
        eid = _create_store_entity(db_conn, owner_id="owner", owner_username="owner", name="my-skill")
        _install_for(db_conn, user_id="u1", entity_id=eid)
        result = resolve_user_marketplace(db_conn, {"id": "u1"})
        assert [p["source"] for p in result] == ["marketplace", "store-bundle"]
