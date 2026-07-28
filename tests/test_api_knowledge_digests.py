"""Tests for /api/admin/knowledge-digests — maintained-digest CRUD (K4, #799).

Admin-only CRUD over the ``knowledge_digests`` table (Tasks 1-2). Covers:

- 401 unauthenticated / 403 non-admin on every method.
- POST create: 201 with status 'pending'; bad slug -> 422/400; unknown
  source_corpus_ids entry -> 400; duplicate slug -> 409.
- GET list: 280-char output_md preview + output_chars.
- GET detail: full output_md.
- PUT update: title/instructions/source_corpus_ids only (slug immutable).
- DELETE: 204, then GET -> 404; also cleans up resource_grants.
"""

from __future__ import annotations

from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_collection(seeded_app, name: str = "Digest Source") -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(
        name=name, slug=name.lower().replace(" ", "-"), description=None, created_by="admin1"
    )


class TestListKnowledgeDigests:
    def test_unauthenticated_returns_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/admin/knowledge-digests")
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, seeded_app):
        resp = seeded_app["client"].get("/api/admin/knowledge-digests", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_list_shape_and_preview(self, seeded_app):
        c = seeded_app["client"]
        long_md = "x" * 500
        create = c.post(
            "/api/admin/knowledge-digests",
            json={
                "slug": "arch-overview",
                "title": "Architecture overview",
                "instructions": "Maintain an overview of our architecture.",
                "source_corpus_ids": [],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert create.status_code == 201, create.text
        digest_id = create.json()["id"]

        from src.repositories import knowledge_digests_repo

        knowledge_digests_repo().set_generated(digest_id, output_md=long_md, source_fingerprint="fp1", model="m")

        resp = c.get("/api/admin/knowledge-digests", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        items = resp.json()["items"]
        row = next(i for i in items if i["id"] == digest_id)
        assert len(row["output_md"]) <= 280
        assert row["output_chars"] == 500
        assert row["status"] == "fresh"


class TestCreateKnowledgeDigest:
    def test_admin_create_returns_pending(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={
                "slug": "created-digest",
                "title": "Created Digest",
                "instructions": "Do the thing.",
                "source_corpus_ids": [],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"].startswith("kd_")
        assert body["status"] == "pending"

    def test_non_admin_create_returns_403(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/knowledge-digests",
            json={"slug": "x", "title": "X", "instructions": "i", "source_corpus_ids": []},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unauthenticated_create_returns_401(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/knowledge-digests",
            json={"slug": "x", "title": "X", "instructions": "i", "source_corpus_ids": []},
        )
        assert resp.status_code == 401

    def test_bad_slug_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={
                "slug": "Bad Slug!",
                "title": "Bad",
                "instructions": "i",
                "source_corpus_ids": [],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code in (400, 422), resp.text

    def test_unknown_source_corpus_id_returns_400(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={
                "slug": "bad-source",
                "title": "Bad Source",
                "instructions": "i",
                "source_corpus_ids": ["col_doesnotexist"],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text

    def test_valid_source_corpus_id_accepted(self, seeded_app):
        corpus_id = _create_collection(seeded_app, "Valid Source")
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={
                "slug": "good-source",
                "title": "Good Source",
                "instructions": "i",
                "source_corpus_ids": [corpus_id],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text

    def test_duplicate_slug_returns_409(self, seeded_app):
        c = seeded_app["client"]
        payload = {
            "slug": "dupe-digest",
            "title": "Dupe",
            "instructions": "i",
            "source_corpus_ids": [],
        }
        first = c.post("/api/admin/knowledge-digests", json=payload, headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 201
        second = c.post("/api/admin/knowledge-digests", json=payload, headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409


class TestGetKnowledgeDigest:
    def _create(self, seeded_app, slug="detail-digest"):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={"slug": slug, "title": "Detail", "instructions": "i", "source_corpus_ids": []},
            headers=_auth(seeded_app["admin_token"]),
        )
        return resp.json()["id"]

    def test_admin_gets_full_output_md(self, seeded_app):
        digest_id = self._create(seeded_app)
        from src.repositories import knowledge_digests_repo

        long_md = "y" * 500
        knowledge_digests_repo().set_generated(digest_id, output_md=long_md, source_fingerprint="fp", model="m")

        c = seeded_app["client"]
        resp = c.get(f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert resp.json()["output_md"] == long_md

    def test_non_admin_returns_403(self, seeded_app):
        digest_id = self._create(seeded_app, "detail-403")
        resp = seeded_app["client"].get(
            f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["analyst_token"])
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, seeded_app):
        digest_id = self._create(seeded_app, "detail-401")
        resp = seeded_app["client"].get(f"/api/admin/knowledge-digests/{digest_id}")
        assert resp.status_code == 401

    def test_missing_digest_returns_404(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/admin/knowledge-digests/kd_doesnotexist", headers=_auth(seeded_app["admin_token"])
        )
        assert resp.status_code == 404


class TestUpdateKnowledgeDigest:
    def _create(self, seeded_app, slug="update-digest"):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={"slug": slug, "title": "Before", "instructions": "before-i", "source_corpus_ids": []},
            headers=_auth(seeded_app["admin_token"]),
        )
        return resp.json()["id"]

    def test_admin_updates_title_and_instructions(self, seeded_app):
        digest_id = self._create(seeded_app)
        c = seeded_app["client"]
        resp = c.put(
            f"/api/admin/knowledge-digests/{digest_id}",
            json={"title": "After", "instructions": "after-i"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "After"
        assert body["instructions"] == "after-i"
        assert body["slug"] == "update-digest"

    def test_update_source_corpus_ids(self, seeded_app):
        digest_id = self._create(seeded_app, "update-sources")
        corpus_id = _create_collection(seeded_app, "Update Source")
        c = seeded_app["client"]
        resp = c.put(
            f"/api/admin/knowledge-digests/{digest_id}",
            json={"source_corpus_ids": [corpus_id]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source_corpus_ids"] == [corpus_id]

    def test_update_unknown_source_corpus_id_returns_400(self, seeded_app):
        digest_id = self._create(seeded_app, "update-bad-source")
        c = seeded_app["client"]
        resp = c.put(
            f"/api/admin/knowledge-digests/{digest_id}",
            json={"source_corpus_ids": ["col_nope"]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400

    def test_non_admin_returns_403(self, seeded_app):
        digest_id = self._create(seeded_app, "update-403")
        resp = seeded_app["client"].put(
            f"/api/admin/knowledge-digests/{digest_id}",
            json={"title": "Nope"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, seeded_app):
        digest_id = self._create(seeded_app, "update-401")
        resp = seeded_app["client"].put(
            f"/api/admin/knowledge-digests/{digest_id}",
            json={"title": "Nope"},
        )
        assert resp.status_code == 401

    def test_missing_digest_returns_404(self, seeded_app):
        resp = seeded_app["client"].put(
            "/api/admin/knowledge-digests/kd_doesnotexist",
            json={"title": "Nope"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404


class TestDeleteKnowledgeDigest:
    def _create(self, seeded_app, slug="delete-digest"):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/knowledge-digests",
            json={"slug": slug, "title": "Delete Me", "instructions": "i", "source_corpus_ids": []},
            headers=_auth(seeded_app["admin_token"]),
        )
        return resp.json()["id"]

    def test_admin_delete_then_404(self, seeded_app):
        digest_id = self._create(seeded_app)
        c = seeded_app["client"]
        del_resp = c.delete(f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["admin_token"]))
        assert del_resp.status_code == 204

        get_resp = c.get(f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["admin_token"]))
        assert get_resp.status_code == 404

    def test_delete_cleans_up_resource_grants(self, seeded_app):
        digest_id = self._create(seeded_app, "delete-with-grant")
        from src.repositories import resource_grants_repo, user_groups_repo

        conn = get_system_db()
        everyone = user_groups_repo().get_by_name("Everyone")
        resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="knowledge_digest",
            resource_id=digest_id,
            assigned_by="test",
        )
        conn.close()

        c = seeded_app["client"]
        resp = c.delete(f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 204

        remaining = resource_grants_repo().list_all(resource_type="knowledge_digest", group_id=everyone["id"])
        assert not [g for g in remaining if g["resource_id"] == digest_id]

    def test_non_admin_returns_403(self, seeded_app):
        digest_id = self._create(seeded_app, "delete-403")
        resp = seeded_app["client"].delete(
            f"/api/admin/knowledge-digests/{digest_id}", headers=_auth(seeded_app["analyst_token"])
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, seeded_app):
        digest_id = self._create(seeded_app, "delete-401")
        resp = seeded_app["client"].delete(f"/api/admin/knowledge-digests/{digest_id}")
        assert resp.status_code == 401

    def test_missing_digest_returns_404(self, seeded_app):
        resp = seeded_app["client"].delete(
            "/api/admin/knowledge-digests/kd_doesnotexist", headers=_auth(seeded_app["admin_token"])
        )
        assert resp.status_code == 404
