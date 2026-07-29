"""Connect probes (introspect / classify / test) on /api/admin/mcp-sources.

Two behaviors guarded here:

1. ExceptionGroup unwrapping — the MCP SDK's streamable-http client wraps
   the real failure (e.g. an httpx 401) in an anyio TaskGroup, whose str()
   is just "unhandled errors in a TaskGroup (1 sub-exception)". The admin
   UI must see the leaf cause instead.
2. per_user-scoped sources: the probes prefer the calling admin's own
   connected secret when one exists, and otherwise stay on the caller-less
   shared-vault path (the pre-existing behavior for shared sources).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.api.admin_mcp import _exc_summary
from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.db import get_system_db
from src.repositories import per_user_secrets_repo
from src.repositories.mcp_sources import MCPSourceRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _seed_source(source_id: str = "src_probe", *, scope: str = "shared") -> None:
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=f"probe-{source_id}",
        transport="http",
        url="https://upstream.example.com/mcp",
        auth_method="bearer",
        scope=scope,
    )
    conn.close()


# ── _exc_summary unit tests ───────────────────────────────────────────────


def test_exc_summary_plain_exception():
    assert _exc_summary(ValueError("boom")) == "ValueError: boom"


def test_exc_summary_empty_message_falls_back_to_type():
    assert _exc_summary(RuntimeError()) == "RuntimeError"


def test_exc_summary_unwraps_nested_exception_groups():
    leaf = RuntimeError("Client error '401 Unauthorized' for url 'https://x/mcp/'")
    grouped = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ExceptionGroup("unhandled errors in a TaskGroup", [leaf])],
    )
    out = _exc_summary(grouped)
    assert "401 Unauthorized" in out
    assert "TaskGroup" not in out


def test_exc_summary_keeps_first_line_and_dedupes():
    leaf = RuntimeError("401 Unauthorized\nFor more information check: https://mdn")
    grouped = ExceptionGroup("g", [leaf, RuntimeError("401 Unauthorized\nsecond line")])
    out = _exc_summary(grouped)
    assert out == "RuntimeError: 401 Unauthorized"


# ── endpoint error surfacing ──────────────────────────────────────────────


def _raise_grouped_401(source, *, caller_user_id=None):
    raise ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [RuntimeError("Client error '401 Unauthorized' for url 'https://upstream.example.com/mcp'")],
    )


def test_introspect_surfaces_leaf_cause(seeded_app, monkeypatch):
    _seed_source()

    async def fake_list_tools(source, *, caller_user_id=None):
        _raise_grouped_401(source)

    monkeypatch.setattr("connectors.mcp.client.list_tools_async", fake_list_tools)
    r = seeded_app["client"].post(
        "/api/admin/mcp-sources/src_probe/introspect",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "401 Unauthorized" in detail
    assert "TaskGroup" not in detail


def test_test_endpoint_surfaces_leaf_cause(seeded_app, monkeypatch):
    _seed_source()

    async def fake_list_tools(source, *, caller_user_id=None):
        _raise_grouped_401(source)

    monkeypatch.setattr("connectors.mcp.client.list_tools_async", fake_list_tools)
    r = seeded_app["client"].post(
        "/api/admin/mcp-sources/src_probe/test",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "401 Unauthorized" in body["error"]
    assert "TaskGroup" not in body["error"]


# ── per_user caller threading ─────────────────────────────────────────────


def _probe_caller_id(seeded_app, monkeypatch, source_id: str):
    captured = {}

    async def fake_list_tools(source, *, caller_user_id=None):
        captured["caller_user_id"] = caller_user_id
        return []

    monkeypatch.setattr("connectors.mcp.client.list_tools_async", fake_list_tools)
    r = seeded_app["client"].post(
        f"/api/admin/mcp-sources/{source_id}/introspect",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200
    return captured["caller_user_id"]


def test_introspect_per_user_prefers_admins_own_secret(seeded_app, monkeypatch):
    _seed_source("src_pu", scope="per_user")
    per_user_secrets_repo().upsert("src_pu", "admin1", "admins-own-token")
    assert _probe_caller_id(seeded_app, monkeypatch, "src_pu") == "admin1"


def test_introspect_per_user_without_own_secret_stays_shared(seeded_app, monkeypatch):
    _seed_source("src_pu2", scope="per_user")
    assert _probe_caller_id(seeded_app, monkeypatch, "src_pu2") is None


def test_introspect_shared_scope_stays_callerless(seeded_app, monkeypatch):
    _seed_source("src_sh", scope="shared")
    per_user_secrets_repo().upsert("src_sh", "admin1", "irrelevant")
    assert _probe_caller_id(seeded_app, monkeypatch, "src_sh") is None
