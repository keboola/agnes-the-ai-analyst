"""Backend-parity tests for the data_packages cluster.

Endpoints under /api/admin/data-packages:
  - GET  ""           list
  - GET  "/{pkg_id}"  detail
  - POST ""           create

Each test seeds state through the backend-aware factory
(``data_packages_repo()``) so the row lands in whichever backend is active,
then exercises the HTTP endpoint via ``seeded_app_both`` — once on DuckDB,
once on real Postgres.

The list/get/create handlers fetch the package row through the factory, so
they are expected to pass on both backends. The ``badges`` projection
(``_badges_for`` in app/api/data_packages.py) is the interesting case: it
reads ``user_group_members``/``user_groups``/``users`` off the raw DuckDB
``conn`` (Depends(_get_db)) to decide the "curated" badge. On Postgres that
raw conn is stale/empty, so the badge silently disappears for a package whose
creator IS an admin — a backend-split divergence.
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_pkg(slug="parity-probe", name="Parity Probe", created_by="admin1", **kw):
    """Seed a data package through the factory and return its id."""
    from src.repositories import data_packages_repo
    return data_packages_repo().create(
        name=name,
        slug=slug,
        description=kw.get("description", "probe pkg"),
        icon=kw.get("icon"),
        color=kw.get("color"),
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# GET "" — list reflects the seeded package
# ---------------------------------------------------------------------------

def test_list_reflects_seeded_package(seeded_app_both):
    pkg_id = _seed_pkg()
    r = seeded_app_both["client"].get(
        "/api/admin/data-packages", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {row.get("id") for row in r.json()}
    slugs = {row.get("slug") for row in r.json()}
    assert pkg_id in ids or "parity-probe" in slugs, (
        f"[{seeded_app_both['backend']}] seeded package missing from list: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET "/{pkg_id}" — detail reflects the seeded package
# ---------------------------------------------------------------------------

def test_detail_reflects_seeded_package(seeded_app_both):
    pkg_id = _seed_pkg(slug="parity-detail", name="Parity Detail")
    r = seeded_app_both["client"].get(
        f"/api/admin/data-packages/{pkg_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] detail returned {r.status_code} "
        f"for a package seeded through the factory: {r.text}"
    )
    body = r.json()
    assert body.get("slug") == "parity-detail", body
    # the detail handler also embeds tables + related_tools projections
    assert "tables" in body and "related_tools" in body, body


# ---------------------------------------------------------------------------
# POST "" — create round-trips back through GET on the same backend
# ---------------------------------------------------------------------------

def test_create_then_get_roundtrips(seeded_app_both):
    r = seeded_app_both["client"].post(
        "/api/admin/data-packages",
        headers=_auth(seeded_app_both),
        json={"name": "Created Via API", "slug": "created-via-api"},
    )
    assert r.status_code == 201, (
        f"[{seeded_app_both['backend']}] create returned {r.status_code}: {r.text}"
    )
    new_id = r.json()["id"]
    g = seeded_app_both["client"].get(
        f"/api/admin/data-packages/{new_id}", headers=_auth(seeded_app_both)
    )
    assert g.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET after create returned {g.status_code}: {g.text}"
    )
    assert g.json().get("slug") == "created-via-api", g.json()


# ---------------------------------------------------------------------------
# DISCRIMINATOR — the "curated" badge is derived from user_group_members read
# off the raw DuckDB conn. admin1 is an Admin-group member (seeded by the
# fixture), and the package's created_by is admin1's email, so the badge MUST
# appear on both backends. If it's missing on PG, the badge lookup is reading
# stale/empty DuckDB instead of the active backend.
# ---------------------------------------------------------------------------

def test_curated_badge_present_for_admin_authored_package(seeded_app_both):
    # created_by must match what the badge query joins on: u.email OR u.id.
    # The fixture seeds admin1 with email admin@test.com in the Admin group.
    pkg_id = _seed_pkg(
        slug="curated-probe", name="Curated Probe", created_by="admin@test.com"
    )
    r = seeded_app_both["client"].get(
        f"/api/admin/data-packages/{pkg_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    badges = r.json().get("badges")
    assert badges is not None, f"[{seeded_app_both['backend']}] no badges field: {r.json()}"
    assert "curated" in badges, (
        f"[{seeded_app_both['backend']}] 'curated' badge missing for an "
        f"admin-authored package — badge derivation reads user_group_members "
        f"off a raw DuckDB conn instead of the factory. badges={badges}"
    )
