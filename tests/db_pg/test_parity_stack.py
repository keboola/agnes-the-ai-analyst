"""Backend-parity tests for the user-stack API (``app/api/stack.py``).

The ``StackResolver`` previously read ``user_group_members`` /
``resource_grants`` / ``data_packages`` / ``memory_domains`` /
``user_stack_subscriptions`` off a raw DuckDB connection it was
constructed with, so on a Postgres instance every ``/api/stack`` read
hit the wrong (empty) DuckDB backend. The fix routes all of its
reads/writes through the repository factory.

These tests seed state THROUGH THE FACTORY (so each run lands in
whichever backend ``seeded_app_both`` configured) and then exercise the
three user-facing endpoints:

  * GET    /api/stack?type=data_package|memory_domain
  * POST   /api/stack/subscribe
  * DELETE /api/stack/subscription/{type}/{id}

Each test runs twice — once on DuckDB, once on real Postgres.
"""
from __future__ import annotations


def _seed_group_with_analyst(group_name: str = "data-team") -> str:
    """Create a group, add ``analyst1`` to it, return the group id."""
    from src.repositories import user_groups_repo, user_group_members_repo

    grp = user_groups_repo().create(name=group_name, description="x")
    gid = grp["id"]
    user_group_members_repo().add_member("analyst1", gid, source="admin")
    return gid


def _seed_data_package(slug: str, name: str) -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        name=name,
        slug=slug,
        description="desc",
        icon="📦",
        color="#abc",
        created_by="admin1",
    )


def _seed_memory_domain(slug: str, name: str) -> str:
    from src.repositories import memory_domains_repo

    return memory_domains_repo().create(
        name=name,
        slug=slug,
        description="desc",
        icon="🧠",
        color="#cde",
        created_by="admin1",
    )


def _grant(gid: str, resource_type: str, resource_id: str, requirement: str) -> None:
    from src.repositories import resource_grants_repo

    resource_grants_repo().create(
        group_id=gid,
        resource_type=resource_type,
        resource_id=resource_id,
        assigned_by="admin1",
        requirement=requirement,
    )


# ---------------------------------------------------------------------------
# GET /api/stack — effective-stack resolution
# ---------------------------------------------------------------------------

def test_required_grant_is_in_stack_without_subscription(seeded_app_both):
    """A ``required`` data-package grant shows up in the analyst's stack
    with ``in_stack=True`` even without an explicit subscription."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]

    gid = _seed_group_with_analyst()
    pkg_id = _seed_data_package("sales", "Sales bundle")
    _grant(gid, "data_package", pkg_id, "required")

    r = client.get(
        "/api/stack?type=data_package",
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert pkg_id in items, items
    assert items[pkg_id]["requirement"] == "required"
    assert items[pkg_id]["in_stack"] is True


def test_available_grant_absent_from_stack_until_subscribed(seeded_app_both):
    """An ``available`` grant is NOT in the stack until the analyst
    subscribes; after a POST /subscribe it appears."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    headers = {"Authorization": f"Bearer {analyst_token}"}

    gid = _seed_group_with_analyst()
    pkg_id = _seed_data_package("optional", "Optional bundle")
    _grant(gid, "data_package", pkg_id, "available")

    # Not subscribed yet → not in the effective stack.
    r = client.get("/api/stack?type=data_package", headers=headers)
    assert r.status_code == 200, r.text
    assert pkg_id not in {i["id"] for i in r.json()["items"]}

    # Subscribe.
    r = client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["subscribed"] is True

    # Now it's in the stack.
    r = client.get("/api/stack?type=data_package", headers=headers)
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert pkg_id in items, items
    assert items[pkg_id]["in_stack"] is True


def test_subscribe_then_unsubscribe_round_trip(seeded_app_both):
    """POST /subscribe then DELETE /subscription removes the package from
    the effective stack (idempotent 204 on delete)."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    headers = {"Authorization": f"Bearer {analyst_token}"}

    gid = _seed_group_with_analyst()
    pkg_id = _seed_data_package("toggle", "Toggle bundle")
    _grant(gid, "data_package", pkg_id, "available")

    client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=headers,
    )

    r = client.delete(
        f"/api/stack/subscription/data_package/{pkg_id}",
        headers=headers,
    )
    assert r.status_code == 204, r.text

    r = client.get("/api/stack?type=data_package", headers=headers)
    assert r.status_code == 200, r.text
    assert pkg_id not in {i["id"] for i in r.json()["items"]}


def test_unsubscribe_required_is_rejected(seeded_app_both):
    """DELETE on a ``required`` grant returns 400 cannot_remove_required —
    the resolver's requirement split must read from the right backend."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    headers = {"Authorization": f"Bearer {analyst_token}"}

    gid = _seed_group_with_analyst()
    pkg_id = _seed_data_package("mandatory", "Mandatory bundle")
    _grant(gid, "data_package", pkg_id, "required")

    r = client.delete(
        f"/api/stack/subscription/data_package/{pkg_id}",
        headers=headers,
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "cannot_remove_required"


def test_memory_domain_stack_resolution(seeded_app_both):
    """The memory_domain resource type resolves through the same factory
    path — a required domain grant lands in the analyst's stack."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    headers = {"Authorization": f"Bearer {analyst_token}"}

    gid = _seed_group_with_analyst()
    dom_id = _seed_memory_domain("ops", "Operations")
    _grant(gid, "memory_domain", dom_id, "required")

    r = client.get("/api/stack?type=memory_domain", headers=headers)
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert dom_id in items, items
    assert items[dom_id]["in_stack"] is True


def test_subscribe_without_grant_is_forbidden(seeded_app_both):
    """POST /subscribe 403s when the analyst has no grant on the package
    (can_access gate routes through the factory)."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]

    # Group membership but NO grant for this package.
    _seed_group_with_analyst()
    pkg_id = _seed_data_package("ungranted", "Ungranted bundle")

    r = client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "no_grant"
