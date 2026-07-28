"""Backend-parity tests for the memory_domains cluster.

Endpoints under test (app/api/memory_domains.py +
app/api/memory_domain_suggestions.py + app/api/stack_views.py):

  - GET  /api/admin/memory-domains                 — list
  - GET  /api/admin/memory-domains/{id}            — detail
  - POST /api/admin/memory-domains  → GET readback — create round-trip
  - GET  /api/admin/memory-domain-suggestions      — admin moderation queue
  - GET  /api/memory/domains/{slug}                — user-facing drill-down

Each test seeds through the backend-aware factory (memory_domains_repo() /
memory_domain_suggestions_repo()) so the row lands in whichever backend is
active, then hits the HTTP route via ``seeded_app_both`` — once on DuckDB,
once on real Postgres. duck-pass + pg-fail == backend-split bug at that
endpoint; duck-fail == the test itself is wrong.
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _as_items(payload):
    if isinstance(payload, list):
        return payload
    for k in ("items", "domains", "results"):
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


# ---------------------------------------------------------------------------
# GET /api/admin/memory-domains — seeded via memory_domains_repo().create
# ---------------------------------------------------------------------------

def test_admin_list_reflects_seeded_domain(seeded_app_both):
    from src.repositories import memory_domains_repo
    memory_domains_repo().create(
        name="Parity Probe",
        slug="parity-probe",
        description="probe",
        icon=None,
        color=None,
        created_by="admin1",
    )
    r = seeded_app_both["client"].get(
        "/api/admin/memory-domains", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    slugs = {d.get("slug") for d in _as_items(r.json())}
    assert "parity-probe" in slugs, (
        f"[{seeded_app_both['backend']}] seeded domain missing from "
        f"/api/admin/memory-domains: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/memory-domains/{id} — detail view
# ---------------------------------------------------------------------------

def test_admin_detail_reflects_seeded_domain(seeded_app_both):
    from src.repositories import memory_domains_repo
    domain_id = memory_domains_repo().create(
        name="Detail Probe",
        slug="detail-probe",
        description="detail",
        icon=None,
        color=None,
        created_by="admin1",
    )
    r = seeded_app_both["client"].get(
        f"/api/admin/memory-domains/{domain_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/admin/memory-domains/{{id}} "
        f"returned {r.status_code} for a domain seeded through the factory: {r.text}"
    )
    body = r.json()
    assert body.get("slug") == "detail-probe", body
    assert "items" in body, body


# ---------------------------------------------------------------------------
# POST /api/admin/memory-domains → GET readback (mutation round-trip)
# ---------------------------------------------------------------------------

def test_admin_create_then_list_roundtrip(seeded_app_both):
    client = seeded_app_both["client"]
    r = client.post(
        "/api/admin/memory-domains",
        headers=_auth(seeded_app_both),
        json={"name": "Created Probe", "slug": "created-probe"},
    )
    assert r.status_code == 201, (
        f"[{seeded_app_both['backend']}] create returned {r.status_code}: {r.text}"
    )
    r2 = client.get("/api/admin/memory-domains", headers=_auth(seeded_app_both))
    assert r2.status_code == 200, r2.text
    slugs = {d.get("slug") for d in _as_items(r2.json())}
    assert "created-probe" in slugs, (
        f"[{seeded_app_both['backend']}] created domain not visible on readback: {r2.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/memory-domain-suggestions — seeded via the suggestions repo
# ---------------------------------------------------------------------------

def test_admin_suggestions_queue_reflects_seeded_suggestion(seeded_app_both):
    from src.repositories import memory_domain_suggestions_repo
    sid = memory_domain_suggestions_repo().create(
        name="Suggested Domain",
        description="please add",
        rationale="useful",
        created_by="analyst1",
    )
    r = seeded_app_both["client"].get(
        "/api/admin/memory-domain-suggestions", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {s.get("id") for s in _as_items(r.json())}
    assert sid in ids, (
        f"[{seeded_app_both['backend']}] seeded suggestion missing from admin "
        f"queue: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/memory/domains/{slug} — user-facing drill-down (stack_views.py)
# ---------------------------------------------------------------------------

def test_user_facing_domain_drilldown_reflects_seeded_domain(seeded_app_both):
    from src.repositories import memory_domains_repo
    memory_domains_repo().create(
        name="Drilldown Probe",
        slug="drilldown-probe",
        description="drill",
        icon=None,
        color=None,
        created_by="admin1",
    )
    r = seeded_app_both["client"].get(
        "/api/memory/domains/drilldown-probe", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/memory/domains/{{slug}} returned "
        f"{r.status_code} for a domain seeded through the factory — route reads "
        f"memory_domains off a raw DuckDB conn instead of the factory: {r.text}"
    )
    body = r.json()
    assert body.get("slug") == "drilldown-probe", body
