"""GET /api/knowledge/search — dual-backend RBAC behaviour (K2, #797).

Runs the same assertions against DuckDB and Postgres via ``seeded_app_both``.
Guards the backend-split bug class: grant resolution must go through the
repository factories, so an analyst's knowledge-domain grants filter
identically on both state backends (raw ``_get_db`` reads would see empty
tables on PG and silently fail closed for everyone — or open for no one).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_search_shape_and_auth_both_backends(seeded_app_both):
    c = seeded_app_both["client"]

    # Unauthenticated → 401 on either backend.
    assert c.get("/api/knowledge/search", params={"q": "x"}).status_code == 401

    # Admin (privileged viewer, None/None grants) → 200 with typed list.
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "anything"},
        headers=_auth(seeded_app_both["admin_token"]),
    )
    assert resp.status_code == 200, f"[{seeded_app_both['backend']}] {resp.text}"
    body = resp.json()
    assert body["query"] == "anything"
    assert isinstance(body["results"], list)


def test_analyst_grant_resolution_is_factory_routed(seeded_app_both):
    """Analyst grants resolve through the factories on both backends.

    Grant a group membership + a memory_domain grant via the repo factories
    (the same write path the admin UI uses), then assert the endpoint
    resolves them without touching the raw DuckDB conn: on the PG backend a
    raw-conn read would see no membership and no grants.
    """
    from src.repositories import (
        memory_domains_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )

    c = seeded_app_both["client"]
    backend = seeded_app_both["backend"]

    domain_id = memory_domains_repo().create(
        name="KS Test",
        slug=f"ks-test-{backend}",
        description=None,
        icon=None,
        color=None,
        created_by="admin1",
    )
    gid = user_groups_repo().create(name=f"ks-searchers-{backend}", created_by="admin1")["id"]
    user_group_members_repo().add_member("analyst1", gid, source="admin")
    resource_grants_repo().create(
        group_id=gid, resource_type="memory_domain", resource_id=domain_id, assigned_by="admin1"
    )

    from app.api.knowledge_search import _resolve_knowledge_grants

    groups, domains = _resolve_knowledge_grants({"id": "analyst1", "email": "analyst@test.com"})
    assert groups == [f"group:ks-searchers-{backend}"], f"[{backend}] groups={groups}"
    assert domains == [domain_id], f"[{backend}] domains={domains}"

    # And end-to-end: the endpoint answers 200 for the analyst with those grants.
    resp = c.get(
        "/api/knowledge/search",
        params={"q": "anything"},
        headers=_auth(seeded_app_both["analyst_token"]),
    )
    assert resp.status_code == 200, f"[{backend}] {resp.text}"


def test_admin_resolves_to_unfiltered(seeded_app_both):
    from app.api.knowledge_search import _resolve_knowledge_grants

    groups, domains = _resolve_knowledge_grants({"id": "admin1", "email": "admin@test.com"})
    assert groups is None and domains is None, f"[{seeded_app_both['backend']}]"
