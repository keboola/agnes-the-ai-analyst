"""Admin per-user secret coverage — "who has connected their own secret,
and when" — surfaced on the ``GET /api/admin/mcp-sources/{id}`` detail
payload (issue #466 residual).

Folded into the existing detail endpoint (same shape as the pre-existing
``tools`` sub-list) rather than a new sibling route: that response has no
declared Pydantic model (an empty ``{}`` schema in the OpenAPI doc), so
widening it carries no REST x CLI x MCP triple-surface obligation, and the
admin gate is already the one every other route in this module uses
(``require_admin``). Never exposes a secret value — identity (user_id /
email / name) + timestamp only.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _seed_source_with_grant(source_id: str, grant_to: list[str], scope: str = "per_user") -> None:
    """Register an MCP source plus one passthrough tool granted to every
    user id in ``grant_to`` — ``PUT …/my-secret`` 403s without a grant."""
    from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=f"cov-{source_id}",
        transport="http",
        url="https://upstream.example.com/mcp",
        auth_method="bearer",
        scope=scope,
    )
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id=f"{source_id}.lookup",
        source_id=source_id,
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="grant target",
    )
    grp = UserGroupsRepository(conn).create(name=f"grant-{source_id}", description=None)
    tools.add_grant(f"{source_id}.lookup", grp["id"])
    members = UserGroupMembersRepository(conn)
    for uid in grant_to:
        members.add_member(uid, grp["id"], source="system_seed")
    conn.close()


def _second_analyst() -> str:
    """Seed a second non-admin user and return a bearer token for them.

    ``seeded_app`` only seeds one analyst; the coverage table needs two
    distinct identities to prove it lists exactly who connected."""
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="analyst2", email="analyst2@test.com", name="Analyst Two")
    conn.close()
    return create_access_token("analyst2", "analyst2@test.com")


def test_coverage_empty_before_any_connect(seeded_app):
    _seed_source_with_grant("src_cov_empty", grant_to=["analyst1"])
    client = seeded_app["client"]
    r = client.get(
        "/api/admin/mcp-sources/src_cov_empty",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["per_user_secrets"] == []


def test_coverage_lists_exactly_the_users_who_connected(seeded_app):
    """Two users PUT their own secret; the admin coverage list names exactly
    those two, each with a timestamp, and never a secret value."""
    source_id = "src_cov_two"
    analyst2_token = _second_analyst()
    _seed_source_with_grant(source_id, grant_to=["analyst1", "analyst2"])
    client = seeded_app["client"]

    client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "analyst-one-secret"},
    )
    client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {analyst2_token}"},
        json={"value": "analyst-two-secret"},
    )

    r = client.get(
        f"/api/admin/mcp-sources/{source_id}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    coverage = r.json()["per_user_secrets"]
    assert {row["user_id"] for row in coverage} == {"analyst1", "analyst2"}
    for row in coverage:
        assert row["updated_at"] is not None
    body_text = r.text
    assert "analyst-one-secret" not in body_text
    assert "analyst-two-secret" not in body_text


def test_coverage_does_not_list_an_unconnected_granted_user(seeded_app):
    """A user who is GRANTED but never stored a secret must not appear —
    coverage tracks who connected, not who is entitled to."""
    source_id = "src_cov_partial"
    analyst2_token = _second_analyst()
    _seed_source_with_grant(source_id, grant_to=["analyst1", "analyst2"])
    client = seeded_app["client"]

    client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "only-analyst-one"},
    )
    # analyst2 is granted but deliberately never connects.
    assert analyst2_token  # sanity: token minted, just unused for a PUT

    r = client.get(
        f"/api/admin/mcp-sources/{source_id}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    coverage = r.json()["per_user_secrets"]
    assert [row["user_id"] for row in coverage] == ["analyst1"]


def test_coverage_resolves_email_and_name(seeded_app):
    source_id = "src_cov_identity"
    _seed_source_with_grant(source_id, grant_to=["analyst1"])
    client = seeded_app["client"]
    client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "tok"},
    )
    r = client.get(
        f"/api/admin/mcp-sources/{source_id}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    row = r.json()["per_user_secrets"][0]
    assert row["user_id"] == "analyst1"
    assert row["email"] == "analyst@test.com"
    assert row["name"] == "Analyst"


def test_coverage_read_requires_admin(seeded_app):
    """A non-admin caller gets 403 on the whole detail route — same gate as
    every other route in this module, including the new coverage data."""
    source_id = "src_cov_403"
    _seed_source_with_grant(source_id, grant_to=["analyst1"])
    client = seeded_app["client"]
    r = client.get(
        f"/api/admin/mcp-sources/{source_id}",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 403
