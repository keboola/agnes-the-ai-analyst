"""Management API `/api/v1/agents` — CRUD, scope, agent-PAT issuance (Task 5).

Covers `app/api/agents_admin.py`. Auth matrix reference:
docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md §2.

Every route requires an interactive session (`require_session_token` already
rejects plain PATs and agent PATs) — ownership is enforced per-{id} route:
non-owner/non-admin -> 404; admin -> GET allowed (read-only governance),
mutations + token minting on a foreign agent -> 403.
"""

from __future__ import annotations

import hashlib
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _AuthedClient:
    """Thin `TestClient` wrapper that injects a bearer token by default, so
    call sites read like `mgmt_client.post(...)` (the task brief's shape)
    while still allowing an explicit `headers=` override for another
    principal (cross-user / admin tests use `mgmt_env["client"]` directly
    instead)."""

    def __init__(self, client: TestClient, token: str):
        self._client = client
        self._token = token

    def _headers(self, headers):
        merged = {"Authorization": f"Bearer {self._token}"}
        if headers:
            merged.update(headers)
        return merged

    def get(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.get(url, **kw)

    def post(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.post(url, **kw)

    def put(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.put(url, **kw)

    def delete(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def mgmt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.main import create_app
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    client = TestClient(create_app())
    return {
        "client": client,
        "owner": {"id": "owner1", "email": "owner@test.com", "token": create_access_token("owner1", "owner@test.com")},
        "other": {"id": "other1", "email": "other@test.com", "token": create_access_token("other1", "other@test.com")},
        "admin": {"id": "admin1", "email": "admin@test.com", "token": create_access_token("admin1", "admin@test.com")},
    }


@pytest.fixture
def mgmt_client(mgmt_env):
    return _AuthedClient(mgmt_env["client"], mgmt_env["owner"]["token"])


@pytest.fixture
def default_agent_id(mgmt_env):
    """The owner's seeded `is_default` agent — all-mode, per spec."""
    from src.repositories import agents_repo

    row = agents_repo().get_or_create_default(mgmt_env["owner"]["id"])
    return row["id"]


@pytest.fixture
def selected_agent_id(mgmt_env):
    from src.repositories import agents_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id=mgmt_env["owner"]["id"],
        name="Selected",
        slug="selected-agent",
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    return agent_id


# ---------------------------------------------------------------------------
# Task-brief snippets
# ---------------------------------------------------------------------------


def test_create_defaults_selected(mgmt_client):
    r = mgmt_client.post("/api/v1/agents", json={"name": "Sales", "slug": "sales"})
    assert r.status_code == 201
    body = r.json()
    assert body["plugins_mode"] == "selected" and body["tables_mode"] == "selected"
    assert body["connections_mode"] == "selected" and body["memory_mode"] == "selected"


def test_token_requires_selected_modes(mgmt_client, default_agent_id, selected_agent_id):
    r = mgmt_client.post(f"/api/v1/agents/{default_agent_id}/tokens", json={"name": "t"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "agent_not_selected_mode"

    r = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert r.status_code == 200 and r.json()["token"].startswith("eyJ")


# ---------------------------------------------------------------------------
# Create validation
# ---------------------------------------------------------------------------


def test_create_slug_invalid_format(mgmt_client):
    r = mgmt_client.post("/api/v1/agents", json={"name": "Bad", "slug": "Not_Valid!"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_slug"


def test_create_slug_reserved(mgmt_client):
    r = mgmt_client.post("/api/v1/agents", json={"name": "Default-ish", "slug": "default"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "slug_reserved"


def test_create_slug_conflict(mgmt_client):
    r1 = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "dup"})
    assert r1.status_code == 201
    r2 = mgmt_client.post("/api/v1/agents", json={"name": "B", "slug": "dup"})
    assert r2.status_code == 409


def test_create_slug_conflict_on_tombstoned_slug_hits_db_constraint(mgmt_client):
    """The `get_by_slug` pre-check only matches `deleted_at IS NULL` rows, so
    a tombstoned (soft-deleted) slug reaches `repo.create()` and trips the
    unconditional UNIQUE(owner_user_id, slug) constraint at insert time —
    the exact path the narrowed `except (duckdb.ConstraintException,
    sa_exc.IntegrityError)` in `create_agent` must still remap to 409."""
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "tombstoned"}).json()
    d = mgmt_client.delete(f"/api/v1/agents/{created['id']}")
    assert d.status_code == 204

    r = mgmt_client.post("/api/v1/agents", json={"name": "B", "slug": "tombstoned"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slug_taken"


# ---------------------------------------------------------------------------
# List envelope
# ---------------------------------------------------------------------------


def test_list_envelope_shape(mgmt_client):
    mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "list-a"})
    r = mgmt_client.get("/api/v1/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert isinstance(body["data"], list)
    assert any(a["slug"] == "list-a" for a in body["data"])


# ---------------------------------------------------------------------------
# PUT — slug immutability + normal field updates
# ---------------------------------------------------------------------------


def test_put_slug_immutable(mgmt_client):
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "immutable-me"}).json()
    r = mgmt_client.put(f"/api/v1/agents/{created['id']}", json={"slug": "new-slug"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "slug_immutable"


def test_put_updates_whitelisted_fields(mgmt_client):
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "editable"}).json()
    r = mgmt_client.put(
        f"/api/v1/agents/{created['id']}",
        json={"name": "Renamed", "description": "new desc", "model": "claude-x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "new desc"
    assert body["model"] == "claude-x"


# ---------------------------------------------------------------------------
# PUT — mode-value validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["plugins_mode", "connections_mode", "tables_mode", "memory_mode"],
)
def test_put_rejects_invalid_scope_mode_value(mgmt_client, field):
    slug = f"bad-mode-{field}".replace("_", "-")
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": slug}).json()
    r = mgmt_client.put(f"/api/v1/agents/{created['id']}", json={field: "bogus"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_mode"


def test_put_rejects_invalid_memory_write_mode_value(mgmt_client):
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "bad-write-mode"}).json()
    r = mgmt_client.put(f"/api/v1/agents/{created['id']}", json={"memory_write_mode": "bogus"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_mode"


def test_put_accepts_valid_mode_values(mgmt_client, selected_agent_id):
    r = mgmt_client.put(
        f"/api/v1/agents/{selected_agent_id}",
        json={"plugins_mode": "all", "memory_write_mode": "auto"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plugins_mode"] == "all"
    assert body["memory_write_mode"] == "auto"


# ---------------------------------------------------------------------------
# PUT — widen-to-'all' guard against live agent PATs (spec §2)
# ---------------------------------------------------------------------------


def test_put_widen_to_all_rejected_with_live_token(mgmt_client, selected_agent_id):
    mint = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert mint.status_code == 200

    r = mgmt_client.put(f"/api/v1/agents/{selected_agent_id}", json={"plugins_mode": "all"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_has_live_tokens"


def test_put_widen_to_all_allowed_after_revoke(mgmt_client, selected_agent_id):
    from src.repositories import access_token_repo

    mint = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert mint.status_code == 200
    token_id = mint.json()["id"]
    access_token_repo().revoke(token_id)

    r = mgmt_client.put(f"/api/v1/agents/{selected_agent_id}", json={"plugins_mode": "all"})
    assert r.status_code == 200
    assert r.json()["plugins_mode"] == "all"


def test_put_narrowing_allowed_with_live_token(mgmt_client, selected_agent_id):
    mint = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert mint.status_code == 200

    r = mgmt_client.put(f"/api/v1/agents/{selected_agent_id}", json={"plugins_mode": "selected"})
    assert r.status_code == 200
    assert r.json()["plugins_mode"] == "selected"


# ---------------------------------------------------------------------------
# DELETE — default-agent guard + PAT revocation
# ---------------------------------------------------------------------------


def test_delete_default_agent_undeletable(mgmt_client, default_agent_id):
    r = mgmt_client.delete(f"/api/v1/agents/{default_agent_id}")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "default_agent_undeletable"


def test_delete_agent_revokes_its_pats(mgmt_client, mgmt_env, selected_agent_id):
    from app.auth.pat_resolver import resolve_token_to_user

    mint = mgmt_client.post(f"/api/v1/agents/{selected_agent_id}/tokens", json={"name": "t"})
    assert mint.status_code == 200
    token = mint.json()["token"]

    req = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/api/v1/agents/x/chat"),
        headers={},
        client=None,
        state=types.SimpleNamespace(),
    )
    user, reason = resolve_token_to_user(None, token, req)
    assert reason is None and user is not None  # valid before delete

    d = mgmt_client.delete(f"/api/v1/agents/{selected_agent_id}")
    assert d.status_code == 204

    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "pat_revoked"


def test_get_after_delete_is_404(mgmt_client):
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "to-delete"}).json()
    d = mgmt_client.delete(f"/api/v1/agents/{created['id']}")
    assert d.status_code == 204
    r = mgmt_client.get(f"/api/v1/agents/{created['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_scope_put_validates_item_type(mgmt_client):
    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "scoped"}).json()
    r = mgmt_client.put(
        f"/api/v1/agents/{created['id']}/scope",
        json={"items": [{"item_type": "bogus", "item_id": "x"}]},
    )
    assert r.status_code == 400


def test_scope_put_success(mgmt_client):
    from src.repositories import agents_repo

    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "scoped-2"}).json()
    r = mgmt_client.put(
        f"/api/v1/agents/{created['id']}/scope",
        json={"items": [{"item_type": "plugin", "item_id": "p1"}, {"item_type": "table", "item_id": "t1"}]},
    )
    assert r.status_code == 200
    stored = agents_repo().get_scope(created["id"])
    assert {"item_type": "plugin", "item_id": "p1"} in stored
    assert {"item_type": "table", "item_id": "t1"} in stored


def test_scope_put_dedupes_duplicate_items(mgmt_client):
    """A duplicated (item_type, item_id) pair in one request must not 500 on
    the composite PK — it collapses to a single row."""
    from src.repositories import agents_repo

    created = mgmt_client.post("/api/v1/agents", json={"name": "A", "slug": "scoped-dupe"}).json()
    r = mgmt_client.put(
        f"/api/v1/agents/{created['id']}/scope",
        json={
            "items": [
                {"item_type": "plugin", "item_id": "p1"},
                {"item_type": "plugin", "item_id": "p1"},
                {"item_type": "table", "item_id": "t1"},
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["items"] == [
        {"item_type": "plugin", "item_id": "p1"},
        {"item_type": "table", "item_id": "t1"},
    ]
    stored = agents_repo().get_scope(created["id"])
    assert sorted((s["item_type"], s["item_id"]) for s in stored) == [("plugin", "p1"), ("table", "t1")]


# ---------------------------------------------------------------------------
# Ownership / admin auth matrix
# ---------------------------------------------------------------------------


def test_cross_user_get_is_404(mgmt_env):
    client = mgmt_env["client"]
    created = client.post(
        "/api/v1/agents", json={"name": "Mine", "slug": "owner-only"}, headers=_auth(mgmt_env["owner"]["token"])
    ).json()
    r = client.get(f"/api/v1/agents/{created['id']}", headers=_auth(mgmt_env["other"]["token"]))
    assert r.status_code == 404


def test_cross_user_delete_is_404(mgmt_env):
    client = mgmt_env["client"]
    created = client.post(
        "/api/v1/agents", json={"name": "Mine", "slug": "owner-only-2"}, headers=_auth(mgmt_env["owner"]["token"])
    ).json()
    r = client.delete(f"/api/v1/agents/{created['id']}", headers=_auth(mgmt_env["other"]["token"]))
    assert r.status_code == 404


def test_admin_can_get_foreign_agent(mgmt_env):
    client = mgmt_env["client"]
    created = client.post(
        "/api/v1/agents", json={"name": "Mine", "slug": "admin-visible"}, headers=_auth(mgmt_env["owner"]["token"])
    ).json()
    r = client.get(f"/api/v1/agents/{created['id']}", headers=_auth(mgmt_env["admin"]["token"]))
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


def test_admin_cannot_post_tokens_for_foreign_agent(mgmt_env):
    from src.repositories import agents_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id=mgmt_env["owner"]["id"],
        name="Owner selected",
        slug="admin-blocked",
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    r = mgmt_env["client"].post(
        f"/api/v1/agents/{agent_id}/tokens",
        json={"name": "t"},
        headers=_auth(mgmt_env["admin"]["token"]),
    )
    assert r.status_code == 403


def test_admin_cannot_mutate_foreign_agent(mgmt_env):
    client = mgmt_env["client"]
    created = client.post(
        "/api/v1/agents", json={"name": "Mine", "slug": "admin-no-write"}, headers=_auth(mgmt_env["owner"]["token"])
    ).json()
    r = client.put(
        f"/api/v1/agents/{created['id']}",
        json={"name": "Hijacked"},
        headers=_auth(mgmt_env["admin"]["token"]),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Non-interactive credentials are rejected on every route
# ---------------------------------------------------------------------------


def test_plain_pat_rejected_on_management_api(mgmt_env):
    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=mgmt_env["owner"]["id"], email=mgmt_env["owner"]["email"], token_id=token_id, typ="pat"
    )
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id, user_id=mgmt_env["owner"]["id"], name="pat", token_hash=token_hash, prefix=token_id[:8]
    )
    r = mgmt_env["client"].post("/api/v1/agents", json={"name": "A", "slug": "pat-blocked"}, headers=_auth(jwt_token))
    assert r.status_code == 403
