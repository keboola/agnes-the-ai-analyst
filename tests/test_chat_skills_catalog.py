"""Tests for app.chat.skills_catalog — the web chat slash-menu source.

Covers the two independent sources (bundled workspace-template skills +
RBAC-filtered marketplace/store plugin skills), the merge/shadowing rule
(marketplace wins name clashes), non-fatal per-source degradation, and the
(currently empty, checked-not-assumed) recognized-commands list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.chat.skills_catalog import (
    list_bundled_skills,
    list_marketplace_skills,
    list_recognized_commands,
    merged_skills,
)


# ---------------------------------------------------------------------------
# DB fixture + marketplace/store seeding helpers (mirrors
# tests/test_marketplace_filter_store.py's conventions).
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


def _register_marketplace(conn, *, id: str, plugins: list[dict]) -> None:
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [id, id.upper(), f"https://example.test/{id}.git", datetime.now(timezone.utc)],
    )
    for p in plugins:
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
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
        group_id=group_id, resource_type="marketplace_plugin", resource_id=f"{marketplace}/{plugin}"
    )


def _subscribe(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    UserCuratedSubscriptionsRepository(conn).subscribe(user_id, marketplace, plugin)


def _grant_and_subscribe(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    gid = _make_group(conn, name=f"G-{marketplace}-{plugin}-{user_id}")
    _grant(conn, group_id=gid, marketplace=marketplace, plugin=plugin)
    _add_member(conn, user_id=user_id, group_id=gid)
    _subscribe(conn, user_id=user_id, marketplace=marketplace, plugin=plugin)


def _write_skill_md(path: Path, *, name: str | None, description: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.append("---")
    lines.append("")
    lines.append("Body text.")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# list_bundled_skills
# ---------------------------------------------------------------------------


class TestListBundledSkills:
    def test_reads_name_and_description_from_frontmatter(self, tmp_path):
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "connector-asana" / "SKILL.md",
            name="connector-asana",
            description="How to use the Asana connector.",
        )
        out = list_bundled_skills(template)
        assert out == [
            {
                "name": "connector-asana",
                "description": "How to use the Asana connector.",
                "source": "bundled",
            }
        ]

    def test_falls_back_to_directory_name_and_null_description(self, tmp_path):
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "no-frontmatter-name" / "SKILL.md",
            name=None,
        )
        out = list_bundled_skills(template)
        assert out == [{"name": "no-frontmatter-name", "description": None, "source": "bundled"}]

    def test_missing_skills_dir_returns_empty_list(self, tmp_path):
        assert list_bundled_skills(tmp_path / "does-not-exist") == []

    def test_ignores_non_directory_and_skillless_entries(self, tmp_path):
        skills_dir = tmp_path / "bundled" / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "stray-file.txt").write_text("noise", encoding="utf-8")
        (skills_dir / "empty-dir").mkdir()
        assert list_bundled_skills(tmp_path / "bundled") == []


# ---------------------------------------------------------------------------
# list_marketplace_skills
# ---------------------------------------------------------------------------


class TestListMarketplaceSkills:
    def test_nested_skills_dir_convention(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(
            plugin_dir / "skills" / "my-skill" / "SKILL.md",
            name="my-skill",
            description="Does a thing.",
        )

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        assert out == [{"name": "my-skill", "description": "Does a thing.", "source": "marketplace"}]

    def test_root_level_skill_md_convention(self, db_conn, tmp_path, monkeypatch):
        """Single-skill plugins (e.g. the built-in marketplace) ship SKILL.md
        directly at the plugin root, not under skills/<name>/."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "agnes-analyst", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="agnes-analyst")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "agnes-analyst"
        _write_skill_md(plugin_dir / "SKILL.md", name=None)  # no frontmatter name

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        # Falls back to the plugin's own directory name.
        assert out == [{"name": "agnes-analyst", "description": None, "source": "marketplace"}]

    def test_store_bundle_skills_scanned_via_bundle_dirs(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import uuid

        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        eid = uuid.uuid4().hex
        StoreEntitiesRepository(db_conn).create(
            id=eid,
            owner_user_id="owner",
            owner_username="owner",
            type="skill",
            name="my-store-skill",
            description="d",
            category=None,
            version="abc1234567890def",
            file_size=10,
            visibility_status="approved",
        )
        UserStoreInstallsRepository(db_conn).install("u1", eid)

        from app.utils import get_store_dir

        plugin_dir = get_store_dir() / eid / "plugin"
        _write_skill_md(
            plugin_dir / "skills" / "my-store-skill" / "SKILL.md",
            name="my-store-skill",
            description="Uploaded via the Store.",
        )

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        assert out == [
            {
                "name": "my-store-skill",
                "description": "Uploaded via the Store.",
                "source": "marketplace",
            }
        ]

    def test_no_grants_yields_empty_list(self, db_conn):
        assert list_marketplace_skills(db_conn, {"id": "nobody"}) == []


# ---------------------------------------------------------------------------
# merged_skills — shadowing + non-fatal degradation
# ---------------------------------------------------------------------------


class TestMergedSkills:
    def test_marketplace_wins_name_clash_with_bundled(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "shared-name" / "SKILL.md",
            name="shared-name",
            description="Bundled description.",
        )

        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(
            plugin_dir / "skills" / "shared-name" / "SKILL.md",
            name="shared-name",
            description="Marketplace description.",
        )

        out = merged_skills(template, db_conn, {"id": "u1"})
        assert out == [
            {
                "name": "shared-name",
                "description": "Marketplace description.",
                "source": "marketplace",
            }
        ]

    def test_sorted_by_name_and_both_sources_present(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        template = tmp_path / "bundled"
        _write_skill_md(template / ".claude" / "skills" / "zzz-bundled" / "SKILL.md", name="zzz-bundled")

        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(plugin_dir / "skills" / "aaa-market" / "SKILL.md", name="aaa-market")

        out = merged_skills(template, db_conn, {"id": "u1"})
        assert [s["name"] for s in out] == ["aaa-market", "zzz-bundled"]
        assert {s["source"] for s in out} == {"bundled", "marketplace"}

    def test_bundled_source_failure_still_returns_marketplace_skills(self, db_conn, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(plugin_dir / "skills" / "still-here" / "SKILL.md", name="still-here")

        import app.chat.skills_catalog as mod

        def _boom(_bundled_template_dir):
            raise RuntimeError("bundled source exploded")

        monkeypatch.setattr(mod, "list_bundled_skills", _boom)

        with caplog.at_level("WARNING"):
            out = mod.merged_skills(tmp_path / "irrelevant", db_conn, {"id": "u1"})

        assert out == [{"name": "still-here", "description": None, "source": "marketplace"}]
        assert "bundled source failed to list" in caplog.text

    def test_marketplace_source_failure_still_returns_bundled_skills(self, db_conn, tmp_path, monkeypatch, caplog):
        template = tmp_path / "bundled"
        _write_skill_md(template / ".claude" / "skills" / "still-here" / "SKILL.md", name="still-here")

        import app.chat.skills_catalog as mod

        def _boom(_conn, _user):
            raise RuntimeError("marketplace resolver exploded")

        monkeypatch.setattr(mod, "list_marketplace_skills", _boom)

        with caplog.at_level("WARNING"):
            out = mod.merged_skills(template, db_conn, {"id": "u1"})

        assert out == [{"name": "still-here", "description": None, "source": "bundled"}]
        assert "marketplace source failed to list" in caplog.text


# ---------------------------------------------------------------------------
# list_recognized_commands
# ---------------------------------------------------------------------------


def test_list_recognized_commands_is_empty():
    """Nothing is currently backend-recognized — see the docstring for what
    was checked. Locks the contract so a future PR can't silently start
    inventing entries without updating this test."""
    assert list_recognized_commands() == []
