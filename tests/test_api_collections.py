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

    def test_whitespace_only_slug_falls_back_to_auto_slug(self, seeded_app):
        # A whitespace-only explicit slug is truthy; it must not survive as an
        # empty slug (unreachable via /library/{slug} + bogus 409 collisions).
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Whitespace Slug", "slug": "   "},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        slug = resp.json()["slug"]
        assert slug.strip()  # non-empty, non-whitespace
        assert slug == "whitespace-slug"

    def test_explicit_slug_normalised_to_url_safe(self, seeded_app):
        # An admin-provided slug with URL-unsafe chars must be normalised so it
        # resolves via /library/{slug} (path params don't consume "/").
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Has Slashes", "slug": "my/collection path"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["slug"] == "my-collection-path"

    def test_auto_slug_no_trailing_hyphen_after_truncation(self):
        # The [:100] cap runs after strip("-"); a name whose 100th char lands on
        # a word boundary would otherwise leave a trailing hyphen.
        from app.api.collections import _auto_slug

        slug = _auto_slug("a" * 99 + " " + "b" * 50)
        assert len(slug) <= 100
        assert not slug.endswith("-")
        assert slug == "a" * 99


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


class TestSearch:
    def _seed_corpus_with_chunk(self, seeded_app, name, text, *, grant):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        if grant:
            _seed_collection_grant(corpus_id, "analyst1")
        from src.repositories import corpus_chunks_repo, corpus_files_repo

        fid = corpus_files_repo().add(
            corpus_id=corpus_id,
            filename="d.txt",
            sha256="s",
            file_type="txt",
            size_bytes=1,
            storage_path="/x",
        )
        corpus_chunks_repo().add_many([{"corpus_id": corpus_id, "file_id": fid, "ordinal": 0, "text": text}])
        return corpus_id

    def test_member_searches_accessible_collection(self, seeded_app):
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Searchable", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert any("magic" in (r.get("text") or "") for r in results)
        assert results[0]["filename"] == "d.txt"

    def test_search_results_carry_confidence(self, seeded_app):
        """#756: the calibrated confidence label from retrieval.search()
        must pass through the API response unchanged."""
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Confident", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results
        assert results[0]["confidence"] in ("high", "medium", "low")

    def test_search_fail_closed_excludes_ungranted(self, seeded_app):
        c = seeded_app["client"]
        # Collection is NOT granted to analyst1.
        self._seed_corpus_with_chunk(seeded_app, "Private", "the magic keyword appears here", grant=False)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        # Fail-closed: an analyst with no grant sees nothing from it.
        assert resp.json()["results"] == []

    def test_admin_search_sees_all(self, seeded_app):
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "AdminSee", "the magic keyword appears here", grant=False)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert any("magic" in (r.get("text") or "") for r in resp.json()["results"])


