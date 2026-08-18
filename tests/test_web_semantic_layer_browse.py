"""Read-only browse UI for the semantic layer (wave 4.2 of the 2026-08-14
UI/agent-parity design) — three levels: model list (`/semantic-layer`),
model detail (`/semantic-layer/{slug}?tab=`), object detail
(`/semantic-layer/{slug}/{object_id}`).

RBAC tier mirrors the rest of the read surface in
``app/api/semantic_models.py`` (``tests/test_semantic_models_api.py``): any
authenticated user, filtered through ``_can_read_model`` (a Data Package
grant or a direct ``semantic_model`` grant) — never admin-only, and never a
write affordance anywhere on these three pages (editing is a later
increment).
"""

from __future__ import annotations

import json

from src.db import get_system_db

_SLUG = "retail"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _document_json(slug: str = _SLUG) -> dict:
    return {
        "semantic_model": [
            {
                "name": slug,
                "description": "Retail domain: orders and customers.",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "db.public.orders",
                        "primary_key": ["order_id"],
                        "description": "Order lines.",
                        "ai_context": {
                            "instructions": "Prefer this dataset for post-checkout analysis.",
                            "synonyms": ["sales orders"],
                            "keywords": ["orders", "revenue"],
                            "anti_keywords": ["refund", "return"],
                            "hints": ["Join via customer_id"],
                            "warnings": ["Excludes refunds"],
                        },
                        "fields": [
                            {"name": "order_id", "datatype": "String", "description": "Primary key of orders."},
                            {
                                "name": "order_date",
                                "datatype": "Date",
                                "dimension": {"is_time": True},
                                "description": "Order date.",
                            },
                            {"name": "region", "datatype": "String", "description": "Sales region."},
                        ],
                    },
                    {"name": "customers", "source": "db.public.customers", "fields": []},
                ],
                "metrics": [
                    {
                        "name": "revenue",
                        "description": "Total order revenue.",
                        "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
                        "custom_extensions": [{"vendor_name": "agnes", "data": json.dumps({"dataset": "orders"})}],
                    }
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "orders",
                        "to": "customers",
                        "from_columns": ["customer_id"],
                        "to_columns": ["customer_id"],
                    }
                ],
                "custom_extensions": [
                    {
                        "vendor_name": "agnes",
                        "data": json.dumps(
                            {
                                "constraints": [
                                    {
                                        "name": "region_filter_required",
                                        "constraint_type": "required_filter",
                                        "rule": "region = 'EU'",
                                        "severity": "error",
                                        "metrics": ["revenue"],
                                    }
                                ],
                                "glossary": [
                                    {
                                        "term": "ARR",
                                        "definition": "Annual recurring revenue.",
                                        "see_also": ["MRR"],
                                    }
                                ],
                            }
                        ),
                    }
                ],
            }
        ]
    }


def _seed_model(
    *,
    id: str = f"manual/_/{_SLUG}",
    slug: str = _SLUG,
    source: str = "manual",
    status: str = "valid",
    validation_errors=None,
) -> dict:
    """Written straight through the repo (like ``test_semantic_models_api.py``
    ``_upsert_model_with_constraints``) so the fixture can carry
    ``custom_extensions``/``ai_context`` extras without fighting the vendored
    Ossie schema's exact upload shape for those provisional fields."""
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=id,
        slug=slug,
        name=slug,
        description="Retail domain: orders and customers.",
        document="# fixture, not schema-authored",
        document_json=_document_json(slug) if status != "invalid" else None,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=None,
        status=status,
        validation_errors=validation_errors,
        validated_at=None,
    )


def _grant_model(model_id: str, group_name: str = "Semantic Model Readers") -> None:
    """Mirrors ``test_semantic_models_api.py``'s ``_grant_model`` — a direct
    grant on the model, the narrowest RBAC path this UI's tests need."""
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


