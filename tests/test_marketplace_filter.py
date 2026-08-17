"""Tests for src.marketplace_filter — user → groups → allowed plugins (v12).

Resolution path is now: user → user_group_members → resource_grants
(resource_type='marketplace_plugin', resource_id='<slug>/<plugin>').
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


def _register_marketplace(conn, *, id: str, registered_at: datetime, plugins: list[dict]) -> None:
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
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
        _register_marketplace(db_conn, id="mkt-a", registered_at=t, plugins=[{"name": "p1", "version": "1.0"}])
        _register_marketplace(
            db_conn,
            id="mkt-b",
            registered_at=t,
            plugins=[{"name": "p2", "version": "2.0"}, {"name": "p3", "version": "3.0"}],
        )
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

    def test_everyone_grants_require_explicit_membership(self, db_conn):
        # Auto-Everyone removal: a freshly-created user is no longer
        # implicitly a member of Everyone, so a grant on Everyone is
        # invisible until the user is added as an explicit member.
        from src.marketplace_filter import resolve_allowed_plugins

        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t, plugins=[{"name": "public", "version": "1.0"}])
        everyone_gid = db_conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
        _grant(db_conn, group_id=everyone_gid, marketplace="mkt", plugin="public")

        _make_user(db_conn, user_id="u1", email="u1@x")
        # No membership written → no plugin visible.
        assert resolve_allowed_plugins(db_conn, {"id": "u1"}) == []

        # After explicit membership the grant resolves.
        _add_member(db_conn, user_id="u1", group_id=everyone_gid)
        result = resolve_allowed_plugins(db_conn, {"id": "u1"})
        assert [p["prefixed_name"] for p in result] == ["mkt-public"]

    def test_multi_group_distinct(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins

        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t, plugins=[{"name": "shared", "version": "1.0"}])
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
        _register_marketplace(db_conn, id="internal", registered_at=t, plugins=[{"name": "grpn-eng", "version": "1.0"}])
        _register_marketplace(db_conn, id="vendor", registered_at=t, plugins=[{"name": "grpn-eng", "version": "9.0"}])
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
        _register_marketplace(db_conn, id="later-mkt", registered_at=later, plugins=[{"name": "p", "version": "1"}])
        _register_marketplace(db_conn, id="earlier-mkt", registered_at=earlier, plugins=[{"name": "p", "version": "1"}])
        _make_user(db_conn, user_id="u2", email="a2@x")
        gid = _make_group(db_conn, name="Order")
        _add_member(db_conn, user_id="u2", group_id=gid)
        _grant(db_conn, group_id=gid, marketplace="earlier-mkt", plugin="p")
        _grant(db_conn, group_id=gid, marketplace="later-mkt", plugin="p")

        result = resolve_allowed_plugins(db_conn, {"id": "u2"})
        order = [p["marketplace_id"] for p in result]
        assert order == ["earlier-mkt", "later-mkt"]

    def test_user_with_no_groups_sees_nothing(self, db_conn):
        from src.marketplace_filter import resolve_allowed_plugins

        t = datetime.now(timezone.utc)
        _register_marketplace(db_conn, id="mkt", registered_at=t, plugins=[{"name": "p", "version": "1"}])
        _make_user(db_conn, user_id="u-nogroup", email="ng@x")
        # Auto-Everyone removal: a brand-new user has zero memberships and
        # therefore sees nothing regardless of what's granted on Everyone.
        result = resolve_allowed_plugins(db_conn, {"id": "u-nogroup"})
        assert result == []


def _seed_grant_and_user(conn, *, slug: str, plugin: str, user_id: str = "u-mn") -> None:
    """Helper for TestManifestName: register a marketplace + plugin, create
    a user in a group with a grant on that plugin."""
    t = datetime.now(timezone.utc)
    _register_marketplace(conn, id=slug, registered_at=t, plugins=[{"name": plugin, "version": "1.0"}])
    gid = _make_group(conn, name=f"G-{slug}")
    _grant(conn, group_id=gid, marketplace=slug, plugin=plugin)
    _make_user(conn, user_id=user_id, email=f"{user_id}@x")
    _add_member(conn, user_id=user_id, group_id=gid)


class TestManifestName:
    """resolve_allowed_plugins must surface the plugin's authoritative name
    from its own .claude-plugin/plugin.json. Claude Code's /plugin UI looks
    a loaded plugin back up against its catalog by plugin.json name; if the
    synth marketplace.json's `name` doesn't match, the Components panel
    errors with "Plugin <X> not found in marketplace"."""

    def test_manifest_name_from_plugin_json(self, db_conn, tmp_path):
        from src.marketplace_filter import resolve_allowed_plugins

        _seed_grant_and_user(db_conn, slug="mkt", plugin="dirname")
        plugin_dir = tmp_path / "marketplaces" / "mkt" / "plugins" / "dirname"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "actual-name", "version": "1.0"}),
            encoding="utf-8",
        )

        result = resolve_allowed_plugins(db_conn, {"id": "u-mn"})
        assert len(result) == 1
        assert result[0]["manifest_name"] == "actual-name"
        # prefixed_name is unchanged — it drives the on-disk dir layout.
        assert result[0]["prefixed_name"] == "mkt-dirname"
        assert result[0]["original_name"] == "dirname"

    def test_manifest_name_falls_back_when_plugin_json_missing(self, db_conn, tmp_path):
        from src.marketplace_filter import resolve_allowed_plugins

        _seed_grant_and_user(db_conn, slug="mkt", plugin="myplugin")
        # No plugin_dir on disk at all → falls back to upstream name.
        result = resolve_allowed_plugins(db_conn, {"id": "u-mn"})
        assert len(result) == 1
        assert result[0]["manifest_name"] == "myplugin"

    def test_manifest_name_falls_back_on_malformed_plugin_json(self, db_conn, tmp_path):
        from src.marketplace_filter import resolve_allowed_plugins

        _seed_grant_and_user(db_conn, slug="mkt", plugin="myplugin")
        plugin_dir = tmp_path / "marketplaces" / "mkt" / "plugins" / "myplugin"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            "{ this is : not json",
            encoding="utf-8",
        )

        result = resolve_allowed_plugins(db_conn, {"id": "u-mn"})
        assert len(result) == 1
        assert result[0]["manifest_name"] == "myplugin"

    def test_manifest_name_falls_back_when_name_field_missing(self, db_conn, tmp_path):
        from src.marketplace_filter import resolve_allowed_plugins

        _seed_grant_and_user(db_conn, slug="mkt", plugin="myplugin")
        plugin_dir = tmp_path / "marketplaces" / "mkt" / "plugins" / "myplugin"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"version": "1.0"}),
            encoding="utf-8",
        )

        result = resolve_allowed_plugins(db_conn, {"id": "u-mn"})
        assert len(result) == 1
        assert result[0]["manifest_name"] == "myplugin"


# ETag tests (unchanged from v11) — still uses the in-process compute_etag helper.


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
        assert e1 == e2 and len(e1) == 16

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

    def test_missing_plugin_dir_does_not_crash(self, tmp_path):
        from src.marketplace_filter import compute_etag

        e = compute_etag([{"prefixed_name": "x", "version": "1", "plugin_dir": tmp_path / "missing"}])
        assert len(e) == 16

    def test_empty_plugin_list(self):
        from src.marketplace_filter import compute_etag

        assert len(compute_etag([])) == 16


class TestResolveManifestNameHygiene:
    """`manifest_name` comes from a curator-controlled `.claude-plugin/plugin.json`
    and is emitted verbatim into the served `marketplace.json` `name` field, so it
    must clear the same bar as its sibling `original_name` (which
    `src.marketplace.is_safe_plugin_name` already gates at ingest).
    """

    @staticmethod
    def _plugin_dir(tmp_path, name):
        d = tmp_path / "plugins" / "p"
        (d / ".claude-plugin").mkdir(parents=True)
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return d

    def test_conformant_name_is_returned(self, tmp_path):
        from src.marketplace_filter import resolve_manifest_name

        d = self._plugin_dir(tmp_path, "acme-tools")
        assert resolve_manifest_name(d, fallback="fb") == "acme-tools"

    @pytest.mark.parametrize(
        "bad",
        [
            "My Plugin",  # space — not one safe segment
            "evil/../escape",  # path separators
            "ac\nme",  # interior newline — `strip()` cannot save this one
            "tab\there",  # control char
            "..",  # traversal token
            "x" * 65,  # over the 64-char cap
        ],
    )
    def test_unsafe_name_falls_back(self, tmp_path, bad):
        from src.marketplace_filter import resolve_manifest_name

        d = self._plugin_dir(tmp_path, bad)
        assert resolve_manifest_name(d, fallback="fb") == "fb"

    def test_name_at_cap_is_kept(self, tmp_path):
        from src.marketplace_filter import resolve_manifest_name

        d = self._plugin_dir(tmp_path, "x" * 64)
        assert resolve_manifest_name(d, fallback="fb") == "x" * 64

    def test_surrounding_whitespace_still_stripped(self, tmp_path):
        from src.marketplace_filter import resolve_manifest_name

        d = self._plugin_dir(tmp_path, "  acme-tools  ")
        assert resolve_manifest_name(d, fallback="fb") == "acme-tools"

    def test_missing_plugin_json_falls_back(self, tmp_path):
        from src.marketplace_filter import resolve_manifest_name

        d = tmp_path / "plugins" / "p"
        d.mkdir(parents=True)
        assert resolve_manifest_name(d, fallback="fb") == "fb"


class TestSourceAwarePluginDir:
    """The catalog's declared ``source`` (a relative path inside the
    marketplace clone) drives ``plugin_dir`` resolution. A plugin may live at
    the repo root (``source: "./"``) or any subdirectory — the previously
    hardcoded ``plugins/<name>`` layout is only the default when no source is
    declared. Untrusted curator content: sources escaping the clone are
    skipped, external (dict) sources have no local files to serve.
    """

    T = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _grant_user(self, conn, *, marketplace: str, plugin: str) -> dict:
        _make_user(conn, user_id="u1", email="u1@x")
        gid = _make_group(conn, name="G1")
        _add_member(conn, user_id="u1", group_id=gid)
        _grant(conn, group_id=gid, marketplace=marketplace, plugin=plugin)
        return {"id": "u1"}

    def _resolve(self, conn, raw_plugin: dict) -> list[dict]:
        from src.marketplace_filter import resolve_allowed_plugins

        _register_marketplace(conn, id="mkt", registered_at=self.T, plugins=[raw_plugin])
        user = self._grant_user(conn, marketplace="mkt", plugin=raw_plugin["name"])
        return resolve_allowed_plugins(conn, user)

    def test_root_source_resolves_to_clone_root(self, db_conn, tmp_path):
        from app.utils import get_marketplaces_dir

        result = self._resolve(db_conn, {"name": "solo", "source": "./"})
        assert len(result) == 1
        assert result[0]["plugin_dir"] == get_marketplaces_dir() / "mkt"

    def test_subdir_source_resolves_within_clone(self, db_conn):
        from app.utils import get_marketplaces_dir

        result = self._resolve(db_conn, {"name": "solo", "source": "./tools/solo"})
        assert len(result) == 1
        assert result[0]["plugin_dir"] == get_marketplaces_dir() / "mkt" / "tools" / "solo"

    def test_missing_source_defaults_to_plugins_layout(self, db_conn):
        from app.utils import get_marketplaces_dir

        result = self._resolve(db_conn, {"name": "solo"})
        assert len(result) == 1
        assert result[0]["plugin_dir"] == get_marketplaces_dir() / "mkt" / "plugins" / "solo"

    def test_traversal_source_is_skipped(self, db_conn):
        assert self._resolve(db_conn, {"name": "solo", "source": "../other-mkt"}) == []

    def test_absolute_source_is_skipped(self, db_conn):
        assert self._resolve(db_conn, {"name": "solo", "source": "/etc"}) == []

    def test_embedded_traversal_source_is_skipped(self, db_conn):
        assert self._resolve(db_conn, {"name": "solo", "source": "./tools/../../escape"}) == []

    def test_external_dict_source_is_skipped(self, db_conn):
        raw = {"name": "solo", "source": {"source": "github", "repo": "acme/solo"}}
        assert self._resolve(db_conn, raw) == []

    def test_symlinked_source_escaping_clone_is_skipped(self, db_conn, tmp_path):
        from app.utils import get_marketplaces_dir

        outside = tmp_path / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        mkt = get_marketplaces_dir() / "mkt"
        mkt.mkdir(parents=True, exist_ok=True)
        (mkt / "ln").symlink_to(outside)
        assert self._resolve(db_conn, {"name": "solo", "source": "./ln"}) == []


class TestUnservedPaths:
    """VCS internals never enter the served tree or the ETag: a root-source
    plugin's ``plugin_dir`` IS the git clone, so ``.git/**`` must be excluded
    exactly like Agnes-only enrichment files."""

    def test_git_dir_is_unserved(self):
        from src.marketplace_filter import is_unserved_path

        assert is_unserved_path((".git", "config"))
        assert is_unserved_path(("sub", ".git", "HEAD"))

    def test_agnes_only_paths_stay_unserved(self):
        from src.marketplace_filter import is_unserved_path

        assert is_unserved_path((".agnes", "cover.png"))
        assert is_unserved_path((".claude-plugin", "marketplace-metadata.json"))

    def test_regular_paths_are_served(self):
        from src.marketplace_filter import is_unserved_path

        assert not is_unserved_path(("skills", "hello", "SKILL.md"))
        assert not is_unserved_path((".claude-plugin", "plugin.json"))
        assert not is_unserved_path((".github", "workflows", "ci.yml"))

    def test_etag_ignores_git_internals(self, tmp_path):
        from src.marketplace_filter import compute_etag

        d = tmp_path / "mkt"
        d.mkdir()
        (d / "CLAUDE.md").write_bytes(b"content")
        plugin = {"prefixed_name": "mkt-solo", "version": "1", "plugin_dir": d}
        e1 = compute_etag([plugin])

        (d / ".git").mkdir()
        (d / ".git" / "config").write_bytes(b"[core]")
        e2 = compute_etag([plugin])
        assert e1 == e2
