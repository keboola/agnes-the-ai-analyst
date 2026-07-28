"""Backend-parity test for the /admin/access page payload (#518 regression).

``GET /api/admin/access-overview`` builds the RBAC page's resource tree. Its
``app/resource_types.py`` projections used to read a raw ``Depends(_get_db)``
DuckDB connection, so on a Postgres instance the page projected the frozen
DuckDB system file instead of live PG state — an admin-registered marketplace
showed on ``/admin/marketplaces`` (factory-routed) but was missing from
``/admin/access`` (DuckDB-routed). That was the janus-marketplace symptom.

This seeds a marketplace + plugin THROUGH THE FACTORY (so the row lands in
whichever backend is active), then asserts the marketplace_plugin block of the
access-overview payload contains it on BOTH backends.

Discriminator (pre-fix): duck PASS + pg FAIL => the backend-split bug.
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_marketplace_with_plugin(slug="janus-probe", plugin="probe-plugin"):
    """Register a marketplace + one plugin via the factory (active backend)."""
    from src.repositories import marketplace_plugins_repo, marketplace_registry_repo

    marketplace_registry_repo().register(
        id=slug,
        name=f"{slug} marketplace",
        url=f"https://example.com/{slug}.git",
    )
    marketplace_plugins_repo().replace_for_marketplace(
        slug,
        [{"name": plugin, "version": "1.0", "category": "data", "description": "probe"}],
    )
    return slug, plugin


def _marketplace_block(payload, marketplace_id):
    """Return the access-overview block for ``marketplace_id`` (or None)."""
    section = next(
        (r for r in payload["resources"] if r["type_key"] == "marketplace_plugin"),
        None,
    )
    assert section is not None, f"no marketplace_plugin section: {payload['resources']}"
    return next((b for b in section["blocks"] if b["id"] == marketplace_id), None)


def test_access_overview_lists_seeded_marketplace_on_both_backends(seeded_app_both):
    slug, plugin = _seed_marketplace_with_plugin()

    r = seeded_app_both["client"].get(
        "/api/admin/access-overview", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    block = _marketplace_block(r.json(), slug)
    assert block is not None, (
        f"[{seeded_app_both['backend']}] seeded marketplace {slug!r} missing from "
        f"/api/admin/access-overview — backend-split: the page read the wrong "
        f"backend (#518)."
    )
    resource_ids = {it["resource_id"] for it in block["items"]}
    assert f"{slug}/{plugin}" in resource_ids, (
        f"[{seeded_app_both['backend']}] plugin {plugin!r} missing from block: {block}"
    )
