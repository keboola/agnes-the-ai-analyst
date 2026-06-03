"""Backend-parity tests for the Store (flea-market) endpoints.

Cluster: store. Each test seeds state THROUGH THE FACTORY
(``store_entities_repo()`` / ``users_repo()``) so the row lands in
whichever backend is active, then exercises the HTTP endpoint via
``seeded_app_both`` — once on DuckDB, once on real Postgres.

Discriminator: a route that reads system state through the factory
returns the seeded row on BOTH backends; a route that reads through a
raw DuckDB connection (``Depends(_get_db)``) returns it on DuckDB but a
stale/empty result on Postgres, so the ``[pg]`` parametrization fails —
pinpointing the backend-split bug at the offending endpoint.

``tests/conftest.py`` autouse fixture ``_flea_guardrails_disabled_by_default``
defaults the upload pipeline OFF, but these tests never upload — they
seed the registry row directly through the repo and hit read endpoints,
so guardrail state is irrelevant here.
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_entity(seeded_app_both, *, id, owner_user_id, owner_username,
                 name, type="skill", category="Other",
                 visibility_status="approved"):
    """Seed one store_entities row through the backend-aware factory."""
    from src.repositories import store_entities_repo
    return store_entities_repo().create(
        id=id,
        owner_user_id=owner_user_id,
        owner_username=owner_username,
        type=type,
        name=name,
        description="probe entity",
        category=category,
        version="v1",
        visibility_status=visibility_status,
    )


# ---------------------------------------------------------------------------
# GET /api/store/entities — list. The entity itself is read via
# ``store_entities_repo().list()`` (factory → backend-aware), so the row
# should appear on BOTH backends. This is the clean baseline discriminator
# for the listing read.
# ---------------------------------------------------------------------------

def test_store_list_reflects_seeded_entity(seeded_app_both):
    _seed_entity(
        seeded_app_both,
        id="ent_list1",
        owner_user_id="admin1",
        owner_username="admin",
        name="parity-probe-list",
        visibility_status="approved",
    )
    r = seeded_app_both["client"].get(
        "/api/store/entities", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {e["id"] for e in r.json()["items"]}
    assert "ent_list1" in ids, (
        f"[{seeded_app_both['backend']}] seeded store entity missing from "
        f"/api/store/entities: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/store/entities/{id} — detail. Read via
# ``store_entities_repo().get()`` (factory). The entity body should appear
# on BOTH backends. ``owner_display_name`` is computed via the raw DuckDB
# ``conn`` (``_resolve_owner_display``) — asserted separately below.
# ---------------------------------------------------------------------------

def test_store_detail_reflects_seeded_entity(seeded_app_both):
    _seed_entity(
        seeded_app_both,
        id="ent_detail1",
        owner_user_id="admin1",
        owner_username="admin",
        name="parity-probe-detail",
        visibility_status="approved",
    )
    r = seeded_app_both["client"].get(
        "/api/store/entities/ent_detail1", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] GET /api/store/entities/ent_detail1 "
        f"returned {r.status_code} for an entity seeded through the factory: "
        f"{r.text}"
    )
    body = r.json()
    assert body["id"] == "ent_detail1"
    assert body["name"] == "parity-probe-detail"


# ---------------------------------------------------------------------------
# owner_display_name on the detail response — computed by
# _resolve_owner_display(conn, owner_user_id), which runs
# `SELECT name, email FROM users` on the RAW DuckDB conn. The owner row is
# seeded into the active backend via users_repo() (the seeded_app_both
# fixture creates admin1 there). On Postgres the users table the raw conn
# sees is empty → owner_display_name comes back None even though the user
# exists in the active backend. Clean duck-pass / pg-fail discriminator.
# ---------------------------------------------------------------------------

def test_store_detail_owner_display_name_resolves(seeded_app_both):
    # admin1 (email admin@test.com, name "Admin") is seeded via users_repo()
    # by the seeded_app_both fixture, into whichever backend is active.
    _seed_entity(
        seeded_app_both,
        id="ent_owner1",
        owner_user_id="admin1",
        owner_username="admin",
        name="parity-probe-owner",
        visibility_status="approved",
    )
    r = seeded_app_both["client"].get(
        "/api/store/entities/ent_owner1", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_display_name"] == "Admin", (
        f"[{seeded_app_both['backend']}] owner_display_name resolved to "
        f"{body.get('owner_display_name')!r} for owner admin1 (name='Admin') "
        f"seeded through users_repo() — _resolve_owner_display reads `users` "
        f"off the raw DuckDB conn instead of users_repo()."
    )


# ---------------------------------------------------------------------------
# GET /api/store/owners — owner filter dropdown. The handler runs raw SQL
# (`SELECT se.owner_user_id ... FROM store_entities se LEFT JOIN users u ...`)
# directly on the DuckDB `conn` (Depends(_get_db)). On Postgres the entity
# rows live in PG, so this DuckDB-side query sees an empty store_entities
# table → the owner of a seeded+approved entity is missing from the list.
# Clean duck-pass / pg-fail discriminator.
# ---------------------------------------------------------------------------

def test_store_owners_reflects_seeded_owner(seeded_app_both):
    _seed_entity(
        seeded_app_both,
        id="ent_owners1",
        owner_user_id="admin1",
        owner_username="admin",
        name="parity-probe-owners",
        visibility_status="approved",
    )
    r = seeded_app_both["client"].get(
        "/api/store/owners", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    owner_ids = {o["user_id"] for o in r.json()}
    assert "admin1" in owner_ids, (
        f"[{seeded_app_both['backend']}] owner of a seeded+approved entity "
        f"missing from /api/store/owners: {r.json()} — the handler runs raw "
        f"SQL against store_entities/users on the DuckDB conn instead of "
        f"store_entities_repo()."
    )
