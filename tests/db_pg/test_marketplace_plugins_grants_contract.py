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

import json
from datetime import datetime, timezone

import duckdb
import pytest


def _make_duckdb_repos(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.marketplace_plugins import (
        MarketplacePluginsRepository,
    )
    from src.repositories.user_groups import UserGroupsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
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
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?)",
            [slug, slug, f"https://example.test/{slug}.git", registered_at],
        )
    else:
        import sqlalchemy as sa
        with repos["engine"].begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                    "VALUES (:id, :name, :url, :ts)"
                ),
                {
                    "id": slug, "name": slug,
                    "url": f"https://example.test/{slug}.git",
                    "ts": registered_at,
                },
            )


def _seed_plugins(
    repos: dict, slug: str, names: list[str], version: str = "1.0",
) -> None:
    """Bulk seed plugins for a marketplace in one ``replace_for_marketplace``
    call so the implicit DELETE doesn't wipe earlier seeds."""
    repos["plugins"].replace_for_marketplace(
        slug,
        [
            {"name": n, "version": version, "description": f"{slug}/{n}"}
            for n in names
        ],
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
                f"grant-{group_id}-{slug}-{name}", group_id,
                f"{slug}/{name}", datetime.now(timezone.utc),
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
