"""Tests for memory-domain-suggestions API (v55).

Non-admin can create + see their own; admin can list and resolve.
Approve creates the real memory_domains row and stamps the suggestion
with ``created_domain_id``.
"""

from __future__ import annotations

from src.db import get_system_db
from src.repositories.memory_domain_suggestions import (
    MemoryDomainSuggestionsRepository,
)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestSuggestEndpoint:
    def test_analyst_can_suggest(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Sales coaching", "description": "Playbooks + decks"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201
        sid = resp.json()["id"]
        assert sid.startswith("sug_")

    def test_suggest_rejects_blank_name(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "   "},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400

    def test_analyst_sees_own_suggestions(self, seeded_app):
        seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Visible to me"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        resp = seeded_app["client"].get(
            "/api/memory-domain-suggestions/mine",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["items"]}
        assert "Visible to me" in names


class TestAdminQueue:
    def test_admin_lists_pending(self, seeded_app):
        seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Queue me"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        resp = seeded_app["client"].get(
            "/api/admin/memory-domain-suggestions?status=pending",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert "Queue me" in names

    def test_count_pending_excludes_resolved(self, seeded_app):
        # Seed two pending; resolve one; expect count = 1.
        conn = get_system_db()
        repo = MemoryDomainSuggestionsRepository(conn)
        s1 = repo.create(name="P1", created_by="u1")
        repo.create(name="P2", created_by="u2")
        repo.resolve(s1, status="rejected", resolved_by="admin1")
        conn.close()
        resp = seeded_app["client"].get(
            "/api/admin/memory-domain-suggestions/count-pending",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_non_admin_cannot_list_queue(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/admin/memory-domain-suggestions",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestApproveReject:
    def test_approve_creates_domain(self, seeded_app):
        sug_resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Approve me"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        sid = sug_resp.json()["id"]
        resp = seeded_app["client"].post(
            f"/api/admin/memory-domain-suggestions/{sid}/approve",
            json={"slug": "approve-me-test"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["created_domain_id"]
        # Suggestion row now has the created_domain_id stamped.
        conn = get_system_db()
        sugg = MemoryDomainSuggestionsRepository(conn).get(sid)
        conn.close()
        assert sugg["created_domain_id"] == body["created_domain_id"]
        assert sugg["status"] == "approved"

    def test_approve_idempotent_via_409(self, seeded_app):
        # Second approve on the same suggestion returns 409.
        sug_resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Once"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        sid = sug_resp.json()["id"]
        seeded_app["client"].post(
            f"/api/admin/memory-domain-suggestions/{sid}/approve",
            json={"slug": "once-only"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = seeded_app["client"].post(
            f"/api/admin/memory-domain-suggestions/{sid}/approve",
            json={"slug": "once-only-2"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409
        assert "already_resolved" in resp.json()["detail"]

    def test_reject_with_note(self, seeded_app):
        sug_resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Reject me"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        sid = sug_resp.json()["id"]
        resp = seeded_app["client"].post(
            f"/api/admin/memory-domain-suggestions/{sid}/reject",
            json={"note": "Already covered by Finance domain"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        conn = get_system_db()
        sugg = MemoryDomainSuggestionsRepository(conn).get(sid)
        conn.close()
        assert sugg["resolution_note"] == "Already covered by Finance domain"

    def test_approve_rejects_existing_slug(self, seeded_app):
        # Seed an existing domain, then try to approve a suggestion onto
        # that same slug → 409 slug_exists.
        from src.repositories.memory_domains import MemoryDomainsRepository
        conn = get_system_db()
        MemoryDomainsRepository(conn).create(
            name="Existing", slug="existing-slug", description=None,
            icon=None, color=None, created_by="seed",
        )
        conn.close()
        sug_resp = seeded_app["client"].post(
            "/api/memory-domain-suggestions",
            json={"name": "Dup"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        sid = sug_resp.json()["id"]
        resp = seeded_app["client"].post(
            f"/api/admin/memory-domain-suggestions/{sid}/approve",
            json={"slug": "existing-slug"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "slug_exists"
