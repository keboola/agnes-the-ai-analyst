"""Cross-engine contract test for the marketplace plugin grant resolver.

Pins the contract for ``list_granted_for_groups`` on both backends — the
read path behind ``src.marketplace_filter.resolve_allowed_plugins`` and
therefore behind the served Claude Code marketplace
(``/marketplace.git/`` + ``/marketplace.zip``). The bug this catches:
prior to this PR, the resolver did raw ``conn.execute`` on the
DuckDB-typed connection, so on Postgres-backed deployments every plugin
was silently filtered out (empty DuckDB tables → 0 rows → only
pre-PG-cutover data survived in the served set). Parametrising over
both backends through the repo factory makes a regression at the
routing layer impossible.

Also covers ``user_groups_repo().list_names_by_ids`` (the second raw-SQL
spot in ``marketplace_filter``, behind ``resolve_user_groups``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def _make_duckdb_repos(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so
    # the session timezone is pinned to UTC — `tests/test_duckdb_session_tz.py`
    # `test_no_bare_duckdb_connect_in_production_code` regression guard
    # catches any new bare connect in `tests/db_pg/`.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.marketplace_plugins import (
        MarketplacePluginsRepository,
    )
    from src.repositories.user_groups import UserGroupsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "plugins": MarketplacePluginsRepository(conn),
        "groups": UserGroupsRepository(conn),
        "conn": conn,
        "backend": "duckdb",
    }


def _make_pg_repos(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.marketplace_plugins_pg import (
        MarketplacePluginsPgRepository,
    )
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    eng = db_pg.get_engine()
    return {
        "plugins": MarketplacePluginsPgRepository(eng),
        "groups": UserGroupsPgRepository(eng),
        "engine": eng,
        "backend": "pg",
    }


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        bundle = _make_duckdb_repos(tmp_path)
        yield bundle
        bundle["conn"].close()
    else:
        bundle = _make_pg_repos(pg_engine, monkeypatch)
        yield bundle


def _seed_registry(repos: dict, slug: str, registered_at: datetime) -> None:
    """Insert a ``marketplace_registry`` row via raw SQL (no repo method
    for it carries an explicit ``registered_at`` write; the test needs to
    fix ordering deterministically)."""
    if repos["backend"] == "duckdb":
        repos["conn"].execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
            [slug, slug, f"https://example.test/{slug}.git", registered_at],
        )
    else:
        import sqlalchemy as sa

        with repos["engine"].begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (:id, :name, :url, :ts)"
                ),
                {
                    "id": slug,
                    "name": slug,
                    "url": f"https://example.test/{slug}.git",
                    "ts": registered_at,
                },
            )


def _seed_plugins(
    repos: dict,
    slug: str,
    names: list[str],
    version: str = "1.0",
) -> None:
    """Bulk seed plugins for a marketplace in one ``replace_for_marketplace``
    call so the implicit DELETE doesn't wipe earlier seeds."""
    repos["plugins"].replace_for_marketplace(
        slug,
        [{"name": n, "version": version, "description": f"{slug}/{n}"} for n in names],
    )


def _seed_plugin(repos: dict, slug: str, name: str, version: str = "1.0") -> None:
    """Single-plugin convenience wrapper. Only safe for fresh marketplaces —
    use ``_seed_plugins`` when seeding multiple plugins under one slug."""
    _seed_plugins(repos, slug, [name], version=version)


def _seed_grant(repos: dict, group_id: str, slug: str, name: str) -> None:
    """Insert a ``resource_grants`` row for ``(group_id, plugin)``."""
    if repos["backend"] == "duckdb":
        repos["conn"].execute(
            "INSERT INTO resource_grants "
            "(id, group_id, resource_type, resource_id, assigned_at, assigned_by) "
            "VALUES (?, ?, 'marketplace_plugin', ?, ?, 'test')",
            [
                f"grant-{group_id}-{slug}-{name}",
                group_id,
                f"{slug}/{name}",
                datetime.now(timezone.utc),
            ],
        )
    else:
        import sqlalchemy as sa

        with repos["engine"].begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO resource_grants "
                    "(id, group_id, resource_type, resource_id, assigned_at, assigned_by) "
                    "VALUES (:id, :g, 'marketplace_plugin', :r, :ts, 'test')"
                ),
                {
                    "id": f"grant-{group_id}-{slug}-{name}",
                    "g": group_id,
                    "r": f"{slug}/{name}",
                    "ts": datetime.now(timezone.utc),
                },
            )


