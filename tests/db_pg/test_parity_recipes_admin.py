"""Backend-parity tests for the recipes_admin cluster.

Endpoints under test (app/api/recipes.py):
  - GET    /api/admin/recipes          (admin_list_recipes)
  - GET    /api/admin/recipes/{id}     (admin_get_recipe)
  - PUT    /api/admin/recipes/{id}     (update_recipe)  — mutation roundtrip
  - DELETE /api/admin/recipes/{id}     (delete_recipe)  — soft delete roundtrip

Each test seeds a recipe THROUGH THE FACTORY (recipes_repo().create(...)) so the
row lands in whichever backend is active, then drives the endpoint via
``seeded_app_both`` once on DuckDB and once on Postgres.

Discriminator: duck PASS + pg FAIL => backend-split bug at that endpoint.
duck FAIL => the test itself is wrong (fix before trusting pg).
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_recipe(slug="admin-probe", title="Admin Probe", status="prod"):
    from src.repositories import recipes_repo
    return recipes_repo().create(
        slug=slug,
        title=title,
        description="probe",
        icon=None,
        color=None,
        sql_template="SELECT 1",
        related_table_ids=None,
        status=status,
        created_by="admin1",
    )


# ---------------------------------------------------------------------------
# GET /api/admin/recipes — admin list (returns a bare list)
# ---------------------------------------------------------------------------

def test_admin_list_reflects_seeded_recipe(seeded_app_both):
    rid = _seed_recipe(slug="admin-list-probe", title="Admin List Probe")
    r = seeded_app_both["client"].get(
        "/api/admin/recipes", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list), r.json()
    ids = {x.get("id") for x in rows}
    assert rid in ids, (
        f"[{seeded_app_both['backend']}] seeded recipe {rid} missing from "
        f"GET /api/admin/recipes: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/recipes/{id} — admin detail by id (reads repo.get(id))
# Admin detail returns drafts too (status='draft'), unlike the public route.
# ---------------------------------------------------------------------------

def test_admin_get_by_id_reflects_seeded_recipe(seeded_app_both):
    rid = _seed_recipe(slug="admin-detail-probe", title="Admin Detail Probe",
                       status="draft")
    r = seeded_app_both["client"].get(
        f"/api/admin/recipes/{rid}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/admin/recipes/{rid} returned "
        f"{r.status_code} for a recipe seeded through the factory: {r.text}"
    )
    body = r.json()
    assert body.get("id") == rid, body
    assert body.get("slug") == "admin-detail-probe", body


# ---------------------------------------------------------------------------
# PUT /api/admin/recipes/{id} — mutation roundtrip. The handler reads
# repo.get(id) (existing), then repo.update(id, ...), then repo.get(id)
# (fresh) for the response. All three go through the factory; the audit
# write uses the raw conn but is best-effort (won't fail the request).
# ---------------------------------------------------------------------------

def test_admin_update_roundtrip(seeded_app_both):
    rid = _seed_recipe(slug="admin-update-probe", title="Before Title")
    r = seeded_app_both["client"].put(
        f"/api/admin/recipes/{rid}",
        headers=_auth(seeded_app_both),
        json={"title": "After Title"},
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] PUT /api/admin/recipes/{rid} returned "
        f"{r.status_code}: {r.text}"
    )
    assert r.json().get("title") == "After Title", r.json()

    # Re-read via the admin detail endpoint to confirm persistence on the
    # active backend.
    r2 = seeded_app_both["client"].get(
        f"/api/admin/recipes/{rid}", headers=_auth(seeded_app_both)
    )
    assert r2.status_code == 200, r2.text
    assert r2.json().get("title") == "After Title", r2.json()


# ---------------------------------------------------------------------------
# DELETE /api/admin/recipes/{id} — soft delete roundtrip. Reads
# repo.get(id), then repo.delete(id). After delete the row is hidden from
# the admin list (list() filters deleted_at IS NULL).
# ---------------------------------------------------------------------------

def test_admin_delete_roundtrip(seeded_app_both):
    rid = _seed_recipe(slug="admin-delete-probe", title="Delete Probe")
    r = seeded_app_both["client"].delete(
        f"/api/admin/recipes/{rid}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 204, (
        f"[{seeded_app_both['backend']}] DELETE /api/admin/recipes/{rid} returned "
        f"{r.status_code}: {r.text}"
    )
    # Soft-deleted: gone from admin list.
    r2 = seeded_app_both["client"].get(
        "/api/admin/recipes", headers=_auth(seeded_app_both)
    )
    assert r2.status_code == 200, r2.text
    ids = {x.get("id") for x in r2.json()}
    assert rid not in ids, (
        f"[{seeded_app_both['backend']}] recipe {rid} still in admin list "
        f"after soft delete: {r2.json()}"
    )
