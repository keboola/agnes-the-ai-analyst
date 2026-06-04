"""Backend-parity tests for the admin MCP cluster (app/api/admin_mcp.py).

Each test seeds state through the backend-aware factory (mcp_sources_repo,
tool_registry_repo, user_groups_repo) so the row lands in whichever backend is
active, then exercises the HTTP endpoint via ``seeded_app_both`` — once on
DuckDB, once on real Postgres.

Discriminator: a handler that reads/validates through the factory returns the
seeded row on BOTH backends; a handler that reads through a raw DuckDB conn
(``Depends(_get_db)``) returns it on DuckDB but stale/empty on Postgres, so the
``[pg]`` parametrization fails — pinpointing a backend-split bug.
"""
from __future__ import annotations

import pytest


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_source(name="probe_src"):
    """Seed an MCP source through the factory; return its id."""
    import uuid
    from src.repositories import mcp_sources_repo

    sid = uuid.uuid4().hex
    mcp_sources_repo().upsert(
        id=sid,
        name=name,
        transport="http",
        url="https://example.com/mcp",
        enabled=True,
        scope="shared",
    )
    return sid


def _seed_tool(source_id, *, tool_id=None, exposed_name="probe_tool"):
    """Seed a passthrough tool against ``source_id`` through the factory."""
    import uuid
    from src.repositories import tool_registry_repo

    tid = tool_id or uuid.uuid4().hex
    tool_registry_repo().upsert(
        tool_id=tid,
        source_id=source_id,
        original_name="orig_probe",
        exposed_name=exposed_name,
        mode="passthrough",
        enabled=True,
    )
    return tid


# ---------------------------------------------------------------------------
# GET /api/admin/mcp-sources — list (handler reads via mcp_sources_repo())
# ---------------------------------------------------------------------------