def test_delete_file_removes_its_chunks(seeded_app):
    """Regression: deleting a file must also remove its corpus_chunks, so they
    don't linger in search results with a null filename."""
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    c = seeded_app["client"]
    cid = c.post("/api/collections", json={"name": "Del Chunks"}, headers=_auth(seeded_app["admin_token"])).json()["id"]
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename="d.txt",
        sha256="s",
        file_type="txt",
        size_bytes=1,
        storage_path=None,
    )
    corpus_chunks_repo().add_many([{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "hello world"}])
    assert len(corpus_chunks_repo().list_for_file(fid)) == 1

    r = c.delete(f"/api/collections/{cid}/files/{fid}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 204, r.text
    assert corpus_chunks_repo().list_for_file(fid) == []


def test_create_collection_non_alphanumeric_name_gets_fallback_slug(seeded_app):
    """A name with no alphanumerics must not yield an empty slug."""
    c = seeded_app["client"]
    r = c.post("/api/collections", json={"name": "!!!"}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    assert r.json()["slug"]  # non-empty (falls back to "collection")


def test_delete_tabular_file_purges_table_registry_row(seeded_app):
    """Deleting a tabular file must remove its derived table_registry row so
    it no longer appears in agnes catalog."""
    import io

    from src.repositories import table_registry_repo

    c = seeded_app["client"]
    cr = c.post(
        "/api/collections",
        json={"name": "Tabular Purge"},
        headers=_auth(seeded_app["admin_token"]),
    )
    corpus_id = cr.json()["id"]
    _seed_collection_grant(corpus_id, "analyst1")

    up = c.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("data.csv", io.BytesIO(b"x,y\n1,2\n3,4\n"), "text/csv")},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert up.status_code == 201, up.text
    file_id = up.json()[0]["file_id"]

    # After ingestion the table_registry must contain a derived row for this corpus.
    rows_before = table_registry_repo().list_by_source("collection")
    corpus_rows_before = [r for r in rows_before if r.get("bucket") == corpus_id]
    assert len(corpus_rows_before) == 1, "Expected one derived table_registry row after tabular ingest"

    # Delete the file — must cascade to the derived table_registry row.
    del_resp = c.delete(
        f"/api/collections/{corpus_id}/files/{file_id}",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert del_resp.status_code == 204, del_resp.text

    rows_after = table_registry_repo().list_by_source("collection")
    corpus_rows_after = [r for r in rows_after if r.get("bucket") == corpus_id]
    assert corpus_rows_after == [], "Derived table_registry row must be purged on file delete"


def test_delete_collection_purges_all_derived_table_registry_rows(seeded_app):
    """Soft-deleting a collection must also purge all derived table_registry
    rows so the tables no longer appear in agnes catalog."""
    import io

    from src.repositories import table_registry_repo

    c = seeded_app["client"]
    cr = c.post(
        "/api/collections",
        json={"name": "Collection Cascade Purge"},
        headers=_auth(seeded_app["admin_token"]),
    )
    corpus_id = cr.json()["id"]
    _seed_collection_grant(corpus_id, "analyst1")

    # Upload two tabular files so we get two derived registry rows.
    for name, content in [("a.csv", b"a,b\n1,2"), ("b.csv", b"c,d\n3,4")]:
        up = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": (name, io.BytesIO(content), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert up.status_code == 201, up.text

    rows_before = [r for r in table_registry_repo().list_by_source("collection") if r.get("bucket") == corpus_id]
    assert len(rows_before) == 2, f"Expected 2 derived rows, got {len(rows_before)}"

    # Delete the collection — cascade must purge all derived rows.
    del_resp = c.delete(
        f"/api/collections/{corpus_id}",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert del_resp.status_code == 204, del_resp.text

    rows_after = [r for r in table_registry_repo().list_by_source("collection") if r.get("bucket") == corpus_id]
    assert rows_after == [], "All derived table_registry rows must be purged on collection delete"


def test_reingest_resets_status_and_reruns(seeded_app, tmp_path):
    """needs_review file + fixed content -> reingest -> indexed."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ri", slug="ri", description=None, created_by="u1")
    csv = tmp_path / "d.csv"
    csv.write_text("a,b\n", encoding="utf-8")  # header-only -> needs_review
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="d.csv",
        sha256="s",
        file_type="csv",
        size_bytes=csv.stat().st_size,
        storage_path=str(csv),
    )
    from src.ingest.runner import ingest_file

    assert ingest_file(fid) == "needs_review"

    csv.write_text("a,b\n1,2\n", encoding="utf-8")  # operator fixes the file
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 202, r.text
    assert r.json()["processing_status"] == "pending"

    # TestClient runs BackgroundTasks synchronously after the response — by now ingest re-ran.
    assert corpus_files_repo().get(fid)["processing_status"] == "indexed"


def test_reingest_404_on_missing_file(seeded_app):
    from src.repositories import file_corpora_repo

    col_a = file_corpora_repo().create(name="ria", slug="ria", description=None, created_by="u1")
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_a}/files/cf_nonexistent/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


def test_reingest_404_when_file_belongs_to_other_collection(seeded_app):
    """A file that exists but belongs to a different collection must 404,
    not be re-ingested through the wrong collection's endpoint."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_a = file_corpora_repo().create(name="ria2", slug="ria2", description=None, created_by="u1")
    col_b = file_corpora_repo().create(name="rib2", slug="rib2", description=None, created_by="u1")
    fid = corpus_files_repo().add(
        corpus_id=col_a,
        filename="x.csv",
        sha256="s",
        file_type="csv",
        size_bytes=1,
        storage_path=None,
    )
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_b}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


def test_reingest_409_while_run_in_flight(seeded_app):
    """A file already in 'processing' must reject reingest with 409 and keep
    its status untouched (no purge/reset) — guards against duplicate racing
    ingest runs from a second admin tab or a direct API caller."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ric", slug="ric", description=None, created_by="u1")
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="busy.csv",
        sha256="s",
        file_type="csv",
        size_bytes=1,
        storage_path=None,
    )
    corpus_files_repo().set_status(fid, status="processing", detail={"reason": "ingest running"})

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "reingest_in_progress"

    row = corpus_files_repo().get(fid)
    assert row["processing_status"] == "processing"  # no reset happened
    assert row["processing_detail"] == {"reason": "ingest running"}  # detail untouched


def test_reingest_stale_processing_is_recoverable(seeded_app, tmp_path):
    """A 'processing' row whose updated_at predates the staleness threshold
    must be treated as crash-abandoned, not in-flight, so reingest proceeds
    (202) instead of 409 — otherwise a crash mid-ingest would permanently
    block the only recovery path for the stuck row."""
    from datetime import datetime, timedelta, timezone

    from app.api.collections import REINGEST_STALE_PROCESSING_MINUTES
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ris", slug="ris", description=None, created_by="u1")
    csv = tmp_path / "s.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="s.csv",
        sha256="s",
        file_type="csv",
        size_bytes=csv.stat().st_size,
        storage_path=str(csv),
    )
    corpus_files_repo().set_status(fid, status="processing", detail={"reason": "ingest running"})

    # Backdate updated_at past the threshold — simulates a crash mid-ingest,
    # where the row never got a chance to move past 'processing'.
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=REINGEST_STALE_PROCESSING_MINUTES + 5)
    conn = get_system_db()
    conn.execute(
        "UPDATE corpus_files SET updated_at = ? WHERE id = ?",
        [stale_at.replace(tzinfo=None), fid],
    )
    conn.close()

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 202, r.text
    assert r.json()["processing_status"] == "pending"

    # TestClient runs BackgroundTasks synchronously after the response — by now ingest re-ran.
    assert corpus_files_repo().get(fid)["processing_status"] == "indexed"


class TestBundleUpload:
    """K1 — zip upload unpacks into ingested child rows."""

    def _create_and_grant(self, seeded_app, name: str = "Bundle Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_upload_zip_bundle_end_to_end(self, seeded_app):
        import zipfile

        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("notes.md", "# Notes\n\nBundle ingestion works end to end.")
            zf.writestr("junk.dwg", "binary")
        buf.seek(0)

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("dump.zip", buf, "application/zip")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        # zip itself accepted; member-level rejection ≠ upload rejection.
        assert resp.status_code == 201, resp.text

        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        by_name = {f["filename"]: f for f in listing.json()["files"]}
        archive = by_name["dump.zip"]
        assert archive["processing_status"] == "indexed"
        assert archive["parent_file_id"] is None
        assert archive["processing_detail"]["kind"] == "bundle"
        assert archive["processing_detail"]["children"] == 2
        assert by_name["notes.md"]["processing_status"] == "indexed"
        assert by_name["notes.md"]["parent_file_id"] == archive["file_id"]
        assert by_name["junk.dwg"]["processing_status"] == "rejected"

        # Bundle content is searchable like any directly-uploaded document.
        hits = c.get(
            "/api/collections/search",
            params={"q": "bundle ingestion works", "corpus_id": corpus_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert hits.status_code == 200
        assert any("notes.md" in str(h) for h in hits.json()["results"])
