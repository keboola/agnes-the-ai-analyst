"""REST API tests for the semantic-model / semantic-source admin surface
and the public export + search endpoints (open semantic-layer contract,
Task 10).

RBAC model under test: admin CRUD lives at ``/api/admin/semantic-models``
and ``/api/admin/semantic-sources`` (``require_admin``); the export and
search endpoints live at the un-prefixed ``/api/semantic-models/*`` and are
gated on a grant instead — a direct ``semantic_model`` grant, or a grant on
any Data Package the model is linked to. That matches the spec's ownership
rule that a semantic model rides the same visibility as the tables/packages
it belongs to, without making the directly grantable resource type that
/admin/access offers a control nothing reads.
"""

from __future__ import annotations

import json

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


def _grant_model(model_id: str, group_name: str = "Direct Model Readers") -> None:
    """Grant the seeded analyst a direct grant on the MODEL, not on a package."""
    from src.repositories import resource_grants_repo, user_groups_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    group = user_groups_repo().create(name=group_name, description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    resource_grants_repo().create(
        group_id=gid,
        resource_type="semantic_model",
        resource_id=model_id,
        assigned_by="test",
    )


def test_export_succeeds_via_a_direct_model_grant(seeded_app):
    """A grant on the model itself must actually grant access.

    `ResourceType.SEMANTIC_MODEL` is registered, so /admin/access offers it as
    a grantable resource. A control that is offered but never read is worse
    than one that is absent — the admin believes access was given and it was
    not. This mirrors how per-table grants layer under the package stack.
    """
    c = seeded_app["client"]
    created = c.post(
        "/api/admin/semantic-models",
        json={"document": DOC},
        headers=_auth(seeded_app["admin_token"]),
    ).json()

    _grant_model(created["id"])

    r = c.get("/api/semantic-models/retail.yaml", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    assert r.text == DOC


# ---------------------------------------------------------------------------
# validate-query — wave 3 (query-validation engine wiring)
# ---------------------------------------------------------------------------


def _upsert_model_with_constraints(*, id: str = "manual/_/retail_vq", slug: str = "retail_vq") -> dict:
    """A ``status='valid'`` model with a checkable error-severity constraint
    on ``revenue``, an unverifiable constraint on the same metric (degrades
    to ``post_execution_checks``), and a metric (``mrr``) whose only declared
    dialect is neither DuckDB nor ANSI SQL (``locally_executable=false``).

    Written straight through the repo (like ``git_backed_model`` above) so
    the fixture can carry ``custom_extensions``/``expression.dialects``
    without fighting the vendored Ossie schema's exact shape for those
    provisional, storage-layer-owned fields.
    """
    import json as jsonlib

    from src.repositories import semantic_model_repo

    document_json = {
        "semantic_model": [
            {
                "name": slug,
                "datasets": [{"name": "orders", "source": "db.public.orders", "fields": [{"name": "region"}]}],
                "metrics": [
                    {
                        "name": "revenue",
                        "dataset": "orders",
                        "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
                    },
                    {
                        "name": "mrr",
                        "dataset": "orders",
                        "expression": {"dialects": [{"dialect": "snowflake", "expression": "SUM(mrr_amount)"}]},
                    },
                ],
                "custom_extensions": [
                    {
                        "vendor_name": "agnes",
                        "data": jsonlib.dumps(
                            {
                                "constraints": [
                                    {
                                        "name": "region_filter_required",
                                        "constraint_type": "required_filter",
                                        "rule": "region = 'EU'",
                                        "severity": "error",
                                        "metrics": ["revenue"],
                                    },
                                    {
                                        "name": "non_negative_value",
                                        "constraint_type": "value_range",
                                        "rule": "value >= 0",
                                        "severity": "warning",
                                        "metrics": ["revenue"],
                                    },
                                ]
                            }
                        ),
                    }
                ],
            }
        ]
    }
    return semantic_model_repo().upsert(
        id=id,
        slug=slug,
        name=slug,
        description=None,
        document="# native fixture, not schema-authored",
        document_json=document_json,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class TestValidateQuery:
    def test_no_semantic_model_is_gated_closed(self, seeded_app):
        """With zero valid models the endpoint must not offer a misleading
        all-clear — it returns an explicit 'unavailable' shape instead of
        `validate_query`'s empty-documents default (`valid: True`)."""
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT * FROM orders"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["error"] == "no_semantic_model"
        assert "valid" not in body

    def test_error_severity_violation_marks_invalid(self, seeded_app):
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT SUM(revenue) FROM orders"},  # no region filter -> violates the constraint
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["valid"] is False
        assert any(v["severity"] == "error" for v in body["violations"])

    def test_unverifiable_rule_is_a_post_execution_check_not_a_violation(self, seeded_app):
        """Fail-open: a rule this module cannot statically check must land in
        `post_execution_checks`, never flip `valid` to False by itself."""
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT SUM(revenue) FROM orders WHERE region = 'EU'"},
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert body["valid"] is True, "the checkable constraint is satisfied; only the unverifiable one remains"
        assert any(chk["name"] == "non_negative_value" for chk in body["post_execution_checks"])
        assert all(v["name"] != "non_negative_value" for v in body["violations"])

    def test_non_local_dialect_metric_is_not_locally_executable(self, seeded_app):
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT mrr FROM orders"},
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert body["locally_executable"] is False
        assert body["mixed_dialect_warning"] is None or "mrr" not in (body["mixed_dialect_warning"] or "")

    def test_rbac_matches_search_and_export(self, seeded_app):
        """Same tier as search/export: a non-admin with no grant sees the
        model as absent from the accessible set -> gated closed for them,
        even though it exists and is valid for an admin."""
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT SUM(revenue) FROM orders"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.json()["available"] is False

    def test_expected_objects_are_diffed_when_passed(self, seeded_app):
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.post(
            "/api/semantic-models/validate-query",
            json={
                "sql": "SELECT SUM(revenue) FROM orders WHERE region = 'EU'",
                "expected": [{"type": "metric", "name": "revenue"}, {"type": "metric", "name": "mrr"}],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert {"type": "metric", "name": "revenue"} in body["matched_expected_objects"]
        assert {"type": "metric", "name": "mrr"} in body["missing_expected_objects"]


# ---------------------------------------------------------------------------
# get_semantic_context / get_semantic_schema — wave 4 (agent read parity)
# ---------------------------------------------------------------------------


def _selections(*selections: dict) -> str:
    return json.dumps(list(selections))


class TestGetSemanticContext:
    def test_admin_sees_all_objects_compactly_by_default(self, seeded_app):
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": _selections({"semantic_type": "dataset"})},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["unknown_types"] == []
        entry = body["results"][0]
        assert entry["semantic_type"] == "dataset"
        assert entry["mode"] == "compact"
        assert {o["name"] for o in entry["objects"]} == {"orders"}
        assert set(entry["objects"][0].keys()) == {"name", "summary", "model"}

    def test_explicit_ids_return_full_attributes(self, seeded_app):
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": _selections({"semantic_type": "metric", "ids": ["revenue"]})},
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        entry = body["results"][0]
        assert entry["mode"] == "full"
        obj = entry["objects"][0]
        assert obj["name"] == "revenue"
        assert "expression" in obj

    def test_model_the_caller_cannot_reach_is_absent(self, seeded_app):
        """RBAC: a model with no grant for the caller contributes no objects,
        same tier as search/export/validate-query."""
        _upsert_model_with_constraints()
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": _selections({"semantic_type": "dataset"})},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200
        assert r.json()["results"][0]["objects"] == []

    def test_model_ids_restricts_to_named_models(self, seeded_app):
        _upsert_model_with_constraints(id="manual/_/other", slug="other_model")
        _upsert_model_with_constraints()  # slug=retail_vq
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={
                "selections": _selections({"semantic_type": "dataset"}),
                "model_ids": ["retail_vq"],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        objects = r.json()["results"][0]["objects"]
        assert {o["model"] for o in objects} == {"retail_vq"}

    def test_unknown_semantic_type_is_reported(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": _selections({"semantic_type": "glossary"})},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["unknown_types"] == ["glossary"]

    def test_malformed_selections_json_is_400(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": "not-json"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400

    def test_selections_must_be_a_json_list(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/context",
            params={"selections": json.dumps({"semantic_type": "dataset"})},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400


class TestGetSemanticSchema:
    def test_returns_ref_and_defs(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/schema",
            params={"semantic_types": ["dataset", "metric"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["types"]["dataset"] == {"$ref": "#/$defs/Dataset"}
        assert "Dataset" in body["$defs"]
        assert "Metric" in body["$defs"]

    def test_not_gated_on_any_semantic_model_existing(self, seeded_app):
        """Any authenticated user can read the schema even with zero
        semantic models registered — it is not model data, it's the
        contract every model is validated against."""
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/schema",
            params={"semantic_types": ["relationship"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200
        assert "Relationship" in r.json()["$defs"]

    def test_unknown_type_is_reported(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/api/semantic-models/schema",
            params={"semantic_types": ["glossary"]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["unknown_types"] == ["glossary"]
