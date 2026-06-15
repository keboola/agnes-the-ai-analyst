"""Tests for /api/collections — Collections Slice 2 (Upload).

Covers:
- Admin creates a collection (201); non-admin gets 403.
- Unauthenticated request gets 401.
- Admin GET list returns the collection; non-member analyst gets empty list.
- RBAC-granted member can GET collection detail; non-member gets 403.
- Member uploads a tier1 file → 200, processing_status='pending'.
- Member uploads a .dwg file → 422, processing_status='rejected'.
- Non-member file upload → 403.
- GET /files for collection lists the uploaded file with correct status.
- Admin soft-deletes collection → 204; then 404 on GET.
"""

from __future__ import annotations

import io


from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_collection_grant(corpus_id: str, user_id: str) -> None:
    """Give ``user_id`` access to the collection.

    Group membership is no longer implicit — ``_user_group_ids``
    (app/auth/access.py) returns only concrete ``user_group_members`` rows, so
    a user is in Everyone only if a real membership row exists (in production
    that row comes from google_sync/system_seed). The seeded_app fixture only
    seeds the admin's membership, so we must add ``user_id`` to Everyone here
    before the Everyone→collection grant has any effect.
    """
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("Everyone")
    assert grp, "Everyone group must be seeded"
    members = UserGroupMembersRepository(conn)
    if grp["id"] not in set(members.list_groups_for_user(user_id)):
        members.add_member(user_id, grp["id"], source="system_seed")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "collection", corpus_id):
        grants.create(
            group_id=grp["id"],
            resource_type="collection",
            resource_id=corpus_id,
            assigned_by="test",
        )
    conn.close()


