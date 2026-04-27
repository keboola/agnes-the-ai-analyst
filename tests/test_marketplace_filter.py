"""Tests for src.marketplace_filter: user → groups → allowed plugins."""

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
    """Register a marketplace in registry + cache its plugins.

    plugins: list of dicts following the shape used in marketplace_plugins.raw
             (e.g. {"name": "p1", "version": "1.0", "source": "./plugins/p1"}).
    """
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
            [
                id,
                p["name"],
                p.get("version"),
                json.dumps(p),
                datetime.now(timezone.utc),
            ],
        )


def _make_plugin_dir(data_dir: Path, slug: str, plugin_name: str, files: dict[str, bytes]) -> None:
    base = data_dir / "marketplaces" / slug / "plugins" / plugin_name
    base.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


# ----------------------------------------------------------------------------
# resolve_user_groups
# ----------------------------------------------------------------------------


class TestResolveUserGroups:
    def test_admin_role_gets_admin_group(self):
        from src.marketplace_filter import resolve_user_groups
        assert resolve_user_groups({"role": "admin"}) == ["Admin"]

    def test_admin_role_overrides_explicit_groups(self):
        from src.marketplace_filter import resolve_user_groups
        user = {"role": "admin", "groups": '["Foo", "Bar"]'}
        assert resolve_user_groups(user) == ["Admin"]

    def test_empty_groups_fallback_to_everyone(self):
        from src.marketplace_filter import resolve_user_groups
        assert resolve_user_groups({"role": "analyst"}) == ["Everyone"]
        assert resolve_user_groups({"role": "analyst", "groups": None}) == ["Everyone"]
        assert resolve_user_groups({"role": "analyst", "groups": "[]"}) == ["Everyone"]
        assert resolve_user_groups({"role": "analyst", "groups": []}) == ["Everyone"]

    def test_explicit_groups_from_json_string(self):
        from src.marketplace_filter import resolve_user_groups
        user = {"role": "analyst", "groups": '["Engineering", "Finance"]'}
        assert resolve_user_groups(user) == ["Engineering", "Finance"]

    def test_explicit_groups_from_python_list(self):
        from src.marketplace_filter import resolve_user_groups
        user = {"role": "analyst", "groups": ["Legal"]}
        assert resolve_user_groups(user) == ["Legal"]

    def test_malformed_groups_treated_as_empty(self):
        from src.marketplace_filter import resolve_user_groups
        assert resolve_user_groups({"role": "analyst", "groups": "not-json"}) == ["Everyone"]
        assert resolve_user_groups({"role": "analyst", "groups": '{"not":"array"}'}) == ["Everyone"]


# ----------------------------------------------------------------------------
# resolve_allowed_plugins
# ----------------------------------------------------------------------------


