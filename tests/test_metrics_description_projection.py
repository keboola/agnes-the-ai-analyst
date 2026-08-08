"""`metric_definitions.description` is a two-dialect column, so every server
surface that RENDERS it must flatten it first.

Markdown for metrics hand-authored in `docs/metrics/*.yaml`; whatever the
upstream catalog stored for metrics written by an import that passes the value
through verbatim (`connectors/keboola/semantic_layer.py`), which is routinely
rich HTML. The web page's own coverage lives in
`tests/test_catalog_semantics_page.py`; this file pins the other two surfaces —
the metrics API (which is what `agnes catalog --metrics --show` renders, the
command CLAUDE.md's agent rails point agents at) and the unified search hit
(which an agent reads through the MCP `search` tool).
"""

from __future__ import annotations

HTML_BLOB = "<p><strong>Live Deals</strong> - deals currently live.</p>"
FLAT = "Live Deals - deals currently live."


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_metric(**overrides) -> dict:
    from src.repositories import metric_repo

    defaults = {
        "id": "keboola/live_deals",
        "name": "live_deals",
        "display_name": "Live Deals",
        "category": "keboola",
        "sql": "SELECT COUNT(*) AS live_deals FROM deals WHERE is_live",
        "description": HTML_BLOB,
        "source": "keboola_semantic_layer",
    }
    defaults.update(overrides)
    return metric_repo().create(**defaults)


class TestMetricsApiProjection:
    def test_detail_carries_a_plain_text_projection(self, seeded_app):
        _make_metric()
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/metrics/keboola/live_deals", headers=_auth(token)).json()
        assert body["description_text"] == FLAT

    def test_detail_keeps_the_stored_column_unchanged(self, seeded_app):
        """The projection is additive — a JSON consumer that wants the source
        still gets it, so this is not a breaking response change."""
        _make_metric()
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/metrics/keboola/live_deals", headers=_auth(token)).json()
        assert body["description"] == HTML_BLOB

    def test_list_carries_it_too(self, seeded_app):
        _make_metric()
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/metrics", headers=_auth(token)).json()
        row = next(m for m in body["metrics"] if m["id"] == "keboola/live_deals")
        assert row["description_text"] == FLAT

    def test_markdown_descriptions_flatten_as_well(self, seeded_app):
        """The same column's other dialect — no HTML involved, but `**` is
        markup too and has no place in a plain-text projection."""
        _make_metric(
            id="revenue/mrr", name="mrr", category="revenue", description="**Total** MRR.", source="manual"
        )
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/metrics/revenue/mrr", headers=_auth(token)).json()
        assert body["description_text"] == "Total MRR."

    def test_angle_bracketed_text_survives_in_a_markdown_description(self, seeded_app):
        """A markdown description may legitimately contain `List<int>` or
        `orders <shipped>`. Routing every row through the permissive renderer
        would DELETE those — markdown-it reads them as unknown tags, nh3's
        allowlist rejects them, and a pseudo-tag carries no child text, so the
        characters vanish rather than being escaped and shown (the `<int>.`
        case eats the rest of the line with it). Which renderer applies is
        therefore keyed on the writer, and this row was not written by the
        HTML-dialect one."""
        prose = "Counts orders <shipped> per day; column type List<int>."
        _make_metric(id="ops/shipped", name="shipped", category="ops", description=prose, source="manual")
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/metrics/ops/shipped", headers=_auth(token)).json()
        assert body["description_text"] == prose


class TestUnifiedSearchProjection:
    def test_metric_hit_description_is_flattened(self, seeded_app):
        """`src/search/unified.py` projects the metric's `description` straight
        into the hit, and an agent reads that through the MCP `search` tool."""
        _make_metric()
        c, token = seeded_app["client"], seeded_app["admin_token"]
        body = c.get("/api/knowledge/search", params={"q": "live deals", "k": 20}, headers=_auth(token)).json()
        hits = [h for h in body["results"] if h["type"] == "metric"]
        assert hits, "expected the seeded metric to match 'live deals'"
        hit = hits[0]
        assert hit["description"] == FLAT
        assert "<strong>" not in hit["description"]