class TestCreateCollection:
    def test_admin_creates_collection(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Test Corp", "description": "test corpus"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "id" in body
        assert body["name"] == "Test Corp"
        assert body["id"].startswith("col_")

    def test_non_admin_create_returns_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Forbidden"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unauthenticated_create_returns_401(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/collections", json={"name": "Anon"})
        assert resp.status_code == 401

    def test_slug_collision_returns_409(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Dupe", "slug": "dupe-slug"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.post(
            "/api/collections",
            json={"name": "Dupe Again", "slug": "dupe-slug"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409

    def test_auto_slug_generated_from_name(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "My Auto Slug Collection"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "slug" in body
        assert body["slug"]  # non-empty


class TestListCollections:
    def test_admin_sees_all_collections(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Visible Col"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.get("/api/collections", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        ids = [col["id"] for col in resp.json()["items"]]
        assert len(ids) >= 1

    def test_non_member_analyst_sees_empty_list(self, seeded_app):
        """Analyst with no grants sees zero collections (fail-closed)."""
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Hidden"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        # analyst1 has no grant — list must be empty (RBAC-filtered)
        assert resp.json()["items"] == []

    def test_granted_member_sees_collection(self, seeded_app):
        c = seeded_app["client"]
        create_resp = c.post(
            "/api/collections",
            json={"name": "Granted Col"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = create_resp.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        resp = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        ids = [col["id"] for col in resp.json()["items"]]
        assert corpus_id in ids

    def test_unauthenticated_list_returns_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/collections")
        assert resp.status_code == 401


class TestGetCollection:
    def test_admin_gets_collection_detail(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Detail Test"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == corpus_id
        assert "files" in body

    def test_non_member_gets_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Members Only"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_granted_member_gets_detail(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Member Detail"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200

    def test_missing_collection_returns_404(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/collections/col_doesnotexist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404


class TestDeleteCollection:
    def test_admin_soft_deletes(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "To Delete"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        del_resp = c.delete(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert del_resp.status_code == 204
        # Subsequent GET returns 404
        get_resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert get_resp.status_code == 404

    def test_non_admin_delete_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Protected"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.delete(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestFileUpload:
    def _create_and_grant(self, seeded_app, name: str = "Upload Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_member_uploads_tier1_file(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Tier1 Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("notes.txt", io.BytesIO(b"hello world"), "text/plain")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        results = resp.json()
        assert len(results) == 1
        assert results[0]["processing_status"] == "pending"
        assert results[0]["filename"] == "notes.txt"
        assert "file_id" in results[0]

    def test_upload_triggers_background_ingestion(self, seeded_app):
        """A tabular upload kicks off ingestion; a follow-up GET shows it
        indexed (TestClient runs BackgroundTasks before the POST returns)."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Ingest Trigger")
        up = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("metrics.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert up.status_code == 201, up.text
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        files = listing.json()["files"]
        assert files[0]["processing_status"] == "indexed"
        assert files[0]["processing_detail"]["kind"] == "tabular"

    def test_member_uploads_unsupported_type_returns_422_rejected(self, seeded_app):
        """DWG file → 422 response but file row persisted with status='rejected'."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Reject Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("blueprint.dwg", io.BytesIO(b"binary data"), "application/octet-stream")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 422, resp.text
        results = resp.json()
        assert len(results) == 1
        assert results[0]["processing_status"] == "rejected"
        assert results[0]["filename"] == "blueprint.dwg"

    def test_non_member_upload_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "No Access"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        # analyst1 has NO grant on this collection
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("doc.pdf", io.BytesIO(b"pdf bytes"), "application/pdf")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unauthenticated_upload_returns_401(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Anon Upload"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("doc.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code == 401

    def test_mixed_upload_returns_422_with_all_results(self, seeded_app):
        """One valid + one rejected file in a single multipart request."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Mixed Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("valid.pdf", io.BytesIO(b"pdf content"), "application/pdf")),
                ("files", ("bad.exe", io.BytesIO(b"exe bytes"), "application/octet-stream")),
            ],
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Any rejected file → 422 for the whole request
        assert resp.status_code == 422
        results = resp.json()
        assert len(results) == 2
        statuses = {r["filename"]: r["processing_status"] for r in results}
        assert statuses["valid.pdf"] == "pending"
        assert statuses["bad.exe"] == "rejected"


class TestListFiles:
    def test_member_lists_uploaded_files(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "List Files"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        # Upload a file first
        c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("data.csv", io.BytesIO(b"a,b\n1,2"), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )

        resp = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert len(files) >= 1
        assert any(f["filename"] == "data.csv" for f in files)

    def test_non_member_list_files_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File List Guard"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestDeleteFile:
    def test_member_deletes_file(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File Del"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        upload_resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("to_del.txt", io.BytesIO(b"bye"), "text/plain")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        file_id = upload_resp.json()[0]["file_id"]

        del_resp = c.delete(
            f"/api/collections/{corpus_id}/files/{file_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert del_resp.status_code == 204

    def test_non_member_file_delete_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File Del Guard"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.delete(
            f"/api/collections/{corpus_id}/files/cf_fakeid",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


def test_list_collections_session_principal_filters_without_crash(seeded_app):
    """Regression: a co-session ``SessionPrincipal`` caller must not crash on
    ``user['id']`` (it is not subscriptable) and must be RBAC-filtered to its
    intersection — not see every collection.
    """
    import asyncio

    from app.api.collections import list_collections
    from app.auth.session_principal import SessionPrincipal
    from src.repositories import file_corpora_repo

    repo = file_corpora_repo()
    granted = repo.create(name="SP Granted", slug="sp-granted", description=None, created_by="admin1")
    other = repo.create(name="SP Other", slug="sp-other", description=None, created_by="admin1")

    principal = SessionPrincipal(
        "chat_sp",
        ["analyst1"],
        ["analyst@test.com"],
        {"collection": frozenset({granted})},
    )
    result = asyncio.run(list_collections(user=principal))
    ids = {c["id"] for c in result["items"]}
    assert granted in ids
    assert other not in ids
