"""Parity tests: data-package table attach + group deletion behave identically
on DuckDB and Postgres.

Both bugs were the backend-split class — a handler that touched app state through
the always-DuckDB ``Depends(_get_db)`` connection instead of the backend-aware
repo factory. On a Postgres deployment that connection is a vestigial/empty
DuckDB, so:

- ``POST /api/admin/data-packages/{id}/tables`` looked the table up via
  ``TableRegistryRepository(conn)`` (DuckDB) → never found a table that lives in
  PG → ``404 table_not_found`` even though the table is in ``/api/v2/catalog``.
- ``DELETE /api/admin/groups/{id}`` cascaded members + grants via
  ``conn.execute("DELETE FROM ...")`` (DuckDB, no-op on PG) before
  ``repo.delete(group_id)`` (PG) → the PG foreign key from
  ``resource_grants.group_id`` was still satisfied → FK violation → ``500``.

These reproduce on a live Postgres instance (observed on a PG-backed VM) and are
caught here by driving the real endpoints on both backends.
"""
from __future__ import annotations


def test_attach_table_to_package_parity(seeded_app_both):
    """A registered table can be attached to a data package on both backends."""
    client = seeded_app_both["client"]
    backend = seeded_app_both["backend"]
    auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}

    # Seed a table in the registry on the ACTIVE backend (factory-routed).
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id="parity_tbl", name="parity_tbl",
        source_type="keboola", query_mode="materialized",
    )

    r = client.post(
        "/api/admin/data-packages",
        json={"name": "Parity pkg", "slug": "parity-pkg"}, headers=auth,
    )
    assert r.status_code in (200, 201), f"[{backend}] pkg create: {r.status_code} {r.text}"
    pkg_id = r.json()["id"]

    r = client.post(
        f"/api/admin/data-packages/{pkg_id}/tables",
        json={"table_id": "parity_tbl"}, headers=auth,
    )
    assert r.status_code in (200, 201), (
        f"[{backend}] attach failed: {r.status_code} {r.text} — the handler likely "
        f"resolves the table off the raw DuckDB _get_db conn instead of the factory."
    )


def test_delete_group_with_grant_parity(seeded_app_both):
    """A group carrying a resource_grant deletes cleanly (cascade) on both backends."""
    client = seeded_app_both["client"]
    backend = seeded_app_both["backend"]
    auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}

    r = client.post("/api/admin/groups", json={"name": "parity-grp"}, headers=auth)
    assert r.status_code == 201, f"[{backend}] group create: {r.status_code} {r.text}"
    gid = r.json()["id"]

    # Give the group a grant so the delete must cascade resource_grants.
    r = client.post(
        "/api/admin/data-packages",
        json={"name": "Grp pkg", "slug": "grp-pkg"}, headers=auth,
    )
    assert r.status_code in (200, 201), f"[{backend}] pkg create: {r.status_code} {r.text}"
    pkg_id = r.json()["id"]
    r = client.post(
        "/api/admin/grants",
        json={
            "group_id": gid, "resource_type": "data_package",
            "resource_id": pkg_id, "requirement": "required",
        },
        headers=auth,
    )
    assert r.status_code in (201, 409), f"[{backend}] grant create: {r.status_code} {r.text}"

    r = client.delete(f"/api/admin/groups/{gid}", headers=auth)
    assert r.status_code == 204, (
        f"[{backend}] group delete: {r.status_code} {r.text} — the cascade likely "
        f"runs on the raw DuckDB _get_db conn while repo.delete hits PG, leaving the "
        f"PG foreign key violated."
    )