def test_list_mcp_sources_reflects_seeded_source(seeded_app_both):
    sid = _seed_source(name="list_probe_src")
    r = seeded_app_both["client"].get(
        "/api/admin/mcp-sources", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {s.get("id") for s in r.json()}
    assert sid in ids, (
        f"[{seeded_app_both['backend']}] seeded source missing from "
        f"GET /api/admin/mcp-sources: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/mcp-sources/{id} — detail incl. tools (mcp_sources_repo +
# tool_registry_repo)
# ---------------------------------------------------------------------------

def test_get_mcp_source_detail_includes_seeded_tool(seeded_app_both):
    sid = _seed_source(name="detail_probe_src")
    tid = _seed_tool(sid, exposed_name="detail_probe_tool")
    r = seeded_app_both["client"].get(
        f"/api/admin/mcp-sources/{sid}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/admin/mcp-sources/{{id}} "
        f"returned {r.status_code} for a factory-seeded source: {r.text}"
    )
    body = r.json()
    assert body.get("id") == sid
    tool_ids = {t.get("tool_id") for t in body.get("tools", [])}
    assert tid in tool_ids, (
        f"[{seeded_app_both['backend']}] seeded tool missing from source "
        f"detail tools[]: {body}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/mcp-tools — list (tool_registry_repo())
# ---------------------------------------------------------------------------

def test_list_mcp_tools_reflects_seeded_tool(seeded_app_both):
    sid = _seed_source(name="tools_list_src")
    tid = _seed_tool(sid, exposed_name="tools_list_tool")
    r = seeded_app_both["client"].get(
        "/api/admin/mcp-tools", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {t.get("tool_id") for t in r.json()}
    assert tid in ids, (
        f"[{seeded_app_both['backend']}] seeded tool missing from "
        f"GET /api/admin/mcp-tools: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/mcp-tools/{id} — detail incl. grants (tool_registry_repo())
# ---------------------------------------------------------------------------

def test_get_mcp_tool_detail_renders_seeded_tool(seeded_app_both):
    sid = _seed_source(name="tool_detail_src")
    tid = _seed_tool(sid, exposed_name="tool_detail_tool")
    r = seeded_app_both["client"].get(
        f"/api/admin/mcp-tools/{tid}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/admin/mcp-tools/{{id}} "
        f"returned {r.status_code} for a factory-seeded tool: {r.text}"
    )
    body = r.json()
    assert body.get("tool_id") == tid
    assert "grants" in body


# ---------------------------------------------------------------------------
# POST /api/admin/mcp-tools/{id}/grants — mutation.
#
# The handler validates the group with a RAW DuckDB conn:
#     conn.execute("SELECT id FROM user_groups WHERE id = ?", [group_id])
# (admin_mcp.py:866-868, conn = Depends(_get_db)). The group is seeded through
# the factory, so on Postgres it lives in PG while the raw conn reads an empty
# DuckDB → 404 user_group_not_found. On DuckDB both share the same conn → 200.
# This is the canonical backend-split discriminator for this cluster.
# ---------------------------------------------------------------------------

def test_add_mcp_tool_grant_finds_factory_seeded_group(seeded_app_both):
    from src.repositories import user_groups_repo

    sid = _seed_source(name="grant_probe_src")
    tid = _seed_tool(sid, exposed_name="grant_probe_tool")
    grp = user_groups_repo().create(name="grant_probe_grp", created_by="admin1")
    gid = grp["id"]

    r = seeded_app_both["client"].post(
        f"/api/admin/mcp-tools/{tid}/grants",
        json={"group_id": gid},
        headers=_auth(seeded_app_both),
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] POST mcp-tools/{{id}}/grants returned "
        f"{r.status_code} for a factory-seeded group {gid} — the handler "
        f"validates the group off a raw DuckDB conn (admin_mcp.py:866-868) "
        f"instead of user_groups_repo()/factory: {r.text}"
    )
    assert r.json().get("granted") is True

    # And the grant should now show on the tool detail (read via factory).
    detail = seeded_app_both["client"].get(
        f"/api/admin/mcp-tools/{tid}", headers=_auth(seeded_app_both)
    )
    assert gid in detail.json().get("grants", []), (
        f"[{seeded_app_both['backend']}] grant not reflected in tool detail: "
        f"{detail.json()}"
    )


# ---------------------------------------------------------------------------
# DELETE /api/admin/mcp-sources/{id} — per-user secret cleanup.
#
# The delete handler purges the source's vault secrets so no orphaned encrypted
# blobs survive. Per-user secrets were migrated to Postgres (#530), but the
# cleanup used a raw ``PerUserSecretsRepository(conn)`` off the always-DuckDB
# connection — so on a PG instance the per-user rows were NOT deleted and
# leaked. The fix routes the cleanup through ``per_user_secrets_repo()``.
# ---------------------------------------------------------------------------


@pytest.fixture
def _vault_key(monkeypatch):
    """Storing a per-user secret requires a configured Fernet vault key."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


def test_delete_source_clears_per_user_secret_on_both_backends(
    seeded_app_both, _vault_key
):
    from src.repositories import per_user_secrets_repo

    sid = _seed_source(name="del_cleanup_src")
    # Seed a per-user secret through the factory (lands in the active backend).
    per_user_secrets_repo().upsert(sid, "analyst1", "to-be-purged")
    assert per_user_secrets_repo().has(sid, "analyst1") is True

    r = seeded_app_both["client"].delete(
        f"/api/admin/mcp-sources/{sid}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 204, (
        f"[{seeded_app_both['backend']}] DELETE mcp-sources/{{id}} returned "
        f"{r.status_code}: {r.text}"
    )

    assert per_user_secrets_repo().has(sid, "analyst1") is False, (
        f"[{seeded_app_both['backend']}] per-user secret survived source delete "
        f"— the cleanup deleted off a raw DuckDB conn instead of "
        f"per_user_secrets_repo(), orphaning the row on Postgres."
    )
