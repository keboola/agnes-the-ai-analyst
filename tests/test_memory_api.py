"""Tests for corporate memory API — knowledge items, voting, governance."""

import pytest


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
    def test_get_stats(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory/stats", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_status" in data
        assert "categories" in data

    def test_get_stats_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/stats")
        assert resp.status_code == 401


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


class TestPersonalItemPrivacy:
    """Regression tests for the is_personal privacy leak (pd-ps review Q5).

    Personal items must be visible only to the contributor and to privileged
    viewers (km_admin/admin). Non-privileged callers must not be able to bypass
    the filter via list, search, provenance, or vote endpoints — even by setting
    exclude_personal=false.
    """

    def _create_and_flag_personal(self, client, contributor_token):
        resp = client.post(
            "/api/memory",
            json={"title": "Confidential note", "content": "private detail", "category": "engineering"},
            headers=_auth(contributor_token),
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]
        flag = client.post(
            f"/api/memory/{item_id}/personal",
            json={"is_personal": True},
            headers=_auth(contributor_token),
        )
        assert flag.status_code == 200
        return item_id

    def test_list_hides_personal_from_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        # admin is contributor; analyst is the non-contributor non-privileged caller.
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_list_exclude_personal_false_is_coerced_for_non_admin(self, seeded_app):
        """Caller sets exclude_personal=false; server must silently coerce to true for non-admins."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?exclude_personal=false",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_search_hides_personal_from_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?search=Confidential",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_provenance_returns_404_for_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            f"/api/memory/{item_id}/provenance",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # 404 (not 403) avoids leaking item existence.
        assert resp.status_code == 404

    def test_vote_returns_404_for_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404

    def test_admin_can_see_personal_when_opting_in(self, seeded_app):
        """Admin (privileged viewer) opting in via exclude_personal=false sees personal items."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id in ids

    def test_contributor_can_access_their_own_personal_item(self, seeded_app):
        """Contributor reaches their personal item via /my-contributions and direct provenance/vote."""
        c = seeded_app["client"]
        # Use analyst as the contributor (non-privileged).
        item_id = self._create_and_flag_personal(c, seeded_app["analyst_token"])

        # /my-contributions exposes contributor's own items including personal ones.
        mine = c.get("/api/memory/my-contributions", headers=_auth(seeded_app["analyst_token"]))
        assert mine.status_code == 200
        assert item_id in {it["id"] for it in mine.json()["items"]}

        # Direct provenance + vote work for the contributor.
        prov = c.get(f"/api/memory/{item_id}/provenance", headers=_auth(seeded_app["analyst_token"]))
        assert prov.status_code == 200

        vote = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert vote.status_code == 200

    def test_admin_search_with_opt_in_returns_personal_item(self, seeded_app):
        """Confirms exclude_personal flows through to repo.search() for privileged callers."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])
        resp = c.get(
            "/api/memory?search=Confidential&exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert item_id in {it["id"] for it in resp.json()["items"]}

    def test_unflag_personal_makes_item_visible_again(self, seeded_app):
        """Round-trip: contributor flags, then un-flags. Non-admins must regain visibility."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        # Pre-condition: analyst can't see it.
        before = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id not in {it["id"] for it in before.json()["items"]}

        # Contributor un-flags.
        unflag = c.post(
            f"/api/memory/{item_id}/personal",
            json={"is_personal": False},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert unflag.status_code == 200

        # Now analyst can see it.
        after = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id in {it["id"] for it in after.json()["items"]}

    def test_stats_excludes_personal_for_non_admin(self, seeded_app):
        """`/stats` aggregates must not include personal items for non-admins
        — otherwise total/by_status/by_domain change in observable ways when
        a colleague flags or unflags a personal item, leaking existence info."""
        c = seeded_app["client"]
        # Admin baseline (admin sees everything by default).
        admin_before = c.get("/api/memory/stats", headers=_auth(seeded_app["admin_token"]))
        analyst_before = c.get("/api/memory/stats", headers=_auth(seeded_app["analyst_token"]))
        admin_total_before = admin_before.json()["total"]
        analyst_total_before = analyst_before.json()["total"]

        # Admin creates AND flags a personal item.
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        # Admin's total reflects the new item; analyst's must not.
        admin_after = c.get("/api/memory/stats", headers=_auth(seeded_app["admin_token"]))
        analyst_after = c.get("/api/memory/stats", headers=_auth(seeded_app["analyst_token"]))
        assert admin_after.json()["total"] == admin_total_before + 1
        assert analyst_after.json()["total"] == analyst_total_before, (
            f"analyst /stats total leaked the personal item creation: "
            f"{analyst_total_before} -> {analyst_after.json()['total']}"
        )
        assert item_id  # silence unused-warning

    def test_non_personal_item_remains_visible_to_non_admin(self, seeded_app):
        """Negative control: an item that is NOT personal must still be visible — confirms
        we did not over-tighten the filter."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/memory",
            json={"title": "Public note", "content": "shared", "category": "engineering"},
            headers=_auth(seeded_app["admin_token"]),
        )
        item_id = resp.json()["id"]

        listed = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id in {it["id"] for it in listed.json()["items"]}

        prov = c.get(f"/api/memory/{item_id}/provenance", headers=_auth(seeded_app["analyst_token"]))
        assert prov.status_code == 200

        vote = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert vote.status_code == 200
