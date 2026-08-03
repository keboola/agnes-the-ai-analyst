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
they are expected to pass on both backends.

The interesting case USED to be the ``badges`` projection: ``_badges_for``
derived a "curated" badge by reading ``user_group_members`` / ``user_groups`` /
``users``, and an earlier version read them off the raw DuckDB ``conn``
(Depends(_get_db)) — stale/empty on Postgres, so the badge silently disappeared
for a package whose creator IS an admin. v113 removed the derivation entirely in
favour of the stored ``publisher_kind`` column, so the discriminator below now
proves that column survives the round trip through each backend's repo instead.
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
        # v113: forwarded explicitly. `**kw` was collected but never passed on,
        # so a test asking for a publisher_kind silently got the default.
        publisher_kind=kw.get("publisher_kind", "user"),
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
# DISCRIMINATOR — v113. This used to prove the DERIVED `curated` badge resolved
# Admin membership through the repository factory rather than a raw DuckDB conn
# (on PG the hand-written JOIN came back empty and the badge silently vanished).
# The derivation is gone: the claim is now the STORED publisher_kind, so what
# needs proving is that the column round-trips through the API on BOTH backends
# — a PG repo that dropped the column from its INSERT or its projection would
# reintroduce the same class of silent disagreement.
# ---------------------------------------------------------------------------


def test_publisher_kind_surfaces_through_the_api_on_both_backends(seeded_app_both):
    pkg_id = _seed_pkg(
        slug="curated-probe",
        name="Curated Probe",
        created_by="admin@test.com",
        publisher_kind="organization",
    )
    r = seeded_app_both["client"].get(
        f"/api/admin/data-packages/{pkg_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("publisher_kind") == "organization", (
        f"[{seeded_app_both['backend']}] publisher_kind did not round-trip — the "
        f"backend's repo is dropping it from its INSERT or its projection. "
        f"got={body.get('publisher_kind')!r}"
    )
    # The derived badge must be gone on both engines, not just on DuckDB.
    assert "curated" not in (body.get("badges") or []), (
        f"[{seeded_app_both['backend']}] the derived 'curated' badge is back"
    )


def test_new_badge_still_derived_on_both_backends(seeded_app_both):
    """`new` is a function of the clock, so it stays derived — the v113 change
    removed one badge, not the mechanism."""
    pkg_id = _seed_pkg(slug="new-probe", name="New Probe", created_by="admin@test.com")
    r = seeded_app_both["client"].get(
        f"/api/admin/data-packages/{pkg_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    assert "new" in (r.json().get("badges") or []), f"[{seeded_app_both['backend']}] lost the 'new' badge"