class TestResolveAllowedPlugins:
    def test_admin_sees_every_plugin_across_marketplaces(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(
            db_conn, id="mkt-a", registered_at=t,
            plugins=[{"name": "p1", "version": "1.0"}],
        )
        _register_marketplace(
            db_conn, id="mkt-b", registered_at=t,
            plugins=[{"name": "p2", "version": "2.0"}, {"name": "p3", "version": "3.0"}],
        )

        admin = {"id": "u-admin", "role": "admin", "groups": None}
        result = resolve_allowed_plugins(db_conn, admin)
        prefixed = {p["prefixed_name"] for p in result}
        assert prefixed == {"mkt-a-p1", "mkt-b-p2", "mkt-b-p3"}

    def test_everyone_fallback_for_user_without_groups(self, db_conn):
        """user.groups = [] → treated as member of 'Everyone'.

        With no grants for Everyone, result is empty. With a grant, they see it.
        """
        from src.marketplace_filter import resolve_allowed_plugins
        from src.repositories.plugin_access import (
            UserGroupsRepository, PluginAccessRepository,
        )
        t = datetime.now(timezone.utc)
        _register_marketplace(
            db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "public-plug", "version": "1.0"}],
        )
        db_conn.execute(
            "INSERT OR IGNORE INTO user_groups (id, name, is_system) VALUES (?, ?, TRUE)",
            ["everyone-id", "Everyone"],
        )
        PluginAccessRepository(db_conn).grant("everyone-id", "mkt", "public-plug")

        user = {"id": "u1", "role": "analyst", "groups": None}
        result = resolve_allowed_plugins(db_conn, user)
        assert [p["prefixed_name"] for p in result] == ["mkt-public-plug"]

    def test_multi_group_distinct(self, db_conn):
        """Two groups grant the same plugin — it must appear once."""
        from src.marketplace_filter import resolve_allowed_plugins
        from src.repositories.plugin_access import (
            UserGroupsRepository, PluginAccessRepository,
        )
        t = datetime.now(timezone.utc)
        _register_marketplace(
            db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "shared", "version": "1.0"}],
        )
        ug = UserGroupsRepository(db_conn)
        g1 = ug.create(name="G1")
        g2 = ug.create(name="G2")
        access = PluginAccessRepository(db_conn)
        access.grant(g1["id"], "mkt", "shared")
        access.grant(g2["id"], "mkt", "shared")

        user = {"id": "u2", "role": "analyst", "groups": json.dumps(["G1", "G2"])}
        result = resolve_allowed_plugins(db_conn, user)
        assert [p["prefixed_name"] for p in result] == ["mkt-shared"]

    def test_same_name_across_marketplaces_kept_as_two_plugins(self, db_conn):
        """A plugin named 'grpn-eng' in two marketplaces is two different plugins."""
        from src.marketplace_filter import resolve_allowed_plugins
        from src.repositories.plugin_access import (
            UserGroupsRepository, PluginAccessRepository,
        )
        t = datetime.now(timezone.utc)
        _register_marketplace(
            db_conn, id="internal", registered_at=t,
            plugins=[{"name": "grpn-eng", "version": "1.0"}],
        )
        _register_marketplace(
            db_conn, id="vendor", registered_at=t,
            plugins=[{"name": "grpn-eng", "version": "9.0"}],
        )
        ug = UserGroupsRepository(db_conn)
        g = ug.create(name="Mixed")
        access = PluginAccessRepository(db_conn)
        access.grant(g["id"], "internal", "grpn-eng")
        access.grant(g["id"], "vendor", "grpn-eng")

        user = {"id": "u", "role": "analyst", "groups": json.dumps(["Mixed"])}
        result = resolve_allowed_plugins(db_conn, user)
        prefixed = sorted(p["prefixed_name"] for p in result)
        assert prefixed == ["internal-grpn-eng", "vendor-grpn-eng"]

    def test_deterministic_order_by_registered_at(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        from datetime import timedelta
        earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = earlier + timedelta(days=30)
        _register_marketplace(
            db_conn, id="later-mkt", registered_at=later,
            plugins=[{"name": "p", "version": "1"}],
        )
        _register_marketplace(
            db_conn, id="earlier-mkt", registered_at=earlier,
            plugins=[{"name": "p", "version": "1"}],
        )
        admin = {"role": "admin"}
        result = resolve_allowed_plugins(db_conn, admin)
        order = [p["marketplace_id"] for p in result]
        assert order == ["earlier-mkt", "later-mkt"]

    def test_user_with_unknown_group_returns_nothing(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins
        t = datetime.now(timezone.utc)
        _register_marketplace(
            db_conn, id="mkt", registered_at=t,
            plugins=[{"name": "p", "version": "1"}],
        )
        user = {"id": "u", "role": "analyst", "groups": json.dumps(["DoesNotExist"])}
        result = resolve_allowed_plugins(db_conn, user)
        assert result == []


# ----------------------------------------------------------------------------
# compute_etag
# ----------------------------------------------------------------------------


class TestComputeEtag:
    def test_same_inputs_same_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        plugin = {
            "prefixed_name": "mkt-p",
            "version": "1.0",
            "plugin_dir": tmp_path / "mkt" / "plugins" / "p",
        }
        plugin["plugin_dir"].mkdir(parents=True)
        (plugin["plugin_dir"] / "file.txt").write_bytes(b"hello")
        e1 = compute_etag([plugin])
        e2 = compute_etag([plugin])
        assert e1 == e2
        assert len(e1) == 16

    def test_content_change_changes_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        plugin = {
            "prefixed_name": "mkt-p",
            "version": "1.0",
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
            "prefixed_name": "mkt-p",
            "version": "1.0",
            "plugin_dir": tmp_path / "mkt" / "plugins" / "p",
        }
        plugin["plugin_dir"].mkdir(parents=True)
        (plugin["plugin_dir"] / "file.txt").write_bytes(b"x")
        e1 = compute_etag([plugin])
        plugin["version"] = "2.0"
        e2 = compute_etag([plugin])
        assert e1 != e2

    def test_plugin_name_change_changes_etag(self, tmp_path):
        from src.marketplace_filter import compute_etag
        d = tmp_path / "mkt" / "plugins" / "p"
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"x")
        e1 = compute_etag([{"prefixed_name": "a", "version": "1", "plugin_dir": d}])
        e2 = compute_etag([{"prefixed_name": "b", "version": "1", "plugin_dir": d}])
        assert e1 != e2

    def test_missing_plugin_dir_does_not_crash(self, tmp_path):
        from src.marketplace_filter import compute_etag
        e = compute_etag(
            [{"prefixed_name": "x", "version": "1", "plugin_dir": tmp_path / "missing"}]
        )
        assert len(e) == 16

    def test_empty_plugin_list(self):
        from src.marketplace_filter import compute_etag
        e = compute_etag([])
        assert len(e) == 16
