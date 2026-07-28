"""Tests for the wave 3C in-chat preview loop: the scoped preview-grant REST
endpoint (`app/api/data_apps.py`, Task 5) and the four chat-surface MCP tools
(`app/api/mcp/foundation_tools.py`, Task 4).

Two fixture idioms:

  - ``preview_api_env`` — the ``api_env`` idiom of ``tests/test_data_apps_api.py``
    (real user/token/app rows, a real ``TestClient(app)``) for direct REST
    assertions against ``POST /{slug}/preview-grant``.
  - ``preview_env`` — ``(client, call_tool)``, mirroring the ASGI-replay idiom
    of ``tests/test_broker_routes.py``: the foundation tools' internal
    ``httpx.AsyncClient()`` self-calls are routed into the SAME in-process
    app via ``httpx.ASGITransport`` (no real network hop), so the MCP tool
    tests exercise the real `preview-grant`/`GET {slug}` endpoints end to
    end rather than mocking their response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


def _enable_data_apps(data_dir) -> None:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None


@pytest.fixture
def preview_api_env(e2e_env, monkeypatch):
    """Real user/token/app rows + TestClient(app), data_apps enabled — for
    direct REST assertions against `POST /{slug}/preview-grant`."""
    from app.main import create_app
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _enable_data_apps(data_dir)

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")
        users.create(id="grantee1", email="grantee@test.local", name="Grantee")

        ug = UserGroupsRepository(conn)
        gid = ug.create("Analysts", is_system=False)["id"]
        UserGroupMembersRepository(conn).add_member("grantee1", gid, source="test")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("owner1", "owner@test.local"),
            ("other1", "other@test.local"),
            ("grantee1", "grantee@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt_token = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt_token

        apps_repo = DataAppsRepository(conn)
        apps_repo.create(slug="dash", name="DASH", owner_user_id="owner1")

        from src.repositories.resource_grants import ResourceGrantsRepository

        ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="dash")
    finally:
        conn.close()

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {"client": client, "owner_pat": pats["owner1"], "other_pat": pats["other1"], "grantee_pat": pats["grantee1"]}


class TestPreviewGrantEndpoint:
    def test_owner_gets_scoped_cookie(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "preview_cookie" in body and "expires_at" in body
        cookie = body["preview_cookie"]
        assert "Path=/apps/dash/" in cookie
        assert "SameSite=Lax" in cookie
        assert "HttpOnly" in cookie
        # The cookie must ALSO ride a real Set-Cookie response header — an
        # HttpOnly cookie can only be installed by the browser via the server's
        # Set-Cookie (the frontend fetches this endpoint same-origin), never
        # through document.cookie, which silently discards HttpOnly cookies.
        set_cookie = r.headers.get("set-cookie", "")
        assert set_cookie.startswith("adp_preview=") and "HttpOnly" in set_cookie

    def test_grantee_can_request_a_grant(self, preview_api_env):
        """Unlike git-credential, preview-grant is view-access, not
        owner/Admin-only — a group grantee (`_can_view` passes via
        `resource_grants`) may mint their own preview cookie."""
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["grantee_pat"]))
        assert r.status_code == 200, r.text

    def test_grant_minted_under_requester_not_app_owner(self, preview_api_env):
        """The preview token belongs to the CALLER who requested it (a grantee
        here, `grantee1`), not the app's owner (`owner1`). This is what lets the
        SessionEnd revoke — keyed on the chat session's own user — actually tear
        the grant down, so a grantee-previews-a-draft grant isn't left live for
        the full TTL after their session ends."""
        from src.repositories import access_token_repo
        from app.api.data_apps import revoke_preview_tokens_for_user

        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["grantee_pat"]))
        assert r.status_code == 200, r.text

        repo = access_token_repo()

        def _live_preview_names(uid):
            return [
                t["name"]
                for t in repo.list_for_user(uid, include_revoked=False)
                if t["name"].startswith("data-app-preview:")
            ]

        assert _live_preview_names("grantee1") == ["data-app-preview:dash"], "token must be under the requester"
        assert _live_preview_names("owner1") == [], "token must NOT be under the app owner"

        # SessionEnd revoke keyed on the requester's id revokes it (the hard cap works;
        # pat_resolver rejects revoked tokens, so it stops serving immediately).
        revoke_preview_tokens_for_user("grantee1")
        assert _live_preview_names("grantee1") == []

    def test_stranger_forbidden(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["other_pat"]))
        assert r.status_code == 403

    def test_missing_app_404s(self, preview_api_env):
        env = preview_api_env
        r = env["client"].post("/api/data-apps/does-not-exist/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 404

    def test_disabled_feature_404s(self, preview_api_env, monkeypatch):
        import app.api.data_apps as data_apps_api

        monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
        env = preview_api_env
        r = env["client"].post("/api/data-apps/dash/preview-grant", headers=_auth(env["owner_pat"]))
        assert r.status_code == 404
        assert r.json()["detail"] == "data_apps_disabled"


# ---------------------------------------------------------------------------
# MCP tools — real REST round-trip via httpx.ASGITransport (no mocked bodies)
# ---------------------------------------------------------------------------


@pytest.fixture
def preview_env(e2e_env, monkeypatch):
    """`(client, call_tool)` — real app + rows, `call_tool` invokes a
    registered foundation tool directly (mirrors `app.api.mcp_http.<name>`
    unit-test idiom), with the tool's internal `httpx.AsyncClient()`
    self-calls routed into the SAME in-process app via `ASGITransport`."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.auth.jwt import create_access_token
    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _enable_data_apps(data_dir)

    conn = get_system_db()
    try:
        UserRepository(conn).create(id="owner1", email="owner@test.local", name="Owner")

        tid = str(uuid.uuid4())
        owner_pat = create_access_token("owner1", "owner@test.local", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid,
            user_id="owner1",
            name="owner1-pat",
            token_hash=hashlib.sha256(owner_pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=None,
        )

        apps_repo = DataAppsRepository(conn)
        apps_repo.create(slug="dash", name="DASH", owner_user_id="owner1")
        apps_repo.create(slug="dash--init", name="DASH INIT", owner_user_id="owner1")
    finally:
        conn.close()

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)

    import app.api.mcp_http as mcp_mod

    _RealAsyncClient = httpx.AsyncClient

    def _asgi_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_async_client)

    def call_tool(name: str, **kwargs):
        token = mcp_mod._current_token.set(owner_pat)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(token)

    return client, call_tool


