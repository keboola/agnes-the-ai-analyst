"""Tests for src.marketplace_filter — user → groups → allowed plugins (v12).

Resolution path is now: user → user_group_members → resource_grants
(resource_type='marketplace_plugin', resource_id='<slug>/<plugin>').
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


def _register_marketplace(
    conn, *, id: str, registered_at: datetime, plugins: list[dict]
) -> None:
    conn.execute(
        "INSERT INTO marketplace_registry "
        "(id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [id, id.upper(), f"https://example.test/{id}.git", registered_at],
    )
    for p in plugins:
        conn.execute(
            """INSERT INTO marketplace_plugins
                (marketplace_id, name, version, raw, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            [id, p["name"], p.get("version"), json.dumps(p), datetime.now(timezone.utc)],
        )


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository
    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _make_group(conn, *, name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    return UserGroupsRepository(conn).create(name=name)["id"]


def _add_member(conn, *, user_id: str, group_id: str) -> None:
    from src.repositories.user_group_members import UserGroupMembersRepository
    UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")


def _grant(conn, *, group_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    ResourceGrantsRepository(conn).create(
        group_id=group_id,
        resource_type="marketplace_plugin",
        resource_id=f"{marketplace}/{plugin}",
    )


class TestResolveAllowedPlugins:
    def test_admin_filtered_through_grants_like_anyone_else(self, db_conn):
        # Admin is just one of the user's groups — no god-mode shortcut for
        # the marketplace feed. Without grants on Admin (or another of their
        # groups), an admin sees nothing; with grants, they see exactly what
        # those grants allow.
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt-a", registered_at=t,
            plugins=[{"name": "p1", "version": "1.0"}])
        _register_marketplace(db_conn, id="mkt-b", registered_at=t,
            plugins=[{"name": "p2", "version": "2.0"}, {"name": "p3", "version": "3.0"}])
        _make_user(db_conn, user_id="u-admin", email="admin@x")
        admin_gid = db_conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
        _add_member(db_conn, user_id="u-admin", group_id=admin_gid)

        admin = {"id": "u-admin"}
        # Without any grants admin sees zero plugins.
        assert resolve_allowed_plugins(db_conn, admin) == []

        # Grant Admin two of the three plugins; admin now sees exactly those.
        _grant(db_conn, group_id=admin_gid, marketplace="mkt-a", plugin="p1")
        _grant(db_conn, group_id=admin_gid, marketplace="mkt-b", plugin="p3")
        result = resolve_allowed_plugins(db_conn, admin)
        prefixed = {p["prefixed_name"] for p in result}
        assert prefixed == {"mkt-a-p1", "mkt-b-p3"}

    def test_everyone_grants_visible_to_all(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "public", "version": "1.0"}])
        everyone_gid = db_conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
        _grant(db_conn, group_id=everyone_gid, marketplace="mkt", plugin="public")

        _make_user(db_conn, user_id="u1", email="u1@x")
        result = resolve_allowed_plugins(db_conn, {"id": "u1"})
        assert [p["prefixed_name"] for p in result] == ["mkt-public"]

    def test_multi_group_distinct(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "shared", "version": "1.0"}])
        g1 = _make_group(db_conn, name="G1")
        g2 = _make_group(db_conn, name="G2")
        _grant(db_conn, group_id=g1, marketplace="mkt", plugin="shared")
        _grant(db_conn, group_id=g2, marketplace="mkt", plugin="shared")
        _make_user(db_conn, user_id="u2", email="u2@x")
        _add_member(db_conn, user_id="u2", group_id=g1)
        _add_member(db_conn, user_id="u2", group_id=g2)

        result = resolve_allowed_plugins(db_conn, {"id": "u2"})
        assert [p["prefixed_name"] for p in result] == ["mkt-shared"]

    def test_same_name_across_marketplaces(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="internal", registered_at=t,
            plugins=[{"name": "grpn-eng", "version": "1.0"}])
        _register_marketplace(db_conn, id="vendor", registered_at=t,
            plugins=[{"name": "grpn-eng", "version": "9.0"}])
        gid = _make_group(db_conn, name="Mixed")
        _grant(db_conn, group_id=gid, marketplace="internal", plugin="grpn-eng")
        _grant(db_conn, group_id=gid, marketplace="vendor", plugin="grpn-eng")
        _make_user(db_conn, user_id="u", email="u@x")
        _add_member(db_conn, user_id="u", group_id=gid)

        result = resolve_allowed_plugins(db_conn, {"id": "u"})
        prefixed = sorted(p["prefixed_name"] for p in result)
        assert prefixed == ["internal-grpn-eng", "vendor-grpn-eng"]

    def test_deterministic_order_by_registered_at(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        from datetime import timedelta
        earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = earlier + timedelta(days=30)
        _register_marketplace(db_conn, id="later-mkt", registered_at=later,
            plugins=[{"name": "p", "version": "1"}])
        _register_marketplace(db_conn, id="earlier-mkt", registered_at=earlier,
            plugins=[{"name": "p", "version": "1"}])
        _make_user(db_conn, user_id="u2", email="a2@x")
        gid = _make_group(db_conn, name="Order")
        _add_member(db_conn, user_id="u2", group_id=gid)
        _grant(db_conn, group_id=gid, marketplace="earlier-mkt", plugin="p")
        _grant(db_conn, group_id=gid, marketplace="later-mkt", plugin="p")

        result = resolve_allowed_plugins(db_conn, {"id": "u2"})
        order = [p["marketplace_id"] for p in result]
        assert order == ["earlier-mkt", "later-mkt"]

    def test_user_with_unknown_group_sees_nothing(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "p", "version": "1"}])
        _make_user(db_conn, user_id="u-nogroup", email="ng@x")
        # Has only Everyone (auto-membership) but no grants on Everyone.
        result = resolve_allowed_plugins(db_conn, {"id": "u-nogroup"})
        assert result == []


