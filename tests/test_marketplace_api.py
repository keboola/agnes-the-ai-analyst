"""Integration tests for the unified /api/marketplace endpoints.

Covers the v28 Model B browse + install surface: per-tab listing,
categories, curated detail with RBAC guard, and subscribe/unsubscribe.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _seed_curated_grant(
    *,
    user_id: str,
    marketplace: str,
    plugin: str,
    plugin_meta: dict | None = None,
    group_name: str | None = None,
) -> tuple[str, str]:
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    conn = get_system_db()
    try:
        existing = conn.execute(
            "SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace],
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES (?, ?, ?, ?)",
                [marketplace, marketplace.upper(),
                 f"https://example.test/{marketplace}.git",
                 datetime.now(timezone.utc)],
            )
        meta = {"name": plugin, "version": "1.0", "description": "desc"}
        if plugin_meta:
            meta.update(plugin_meta)
        conn.execute(
            "INSERT INTO marketplace_plugins "
            "(marketplace_id, name, description, version, category, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                marketplace, plugin, meta.get("description"), meta.get("version"),
                meta.get("category"), json.dumps(meta),
                datetime.now(timezone.utc),
            ],
        )
        gname = group_name or f"G-{user_id}-{marketplace}"
        gid = UserGroupsRepository(conn).create(name=gname)["id"]
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
        grant_id = ResourceGrantsRepository(conn).create(
            group_id=gid, resource_type="marketplace_plugin",
            resource_id=f"{marketplace}/{plugin}",
        )
        return gid, grant_id
    finally:
        conn.close()


def _make_skill_zip(skill_name: str = "code-review") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: A test skill.\n---\nbody",
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /api/marketplace/items
# ---------------------------------------------------------------------------


class TestListItems:
    def test_curated_empty_for_user_without_grants(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get("/api/marketplace/items?tab=curated", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_curated_lists_granted_plugins(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get("/api/marketplace/items?tab=curated", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["source"] == "curated"
        assert data["items"][0]["name"] == "alpha"
        assert data["items"][0]["installed"] is False
        assert data["items"][0]["marketplace_slug"] == "mkt-x"

    def test_flea_lists_uploads(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["source"] == "flea"
        # Invocation name suffixed with -by-<owner>
        assert "alpha" in data["items"][0]["name"]

    def test_my_subscriptions_default_empty(self, web_client):
        """Without explicit install, a granted curated plugin doesn't show
        up under tab=my (Model B)."""
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get("/api/marketplace/items?tab=my", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# /api/marketplace/categories
# ---------------------------------------------------------------------------


class TestCategories:
    def test_curated_categories_count(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="alpha",
            plugin_meta={"category": "Code & Engineering"},
        )
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="beta",
            plugin_meta={"category": "Code & Engineering"},
            group_name="G-alice-mkt-x-beta",
        )
        r = web_client.get(
            "/api/marketplace/categories?tab=curated", cookies=cookies,
        )
        assert r.status_code == 200
        data = r.json()
        cats = {c["name"]: c["count"] for c in data["items"]}
        assert cats.get("Code & Engineering") == 2

    def test_categories_skip_empty(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get(
            "/api/marketplace/categories?tab=curated", cookies=cookies,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []  # no plugins in scope → no categories


# ---------------------------------------------------------------------------
# Curated detail + install
# ---------------------------------------------------------------------------


class TestCuratedDetail:
    def test_detail_403_without_grant(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get(
            "/api/marketplace/curated/some-mp/some-plugin", cookies=cookies,
        )
        assert r.status_code == 403

    def test_detail_200_with_grant(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["plugin_name"] == "alpha"
        assert data["installed"] is False
        # New fields populated for the redesigned plugin detail page.
        assert "files" in data and isinstance(data["files"], list)
        assert "docs" in data and isinstance(data["docs"], list)
        assert data["install_count"] == 0

    def test_install_403_without_grant(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.post(
            "/api/marketplace/curated/some-mp/some-plugin/install",
            cookies=cookies,
        )
        assert r.status_code == 403

    def test_install_uninstall_round_trip(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        # Install.
        r = web_client.post(
            "/api/marketplace/curated/mkt-x/alpha/install", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        # Verify in DB.
        from src.db import get_system_db
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        conn = get_system_db()
        try:
            assert UserCuratedSubscriptionsRepository(conn).is_subscribed(
                user_id, "mkt-x", "alpha",
            )
        finally:
            conn.close()

        # Detail now reports installed=True.
        d = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha", cookies=cookies,
        ).json()
        assert d["installed"] is True

        # Uninstall.
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install", cookies=cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        try:
            assert not UserCuratedSubscriptionsRepository(conn).is_subscribed(
                user_id, "mkt-x", "alpha",
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Curated nested skill / agent detail — extended response shape
# ---------------------------------------------------------------------------


def _seed_curated_skill_on_disk(
    tmp_path, marketplace: str, plugin: str, skill: str,
    *, files: dict[str, str] | None = None,
):
    """Materialize a skill on disk so curated_skill_detail can read it.

    `files` maps relative paths inside the skill dir to file contents.
    SKILL.md is always written; extra files surface in the Files section.
    """
    skill_dir = tmp_path / "marketplaces" / marketplace / "plugins" / plugin / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: A test skill.\n---\nbody",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _seed_curated_agent_on_disk(
    tmp_path, marketplace: str, plugin: str, agent: str,
):
    agents_dir = tmp_path / "marketplaces" / marketplace / "plugins" / plugin / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent}.md").write_text(
        f"---\nname: {agent}\ndescription: A test agent.\n---\nbody",
        encoding="utf-8",
    )


class TestCuratedInnerDetail:
    def test_skill_detail_includes_parent_metadata_and_files(
        self, web_client, tmp_path,
    ):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="alpha",
            plugin_meta={"category": "Data", "author": {"name": "ops-team"}},
        )
        _seed_curated_skill_on_disk(
            tmp_path, "mkt-x", "alpha", "data-explorer",
            files={"REFERENCE.md": "ref docs"},
        )
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha/skill/data-explorer",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Inner-detail fields.
        assert d["kind"] == "skill"
        assert d["name"] == "data-explorer"
        assert d["description"] == "A test skill."
        # Parent plugin metadata surfaced for the redesigned hero / sidebar.
        assert d["category"] == "Data"
        assert d["marketplace_name"]  # registry display name
        assert d["parent_updated_at"] is not None
        # Bundle + files.
        assert d["bundle_size"] is not None and d["bundle_size"] > 0
        names = {f["path"] for f in d["files"]}
        assert "SKILL.md" in names
        assert "REFERENCE.md" in names

    def test_agent_detail_single_file(self, web_client, tmp_path):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        _seed_curated_agent_on_disk(tmp_path, "mkt-x", "alpha", "incident-responder")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha/agent/incident-responder",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["kind"] == "agent"
        # Agents are flat single-file .md → exactly one file entry.
        assert len(d["files"]) == 1
        assert d["files"][0]["path"] == "incident-responder.md"
        assert d["bundle_size"] == d["files"][0]["size"]


# ---------------------------------------------------------------------------
# Flea standalone detail — extended response shape
# ---------------------------------------------------------------------------


class TestFleaDetail:
    def test_flea_skill_detail_populates_files_owner_install_count(
        self, web_client,
    ):
        _, cookies = _create_user(web_client, "alice@x.com")
        # Upload a skill into the Store.
        up = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert up.status_code == 201, up.text
        entity_id = up.json()["id"]

        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/detail", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["source"] == "flea"
        assert d["entity_id"] == entity_id
        # Files walked from disk.
        assert isinstance(d["files"], list) and len(d["files"]) >= 1
        # Friendly owner_display falls through to users.name (email local-part
        # is the seeded `name` in _create_user → 'alice').
        assert d["owner_display"] == "alice"
        # install_count starts at 0; bumps after install/uninstall toggle.
        assert d["install_count"] == 0
        # docs is always a list (empty when uploader didn't ship any).
        assert isinstance(d["docs"], list)