def test_preview_placeholder_then_live(preview_env):
    client, call_tool = preview_env
    first = call_tool("agnes_data_app_preview", slug="dash--init")
    assert first["render"] == "data_app_preview" and first["url"] is None
    assert first["preview_cookie"] is None

    live = call_tool("agnes_data_app_preview", slug="dash--init", url="/apps/dash--init/")
    assert live["render"] == "data_app_preview"
    assert live["slug"] == "dash--init"
    assert live["url"] == "/apps/dash--init/"
    assert "data-app-preview" in live["preview_cookie"] or "Max-Age" in live["preview_cookie"]
    assert "Path=/apps/dash--init/" in live["preview_cookie"]


def test_preview_refresh_render_directive(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_refresh", slug="dash")
    assert r == {"render": "data_app_preview_refresh", "slug": "dash"}


def test_preview_close_render_directive(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_close", slug="dash")
    assert r == {"render": "data_app_preview_close", "slug": "dash"}


def test_credentials_is_terminal_render(preview_env):
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_credentials", slug="dash")
    assert r["render"] == "data_app_credentials" and r["url"]
    assert r["slug"] == "dash"


def test_preview_live_url_friendly_when_disabled(preview_env, monkeypatch):
    import app.api.data_apps as data_apps_api

    monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_preview", slug="dash", url="/apps/dash/")
    assert r == {
        "error": "data_apps_disabled",
        "message": "Data apps are disabled on this instance.",
    }


def test_credentials_friendly_when_disabled(preview_env, monkeypatch):
    import app.api.data_apps as data_apps_api

    monkeypatch.setattr(data_apps_api, "feature_enabled", lambda *a, **k: False)
    client, call_tool = preview_env
    r = call_tool("agnes_data_app_credentials", slug="dash")
    assert r["error"] == "data_apps_disabled"