class TestModelList:
    def test_list_shows_counts_and_source_badge(self, seeded_app):
        _seed_model(source="manual")
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert "retail" in body
        # Object counts per type: 2 datasets, 1 metric, 1 constraint,
        # 1 relationship, 1 glossary term.
        assert "2 datasets" in body
        assert "1 metric<" in body or "1 metric " in body
        assert "1 constraint" in body
        assert "1 relationship" in body
        assert "1 glossary term" in body
        # Native (source='manual') carries no "Imported from" badge.
        assert "Imported from" not in body

    def test_imported_model_carries_the_source_badge(self, seeded_app):
        _seed_model(id="manual/_/kb", slug="kb_retail", source="keboola_metastore")
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Imported from Keboola" in r.text

    def test_invalid_model_renders_stored_errors_not_silently(self, seeded_app):
        _seed_model(
            id="manual/_/broken",
            slug="broken_model",
            status="invalid",
            validation_errors=["datasets: this document has no datasets"],
        )
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "broken_model" in r.text
        assert "Invalid" in r.text
        assert "this document has no datasets" in r.text

    def test_non_admin_without_a_grant_does_not_see_the_model(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "retail" not in r.text

    def test_non_admin_with_a_direct_grant_sees_the_model(self, seeded_app):
        row = _seed_model()
        _grant_model(row["id"])
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "retail" in r.text


class TestModelDetail:
    def test_each_tab_renders(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        for tab, needle in [
            ("datasets", "orders"),
            ("metrics", "revenue"),
            ("constraints", "region_filter_required"),
            ("relationships", "orders_to_customers"),
            ("glossary", "ARR"),
        ]:
            r = c.get(f"/semantic-layer/{_SLUG}?tab={tab}", headers=_auth(seeded_app["admin_token"]))
            assert r.status_code == 200, tab
            assert needle in r.text, f"tab={tab} did not render {needle!r}"

    def test_default_tab_is_datasets(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "orders" in r.text

    def test_unknown_slug_is_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get("/semantic-layer/does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_non_admin_without_a_grant_gets_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404

    def test_cross_link_from_dataset_row_to_metrics_tab(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=datasets", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "tab=metrics&amp;q=orders" in r.text

    def test_q_prefilters_the_active_tab(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=datasets&q=customers", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "customers" in r.text
        assert ">orders<" not in r.text


class TestObjectDetail:
    def test_dataset_object_renders_fields_table_and_all_five_ai_groups(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        # Fields table: name, type, role, description.
        assert "order_id" in body
        assert "order_date" in body
        assert "Primary key" in body
        assert "Time dimension" in body
        assert "Primary key of orders." in body
        # All five AI groups, including the negative signal.
        assert "Keywords" in body
        assert "Synonyms" in body
        assert "Anti-keywords" in body
        assert "Hints" in body
        assert "Warnings" in body
        assert "refund" in body  # an anti_keywords value actually rendered
        assert "sales orders" in body  # a synonyms value
        assert "Join via customer_id" in body  # a hints value
        assert "Excludes refunds" in body  # a warnings value

    def test_anti_keywords_group_renders_even_when_empty(self, seeded_app):
        """The negative signal must render as an empty group, not vanish,
        when a document declares no anti_keywords."""
        from src.repositories import semantic_model_repo

        doc = _document_json("no_anti")
        del doc["semantic_model"][0]["datasets"][0]["ai_context"]["anti_keywords"]
        semantic_model_repo().upsert(
            id="manual/_/no_anti",
            slug="no_anti",
            name="no_anti",
            description=None,
            document="# fixture",
            document_json=doc,
            spec_version="0.2.0.dev0",
            content_hash="hash-no-anti",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=None,
        )
        c = seeded_app["client"]
        r = c.get("/semantic-layer/no_anti/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Anti-keywords" in r.text
        assert "None declared." in r.text

    def test_metric_object_renders_sql_and_dialect(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/metric:revenue", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "SUM(amount)" in r.text
        assert "duckdb" in r.text

    def test_relationship_object_links_both_sides(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/relationship:orders_to_customers", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert 'href="/semantic-layer/retail/dataset:orders"' in body
        assert 'href="/semantic-layer/retail/dataset:customers"' in body

    def test_imported_source_shows_readonly_badge_and_no_edit_controls(self, seeded_app):
        _seed_model(id="manual/_/kb2", slug="kb_retail2", source="keboola_metastore")
        c = seeded_app["client"]
        r = c.get("/semantic-layer/kb_retail2/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert "Imported from Keboola" in body
        assert "read-only" in body
        # No write affordance anywhere on the page — this increment is
        # rendering-only, no edit controls exist for any model, imported or
        # native.
        for marker in ('method="post"', ">Edit<", ">Save<", ">Delete<", "/api/admin/semantic-models"):
            assert marker not in body, f"unexpected edit affordance: {marker!r}"

    def test_native_source_has_no_imported_badge(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Imported from" not in r.text
        assert ">Native<" in r.text

    def test_unknown_object_type_is_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/bogus:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_unknown_object_name_is_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_non_admin_without_a_grant_gets_404_on_direct_object_access(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404


class TestLibraryEntryPoint:
    """Inbound-link guard, same bug class ``tests/test_web_nav_agents.py``
    guards against: a route is not a shipped page until something links to
    it. `/semantic-layer` is not a rail row (design-system.md's rail is
    fixed rows; a new content surface reaches the caller through an existing
    destination) — the Library page's "Definitions" footer, which already
    opens `/catalog/semantics`, is where it hangs, for both admin and
    non-admin (this is a read-tier page, not admin-only)."""

    def test_semantic_layer_linked_from_library_for_non_admin(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer"' in r.text

    def test_semantic_layer_linked_from_library_for_admin(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer"' in r.text
