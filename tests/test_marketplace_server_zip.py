"""Integration tests for /marketplace.zip and /marketplace/info.

v13: uses user_group_members + resource_grants (no PluginAccessRepository,
no users.groups JSON). Admin is a regular group for marketplace filtering —
no god-mode shortcut.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def marketplace_env(e2e_env, monkeypatch):
    """Spin up the FastAPI app with two fake marketplaces populated on disk.

    Populates:
      - marketplace_registry with 2 slugs: 'mkt-a', 'mkt-b'
      - marketplace_plugins with:
          mkt-a: plug-x (v1.0)
          mkt-b: plug-y (v2.0), plug-z (v3.0)
      - DATA_DIR/marketplaces/<slug>/plugins/<plugin>/ with a tiny CLAUDE.md
      - admin user in Admin group with grants for all 3 plugins
      - analyst user in TestGroup with grant for plug-y only
      - nogroups user (only Everyone, no grants)
    """
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = e2e_env["data_dir"]

    # Plugin folders on disk
    for slug, plug in [("mkt-a", "plug-x"), ("mkt-b", "plug-y"), ("mkt-b", "plug-z")]:
        d = data_dir / "marketplaces" / slug / "plugins" / plug
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(
            f"# {plug}\nThis is {plug} from {slug}.\n", encoding="utf-8"
        )
        skills = d / "skills"
        skills.mkdir()
        (skills / "hello.md").write_text(f"skill for {plug}", encoding="utf-8")

    # DB setup
    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            [
                "mkt-a", "Market A", "https://example.test/a.git", t,
                "mkt-b", "Market B", "https://example.test/b.git", t,
            ],
        )
        for slug, name, ver in [
            ("mkt-a", "plug-x", "1.0"),
            ("mkt-b", "plug-y", "2.0"),
            ("mkt-b", "plug-z", "3.0"),
        ]:
            raw = {"name": name, "version": ver, "source": f"./plugins/{name}",
                   "description": f"{name} from {slug}"}
            conn.execute(
                "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [slug, name, ver, json.dumps(raw), t],
            )

        users = UserRepository(conn)
        users.create(id="admin1", email="admin@test.local", name="Admin", role="admin")
        users.create(id="analyst1", email="analyst@test.local", name="Analyst", role="analyst")
        users.create(id="nogroups1", email="nobody@test.local", name="Nobody", role="analyst")

        # System groups are seeded by db.init_schema(); look them up.
        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")

        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]

        # Create a custom group for the analyst
        tg = ug.create(name="TestGroup", description="granted plug-y only")
        test_group_gid = tg["id"]

        # Assign memberships
        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")
        ugm.add_member("analyst1", test_group_gid, source="admin")
        # nogroups1 is only in Everyone (auto-membership, no explicit row needed)

        # Grant plugins via resource_grants
        rg = ResourceGrantsRepository(conn)
        # Admin group gets all 3 plugins
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-a/plug-x")
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-z")
        # TestGroup gets only plug-y
        rg.create(group_id=test_group_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")
    finally:
        conn.close()

    # Tokens
    admin_token = create_access_token("admin1", "admin@test.local", "admin")
    analyst_token = create_access_token("analyst1", "analyst@test.local", "analyst")
    nogroups_token = create_access_token("nogroups1", "nobody@test.local", "analyst")

    app = create_app()
    client = TestClient(app)
    return {
        "client": client,
        "admin_token": admin_token,
        "analyst_token": analyst_token,
        "nogroups_token": nogroups_token,
        "data_dir": data_dir,
    }


def _read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


class TestMarketplaceInfo:
    def test_admin_sees_all_plugins(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        info = resp.json()
        names = {p["name"] for p in info["plugins"]}
        assert names == {"mkt-a-plug-x", "mkt-b-plug-y", "mkt-b-plug-z"}
        assert "Admin" in info["groups"]
        assert info["marketplace_name"] == "agnes"
        assert info["plugin_count"] == 3

    def test_analyst_sees_only_granted_plugin(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["analyst_token"]))
        assert resp.status_code == 200
        info = resp.json()
        names = {p["name"] for p in info["plugins"]}
        assert names == {"mkt-b-plug-y"}
        assert "TestGroup" in info["groups"]

    def test_user_with_no_groups_falls_back_to_everyone(self, marketplace_env):
        """Everyone has no grants here, so the list is empty but call succeeds."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["nogroups_token"]))
        assert resp.status_code == 200
        info = resp.json()
        assert "Everyone" in info["groups"]
        assert info["plugins"] == []

    def test_missing_auth_returns_401(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info")
        assert resp.status_code == 401


class TestMarketplaceZip:
    def test_admin_zip_contains_all_plugins_with_prefix(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.headers["etag"].startswith('"') and resp.headers["etag"].endswith('"')

        zip_contents = _read_zip(resp.content)
        # Manifest at the expected path
        assert ".claude-plugin/marketplace.json" in zip_contents
        manifest = json.loads(zip_contents[".claude-plugin/marketplace.json"])
        assert manifest["name"] == "agnes"
        names = {p["name"] for p in manifest["plugins"]}
        assert names == {"mkt-a-plug-x", "mkt-b-plug-y", "mkt-b-plug-z"}
        # source paths flattened to prefixed names
        sources = {p["source"] for p in manifest["plugins"]}
        assert sources == {
            "./plugins/mkt-a-plug-x",
            "./plugins/mkt-b-plug-y",
            "./plugins/mkt-b-plug-z",
        }
        # Files from every marketplace copied over
        assert "plugins/mkt-a-plug-x/CLAUDE.md" in zip_contents
        assert "plugins/mkt-b-plug-y/CLAUDE.md" in zip_contents
        assert "plugins/mkt-b-plug-z/skills/hello.md" in zip_contents

    def test_analyst_zip_contains_only_granted(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["analyst_token"]))
        assert resp.status_code == 200
        zip_contents = _read_zip(resp.content)
        plugin_dirs = {p.split("/")[1] for p in zip_contents if p.startswith("plugins/")}
        assert plugin_dirs == {"mkt-b-plug-y"}

    def test_if_none_match_returns_304(self, marketplace_env):
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        first = c.get("/marketplace.zip", headers=headers)
        etag = first.headers["etag"].strip('"')
        second = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": f'"{etag}"'},
        )
        assert second.status_code == 304
        assert second.headers["etag"].strip('"') == etag
        assert second.content == b""

    def test_etag_changes_when_content_changes(self, marketplace_env):
        from app.marketplace_server.packager import invalidate_etag_cache

        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        first = c.get("/marketplace.zip", headers=headers)
        etag1 = first.headers["etag"]

        # Mutate a plugin file on disk → etag must change.
        target = marketplace_env["data_dir"] / "marketplaces" / "mkt-a" / "plugins" / "plug-x" / "CLAUDE.md"
        target.write_text("# plug-x\nMUTATED\n", encoding="utf-8")

        # Invalidate the in-process ETag cache so the next request
        # re-hashes from disk instead of returning the stale cached value.
        invalidate_etag_cache()

        second = c.get("/marketplace.zip", headers=headers)
        etag2 = second.headers["etag"]
        assert etag1 != etag2

    def test_missing_auth_returns_401(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip")
        assert resp.status_code == 401

    # --- New tests for ETag + auth coverage ---

    def test_zip_returns_correct_content_with_etag_header(self, marketplace_env):
        """GET /marketplace.zip returns ZIP body with a valid ETag header."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        resp = c.get("/marketplace.zip", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        etag = resp.headers["etag"]
        assert etag.startswith('"') and etag.endswith('"')
        # ETag is a 16-char hex string (sha256 prefix)
        etag_val = etag.strip('"')
        assert len(etag_val) == 16
        # Body is a valid ZIP
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert ".claude-plugin/marketplace.json" in zf.namelist()

    def test_if_none_match_returns_full_content_when_changed(self, marketplace_env):
        """GET /marketplace.zip with a stale If-None-Match returns full content."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        first = c.get("/marketplace.zip", headers=headers)
        stale_etag = "0000000000000000"  # definitely wrong
        second = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": f'"{stale_etag}"'},
        )
        assert second.status_code == 200
        assert len(second.content) > 0
        # The returned ETag is the real one, not the stale one
        assert second.headers["etag"].strip('"') != stale_etag

    def test_zip_requires_pat_authentication(self, marketplace_env):
        """GET /marketplace.zip without any auth returns 401."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip")
        assert resp.status_code == 401

    def test_zip_with_invalid_token_returns_401(self, marketplace_env):
        """GET /marketplace.zip with a garbage Bearer token returns 401."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401

    def test_if_none_match_with_wrong_etag_returns_full_zip(self, marketplace_env):
        """If-None-Match with a non-matching ETag returns 200 + full ZIP."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        resp = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": '"wrong-etag-value"'},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
