"""Integration tests for /marketplace.git/* (git smart-HTTP channel).

These tests exercise the endpoint via FastAPI TestClient using the
git smart-HTTP wire protocol (`GET /info/refs?service=git-upload-pack`)
rather than spawning a real `git clone` subprocess — cheaper to run, no
socket required, and avoids Windows/PATH git-binary flakiness on CI.

v13: uses user_group_members + resource_grants (no PluginAccessRepository,
no users.groups JSON). PAT auth via HTTP Basic where password = PAT.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.fixture
def git_env(e2e_env, monkeypatch):
    """Identical setup to the ZIP fixture but returns raw PAT strings usable
    as HTTP Basic passwords. A valid PAT requires a real row in
    personal_access_tokens (the PAT resolver does a DB round-trip), so we
    create two: one admin, one analyst with group membership via
    user_group_members + resource_grants."""
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = e2e_env["data_dir"]

    # Plugin folders on disk — each ships a real .claude-plugin/plugin.json
    # so the bare repo's synth marketplace.json picks up the plugin's
    # authoritative name (matches what real upstream marketplaces do, and
    # exercises the manifest_name resolution path).
    for slug, plug in [("mkt-a", "plug-x"), ("mkt-b", "plug-y")]:
        d = data_dir / "marketplaces" / slug / "plugins" / plug
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(f"# {plug}\n", encoding="utf-8")
        (d / ".claude-plugin").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": plug, "version": "1.0"}), encoding="utf-8",
        )

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
        ]:
            raw = {"name": name, "version": ver, "source": f"./plugins/{name}"}
            conn.execute(
                "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [slug, name, ver, json.dumps(raw), t],
            )

        users = UserRepository(conn)
        users.create(id="admin1", email="admin@test.local", name="Admin")
        users.create(id="analyst1", email="analyst@test.local", name="Analyst")

        # System groups
        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")

        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]

        # Create TestGroup for analyst
        tg = ug.create(name="TestGroup", description="granted plug-y only")
        test_group_gid = tg["id"]

        # Assign memberships
        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")
        ugm.add_member("analyst1", test_group_gid, source="admin")

        # Grant plugins via resource_grants
        rg = ResourceGrantsRepository(conn)
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-a/plug-x")
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")
        rg.create(group_id=test_group_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")

        # Create real PAT rows so resolve_token_to_user passes.
        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email, _role in [
            ("admin1", "admin@test.local", "admin"),
            ("analyst1", "analyst@test.local", "analyst"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(
                uid, email, token_id=tid, typ="pat",
            )
            token_repo.create(
                id=tid, user_id=uid, name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt
    finally:
        conn.close()

    app = create_app()
    client = TestClient(app)
    return {
        "client": client,
        "admin_pat": pats["admin1"],
        "analyst_pat": pats["analyst1"],
        "data_dir": data_dir,
    }


class TestGitSmartHttp:
    """Verify the WSGI app at /marketplace.git responds to the git protocol."""

    def test_missing_auth_returns_401(self, git_env):
        c = git_env["client"]
        resp = c.get("/marketplace.git/info/refs?service=git-upload-pack")
        assert resp.status_code == 401
        assert "basic realm" in resp.headers.get("www-authenticate", "").lower()

    def test_bad_basic_password_returns_401(self, git_env):
        c = git_env["client"]
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", "not-a-real-token")},
        )
        assert resp.status_code == 401

    def test_info_refs_returns_git_protocol(self, git_env):
        """Good PAT → dulwich serves `info/refs` with a pkt-line body."""
        c = git_env["client"]
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-git-upload-pack-advertisement"
        # First line of smart-HTTP advertisement: pkt-line "# service=git-upload-pack"
        body = resp.content
        assert b"# service=git-upload-pack" in body
        # Should include a ref to main
        assert b"refs/heads/main" in body

    def test_cache_dir_populated_after_first_hit(self, git_env):
        """Hitting the endpoint materializes `${DATA_DIR}/marketplaces/git-cache/<etag>.git`."""
        c = git_env["client"]
        cache = git_env["data_dir"] / "marketplaces" / "git-cache"
        assert not cache.exists() or not any(cache.iterdir())

        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert resp.status_code == 200

        # Exactly one bare repo must have appeared.
        entries = [p for p in cache.iterdir() if p.is_dir() and p.name.endswith(".git")]
        assert len(entries) == 1
        # Name is the ETag (16 hex chars) + ".git"
        assert len(entries[0].name) == 16 + len(".git")

    def test_admin_and_analyst_get_different_repos(self, git_env):
        """Different RBAC views → different content hashes → different bare repos."""
        c = git_env["client"]
        cache = git_env["data_dir"] / "marketplaces" / "git-cache"

        c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["analyst_pat"])},
        )

        entries = [p for p in cache.iterdir() if p.is_dir() and p.name.endswith(".git")]
        assert len(entries) == 2

    # --- New tests for git smart HTTP protocol coverage ---

    def test_git_upload_pack_endpoint_requires_auth(self, git_env):
        """POST /marketplace.git/git-upload-pack requires HTTP Basic auth."""
        c = git_env["client"]
        resp = c.post("/marketplace.git/git-upload-pack")
        assert resp.status_code == 401

    def test_git_endpoints_require_http_basic_with_pat(self, git_env):
        """Git endpoints require HTTP Basic auth where password = PAT.
        Bearer auth is not accepted for git endpoints."""
        c = git_env["client"]
        # Bearer auth should fail — git uses Basic
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Bearer {git_env['admin_pat']}"},
        )
        assert resp.status_code == 401

    def test_info_refs_with_valid_pat_returns_200(self, git_env):
        """GET /marketplace.git/info/refs with valid PAT returns git protocol response."""
        c = git_env["client"]
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert resp.status_code == 200
        assert "git-upload-pack" in resp.headers["content-type"]

    def test_analyst_sees_filtered_content_via_git(self, git_env):
        """Analyst with limited grants gets a different (smaller) repo than admin."""
        c = git_env["client"]
        cache = git_env["data_dir"] / "marketplaces" / "git-cache"

        # Admin request
        admin_resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert admin_resp.status_code == 200

        # Analyst request
        analyst_resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["analyst_pat"])},
        )
        assert analyst_resp.status_code == 200

        # Two different cache entries (different RBAC views)
        entries = [p for p in cache.iterdir() if p.is_dir() and p.name.endswith(".git")]
        assert len(entries) == 2

    def test_bare_repo_manifest_uses_plugin_json_name(self, git_env):
        """The bare repo's .claude-plugin/marketplace.json must list each
        plugin under the name declared in its own plugin.json (not the
        slug-prefixed dir name). Otherwise Claude Code's /plugin UI can't
        link the loaded plugin back to its catalog entry."""
        from dulwich.repo import Repo

        c = git_env["client"]
        c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        cache = git_env["data_dir"] / "marketplaces" / "git-cache"
        bare = next(p for p in cache.iterdir() if p.is_dir() and p.name.endswith(".git"))

        repo = Repo(str(bare))
        try:
            head = repo[repo.refs[b"HEAD"]]
            tree = repo[head.tree]
            # dulwich tree.items() yields TreeEntry tuples (path, mode, sha)
            cp_entry = next(e for e in tree.items() if e.path == b".claude-plugin")
            cp_subtree = repo[cp_entry.sha]
            manifest_entry = next(
                e for e in cp_subtree.items() if e.path == b"marketplace.json"
            )
            manifest = json.loads(repo[manifest_entry.sha].data.decode("utf-8"))
        finally:
            repo.close()

        names = {p["name"] for p in manifest["plugins"]}
        assert names == {"plug-x", "plug-y"}
        sources = {p["source"] for p in manifest["plugins"]}
        assert sources == {"./plugins/mkt-a-plug-x", "./plugins/mkt-b-plug-y"}
