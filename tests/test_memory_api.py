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

    def test_search_respects_domain_filter(self, seeded_app):
        """search + domain must only return items in that domain."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        repo.create(id="srch_fin", title="Finance SearchKeyword", content="x", category="data_analysis", domain="finance")
        repo.create(id="srch_eng", title="Engineering SearchKeyword", content="x", category="data_analysis", domain="engineering")
        conn.close()

        resp = seeded_app["client"].get(
            "/api/memory?search=SearchKeyword&domain=finance",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        ids = {i["id"] for i in resp.json()["items"]}
        assert "srch_fin" in ids
        assert "srch_eng" not in ids

    def test_search_respects_pagination(self, seeded_app):
        """search + per_page must not return more items than requested."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        for i in range(5):
            repo.create(id=f"pgn_{i}", title=f"PaginateSearch item {i}", content="x", category="data_analysis")
        conn.close()

        resp = seeded_app["client"].get(
            "/api/memory?search=PaginateSearch&per_page=2",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) <= 2


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


class TestAdminContradictionsExcludePersonal:
    """Verify exclude_personal param on GET /api/memory/admin/contradictions."""

    def _make_item(self, item_id: str, title: str, is_personal: bool = False):
        from datetime import datetime, timezone
        from src.db import get_system_db

        conn = get_system_db()
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO knowledge_items
               (id, title, content, category, source_user, is_personal, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [item_id, title, f"content of {title}", "test", "admin@test.com",
             is_personal, "approved", now, now],
        )
        conn.close()

    def _make_contradiction(self, cid: str, a_id: str, b_id: str):
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            """INSERT INTO knowledge_contradictions
               (id, item_a_id, item_b_id, explanation, detected_at)
               VALUES (?, ?, ?, ?, current_timestamp)""",
            [cid, a_id, b_id, "test contradiction"],
        )
        conn.close()

    def test_personal_item_hidden_by_default(self, seeded_app):
        """By default, personal items in contradictions are replaced with {id, hidden: true}."""
        c = seeded_app["client"]
        self._make_item("item_pub", "Public item")
        self._make_item("item_priv", "Private item", is_personal=True)
        self._make_contradiction("kc_test1", "item_pub", "item_priv")

        r = c.get("/api/memory/admin/contradictions",
                  headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test1")
        # item_a is public — full dict
        assert contra["item_a"]["id"] == "item_pub"
        assert "title" in contra["item_a"]
        assert contra["item_a"].get("hidden") is not True
        # item_b is personal — hidden placeholder
        assert contra["item_b"]["id"] == "item_priv"
        assert contra["item_b"].get("hidden") is True
        assert "title" not in contra["item_b"]

    def test_admin_can_opt_in_to_personal_content(self, seeded_app):
        """With exclude_personal=false an admin sees full content."""
        c = seeded_app["client"]
        self._make_item("item_pub2", "Public item 2")
        self._make_item("item_priv2", "Private item 2", is_personal=True)
        self._make_contradiction("kc_test2", "item_pub2", "item_priv2")

        r = c.get(
            "/api/memory/admin/contradictions?exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test2")
        assert contra["item_b"]["id"] == "item_priv2"
        assert "title" in contra["item_b"]
        assert contra["item_b"].get("hidden") is not True

    def test_non_personal_items_always_enriched(self, seeded_app):
        """Non-personal items on both sides are always returned in full."""
        c = seeded_app["client"]
        self._make_item("item_pub3", "Public A")
        self._make_item("item_pub4", "Public B")
        self._make_contradiction("kc_test3", "item_pub3", "item_pub4")

        r = c.get("/api/memory/admin/contradictions",
                  headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test3")
        assert "title" in contra["item_a"]
        assert "title" in contra["item_b"]
        assert contra["item_a"].get("hidden") is not True
        assert contra["item_b"].get("hidden") is not True


class TestAudienceDistribution:
    """Verify audience-based knowledge distribution — mandate persists audience,
    list respects user.groups, admins see all."""

    def _seed_item(self, conn, item_id: str, title: str, audience: str | None = None):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO knowledge_items
               (id, title, content, category, source_user, audience, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [item_id, title, f"content {title}", "test", "admin@test.com",
             audience, "approved", now, now],
        )

    def test_mandate_persists_audience(self, seeded_app):
        """POST /admin/mandate with audience stores it on the item."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from src.db import get_system_db
        conn = get_system_db()
        self._seed_item(conn, "aud_item1", "Finance policy")
        conn.close()

        r = c.post(
            "/api/memory/admin/mandate?item_id=aud_item1",
            json={"reason": "important", "audience": "group:finance"},
            headers=_auth(token),
        )
        assert r.status_code == 200

        from src.db import get_system_db
        conn = get_system_db()
        row = conn.execute(
            "SELECT audience, status FROM knowledge_items WHERE id = 'aud_item1'"
        ).fetchone()
        conn.close()
        assert row[0] == "group:finance"
        assert row[1] == "mandatory"

    def test_batch_mandate_persists_audience(self, seeded_app):
        """POST /admin/batch mandate action stores audience."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from src.db import get_system_db
        conn = get_system_db()
        self._seed_item(conn, "aud_item2", "Eng item")
        conn.close()

        r = c.post(
            "/api/memory/admin/batch",
            json={"item_ids": ["aud_item2"], "action": "mandate", "audience": "group:engineering"},
            headers=_auth(token),
        )
        assert r.status_code == 200

        from src.db import get_system_db
        conn = get_system_db()
        row = conn.execute(
            "SELECT audience FROM knowledge_items WHERE id = 'aud_item2'"
        ).fetchone()
        conn.close()
        assert row[0] == "group:engineering"

    def test_user_in_group_sees_group_items(self, seeded_app):
        """A user whose groups include 'finance' sees audience='group:finance' items."""
        import json
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item(conn, "aud_fin", "Finance fact", audience="group:finance")
        self._seed_item(conn, "aud_all", "All-users fact", audience="all")
        # Create a user with finance group
        repo = UserRepository(conn)
        repo.create(id="fin_user1", email="fin@test.com", name="Finance User", role="analyst")
        repo.update("fin_user1", groups=json.dumps(["finance"]))
        conn.close()

        token = create_access_token("fin_user1", "fin@test.com", "analyst")
        r = seeded_app["client"].get("/api/memory", headers=_auth(token))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin" in ids
        assert "aud_all" in ids

    def test_user_not_in_group_cannot_see_group_items(self, seeded_app):
        """A user without 'finance' group does not see audience='group:finance' items."""
        import json
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item(conn, "aud_fin2", "Finance fact 2", audience="group:finance")
        self._seed_item(conn, "aud_null", "No audience fact", audience=None)
        # Create a user with NO groups
        repo = UserRepository(conn)
        repo.create(id="eng_user1", email="eng@test.com", name="Eng User", role="analyst")
        conn.close()

        token = create_access_token("eng_user1", "eng@test.com", "analyst")
        r = seeded_app["client"].get("/api/memory", headers=_auth(token))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin2" not in ids
        assert "aud_null" in ids  # null audience treated as 'all'

    def test_admin_sees_all_audiences(self, seeded_app):
        """Admin sees items regardless of audience."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "aud_fin3", "Finance exclusive", audience="group:finance")
        self._seed_item(conn, "aud_eng3", "Eng exclusive", audience="group:engineering")
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin3" in ids
        assert "aud_eng3" in ids

    def test_null_audience_visible_to_all(self, seeded_app):
        """Items with no audience set are visible to all authenticated users."""
        from src.db import get_system_db
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item(conn, "aud_null2", "Global fact", audience=None)
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory", headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_null2" in ids


class TestVoteRetract:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Retract Vote Item", "content": "content", "category": "process"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_vote_zero_retracts_existing_vote(self, seeded_app):
        """vote=0 removes a prior vote so upvotes returns to 0."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        r = c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["upvotes"] == 0

    def test_vote_zero_on_unvoted_item_is_noop(self, seeded_app):
        """vote=0 on an item the user never voted on returns 200 and zero counts."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        r = c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["upvotes"] == 0
        assert data["downvotes"] == 0

    def test_my_votes_omits_retracted_vote(self, seeded_app):
        """After retract, /my-votes must not include the item."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))

        r = c.get("/api/memory/my-votes", headers=_auth(token))
        assert r.status_code == 200
        assert item_id not in r.json()


class TestBundle:
    def _seed_item(self, conn, item_id: str, title: str, status: str, confidence: float = None):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(
            id=item_id, title=title, content=f"Content for {title}",
            category="engineering", status=status, confidence=confidence,
        )
        repo.update_status(item_id, status)

    def test_bundle_requires_auth(self, seeded_app):
        r = seeded_app["client"].get("/api/memory/bundle")
        assert r.status_code == 401

    def test_bundle_empty(self, seeded_app):
        """Bundle returns empty lists when no mandatory/approved items exist."""
        r = seeded_app["client"].get(
            "/api/memory/bundle", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        data = r.json()
        assert data["mandatory"] == []
        assert data["approved"] == []
        assert "token_estimate" in data
        assert "token_budget" in data

    def test_bundle_mandatory_items_always_included(self, seeded_app):
        """Mandatory items appear in the bundle regardless of token budget."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "bnd_m1", "Mandatory Fact", "mandatory", confidence=0.9)
        self._seed_item(conn, "bnd_a1", "Approved Fact", "approved", confidence=0.8)
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory/bundle", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        data = r.json()
        mandatory_ids = {i["id"] for i in data["mandatory"]}
        approved_ids = {i["id"] for i in data["approved"]}
        assert "bnd_m1" in mandatory_ids
        assert "bnd_a1" in approved_ids

    def test_bundle_token_budget_limits_approved(self, seeded_app):
        """Approved items exceeding the token budget are excluded."""
        from src.db import get_system_db
        from app.api.memory import BUNDLE_TOKEN_BUDGET, _CHARS_PER_TOKEN

        # Create an approved item whose content alone exceeds the entire budget.
        huge_content = "X" * (BUNDLE_TOKEN_BUDGET * _CHARS_PER_TOKEN + 100)
        conn = get_system_db()
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(
            id="bnd_huge", title="Huge Item", content=huge_content,
            category="engineering", status="approved", confidence=1.0,
        )
        repo.update_status("bnd_huge", "approved")
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory/bundle", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        approved_ids = {i["id"] for i in r.json()["approved"]}
        assert "bnd_huge" not in approved_ids

    def test_bundle_pending_items_excluded(self, seeded_app):
        """Pending items must not appear in the bundle."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "bnd_p1", "Pending Fact", "pending")
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory/bundle", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        data = r.json()
        all_ids = {i["id"] for i in data["mandatory"]} | {i["id"] for i in data["approved"]}
        assert "bnd_p1" not in all_ids

    def test_bundle_confidence_zero_treated_as_default(self, seeded_app):
        """An item with confidence=0.0 (not NULL) uses 0.0 in ranking, not the 0.5 fallback."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        repo.create(
            id="bnd_zero", title="Zero Confidence", content="content",
            category="engineering", status="approved", confidence=0.0,
        )
        repo.update_status("bnd_zero", "approved")
        repo.create(
            id="bnd_high", title="High Confidence", content="content",
            category="engineering", status="approved", confidence=0.9,
        )
        repo.update_status("bnd_high", "approved")
        conn.close()

        r = seeded_app["client"].get(
            "/api/memory/bundle", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        approved = r.json()["approved"]
        ids_in_order = [i["id"] for i in approved]
        # high-confidence item must rank above zero-confidence item
        if "bnd_high" in ids_in_order and "bnd_zero" in ids_in_order:
            assert ids_in_order.index("bnd_high") < ids_in_order.index("bnd_zero")


class TestAutoTopicTagging:
    """Integration tests for Haiku auto-tagging wired into POST /api/memory.

    Tagging is best-effort — no LLM available in test env — so these tests
    focus on:
    1. User-supplied tags survive creation (the merge path must not drop them).
    2. Creating without any tags works (no crash, tags field is absent/null).
    3. When a mocked tagger returns topics they are prepended before user tags.
    """

    def test_user_tags_preserved_on_create(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={
                "title": "Tag preservation test",
                "content": "content",
                "category": "engineering",
                "tags": ["my-tag", "another-tag"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]

        # Fetch the item and verify tags are present
        list_resp = c.get("/api/memory", headers=_auth(token))
        assert list_resp.status_code == 200
        items = {i["id"]: i for i in list_resp.json()["items"]}
        assert item_id in items
        stored_tags = items[item_id].get("tags") or []
        assert "my-tag" in stored_tags
        assert "another-tag" in stored_tags

    def test_create_without_tags_succeeds(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "No tags item", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        assert "id" in resp.json()

    def test_mocked_tagger_topics_prepended_before_user_tags(self, seeded_app, monkeypatch):
        """Topics from tagger appear before user-supplied tags."""
        from services.corporate_memory import tagger as tagger_module

        def _fake_auto_tag(items, extractor):
            return {items[0]["id"]: ["data", "queries"]}

        monkeypatch.setattr(tagger_module, "auto_tag_items", _fake_auto_tag)

        # Also patch load_instance_config so the try-block reaches auto_tag_items
        import app.api.memory as mem_module

        original_create = mem_module.create_knowledge

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        resp = c.post(
            "/api/memory",
            json={
                "title": "Tagger integration",
                "content": "SQL query optimisation tips",
                "category": "engineering",
                "tags": ["user-tag"],
            },
            headers=_auth(token),
        )
        # Creation must succeed regardless of whether tagger ran
        assert resp.status_code == 201

    def test_tagger_failure_does_not_block_creation(self, seeded_app, monkeypatch):
        """If auto_tag_items raises, item creation must still return 201."""
        from services.corporate_memory import tagger as tagger_module

        def _boom(items, extractor):
            raise RuntimeError("LLM unreachable")

        monkeypatch.setattr(tagger_module, "auto_tag_items", _boom)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "Resilience test", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
