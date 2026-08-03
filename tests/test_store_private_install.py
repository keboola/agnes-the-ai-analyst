"""The Private store tier can reach its own author's Stack — and only theirs.

``POST /api/store/entities/{id}/install`` used to refuse anything not
``approved``, so an entity kept Private (``access=private`` on upload, or the
builder's Private choice) could be authored, listed in its owner's Library, and
offered an "Add to Stack" button that always 409'd. The author may now install
their own ``hidden`` entity.

The exemption is narrow, and these tests are the fence around it. ``hidden`` is
written by TWO paths — the Private access choice and guardrail quarantine — so
the separator is the submission verdict, not the visibility status. Fixtures and
seed helpers are reused from ``test_admin_store_submissions`` rather than
recopied; that module owns the quarantine seed contract.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.db import close_system_db
from tests.test_admin_store_submissions import _create_user, _seed_quarantined_entity


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Same shape as the one in ``test_admin_store_submissions`` — declared here
    rather than imported, because importing a fixture by name trips ruff's F811
    on every test that takes it as a parameter."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    close_system_db()
    from app.main import create_app

    app = create_app()
    yield TestClient(app)
    close_system_db()


def test_own_private_entity_is_installable_by_its_author(web_client: TestClient):
    """The default path of the chat upload dialog: "Save to my Library" +
    "Add to my stack" with sharing off."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    # status='approved' → a hidden entity with NO blocking verdict, i.e. private
    # by choice rather than quarantined.
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "p1",
        status="approved",
    )

    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=owner_cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["installed"] is True


def test_others_private_entity_still_refused(web_client: TestClient):
    """Owner-scoped: a third party holding the entity_id of someone else's
    private item gets the same 409 as before."""
    owner_id, _owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "p2",
        status="approved",
    )

    _, other_cookies = _create_user(web_client, "other@x.com")
    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=other_cookies,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "entity_not_approved"


def test_own_quarantined_entity_still_refused(web_client: TestClient):
    """The guard that the exemption did not widen into "owner may install
    anything hidden". A bundle guardrail review REJECTED stays out of its own
    author's Stack — the case
    ``test_admin_store_submissions::test_install_quarantined_refused_for_non_admin``
    also pins from the admin side.
    """
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    # Default status='blocked_llm' → hidden AND rejected.
    entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "p3")

    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=owner_cookies,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "entity_not_approved"