# ETag tests (unchanged from v11) — still uses the in-process compute_etag helper.


class TestComputeEtag:
    def test_same_inputs_same_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        plugin = {
            "prefixed_name": "mkt-p", "version": "1.0",
            "plugin_dir": tmp_path / "mkt" / "plugins" / "p",
        }
        plugin["plugin_dir"].mkdir(parents=True)
        (plugin["plugin_dir"] / "file.txt").write_bytes(b"hello")
        e1 = compute_etag([plugin])
        e2 = compute_etag([plugin])
        assert e1 == e2 and len(e1) == 16

    def test_content_change_changes_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        plugin = {
            "prefixed_name": "mkt-p", "version": "1.0",
            "plugin_dir": tmp_path / "mkt" / "plugins" / "p",
        }
        plugin["plugin_dir"].mkdir(parents=True)
        f = plugin["plugin_dir"] / "file.txt"
        f.write_bytes(b"hello")
        before = compute_etag([plugin])
        f.write_bytes(b"world")
        after = compute_etag([plugin])
        assert before != after

    def test_version_change_changes_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        plugin = {
            "prefixed_name": "mkt-p", "version": "1.0",
            "plugin_dir": tmp_path / "mkt" / "plugins" / "p",
        }
        plugin["plugin_dir"].mkdir(parents=True)
        (plugin["plugin_dir"] / "file.txt").write_bytes(b"x")
        e1 = compute_etag([plugin])
        plugin["version"] = "2.0"
        e2 = compute_etag([plugin])
        assert e1 != e2

    def test_missing_plugin_dir_does_not_crash(self, tmp_path):
        from src.marketplace_filter import compute_etag
        e = compute_etag(
            [{"prefixed_name": "x", "version": "1", "plugin_dir": tmp_path / "missing"}]
        )
        assert len(e) == 16

    def test_empty_plugin_list(self):
        from src.marketplace_filter import compute_etag
        assert len(compute_etag([])) == 16
