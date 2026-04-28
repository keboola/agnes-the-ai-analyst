"""Tests for corporate memory API — knowledge items, voting, governance."""

import pytest
from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestMemoryCreate:
    def test_create_knowledge_item(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "Best Practice", "content": "Always document your code.", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["status"] == "pending"

    def test_create_with_tags(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/memory",
            json={
                "title": "Tagged Item",
                "content": "Content here",
                "category": "process",
                "tags": ["tag1", "tag2"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        assert "id" in resp.json()

    def test_create_missing_title_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"content": "No title", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_create_missing_content_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "No content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_create_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/memory",
            json={"title": "Test", "content": "Content", "category": "engineering"},
        )
        assert resp.status_code == 401


class TestMemoryList:
    def _create_item(self, c, token, title="Test Item", category="engineering"):
        resp = c.post(
            "/api/memory",
            json={"title": title, "content": f"Content for {title}", "category": category},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_list_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "count" in data

    def test_list_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory")
        assert resp.status_code == 401

    def test_list_pagination(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Create 3 items
        for i in range(3):
            self._create_item(c, token, title=f"Item {i}")

        # Page 1 with per_page=2
        resp = c.get("/api/memory?page=1&per_page=2", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["per_page"] == 2
        assert data["page"] == 1
        assert len(data["items"]) <= 2

    def test_list_search(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        self._create_item(c, token, title="Unique Keyword SearchTarget")
        self._create_item(c, token, title="Another Item")

        resp = c.get("/api/memory?search=SearchTarget", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        titles = [item["title"] for item in data["items"]]
        assert any("SearchTarget" in t for t in titles)


class TestMemoryStats:
    def test_get_stats_returns_counts(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory/stats", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert isinstance(data["total"], int)
        assert data["total"] >= 0
        assert "by_status" in data
        assert isinstance(data["by_status"], dict)
        assert "categories" in data
        assert isinstance(data["categories"], list)

    def test_get_stats_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/stats")
        assert resp.status_code == 401

    def test_get_stats_does_not_load_all_items(self, seeded_app):
        """Stats endpoint uses SQL aggregation, not list_items()."""
        from unittest.mock import patch
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch.object(KnowledgeRepository, "list_items", side_effect=AssertionError("list_items should not be called")):
            resp = c.get("/api/memory/stats", headers=_auth(token))
            assert resp.status_code == 200


class TestMemoryVote:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Voteable", "content": "vote me", "category": "process"},
            headers=_auth(token),
        )
        return resp.json()["id"]

    def test_vote_upvote(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["upvotes"] >= 1

    def test_vote_downvote(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": -1}, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["downvotes"] >= 1

    def test_vote_invalid_value_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": 5}, headers=_auth(token))
        assert resp.status_code == 400

    def test_vote_nonexistent_item_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/memory/nonexistent-id/vote", json={"vote": 1}, headers=_auth(token))
        assert resp.status_code == 404

    def test_vote_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/memory/some-id/vote", json={"vote": 1})
        assert resp.status_code == 401


class TestMemoryMyVotes:
    def test_get_my_votes_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/memory/my-votes", headers=_auth(token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_get_my_votes_after_voting(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Create and vote
        item_resp = c.post(
            "/api/memory",
            json={"title": "My Vote Item", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        item_id = item_resp.json()["id"]
        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))

        # Check my-votes
        resp = c.get("/api/memory/my-votes", headers=_auth(token))
        assert resp.status_code == 200
        votes = resp.json()
        assert item_id in votes
        assert votes[item_id] == 1

    def test_my_votes_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/my-votes")
        assert resp.status_code == 401


class TestMemoryAdminEndpoints:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Admin Test", "content": "content", "category": "policy"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_admin_approve(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(f"/api/memory/admin/approve?item_id={item_id}", headers=_auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_admin_reject(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={"reason": "not relevant"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_admin_mandate(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/mandate?item_id={item_id}",
            json={"reason": "company policy", "audience": "all"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mandatory"

    def test_admin_approve_analyst_gets_403(self, seeded_app):
        """Analyst cannot use admin governance endpoints."""
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(f"/api/memory/admin/approve?item_id={item_id}", headers=_auth(analyst_token))
        assert resp.status_code == 403

    def test_admin_reject_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={"reason": "nope"},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 403

    def test_admin_mandate_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/mandate?item_id={item_id}",
            json={"reason": "policy"},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 403

    def test_admin_approve_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/memory/admin/approve?item_id=some-id")
        assert resp.status_code == 401
