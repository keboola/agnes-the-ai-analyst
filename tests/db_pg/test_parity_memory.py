"""Backend-parity endpoint tests for the corporate-memory cluster.

Each test seeds state through the backend-aware factory (knowledge_repo() /
memory_domains_repo()) so the row lands in whichever backend is active, then
exercises the HTTP endpoint via ``seeded_app_both`` — once on DuckDB, once on
real Postgres.

Discriminator: an endpoint that reads system state through the factory returns
the seeded row on BOTH backends. An endpoint that reads through a raw DuckDB
``conn`` (Depends(_get_db) — always get_system_db()) or a direct
``KnowledgeRepository(conn)`` instantiation returns it on DuckDB but an
empty/stale result on Postgres, so the ``[pg]`` parametrization fails.
"""
from __future__ import annotations

import uuid


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_domain(slug: str, name: str) -> str:
    from src.repositories import memory_domains_repo
    return memory_domains_repo().create(
        name=name,
        slug=slug,
        description="probe domain",
        icon=None,
        color=None,
        created_by="admin@test.com",
    )


def _seed_item(*, title: str, status: str = "pending", category: str = "fact",
               source_user: str = "admin@test.com", domain: str | None = None) -> str:
    from src.repositories import knowledge_repo
    item_id = str(uuid.uuid4())
    knowledge_repo().create(
        id=item_id,
        title=title,
        content="probe content for " + title,
        category=category,
        source_user=source_user,
        status=status,
        domain=domain,
    )
    return item_id


# ---------------------------------------------------------------------------
# GET /api/memory/domains — pure memory_domains_repo().list() (factory). Clean.
# ---------------------------------------------------------------------------

def test_domains_reflects_seeded_domain(seeded_app_both):
    _seed_domain("md_probe", "Probe Domain")
    r = seeded_app_both["client"].get(
        "/api/memory/domains", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    slugs = {d["slug"] for d in r.json()["domains"]}
    assert "md_probe" in slugs, (
        f"[{seeded_app_both['backend']}] seeded domain missing from /domains: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/memory/admin/pending — knowledge_repo().list_items() (factory). Clean.
# ---------------------------------------------------------------------------

def test_admin_pending_reflects_seeded_pending_item(seeded_app_both):
    _seed_item(title="Pending Probe", status="pending")
    r = seeded_app_both["client"].get(
        "/api/memory/admin/pending", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    titles = {it["title"] for it in r.json()["items"]}
    assert "Pending Probe" in titles, (
        f"[{seeded_app_both['backend']}] seeded pending item missing from "
        f"/admin/pending: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/memory/my-contributions — knowledge_repo().get_user_contributions()
# (factory). Admin's own contributions, keyed by email. Clean.
# ---------------------------------------------------------------------------

def test_my_contributions_reflects_seeded_item(seeded_app_both):
    _seed_item(title="My Contribution Probe", source_user="admin@test.com")
    r = seeded_app_both["client"].get(
        "/api/memory/my-contributions", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    titles = {it["title"] for it in r.json()["items"]}
    assert "My Contribution Probe" in titles, (
        f"[{seeded_app_both['backend']}] seeded contribution missing from "
        f"/my-contributions: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/memory/stats — total/by_status/categories/by_domain/by_source_type
# are computed via RAW conn.execute(...) (Depends(_get_db) → get_system_db(),
# always DuckDB). On Postgres the seeded row lives in PG but the raw conn reads
# the empty DuckDB → total stays 0. SUSPECTED backend-split bug.
# ---------------------------------------------------------------------------

def test_stats_total_reflects_seeded_item(seeded_app_both):
    _seed_item(title="Stats Probe", status="pending", category="probecat")
    r = seeded_app_both["client"].get(
        "/api/memory/stats", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1, (
        f"[{seeded_app_both['backend']}] /stats total={body['total']} but an item "
        f"was seeded through the factory — stats reads counts off a raw DuckDB "
        f"conn, so PG-backed state is invisible. body={body}"
    )
    assert "probecat" in body["categories"], (
        f"[{seeded_app_both['backend']}] seeded category missing from /stats "
        f"categories: {body['categories']}"
    )


# ---------------------------------------------------------------------------
# GET /api/memory/tree — list_items() via factory; counts should be backend
# correct. Clean (the raw-conn calls in the handler are only RBAC group lookups
# for the admin caller, who short-circuits to None → no group SQL hit).
# ---------------------------------------------------------------------------

def test_tree_reflects_seeded_item(seeded_app_both):
    _seed_item(title="Tree Probe", status="pending", category="treecat")
    r = seeded_app_both["client"].get(
        "/api/memory/tree?axis=category", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_items"] >= 1, (
        f"[{seeded_app_both['backend']}] /tree total_items={body['total_items']} "
        f"but an item was seeded through the factory. body={body}"
    )
    all_titles = {
        it["title"] for g in body["groups"] for it in g["items"]
    }
    assert "Tree Probe" in all_titles, (
        f"[{seeded_app_both['backend']}] seeded item missing from /tree groups: {body}"
    )
