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
        from src.db import get_system_db
        from src.repositories import table_registry_repo
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        for tid in ("orders", "order_items"):
            table_registry_repo().register(
                id=tid,
                name=tid,
                description="test table",
                source_type="keboola",
                query_mode="materialized",
            )
            grant_table_via_package(conn, tid, "analyst1")
        conn.close()

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

    def test_glossary_client_fetch_limit_matches_server_count_limit(self, seeded_app):
        """The tab label's initial count comes from glossary_repo().list(limit=500)
        (app/web/router.py); the client re-fetch on tab-open must use the same
        limit, or the displayed count silently shrinks from up to 500 to
        whatever the client asked for once the user opens the Glossary tab."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert "/api/glossary?limit=500" in body
        assert "/api/glossary?limit=200" not in body


class TestCatalogSemanticsRBAC:
    """Metric visibility on this page must match `GET /api/metrics` — a
    metric whose table(s) the analyst can't access via their Data Package
    stack must not be server-rendered here either (#953 security fix)."""

    def _register_table(self, table_id: str, table_name: str | None = None):
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id=table_id,
            name=table_name or table_id,
            description="test table",
            source_type="keboola",
            query_mode="materialized",
        )

    def _grant(self, table_id: str, user_id: str = "analyst1"):
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        grant_table_via_package(conn, table_id, user_id)
        conn.close()

    def test_analyst_without_grant_does_not_see_metric_or_category(self, seeded_app):
        self._register_table("orders_tbl")
        _make_metric(
            id="finance/orders_total",
            name="orders_total",
            category="finance_only",
            table_name="orders_tbl",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total" not in body
        # The category has zero visible metrics — its header must not render.
        assert "finance_only" not in body

    def test_analyst_with_grant_sees_metric(self, seeded_app):
        self._register_table("orders_tbl2")
        self._grant("orders_tbl2")
        _make_metric(
            id="finance/orders_total2",
            name="orders_total2",
            category="finance_only2",
            table_name="orders_tbl2",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total2" in body
        assert "finance_only2" in body

    def test_admin_sees_metrics_regardless_of_stack(self, seeded_app):
        self._register_table("orders_tbl3")
        _make_metric(
            id="finance/orders_total3",
            name="orders_total3",
            category="finance_only3",
            table_name="orders_tbl3",
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total3" in body


class TestCatalogSemanticsLinkFromCatalog:
    def test_catalog_page_links_to_semantics(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        assert "/catalog/semantics" in resp.text


class TestCatalogSemanticsDetailRendering:
    """The expanded detail renders the full definition (description as
    sanitized markdown, a type/unit/grain meta line, and dimensions), and
    the row preview / filter index are plain-text projections (no literal
    markdown markup, synonyms searchable)."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        return resp.text

    def test_description_markdown_rendered_in_detail(self, seeded_app):
        _make_metric(
            description="**Bold definition** with `inline_code` term.",
        )
        body = self._page(seeded_app)
        assert "<strong>Bold definition</strong>" in body
        assert "<code>inline_code</code>" in body
        # Raw markdown markup must not appear anywhere (preview or detail).
        assert "**Bold definition**" not in body

    def test_preview_is_plain_text_with_block_boundaries(self, seeded_app):
        import re

        _make_metric(
            description="## Heading Alpha\n\nFirst paragraph beta.",
        )
        body = self._page(seeded_app)
        m = re.search(r'<div class="sl-row__desc">([^<]*)</div>', body)
        assert m, "plain-text preview div missing"
        preview = m.group(1)
        assert "Heading Alpha" in preview
        assert "First paragraph beta." in preview
        # Adjacent blocks must not fuse into "AlphaFirst".
        assert "AlphaFirst" not in preview
        assert "#" not in preview

    def test_description_is_sanitized(self, seeded_app):
        _make_metric(
            description="[click](javascript:alert(1)) <script>alert(2)</script>",
        )
        body = self._page(seeded_app)
        assert 'href="javascript:' not in body
        assert "<script>alert(2)</script>" not in body

    def test_meta_line_shows_type_unit_grain_and_dimensions(self, seeded_app):
        _make_metric(
            type="ratio",
            unit="percentage",
            grain="session-week",
            dimensions=["Country", "Traffic Source"],
        )
        body = self._page(seeded_app)
        assert "ratio" in body
        assert "percentage" in body
        assert "session-week" in body
        assert "Country, Traffic Source" in body

    def test_filter_index_includes_synonyms(self, seeded_app):
        import re

        _make_metric(
            synonyms=["average order value", "AOV"],
        )
        body = self._page(seeded_app)
        m = re.search(r'data-filter-text="([^"]*)"', body)
        assert m, "filter index attribute missing"
        idx = m.group(1)
        assert "average order value" in idx
        assert "aov" in idx
