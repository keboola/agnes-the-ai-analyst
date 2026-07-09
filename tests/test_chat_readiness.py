"""Tests for cloud-chat readiness — secret presence + live key probes + the
admin endpoints that surface/set them.

The readiness module never returns secret *values* — only presence — and the
live probes classify auth failures distinctly from connectivity errors.
"""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import _ensure_schema
from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.chat import readiness


TEST_ADMIN = {"id": "admin1", "email": "admin@test.com", "is_admin": True}


def _cfg(**kw):
    base = dict(enabled=True, provider="e2b", e2b_template_id="agnes-chat")
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# secret_status — presence only, required vs set
# ---------------------------------------------------------------------------


def test_secret_status_disabled_config_is_never_ready():
    s = readiness.secret_status(None)
    assert s["enabled"] is False
    assert s["ready"] is False
    # Nothing is "required" when chat is disabled.
    assert all(not v["required"] for v in s["secrets"].values())


def test_secret_status_ready_when_all_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("E2B_API_KEY", "e2b_xxx")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg())
    assert s["ready"] is True
    assert s["missing"] == []
    assert s["secrets"]["e2b_api_key"]["set"] is True
    # No secret value is echoed back anywhere in the payload.
    assert "sk-ant-xxx" not in str(s)
    assert "e2b_xxx" not in str(s)


def test_secret_status_flags_missing_required(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg())
    assert s["ready"] is False
    assert "anthropic_api_key" in s["missing"]
    assert "e2b_api_key" in s["missing"]


def test_secret_status_weak_jwt_is_not_set(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "short")  # < 32 bytes
    s = readiness.secret_status(_cfg())
    assert s["secrets"]["jwt_secret_key"]["set"] is False
    assert "jwt_secret_key" in s["missing"]


def test_secret_status_e2b_not_required_for_other_provider(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    s = readiness.secret_status(_cfg(provider="local"))
    assert s["secrets"]["e2b_api_key"]["required"] is False
    assert "e2b_api_key" not in s["missing"]


# ---------------------------------------------------------------------------
# Live probes — E2B
# ---------------------------------------------------------------------------


def test_test_e2b_key_missing(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    import asyncio

    r = asyncio.run(readiness.test_e2b_key())
    assert r["ok"] is False
    assert "not set" in r["detail"]


def test_test_e2b_key_valid(monkeypatch):
    import asyncio
    import e2b

    async def _fake_list(*a, **k):
        return []

    monkeypatch.setattr(e2b.AsyncSandbox, "list", staticmethod(_fake_list))
    r = asyncio.run(readiness.test_e2b_key(api_key="e2b_good"))
    assert r["ok"] is True


def test_test_e2b_key_auth_failure_classified(monkeypatch):
    import asyncio
    import e2b

    class _AuthErr(Exception):
        status_code = 401

    async def _fake_list(*a, **k):
        raise _AuthErr("unauthorized")

    monkeypatch.setattr(e2b.AsyncSandbox, "list", staticmethod(_fake_list))
    r = asyncio.run(readiness.test_e2b_key(api_key="e2b_bad"))
    assert r["ok"] is False
    assert "authentication failed" in r["detail"]


# ---------------------------------------------------------------------------
# Live probes — Anthropic
# ---------------------------------------------------------------------------


def test_test_anthropic_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import asyncio

    r = asyncio.run(readiness.test_anthropic_key())
    assert r["ok"] is False
    assert "not set" in r["detail"]


def test_test_anthropic_key_valid(monkeypatch):
    import asyncio
    import anthropic

    class _Msgs:
        def create(self, **kw):
            return SimpleNamespace(content=[])

    class _FakeClient:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    r = asyncio.run(readiness.test_anthropic_key(api_key="sk-good"))
    assert r["ok"] is True


def test_test_anthropic_key_auth_failure_classified(monkeypatch):
    import asyncio
    import anthropic

    class _AuthErr(Exception):
        status_code = 401

    class _Msgs:
        def create(self, **kw):
            raise _AuthErr("invalid x-api-key")

    class _FakeClient:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    r = asyncio.run(readiness.test_anthropic_key(api_key="sk-bad"))
    assert r["ok"] is False
    assert "authentication failed" in r["detail"]


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


def _make_app(*, chat_enabled: bool = True) -> tuple[TestClient, duckdb.DuckDBPyConnection]:
    from app.api.admin_chat import router as admin_chat_router

    app = FastAPI()
    app.include_router(admin_chat_router)
    app.state.chat_config = SimpleNamespace(
        enabled=chat_enabled,
        provider="e2b",
        e2b_template_id="agnes-chat",
    )
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    app.dependency_overrides[require_admin] = lambda: TEST_ADMIN
    app.dependency_overrides[_get_db] = lambda: conn
    return TestClient(app), conn


def test_readiness_endpoint_returns_presence(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    client, _ = _make_app()
    r = client.get("/admin/chat/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["secrets"]["e2b_api_key"]["set"] is False
    assert body["secrets"]["anthropic_api_key"]["set"] is True
    assert "e2b_api_key" in body["missing"]


def test_set_secrets_persists_only_provided(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.secrets.persist_overlay_token",
        lambda name, value: calls.append((name, value)),
    )
    client, _ = _make_app()
    r = client.post("/admin/chat/secrets", json={"e2b_api_key": "e2b_new"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["e2b_api_key"]
    assert body["restart_required"] is True
    # Only the provided key was persisted; the omitted one was untouched.
    assert calls == [("E2B_API_KEY", "e2b_new")]


def test_set_secrets_rejects_empty_payload():
    client, _ = _make_app()
    r = client.post("/admin/chat/secrets", json={})
    assert r.status_code == 422


def test_set_secrets_audits_without_value(monkeypatch, e2e_env):
    # The endpoint writes its audit row through the backend-aware
    # ``audit_repo()`` factory, which resolves to ``get_system_db()`` on
    # DuckDB — not the isolated ``conn`` this fixture wires up for the
    # request's own ``_get_db`` override. Read the row back from the same
    # system DB (``e2e_env`` gives it a fresh, test-scoped DATA_DIR).
    from src.db import get_system_db

    monkeypatch.setattr("app.secrets.persist_overlay_token", lambda name, value: None)
    client, _conn = _make_app()
    r = client.post("/admin/chat/secrets", json={"anthropic_api_key": "sk-secret-value"})
    assert r.status_code == 200
    sys_conn = get_system_db()
    row = sys_conn.execute("SELECT action, params FROM audit_log WHERE action = 'chat.secrets.update'").fetchone()
    sys_conn.close()
    assert row is not None
    # The secret value must never land in the audit row.
    assert "sk-secret-value" not in (row[1] or "")
    assert "anthropic_api_key" in (row[1] or "")


def test_secrets_endpoints_require_admin(monkeypatch):
    """Without the require_admin override, a non-admin is refused."""
    from app.api.admin_chat import router as admin_chat_router

    app = FastAPI()
    app.include_router(admin_chat_router)
    app.state.chat_config = SimpleNamespace(enabled=True, provider="e2b", e2b_template_id="t")
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    # get_current_user returns a non-admin; require_admin runs for real and 403s.
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "email": "u@test.com"}
    app.dependency_overrides[_get_db] = lambda: conn
    client = TestClient(app)
    assert client.get("/admin/chat/readiness").status_code == 403
    assert client.post("/admin/chat/secrets", json={"e2b_api_key": "x"}).status_code == 403
    assert client.post("/admin/chat/secrets/test").status_code == 403
