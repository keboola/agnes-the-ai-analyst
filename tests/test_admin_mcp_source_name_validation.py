"""MCP source ``name`` must be a safe SQL identifier.

The orchestrator's extract scan validates each ``/data/extracts/<name>``
directory with the STRICT identifier rule (``validate_identifier`` in
``src/identifier_validation.py``) before ATTACHing. A source created with
e.g. a hyphenated name passed the admin API, materialized "successfully",
and then silently never appeared in analytics/catalog — the only signal
was a server-log WARNING ("Rejected unsafe source_name identifier").
These tests pin the fix: the create + rename paths reject unsafe names
up front with an actionable 400, using the same validator the
orchestrator enforces (no second regex to drift).
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _create(seeded_app, name):
    return seeded_app["client"].post(
        "/api/admin/mcp-sources",
        headers=_auth(seeded_app),
        json={"name": name, "transport": "http", "url": "https://up.example.com/mcp"},
    )


def test_create_rejects_hyphenated_name(seeded_app):
    r = _create(seeded_app, "keboola-crm")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "identifier" in detail and "keboola-crm" in detail


def test_create_rejects_leading_digit_name(seeded_app):
    r = _create(seeded_app, "1crm")
    assert r.status_code == 400


def test_create_accepts_underscore_name(seeded_app):
    r = _create(seeded_app, "keboola_crm")
    assert r.status_code == 201


def test_rename_rejects_unsafe_name(seeded_app):
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_rename", name="safe_name", transport="http",
        url="https://up.example.com/mcp",
    )
    conn.close()
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_rename",
        headers=_auth(seeded_app),
        json={"name": "bad-name"},
    )
    assert r.status_code == 400
    assert "identifier" in r.json()["detail"]


def test_rename_strips_whitespace_before_storing(seeded_app):
    """A padded-but-valid name must be stored STRIPPED — _merge_source_patch
    used to pass the raw payload value through, so ' padded' bypassed the
    identifier check at attach time (the orchestrator rejects it)."""
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_pad", name="old_name", transport="http",
        url="https://up.example.com/mcp",
    )
    conn.close()
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_pad",
        headers=_auth(seeded_app),
        json={"name": "  padded_name  "},
    )
    assert r.status_code == 200
    g = client.get("/api/admin/mcp-sources/src_pad", headers=_auth(seeded_app))
    assert g.json()["name"] == "padded_name"


def test_rename_padded_same_name_stores_clean(seeded_app):
    """' existing_name ' equals the existing name after strip — validation is
    rightly skipped, but the STORED name must stay the clean one."""
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_same", name="same_name", transport="http",
        url="https://up.example.com/mcp",
    )
    conn.close()
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_same",
        headers=_auth(seeded_app),
        json={"name": "  same_name  "},
    )
    assert r.status_code == 200
    g = client.get("/api/admin/mcp-sources/src_same", headers=_auth(seeded_app))
    assert g.json()["name"] == "same_name"


def test_rename_whitespace_only_name_rejected(seeded_app):
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_blank", name="keep_name", transport="http",
        url="https://up.example.com/mcp",
    )
    conn.close()
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_blank",
        headers=_auth(seeded_app),
        json={"name": "   "},
    )
    assert r.status_code == 400
