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
import subprocess
import uuid
from datetime import datetime, timezone

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
            json.dumps({"name": plug, "version": "1.0"}),
            encoding="utf-8",
        )

    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            [
                "mkt-a",
                "Market A",
                "https://example.test/a.git",
                t,
                "mkt-b",
                "Market B",
                "https://example.test/b.git",
                t,
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

        # Model B (v27+): explicit subscriptions are required for plugins
        # to enter the served set. Pre-existing tests below assume the
        # admin sees both plugins and the analyst sees plug-y; mirror
        # those expectations by subscribing both users.
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )

        subs = UserCuratedSubscriptionsRepository(conn)
        subs.subscribe("admin1", "mkt-a", "plug-x")
        subs.subscribe("admin1", "mkt-b", "plug-y")
        subs.subscribe("analyst1", "mkt-b", "plug-y")

        # Create real PAT rows so resolve_token_to_user passes.
        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email, _role in [
            ("admin1", "admin@test.local", "admin"),
            ("analyst1", "analyst@test.local", "analyst"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(
                uid,
                email,
                token_id=tid,
                typ="pat",
            )
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
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
        "app": app,
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
        # Name is the 16-hex ETag + a packaging-format version suffix + ".git"
        import re

        assert re.fullmatch(r"[0-9a-f]{16}\.v\d+\.git", entries[0].name), entries[0].name

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
            manifest_entry = next(e for e in cp_subtree.items() if e.path == b"marketplace.json")
            manifest = json.loads(repo[manifest_entry.sha].data.decode("utf-8"))
        finally:
            repo.close()

        names = {p["name"] for p in manifest["plugins"]}
        assert names == {"plug-x", "plug-y"}
        sources = {p["source"] for p in manifest["plugins"]}
        assert sources == {"./plugins/mkt-a-plug-x", "./plugins/mkt-b-plug-y"}

    def test_auth_and_repo_build_run_off_the_event_loop(self, git_env, monkeypatch):
        """resolve_token_to_user / ensure_repo_for_user must run in a worker
        thread via run_in_threadpool, not directly on the event loop —
        otherwise every git fetch blocks concurrent requests (health checks
        included), the exact regression this endpoint exists to avoid, just
        moved one call earlier than the subprocess step.

        `token_from_basic_auth` is a pure sync function that intentionally
        still runs directly on the coroutine (it's a µs-scale string parse,
        not worth offloading) — its thread ident is the event-loop thread's
        ident, used here as the baseline to compare against.
        """
        import threading

        from app.marketplace_server import git_router

        captured: dict[str, int] = {}

        real_token_from_basic_auth = git_router.token_from_basic_auth
        real_resolve_token_to_user = git_router.resolve_token_to_user

        def spy_token_from_basic_auth(auth_header):
            captured["event_loop_thread"] = threading.get_ident()
            return real_token_from_basic_auth(auth_header)

        def spy_resolve_token_to_user(conn, token):
            captured["resolve_thread"] = threading.get_ident()
            return real_resolve_token_to_user(conn, token)

        monkeypatch.setattr(git_router, "token_from_basic_auth", spy_token_from_basic_auth)
        monkeypatch.setattr(git_router, "resolve_token_to_user", spy_resolve_token_to_user)

        c = git_env["client"]
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert resp.status_code == 200
        assert "event_loop_thread" in captured and "resolve_thread" in captured
        assert captured["resolve_thread"] != captured["event_loop_thread"], (
            "resolve_token_to_user ran on the event-loop thread — it must be offloaded via run_in_threadpool"
        )


@pytest.fixture
def git_live_server(git_env):
    """Run `git_env`'s app on a real TCP socket via uvicorn in a background
    thread, so a real `git` CLI subprocess can clone/fetch against it.

    This is the behavior-preserving-refactor check: the endpoint now shells
    out to `git http-backend` as a CGI subprocess instead of dulwich's
    pure-Python WSGI handler, and the only way to prove the CGI env-var
    wiring (GIT_PROJECT_ROOT/PATH_INFO/Status: header parsing) is correct is
    to have an actual `git` client — not TestClient — speak the protocol.
    """
    import socket
    import threading

    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(git_env["app"], host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        import time

        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("live uvicorn server did not start in time")

        yield {**git_env, "port": port}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class TestGitRealClient:
    """End-to-end: an actual `git` CLI subprocess against the live server,
    proving the `git http-backend` CGI subprocess wiring is correct — not
    just that TestClient gets a 200."""

    def test_git_ls_remote_lists_main(self, git_live_server, tmp_path):
        url = f"http://x:{git_live_server['admin_pat']}@127.0.0.1:{git_live_server['port']}/marketplace.git/"
        result = subprocess.run(
            ["git", "ls-remote", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "refs/heads/main" in result.stdout

    def test_git_ls_remote_bad_pat_fails(self, git_live_server):
        url = f"http://x:not-a-real-token@127.0.0.1:{git_live_server['port']}/marketplace.git/"
        result = subprocess.run(
            ["git", "ls-remote", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_git_clone_serves_matching_content(self, git_live_server, tmp_path):
        """Clone over the real git protocol and verify the served tree
        matches what the bare-repo-introspection test above expects —
        content parity between the old dulwich path and the new
        `git http-backend` subprocess path."""
        url = f"http://x:{git_live_server['admin_pat']}@127.0.0.1:{git_live_server['port']}/marketplace.git/"
        dest = tmp_path / "clone"
        result = subprocess.run(
            ["git", "clone", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        manifest_path = dest / ".claude-plugin" / "marketplace.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text())
        names = {p["name"] for p in manifest["plugins"]}
        assert names == {"plug-x", "plug-y"}
        sources = {p["source"] for p in manifest["plugins"]}
        assert sources == {"./plugins/mkt-a-plug-x", "./plugins/mkt-b-plug-y"}

        assert (dest / "plugins" / "mkt-a-plug-x" / "CLAUDE.md").is_file()
        assert (dest / "plugins" / "mkt-b-plug-y" / "CLAUDE.md").is_file()


class TestRunGitHttpBackendStderrDrain:
    """`_run_git_http_backend` must drain stderr concurrently with stdout.

    Regression test for the classic subprocess pipe deadlock: if a child
    writes enough to stderr to fill its OS pipe buffer (~64KB on Linux)
    before the parent reads it, the child blocks on that `write()` — and
    since it's blocked, it never finishes producing stdout either, so a
    parent that only reads stderr *after* stdout EOF / process exit hangs
    forever. `asyncio.wait_for` below turns a regression into a fast
    failure instead of a hung test process.
    """

    def test_large_stderr_does_not_deadlock(self, monkeypatch):
        import asyncio
        import sys

        from app.marketplace_server import git_router

        # A child that writes well past the ~64KB pipe buffer to stderr
        # *before* writing anything to stdout — if stderr isn't drained
        # concurrently, the parent's stdout read blocks forever waiting for
        # a child that is itself blocked writing to a full stderr pipe.
        stub = (
            "import sys\n"
            "sys.stderr.write('E' * (10 * 1024 * 1024))\n"
            "sys.stderr.flush()\n"
            "sys.stdout.buffer.write(b'Status: 200 OK\\r\\n\\r\\nbody')\n"
            "sys.stdout.flush()\n"
        )
        monkeypatch.setattr(
            git_router,
            "_GIT_HTTP_BACKEND",
            (sys.executable, "-c", stub),
        )

        async def run():
            status, headers, stream = await git_router._run_git_http_backend(env={}, body=b"")
            chunks = [chunk async for chunk in stream]
            return status, headers, b"".join(chunks)

        status, _headers, body = asyncio.run(asyncio.wait_for(run(), timeout=10))
        assert status == 200
        assert body == b"body"


class TestBuildCgiEnvContentLength:
    """CONTENT_LENGTH must reflect the buffered body actually sent to the
    subprocess's stdin, not the client's Content-Length header — a chunked
    request (no Content-Length header at all) would otherwise leave
    CONTENT_LENGTH unset and git http-backend reads a zero-length body."""

    def test_content_length_uses_buffered_body_length_not_header(self):
        from pathlib import Path
        from unittest.mock import Mock

        from app.marketplace_server.git_router import _build_cgi_env

        request = Mock()
        request.method = "POST"
        request.url.query = ""
        # No Content-Length header at all (simulates chunked transfer).
        request.headers = {"content-type": "application/x-git-upload-pack-request"}

        env = _build_cgi_env(
            request,
            path="git-upload-pack",
            repo_path=Path("/tmp/does-not-matter"),
            remote_user=None,
            body_length=12345,
        )
        assert env["CONTENT_LENGTH"] == "12345"

    def test_content_length_ignores_mismatched_header(self):
        """Even when a (possibly stale/wrong) header is present, the actual
        buffered length wins — that's what's really written to stdin."""
        from pathlib import Path
        from unittest.mock import Mock

        from app.marketplace_server.git_router import _build_cgi_env

        request = Mock()
        request.method = "POST"
        request.url.query = ""
        request.headers = {"content-length": "1"}

        env = _build_cgi_env(
            request,
            path="git-upload-pack",
            repo_path=Path("/tmp/does-not-matter"),
            remote_user=None,
            body_length=999,
        )
        assert env["CONTENT_LENGTH"] == "999"


class TestRunGitHttpBackendKillsOnClientDisconnect:
    """A client disconnecting mid-stream must not leak the `git http-backend`
    subprocess. `StreamingResponse` signals this by calling `aclose()` on the
    body generator, raising `GeneratorExit` at the `yield` — the generator's
    `finally` must kill a still-running child rather than wait indefinitely
    for pack output nobody will read anymore."""

    def test_early_aclose_kills_still_running_child(self, monkeypatch):
        import asyncio
        import sys

        from app.marketplace_server import git_router

        # A child that emits CGI headers + a first stdout chunk, then sleeps
        # "indefinitely" (well past the test timeout) before it would ever
        # produce more output or exit on its own — models a large pack
        # transfer interrupted by a client disconnect.
        stub = (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'Status: 200 OK\\r\\n\\r\\nfirst-chunk')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        monkeypatch.setattr(
            git_router,
            "_GIT_HTTP_BACKEND",
            (sys.executable, "-c", stub),
        )

        async def run():
            _status, _headers, stream = await git_router._run_git_http_backend(env={}, body=b"")
            agen = stream.__aiter__()
            first_chunk = await agen.__anext__()
            assert first_chunk == b"first-chunk"
            # Simulate StreamingResponse's early disconnect cleanup.
            await agen.aclose()

        asyncio.run(asyncio.wait_for(run(), timeout=10))
