"""REST API tests for the semantic-model / semantic-source admin surface
and the public export + search endpoints (open semantic-layer contract,
Task 10).

RBAC model under test: admin CRUD lives at ``/api/admin/semantic-models``
and ``/api/admin/semantic-sources`` (``require_admin``); the export and
search endpoints live at the un-prefixed ``/api/semantic-models/*`` and are
gated on the linked data package's grant instead — any authenticated user
who can reach a Data Package the model is linked to can read it, matching
the spec's ownership rule that a semantic model rides the same visibility
as the tables/packages it belongs to.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db

# NOTE: the plan's own inline fixture for this test
# ("version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n# trailing comment\n")
# is schema-INVALID against the vendored Ossie schema — `datasets` is a
# required property per model (`minItems: 1`, per Task 7's own
# `_stub_dataset()` note). Posting it 422s instead of 201ing, so every
# "valid document" scenario in Task 10 needs a real `datasets` entry.
DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
    "# trailing comment\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_package() -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        name="Semantic Pkg", slug="semantic-pkg", description=None, icon=None, color=None, created_by="test"
    )


def _grant_package(pkg_id: str, group_name: str = "Semantic Readers") -> None:
    """Create a group, add the seeded analyst to it, grant the group access
    to ``pkg_id``. Mirrors the pattern in
    ``tests/test_api_knowledge_digests_distribution.py`` — ``seeded_app``
    users are not auto-members of Everyone.
    """
    from src.repositories import resource_grants_repo, user_groups_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    group = user_groups_repo().create(name=group_name, description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    resource_grants_repo().create(
        group_id=gid,
        resource_type="data_package",
        resource_id=pkg_id,
        assigned_by="test",
    )


@pytest.fixture
def uploaded_model(seeded_app):
    """A model created directly through the admin API (``source='manual'``)
    — the ownership rule leaves it editable."""
    c = seeded_app["client"]
    r = c.post(
        "/api/admin/semantic-models",
        json={"document": DOC},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def git_backed_model(seeded_app):
    """A model as it would land via a registered git-kind semantic source —
    read-only through the API by the ownership rule."""
    from src.repositories import semantic_model_repo

    repo = semantic_model_repo()
    doc = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: finance\n"
    return repo.upsert(
        id="ossie_git/src1/finance",
        slug="finance",
        name="finance",
        description=None,
        document=doc,
        document_json={"semantic_model": [{"name": "finance"}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source="ossie_git",
        source_ref="src1",
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class TestAdminRbac:
    def test_admin_endpoints_reject_non_admin(self, seeded_app):
        c = seeded_app["client"]
        for method, path in [
            ("get", "/api/admin/semantic-models"),
            ("post", "/api/admin/semantic-sources"),
        ]:
            r = getattr(c, method)(path, headers=_auth(seeded_app["analyst_token"]))
            assert r.status_code == 403


class TestSemanticModelCrud:
    def test_create_then_list(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["slug"] == "retail"
        assert body["source"] == "manual"

        listed = c.get("/api/admin/semantic-models", headers=_auth(seeded_app["admin_token"]))
        assert listed.status_code == 200
        assert isinstance(listed.json(), list)
        assert any(m["slug"] == "retail" for m in listed.json())

    def test_posting_an_invalid_document_returns_422_with_the_schema_errors(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-models",
            json={"document": "semantic_model: [oops"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["errors"]

    def test_get_missing_model_is_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get("/api/admin/semantic-models/nope", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_a_source_owned_model_cannot_be_edited_through_the_api(self, seeded_app, git_backed_model):
        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/semantic-models/{git_backed_model['id']}",
            json={"name": "renamed"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 409
        body = r.json()["detail"]
        assert body["code"] == "source_owned"
        assert "git" in body["message"], "the error must name where to go and edit it"

    def test_an_uploaded_model_remains_editable(self, seeded_app, uploaded_model):
        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/semantic-models/{uploaded_model['id']}",
            json={"name": "renamed"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["name"] == "renamed"

    def test_delete_model(self, seeded_app, uploaded_model):
        c = seeded_app["client"]
        r = c.delete(
            f"/api/admin/semantic-models/{uploaded_model['id']}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 204
        assert (
            c.get(
                f"/api/admin/semantic-models/{uploaded_model['id']}",
                headers=_auth(seeded_app["admin_token"]),
            ).status_code
            == 404
        )


class TestSemanticSourceCrud:
    def test_create_list_get_update_delete(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-sources",
            json={
                "kind": "git",
                "name": "Finance models",
                "adapter": "native",
                "config": {"repo_url": "https://example.com/x.git", "glob": "semantic/**/*.yaml"},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201
        source_id = r.json()["id"]
        assert r.json()["enabled"] is True

        listed = c.get("/api/admin/semantic-sources", headers=_auth(seeded_app["admin_token"]))
        assert any(s["id"] == source_id for s in listed.json())

        got = c.get(f"/api/admin/semantic-sources/{source_id}", headers=_auth(seeded_app["admin_token"]))
        assert got.status_code == 200
        assert got.json()["kind"] == "git"

        updated = c.put(
            f"/api/admin/semantic-sources/{source_id}",
            json={"enabled": False},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False

        deleted = c.delete(f"/api/admin/semantic-sources/{source_id}", headers=_auth(seeded_app["admin_token"]))
        assert deleted.status_code == 204

    def test_sync_unknown_source_is_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.post("/api/admin/semantic-sources/nope/sync", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_sync_upload_source(self, seeded_app):
        """A ``kind='upload'`` source's ``config.documents`` is imported directly."""
        c = seeded_app["client"]
        r = c.post(
            "/api/admin/semantic-sources",
            json={
                "kind": "upload",
                "name": "Manual bundle",
                "adapter": "native",
                "config": {"documents": [DOC]},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        source_id = r.json()["id"]

        synced = c.post(f"/api/admin/semantic-sources/{source_id}/sync", headers=_auth(seeded_app["admin_token"]))
        assert synced.status_code == 200, synced.text
        report = synced.json()
        assert report["models_written"] == 1

        row = c.get(f"/api/admin/semantic-sources/{source_id}", headers=_auth(seeded_app["admin_token"]))
        assert row.json()["last_sync_status"] == "ok"


class TestExport:
    def test_export_returns_the_stored_document_byte_for_byte(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        )
        r = c.get("/api/semantic-models/retail.yaml", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert r.text == DOC, "export must not re-serialize; comments and key order survive"

    def test_export_missing_model_is_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get("/api/semantic-models/nope.yaml", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_export_is_not_admin_only_but_still_gated(self, seeded_app):
        """A non-admin with no grant on any package the model belongs to is
        denied — export is resource-gated, not merely 'any authenticated
        user'."""
        c = seeded_app["client"]
        c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        )
        r = c.get("/api/semantic-models/retail.yaml", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_export_succeeds_via_a_linked_package_grant(self, seeded_app):
        """The point of the junction: a non-admin who can access a Data
        Package the model is linked to can export it without an admin
        token."""
        c = seeded_app["client"]
        from src.repositories import semantic_model_repo

        created = c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        ).json()
        pkg_id = _make_package()
        _grant_package(pkg_id)
        semantic_model_repo().link_package(pkg_id, created["id"])

        r = c.get("/api/semantic-models/retail.yaml", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert r.text == DOC


class TestSearch:
    def test_search_only_returns_accessible_models(self, seeded_app):
        c = seeded_app["client"]
        from src.repositories import semantic_model_repo

        created = c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        ).json()

        # Not yet linked to any package the analyst can reach.
        miss = c.get(
            "/api/semantic-models/search",
            params={"q": "retail"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert miss.status_code == 200
        assert miss.json()["count"] == 0

        pkg_id = _make_package()
        _grant_package(pkg_id)
        semantic_model_repo().link_package(pkg_id, created["id"])

        hit = c.get(
            "/api/semantic-models/search",
            params={"q": "retail"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert hit.status_code == 200
        assert hit.json()["count"] == 1
        assert hit.json()["models"][0]["slug"] == "retail"

    def test_admin_search_sees_everything(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/admin/semantic-models",
            json={"document": DOC},
            headers=_auth(seeded_app["admin_token"]),
        )
        r = c.get(
            "/api/semantic-models/search",
            params={"q": "retail"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1
