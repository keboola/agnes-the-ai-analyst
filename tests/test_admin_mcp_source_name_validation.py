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
