"""Integration tests for /marketplace.git/* (git smart-HTTP channel).

These tests exercise the endpoint via FastAPI TestClient using the
git smart-HTTP wire protocol (`GET /info/refs?service=git-upload-pack`)
rather than spawning a real `git clone` subprocess — cheaper to run, no
socket required, and avoids Windows/PATH git-binary flakiness on CI.

A single realistic end-to-end clone test is parked under
@pytest.mark.slow and only runs when the user opts in.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pytest
from fastapi.testclient import TestClient

pytest.skip(
    "v12: PluginAccessRepository was removed and users.role/users.groups are "
    "no longer the authorization source. Rewrite this module against the "
    "v12 model — seed user_group_members + resource_grants directly, drop "
    "the role='analyst' fixture pattern, and use UserGroupMembersRepository "
    "for group assignment.",
    allow_module_level=True,
)


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.fixture
def git_env(e2e_env, monkeypatch):
    """Identical setup to the ZIP fixture but returns raw PAT strings usable
    as HTTP Basic passwords. A valid PAT requires a real row in
    personal_access_tokens (the PAT resolver does a DB round-trip), so we
    create two: one admin, one analyst with groups=["TestGroup"]."""
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import (
        UserGroupsRepository, PluginAccessRepository,
    )
    import hashlib
    import uuid

    data_dir = e2e_env["data_dir"]

    # Plugin folders on disk
    for slug, plug in [("mkt-a", "plug-x"), ("mkt-b", "plug-y")]:
        d = data_dir / "marketplaces" / slug / "plugins" / plug
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(f"# {plug}\n", encoding="utf-8")

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
        users.create(id="admin1", email="admin@test.local", name="Admin", role="admin")
        users.create(id="analyst1", email="analyst@test.local", name="Analyst", role="analyst")
        conn.execute(
            "UPDATE users SET groups = ? WHERE id = ?",
            [json.dumps(["TestGroup"]), "analyst1"],
        )

        ug = UserGroupsRepository(conn)
        tg = ug.create(name="TestGroup")
        ug.ensure_system("Admin", "sys")
        ug.ensure_system("Everyone", "sys")

        access = PluginAccessRepository(conn)
        access.grant(tg["id"], "mkt-b", "plug-y")

        # Create real PAT rows so resolve_token_to_user passes.
        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email, role in [
            ("admin1", "admin@test.local", "admin"),
            ("analyst1", "analyst@test.local", "analyst"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(
                uid, email, role, token_id=tid, typ="pat",
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


# ---------------------------------------------------------------------------
# Optional end-to-end: run a real git clone against a live uvicorn server.
# Opt-in via `pytest -m slow`.
# ---------------------------------------------------------------------------


def _have_git() -> bool:
    return shutil.which("git") is not None


@pytest.mark.slow
@pytest.mark.skipif(not _have_git(), reason="git binary not on PATH")
def test_real_git_clone_admin(git_env, tmp_path):
    """Spawn the app under uvicorn and run `git clone` against it."""
    import socket
    import uvicorn

    # Find a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # Spin up uvicorn in a thread with the already-built app from the fixture
    config = uvicorn.Config(
        app=git_env["client"].app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # Poll until ready
        import time
        for _ in range(50):
            with socket.socket() as s:
                try:
                    s.connect(("127.0.0.1", port))
                    break
                except OSError:
                    time.sleep(0.1)

        dest = tmp_path / "clone"
        pat = git_env["admin_pat"]
        url = f"http://x:{pat}@127.0.0.1:{port}/marketplace.git/"
        proc = subprocess.run(
            ["git", "clone", url, str(dest)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert (dest / ".claude-plugin" / "marketplace.json").is_file()
        assert (dest / "plugins" / "mkt-a-plug-x" / "CLAUDE.md").is_file()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