# ---------------------------------------------------------------------------
# list_granted_for_groups — the load-bearing JOIN behind the served marketplace
# ---------------------------------------------------------------------------


class TestListGrantedForGroups:
    def test_empty_group_ids_returns_empty(self, repos):
        assert repos["plugins"].list_granted_for_groups([]) == []

    def test_no_grants_returns_empty(self, repos):
        group = repos["groups"].create(name="g-empty", created_by="test")
        _seed_registry(repos, "mkt-x", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mkt-x", "plug-a")
        # No resource_grants row — must come back empty.
        assert repos["plugins"].list_granted_for_groups([group["id"]]) == []

    def test_returns_granted_plugin(self, repos):
        group = repos["groups"].create(name="g-1", created_by="test")
        _seed_registry(repos, "mkt-x", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mkt-x", "plug-a", version="1.0")
        _seed_grant(repos, group["id"], "mkt-x", "plug-a")

        rows = repos["plugins"].list_granted_for_groups([group["id"]])
        assert len(rows) == 1
        r = rows[0]
        assert r["marketplace_id"] == "mkt-x"
        assert r["name"] == "plug-a"
        assert r["version"] == "1.0"
        assert isinstance(r["raw"], dict)

    def test_ordered_by_registered_at_then_name(self, repos):
        g = repos["groups"].create(name="g-2", created_by="test")
        _seed_registry(repos, "mkt-b", datetime(2026, 2, 1, tzinfo=timezone.utc))
        _seed_registry(repos, "mkt-a", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mkt-b", "z-plug")
        _seed_plugins(repos, "mkt-a", ["alpha", "beta"])
        for slug, name in [("mkt-b", "z-plug"), ("mkt-a", "alpha"), ("mkt-a", "beta")]:
            _seed_grant(repos, g["id"], slug, name)

        rows = repos["plugins"].list_granted_for_groups([g["id"]])
        # mkt-a registered first (Jan 1), then mkt-b (Feb 1). Within
        # mkt-a, plugins ordered by name.
        assert [(r["marketplace_id"], r["name"]) for r in rows] == [
            ("mkt-a", "alpha"),
            ("mkt-a", "beta"),
            ("mkt-b", "z-plug"),
        ]

    def test_distinct_across_overlapping_groups(self, repos):
        g1 = repos["groups"].create(name="g-A", created_by="test")
        g2 = repos["groups"].create(name="g-B", created_by="test")
        _seed_registry(repos, "mkt", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mkt", "shared")
        _seed_grant(repos, g1["id"], "mkt", "shared")
        _seed_grant(repos, g2["id"], "mkt", "shared")

        rows = repos["plugins"].list_granted_for_groups([g1["id"], g2["id"]])
        assert len(rows) == 1
        assert rows[0]["name"] == "shared"

    def test_skips_plugin_without_registry_row(self, repos):
        """The INNER JOIN to marketplace_registry drops plugins whose parent
        slug is missing — matches the served-set behaviour where an
        un-registered marketplace shouldn't surface in the manifest."""
        g = repos["groups"].create(name="g-3", created_by="test")
        # NO registry row for "orphan" — only a plugin row + grant.
        _seed_plugin(repos, "orphan", "plug")
        _seed_grant(repos, g["id"], "orphan", "plug")
        assert repos["plugins"].list_granted_for_groups([g["id"]]) == []


# ---------------------------------------------------------------------------
# list_names_by_ids — backs resolve_user_groups (diagnostic)
# ---------------------------------------------------------------------------


class TestListNamesByIds:
    def test_empty_returns_empty(self, repos):
        assert repos["groups"].list_names_by_ids([]) == []

    def test_returns_only_listed_ids_sorted(self, repos):
        a = repos["groups"].create(name="zeta", created_by="test")
        b = repos["groups"].create(name="alpha", created_by="test")
        c = repos["groups"].create(name="middle", created_by="test")
        # Pass them in non-sorted order to confirm the repo sorts them.
        result = repos["groups"].list_names_by_ids([a["id"], b["id"], c["id"]])
        assert result == ["alpha", "middle", "zeta"]

    def test_subset_of_ids(self, repos):
        a = repos["groups"].create(name="aaa", created_by="test")
        b = repos["groups"].create(name="bbb", created_by="test")
        repos["groups"].create(name="ccc", created_by="test")
        result = repos["groups"].list_names_by_ids([a["id"], b["id"]])
        assert result == ["aaa", "bbb"]

    def test_unknown_id_silently_skipped(self, repos):
        a = repos["groups"].create(name="known", created_by="test")
        result = repos["groups"].list_names_by_ids([a["id"], "nonexistent-id"])
        assert result == ["known"]


# ---------------------------------------------------------------------------
# v77 built-in marketplace: is_builtin, admin_disabled, list_non_builtin
# ---------------------------------------------------------------------------


def _make_registry_repo(repos: dict):
    """Return a MarketplaceRegistryRepository / Pg sibling from the bundle."""
    if repos["backend"] == "duckdb":
        from src.repositories.marketplace_registry import MarketplaceRegistryRepository

        return MarketplaceRegistryRepository(repos["conn"])
    else:
        from src.repositories.marketplace_registry_pg import MarketplaceRegistryPgRepository

        return MarketplaceRegistryPgRepository(repos["engine"])


class TestIsBuiltin:
    """Contract tests for marketplace_registry.is_builtin and list_non_builtin."""

    def test_register_defaults_to_not_builtin(self, repos):
        reg = _make_registry_repo(repos)
        reg.register(id="reg-a", name="Reg A", url="https://example.test/a.git")
        row = reg.get("reg-a")
        assert row is not None
        assert row.get("is_builtin") is False

    def test_register_builtin_flag(self, repos):
        reg = _make_registry_repo(repos)
        reg.register(
            id="builtin-x",
            name="Built-in X",
            url="builtin://builtin-x",
            is_builtin=True,
        )
        row = reg.get("builtin-x")
        assert row is not None
        assert row.get("is_builtin") is True

    def test_list_builtin_returns_only_builtin(self, repos):
        reg = _make_registry_repo(repos)
        reg.register(id="normal-1", name="Normal 1", url="https://example.test/n1.git")
        reg.register(
            id="builtin-1",
            name="Built-in 1",
            url="builtin://builtin-1",
            is_builtin=True,
        )
        builtin_rows = reg.list_builtin()
        ids = [r["id"] for r in builtin_rows]
        assert "builtin-1" in ids
        assert "normal-1" not in ids

    def test_list_non_builtin_excludes_builtin(self, repos):
        reg = _make_registry_repo(repos)
        reg.register(id="normal-2", name="Normal 2", url="https://example.test/n2.git")
        reg.register(
            id="builtin-2",
            name="Built-in 2",
            url="builtin://builtin-2",
            is_builtin=True,
        )
        non_builtin = reg.list_non_builtin()
        ids = [r["id"] for r in non_builtin]
        assert "normal-2" in ids
        assert "builtin-2" not in ids

    def test_re_register_does_not_flip_is_builtin(self, repos):
        """ON CONFLICT path must not touch is_builtin — idempotent re-seed."""
        reg = _make_registry_repo(repos)
        reg.register(
            id="builtin-3",
            name="Built-in 3",
            url="builtin://builtin-3",
            is_builtin=True,
        )
        # Re-seed with is_builtin=False should be ignored (ON CONFLICT excludes it).
        reg.register(
            id="builtin-3",
            name="Built-in 3 Updated",
            url="builtin://builtin-3",
            is_builtin=False,
        )
        row = reg.get("builtin-3")
        assert row is not None
        # Name update was applied; is_builtin was NOT flipped.
        assert row["name"] == "Built-in 3 Updated"
        assert row.get("is_builtin") is True


class TestAdminDisabled:
    """Contract tests for marketplace_plugins.admin_disabled and set_admin_disabled."""

    def test_new_plugin_defaults_not_disabled(self, repos):
        _seed_registry(repos, "mp-d1", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mp-d1", "plug-x")
        row = repos["plugins"].get("mp-d1", "plug-x")
        assert row is not None
        assert row.get("admin_disabled") is False

    def test_set_admin_disabled_true(self, repos):
        _seed_registry(repos, "mp-d2", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mp-d2", "plug-y")
        found = repos["plugins"].set_admin_disabled("mp-d2", "plug-y", True)
        assert found is True
        row = repos["plugins"].get("mp-d2", "plug-y")
        assert row is not None
        assert row.get("admin_disabled") is True

    def test_set_admin_disabled_false_re_enables(self, repos):
        _seed_registry(repos, "mp-d3", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugin(repos, "mp-d3", "plug-z")
        repos["plugins"].set_admin_disabled("mp-d3", "plug-z", True)
        repos["plugins"].set_admin_disabled("mp-d3", "plug-z", False)
        row = repos["plugins"].get("mp-d3", "plug-z")
        assert row is not None
        assert row.get("admin_disabled") is False

    def test_disabled_plugin_excluded_from_list_granted(self, repos):
        """admin_disabled=TRUE plugins must not appear in list_granted_for_groups."""
        g = repos["groups"].create(name="g-dis", created_by="test")
        _seed_registry(repos, "mp-d4", datetime(2026, 1, 1, tzinfo=timezone.utc))
        # Bulk-seed both in one replace_for_marketplace call — calling the
        # singular _seed_plugin twice would DELETE the first (replace semantics).
        _seed_plugins(repos, "mp-d4", ["plug-vis", "plug-hidden"])
        _seed_grant(repos, g["id"], "mp-d4", "plug-vis")
        _seed_grant(repos, g["id"], "mp-d4", "plug-hidden")
        repos["plugins"].set_admin_disabled("mp-d4", "plug-hidden", True)

        rows = repos["plugins"].list_granted_for_groups([g["id"]])
        names = [r["name"] for r in rows]
        assert "plug-vis" in names
        assert "plug-hidden" not in names

    def test_disabled_plugin_excluded_from_browse_and_counts(self, repos):
        """admin_disabled=TRUE plugins must also be hidden from the browse
        listing (list_with_filters) and category_counts, not just the served
        feed — mirrors the list_granted_for_groups filter on both backends."""
        g = repos["groups"].create(name="g-dis-browse", created_by="test")
        _seed_registry(repos, "mp-d6", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugins(repos, "mp-d6", ["plug-shown", "plug-off"])
        _seed_grant(repos, g["id"], "mp-d6", "plug-shown")
        _seed_grant(repos, g["id"], "mp-d6", "plug-off")
        repos["plugins"].set_admin_disabled("mp-d6", "plug-off", True)

        items, total = repos["plugins"].list_with_filters(group_ids=[g["id"]])
        names = [r["name"] for r in items]
        assert "plug-shown" in names
        assert "plug-off" not in names
        assert total == 1

        counts = repos["plugins"].category_counts(group_ids=[g["id"]])
        assert sum(counts.values()) == 1

    def test_set_admin_disabled_nonexistent_returns_false(self, repos):
        found = repos["plugins"].set_admin_disabled("no-market", "no-plug", True)
        assert found is False

    def test_list_admin_disabled(self, repos):
        _seed_registry(repos, "mp-d5", datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_plugins(repos, "mp-d5", ["pa", "pb", "pc"])
        repos["plugins"].set_admin_disabled("mp-d5", "pb", True)
        disabled = repos["plugins"].list_admin_disabled("mp-d5")
        assert disabled == ["pb"]
