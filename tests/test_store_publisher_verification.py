"""Publisher + verification — the card's trust line (v104).

Covers the three axes this feature keeps deliberately separate:

* **Publisher** — who stands behind an item. Stored, admin-set, and the basis
  for the unified Browse shelf that replaced the Curated / Flea tabs.
* **Verification** — the org's *advisory* verdict on a user-published item.
  Must never gate a read, is per-instance (off by default — an opt-in enabled
  together with `library.show_unverified_trust`), and never renders a
  negative label.
* **Required** — "In stack, locked", admissible only on organization-published
  items.

The regressions worth naming: verification leaking into a read path (that is
the old approval queue wearing a new name), and "required" being writable
against a colleague's personal upload.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _make_user(conn, email, *, admin=False):
    from app.auth.jwt import create_access_token
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0].title())
    if admin:
        admin_group = next((g for g in UserGroupsRepository(conn).list_all() if g["name"] == "Admin"), None)
        assert admin_group is not None, "seeded Admin group missing"
        UserGroupMembersRepository(conn).add_member(uid, admin_group["id"], source="test")
    return uid, create_access_token(user_id=uid, email=email)


def _make_group(conn, name, *, members=()):
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    group = UserGroupsRepository(conn).create(name=name, created_by="test")
    for uid in members:
        UserGroupMembersRepository(conn).add_member(uid, group["id"], source="test")
    return group["id"]


def _make_entity(conn, *, entity_id, owner_id, owner_username, name, status="approved"):
    from src.repositories.store_entities import StoreEntitiesRepository

    return StoreEntitiesRepository(conn).create(
        id=entity_id,
        owner_user_id=owner_id,
        owner_username=owner_username,
        type="skill",
        name=name,
        description="A skill used by the publisher/verification tests.",
        category="Productivity",
        version="v1",
        visibility_status=status,
    )


# ---------------------------------------------------------------------------
# Publisher projection
# ---------------------------------------------------------------------------


def test_entity_response_carries_publisher_byline(fresh_db):
    """A user-published item's byline is the author's display name — not the
    kebab-case username, and not their email when a name exists."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user(conn, "anna@example.com")
        _make_entity(conn, entity_id="e1", owner_id=uid, owner_username="anna", name="customer-research")
    finally:
        conn.close()
        close_system_db()

    r = _client().get("/api/store/entities/e1", cookies={"access_token": sess})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["publisher_kind"] == "user"
    assert body["publisher_name"] == "Anna"
    assert body["verification_state"] == "none"


