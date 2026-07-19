"""GET /catalog/semantics — read-only browser for the semantic layer

(business metrics from `metric_definitions` + the glossary from
`glossary_terms`, both already shipped via `GET /api/metrics` and
`GET /api/glossary*`). Analyst-facing tier (get_current_user, no admin
gate) — mirrors the RBAC tier of the underlying REST endpoints and of
/catalog itself. Picks up issue #853 plus the glossary.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_metric(**overrides) -> dict:
    from src.repositories import metric_repo

    defaults = {
        "id": "revenue/mrr",
        "name": "mrr",
        "display_name": "Monthly Recurring Revenue",
        "category": "revenue",
        "sql": "SELECT SUM(mrr_amount) AS mrr FROM subscriptions",
        "description": "Total MRR from active subscriptions.",
    }
    defaults.update(overrides)
    return metric_repo().create(**defaults)


def _make_term(**overrides) -> dict:
    from src.repositories import glossary_repo

    defaults = {
        "id": "kb/m/churn",
        "term": "Churn Rate",
        "definition": "Percent of customers lost in a period.",
    }
    defaults.update(overrides)
    return glossary_repo().create(**defaults)


class TestCatalogSemanticsAuth:
    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/catalog/semantics", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)

    def test_analyst_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200

    def test_admin_can_also_load_page(self, seeded_app):
        """Not admin-gated (matches GET /api/metrics / GET /api/glossary — both
        get_current_user-only), but an admin should be able to load it too."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200


class TestCatalogSemanticsContent:
    def test_tabs_and_key_content_present(self, seeded_app):
        _make_metric()
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        # Tab strip — plain text + count, canonical `.tab-strip` component.
        assert "tab-strip" in body
        assert 'data-tab="metrics"' in body
        assert 'data-tab="glossary"' in body
        assert "Metrics" in body
        assert "Glossary" in body

        # Server-rendered metrics list: category grouping + row content.
        assert "revenue" in body
        assert "Monthly Recurring Revenue" in body
        assert "Total MRR from active subscriptions." in body

        # Client-side filter input for metrics (no new search endpoint).
        assert 'id="metric-filter"' in body

        # Glossary search input, wired to the existing search endpoint.
        assert 'id="glossary-search"' in body
        assert "/api/glossary/search" in body
        assert "/api/glossary" in body

    def test_metrics_grouped_by_category(self, seeded_app):
        _make_metric(id="revenue/mrr", name="mrr", category="revenue")
        _make_metric(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert "revenue" in body
        assert "engagement" in body
        assert "Daily Active Users" in body

    def test_join_tag_shown_for_relationship_metrics(self, seeded_app):
        _make_metric(
            id="sales/attach_rate",
            name="attach_rate",
            display_name="Attach Rate",
            category="sales",
            tables=["orders", "order_items"],
            sql="SELECT * FROM orders JOIN order_items USING (order_id)",
        )
        _make_metric(
            id="sales/order_count",
            name="order_count",
            display_name="Order Count",
            category="sales",
            table_name="orders",
            sql="SELECT COUNT(*) FROM orders",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert ">JOIN<" in body

    def test_accordion_detail_has_full_sql_and_extras(self, seeded_app):
        full_sql = "SELECT DATE_TRUNC('month', billing_date) AS m, SUM(mrr_amount) AS mrr FROM subscriptions GROUP BY 1"
        _make_metric(
            sql=full_sql,
            synonyms=["monthly_revenue"],
            notes=["Excludes one-time fees"],
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        # Jinja HTML-escapes the SQL (correct — it renders inside <code>);
        # single quotes come out as &#39;, everything else round-trips as-is.
        assert full_sql.replace("'", "&#39;") in body
        assert "monthly_revenue" in body
        assert "Excludes one-time fees" in body
        # No modal JS/CSS reused — this page builds its own accordion.
        assert "metric_modal.css" not in body
        assert "metric_modal.js" not in body

    def test_source_badge_mapping(self, seeded_app):
        _make_metric(id="a/1", name="a1", category="a", source="manual")
        _make_metric(id="a/2", name="a2", category="a", source="yaml_import")
        _make_metric(id="a/3", name="a3", category="a", source="openmetadata")
        _make_metric(id="a/4", name="a4", category="a", source="keboola_semantic_layer")
        _make_metric(id="a/5", name="a5", category="a", source="some_future_source")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        # 4-slot vocabulary: keboola_semantic_layer -> success, yaml_import ->
        # info, openmetadata -> warn, manual + unknown -> neutral (no accent).
        assert "badge--success" in body
        assert "badge--info" in body
        assert "badge--warn" in body


class TestCatalogSemanticsLinkFromCatalog:
    def test_catalog_page_links_to_semantics(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        assert "/catalog/semantics" in resp.text