def test_publish_as_organization_swaps_the_publisher_label(fresh_db):
    """Organization-published items speak for the instance, so the byline
    becomes the institutional label rather than the uploading admin's name."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=uid, owner_username="anna", name="revenue-analysis")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    r = client.put(
        "/api/store/entities/e1/publisher",
        json={"publisher_kind": "organization"},
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 200, r.text
    assert r.json()["publisher_kind"] == "organization"
    assert r.json()["publisher_name"] == "Your organization"

    # The author sees the same label — publisher is a property of the item, not
    # of who is looking at it.
    body = client.get("/api/store/entities/e1", cookies={"access_token": sess}).json()
    assert body["publisher_name"] == "Your organization"


def test_publish_as_organization_is_admin_only(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user(conn, "anna@example.com")
        _make_entity(conn, entity_id="e1", owner_id=uid, owner_username="anna", name="mine")
    finally:
        conn.close()
        close_system_db()

    r = _client().put(
        "/api/store/entities/e1/publisher",
        json={"publisher_kind": "organization"},
        cookies={"access_token": sess},
    )
    assert r.status_code == 403, r.text


def test_publisher_facet_splits_me_from_other_users(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com")
        bob, _bob_sess = _make_user(conn, "bob@example.com")
        _make_entity(conn, entity_id="e-mine", owner_id=anna, owner_username="anna", name="mine")
        _make_entity(conn, entity_id="e-theirs", owner_id=bob, owner_username="bob", name="theirs")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    cookies = {"access_token": anna_sess}

    mine = client.get("/api/store/entities?publisher=me", cookies=cookies).json()
    assert [i["id"] for i in mine["items"]] == ["e-mine"]

    theirs = client.get("/api/store/entities?publisher=other_users", cookies=cookies).json()
    assert [i["id"] for i in theirs["items"]] == ["e-theirs"]

    both = client.get("/api/store/entities", cookies=cookies).json()
    assert {i["id"] for i in both["items"]} == {"e-mine", "e-theirs"}


def test_invalid_facet_values_are_rejected(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        _uid, sess = _make_user(conn, "anna@example.com")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    cookies = {"access_token": sess}
    assert client.get("/api/store/entities?publisher=admin", cookies=cookies).status_code == 400
    assert client.get("/api/store/entities?verification=ok", cookies=cookies).status_code == 400


# ---------------------------------------------------------------------------
# Verification — advisory, opt-in, never a gate
# ---------------------------------------------------------------------------


def test_verification_endpoints_absent_when_disabled(fresh_db, monkeypatch):
    """The axis is opt-in per instance, and disabled — the default — must
    remove the endpoints, not merely hide the buttons that call them."""
    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: False, raising=False)
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=uid, owner_username="anna", name="mine")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    r = client.put(
        "/api/store/entities/e1/verification",
        json={"verification_state": "verified"},
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "verification_disabled"

    r2 = client.post("/api/store/entities/e1/verification/request", cookies={"access_token": sess})
    assert r2.status_code == 404


def test_verify_then_filter(fresh_db, monkeypatch):
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        uid, sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e-ver", owner_id=uid, owner_username="anna", name="verified-1")
        _make_entity(conn, entity_id="e-raw", owner_id=uid, owner_username="anna", name="raw-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    r = client.put(
        "/api/store/entities/e-ver/verification",
        json={"verification_state": "verified"},
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification_state"] == "verified"
    assert r.json()["verified_at"] is not None

    cookies = {"access_token": sess}
    verified = client.get("/api/store/entities?verification=verified", cookies=cookies).json()
    assert [i["id"] for i in verified["items"]] == ["e-ver"]

    unverified = client.get("/api/store/entities?verification=unverified", cookies=cookies).json()
    assert [i["id"] for i in unverified["items"]] == ["e-raw"]


def test_verification_never_hides_an_entity(fresh_db, monkeypatch):
    """THE regression guard. Verification is a chip and a filter — never a read
    gate. An unverified entity stays fully readable, or this has silently become
    the approval queue again."""
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        anna, _anna_sess = _make_user(conn, "anna@example.com")
        _bob, bob_sess = _make_user(conn, "bob@example.com")
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="unverified-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    cookies = {"access_token": bob_sess}
    # Someone else's unverified item: visible in the default listing...
    listing = client.get("/api/store/entities", cookies=cookies).json()
    assert [i["id"] for i in listing["items"]] == ["e1"]
    # ...and readable on its detail endpoint.
    assert client.get("/api/store/entities/e1", cookies=cookies).status_code == 200


def test_verification_note_is_author_and_admin_only(fresh_db, monkeypatch):
    """Other users see the verdict, never the internal workflow detail."""
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com")
        _bob, bob_sess = _make_user(conn, "bob@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="noted-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    client.put(
        "/api/store/entities/e1/verification",
        json={"verification_state": "changes_requested", "note": "Description is too thin."},
        cookies={"access_token": admin_sess},
    )
    author = client.get("/api/store/entities/e1", cookies={"access_token": anna_sess}).json()
    assert author["verification_note"] == "Description is too thin."
    other = client.get("/api/store/entities/e1", cookies={"access_token": bob_sess}).json()
    assert other["verification_note"] is None
    assert other["verification_state"] == "changes_requested"


def test_organization_published_cannot_be_verified(fresh_db, monkeypatch):
    """Invariant 1 — an org item already carries the stronger claim, so a
    checkmark on top would invent a fourth trust tier."""
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        anna, _sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="org-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    client.put(
        "/api/store/entities/e1/publisher",
        json={"publisher_kind": "organization"},
        cookies={"access_token": admin_sess},
    )
    r = client.put(
        "/api/store/entities/e1/verification",
        json={"verification_state": "verified"},
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "publisher_is_organization"


def test_only_owner_can_request_verification(fresh_db, monkeypatch):
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com")
        _bob, bob_sess = _make_user(conn, "bob@example.com")
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="askable")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    assert (
        client.post("/api/store/entities/e1/verification/request", cookies={"access_token": bob_sess}).status_code
        == 403
    )
    r = client.post("/api/store/entities/e1/verification/request", cookies={"access_token": anna_sess})
    assert r.status_code == 200, r.text
    assert r.json()["verification_state"] == "requested"


def test_undiscoverable_item_cannot_request_verification(fresh_db, monkeypatch):
    """Nothing to verify while nobody else can see it."""
    from src.db import close_system_db, get_system_db

    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True, raising=False)
    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com")
        _make_entity(
            conn,
            entity_id="e1",
            owner_id=anna,
            owner_username="anna",
            name="pending-1",
            status="pending",
        )
    finally:
        conn.close()
        close_system_db()

    r = _client().post("/api/store/entities/e1/verification/request", cookies={"access_token": anna_sess})
    assert r.status_code == 409
    assert r.json()["detail"] == "not_discoverable"


# ---------------------------------------------------------------------------
# Required ("In stack, locked")
# ---------------------------------------------------------------------------


def test_required_rejected_on_user_published_entity(fresh_db):
    """An admin cannot conscript a colleague's personal upload into everyone's
    stack — Required is an institutional commitment, so the organization has to
    be the publisher standing behind it."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, _sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="personal-1")
        group_id = _make_group(conn, "Data Team")
    finally:
        conn.close()
        close_system_db()

    r = _client().post(
        "/api/admin/grants",
        json={
            "group_id": group_id,
            "resource_type": "store_entity",
            "resource_id": "e1",
            "requirement": "required",
        },
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 422, r.text
    assert "organization-published" in r.json()["detail"]


def test_required_org_entity_installs_for_group_members_and_locks(fresh_db):
    """The lock has to have something behind it: a Required org item is
    materialized into each member's installs, and the member cannot remove it."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com")
        _admin_id, admin_sess = _make_user(conn, "boss@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="mandated-1")
        group_id = _make_group(conn, "Data Team", members=[anna])
    finally:
        conn.close()
        close_system_db()

    client = _client()
    client.put(
        "/api/store/entities/e1/publisher",
        json={"publisher_kind": "organization"},
        cookies={"access_token": admin_sess},
    )
    r = client.post(
        "/api/admin/grants",
        json={
            "group_id": group_id,
            "resource_type": "store_entity",
            "resource_id": "e1",
            "requirement": "required",
        },
        cookies={"access_token": admin_sess},
    )
    assert r.status_code == 201, r.text

    # Fan-out happened: the member has it without ever clicking Add.
    from src.db import close_system_db as _close, get_system_db as _get

    conn2 = _get()
    try:
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        assert UserStoreInstallsRepository(conn2).is_installed(anna, "e1") is True
    finally:
        conn2.close()
        _close()

    # And the lock holds.
    r2 = client.delete("/api/store/entities/e1/install", cookies={"access_token": anna_sess})
    assert r2.status_code == 409
    assert r2.json()["detail"] == "entity_required"


# ---------------------------------------------------------------------------
# The unified Browse shelf
# ---------------------------------------------------------------------------


def test_browse_merges_both_sources_with_exact_totals(fresh_db):
    """One shelf. The old page fanned out two fetches and merged client-side,
    which made `total` (and therefore pagination) approximate; `tab=browse`
    merges server-side so the total is the sum of two exact counts."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="skill-one")
        _make_entity(conn, entity_id="e2", owner_id=anna, owner_username="anna", name="skill-two")
    finally:
        conn.close()
        close_system_db()

    r = _client().get("/api/marketplace/items?tab=browse&page_size=50", cookies={"access_token": anna_sess})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert {i["publisher_kind"] for i in body["items"]} == {"user"}
    assert {i["publisher_name"] for i in body["items"]} == {"Anna"}


def test_browse_publisher_facet_filters(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com", admin=True)
        bob, _ = _make_user(conn, "bob@example.com")
        _make_entity(conn, entity_id="e-mine", owner_id=anna, owner_username="anna", name="mine-1")
        _make_entity(conn, entity_id="e-theirs", owner_id=bob, owner_username="bob", name="theirs-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    cookies = {"access_token": anna_sess}

    org = client.get("/api/marketplace/items?tab=browse&publisher=organization", cookies=cookies)
    assert org.status_code == 200, org.text
    assert org.json()["total"] == 0

    mine = client.get("/api/marketplace/items?tab=browse&publisher=me", cookies=cookies)
    assert mine.status_code == 200, mine.text
    assert [i["id"] for i in mine.json()["items"]] == ["flea-e-mine"]

    theirs = client.get("/api/marketplace/items?tab=browse&publisher=other_users", cookies=cookies)
    assert theirs.status_code == 200, theirs.text
    assert [i["id"] for i in theirs.json()["items"]] == ["flea-e-theirs"]


def test_browse_verification_facet_excludes_curated(fresh_db):
    """Verification is only meaningful on user-published items, so asking for a
    verification state must not silently treat "no state" as one or the other."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="raw-1")
    finally:
        conn.close()
        close_system_db()

    client = _client()
    cookies = {"access_token": anna_sess}
    unverified = client.get("/api/marketplace/items?tab=browse&verification=unverified", cookies=cookies)
    assert unverified.status_code == 200, unverified.text
    assert [i["id"] for i in unverified.json()["items"]] == ["flea-e1"]
    verified = client.get("/api/marketplace/items?tab=browse&verification=verified", cookies=cookies)
    assert verified.status_code == 200, verified.text
    assert verified.json()["total"] == 0


def test_browse_categories_endpoint_accepts_the_new_tab(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        anna, anna_sess = _make_user(conn, "anna@example.com", admin=True)
        _make_entity(conn, entity_id="e1", owner_id=anna, owner_username="anna", name="cat-1")
    finally:
        conn.close()
        close_system_db()

    r = _client().get("/api/marketplace/categories?tab=browse", cookies={"access_token": anna_sess})
    assert r.status_code == 200, r.text
    assert any(c["name"] == "Productivity" for c in r.json()["items"])


def test_show_unverified_trust_global_respects_the_off_switch(monkeypatch):
    """The Community marker's instance switch must hold on EVERY surface.

    `show_unverified_trust` is resolved by a Jinja global
    (`app.web.router._show_unverified_trust`) rather than threaded through each
    route's context. That is deliberate: it briefly rode
    `library_show_unverified_trust|default(true)` in
    `marketplace_item_detail.html`, which fixed the marker disappearing on routes
    that omitted the value and broke the off switch instead — any such route then
    rendered the marker on an instance that had explicitly disabled it. Resolving
    it centrally removes both failure modes, because no per-route value is left
    to forget.
    """
    from app.web.router import _show_unverified_trust

    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "false")
    assert not _show_unverified_trust(), "the off switch must actually turn it off"

    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    assert _show_unverified_trust()

    # Absent config is ON: the Library states all three provenance levels rather
    # than leaving every unverified row bare. Upgrade parity for a default blue
    # instance comes from the paper gate on `mark()`, not from this flag — see
    # tests/test_feature_flags.py::test_positive_trust_vocabulary_is_on_by_default.
    monkeypatch.delenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", raising=False)
    assert _show_unverified_trust()


def test_no_template_applies_a_jinja_default_to_the_unverified_trust_flag():
    """Guard the bug class, not just the one line that had it.

    A `|default(...)` on this flag lets a stray template literal override the
    operator's setting wherever the variable is missing — when the flag was
    briefly opt-out, exactly that happened. The hazard is symmetric and survives
    the default flipping on: a `|default(false)` now silently suppresses the
    markers on an instance that never asked for the silent reading, just as a
    `|default(true)` used to resurrect them on one that had disabled them. The
    flag must resolve through the central `show_unverified_trust_enabled()`
    global, never per-template fallbacks.
    """
    from pathlib import Path

    offenders = []
    for path in sorted(Path("app/web/templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "show_unverified_trust" in line and "default(" in line:
                offenders.append(f"{path}:{lineno}: {line.strip()[:100]}")
    assert not offenders, (
        "show_unverified_trust must never take a per-template Jinja default() — "
        "a stray literal overrides the operator's setting on any route that "
        "omits the variable. Call the show_unverified_trust_enabled() global "
        "instead:\n" + "\n".join(offenders)
    )
